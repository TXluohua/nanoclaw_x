"""
OpenAI 兼容 Provider 实现

兼容 OpenAI API 格式的各种服务商，包括：
- OpenAI 官方 API
- 硅基流动 (SiliconFlow)
- Kimi (Moonshot)
- DeepSeek
- 其他 OpenAI 兼容的第三方服务

通过统一接口封装，自动处理工具调用参数、推理内容提取、异常容错、
超时控制与失败重试。
"""

import asyncio
import inspect
import json
import random
from typing import Any

import openai
from openai import AsyncOpenAI

from providers.base import LLMProvider, LLMResponse, ToolCallRequest

# ---- 异常分类 ----
# 可重试：连接抖动 / 超时 / 限流 / 服务端 5xx，隔一会儿再来大概率就好了
_RETRYABLE: tuple[type[BaseException], ...] = tuple(
    exc
    for exc in (
        getattr(openai, "APITimeoutError", None),
        getattr(openai, "APIConnectionError", None),
        getattr(openai, "RateLimitError", None),
        getattr(openai, "InternalServerError", None),
    )
    if exc is not None
)

# 不可重试：重试一万次也不会好，必须立刻告诉人"该改哪里"
_FATAL_HINTS: tuple[tuple[type[BaseException], str], ...] = tuple(
    (exc, hint)
    for exc, hint in (
        (
            getattr(openai, "AuthenticationError", None),
            "API 密钥无效或已过期（401），请检查 NANOCLAW_API_KEY",
        ),
        (
            getattr(openai, "PermissionDeniedError", None),
            "当前密钥无权访问该模型（403）",
        ),
        (
            getattr(openai, "NotFoundError", None),
            "模型名或接口路径不存在（404），请检查 model / base_url",
        ),
        (
            getattr(openai, "BadRequestError", None),
            "请求参数不合法（400）。常见原因：消息历史里 tool_call 与 tool 消息"
            "没有严格配对，或 reasoning_content 回传格式不被该模型接受",
        ),
    )
    if exc is not None
)


class OpenAICompatProvider(LLMProvider):
    """
    OpenAI 兼容 API Provider

    支持所有 OpenAI API 格式的服务，通过 base_url 切换不同提供商。
    内置异常容错，API 调用失败时返回错误响应而非抛出异常。

    关键特性：
    - 仅在 tools 参数存在时传递 tool_choice，避免部分 API 报错
    - 自动提取 reasoning_content（支持 Kimi-K2.5、DeepSeek-R1 等思考模型）
    - 自动解析 tool_calls 的 arguments JSON
    - 请求超时控制 + 指数退避重试 + 错误分类

    使用示例：
        # DeepSeek
        provider = OpenAICompatProvider(
            api_key="sk-xxx",
            base_url="https://api.deepseek.com",
            model="deepseek-chat"
        )

        # 硅基流动
        provider = OpenAICompatProvider(
            api_key="sk-xxx",
            base_url="https://api.siliconflow.cn/v1",
            model="Qwen/Qwen2.5-72B-Instruct"
        )
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        """
        Args:
            api_key: API 密钥
            base_url: API 基础 URL，不同服务商地址不同
            model: 默认使用的模型名称
            timeout: 单次请求超时秒数
            max_retries: 最大尝试次数（含首次）
        """
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

        # ★ max_retries=0：关掉 SDK 自带的重试，改由本类统一控制。
        #   两边都重试次数会相乘（3×3=9 次），一次小幅故障就能把额度和
        #   用户等待时间一起打穿，而且日志里看不出到底重了几层。
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )
        self._client_closed = False

    async def close(self) -> None:
        """关闭底层 HTTP 客户端与连接池（幂等）。

        ★ 这一步不能省。`AsyncOpenAI` 内部持有 `httpx.AsyncClient`，
          里面有连接池 + 若干异步生成器。不显式关的话，它们要等垃圾回收
          才释放；而 GC 经常发生在解释器退出、事件循环已关闭之后，那时
          异步生成器无法完成 `athrow`，就会抛：

              RuntimeError: generator didn't stop after athrow()
              asyncgen: <async_generator object PoolByteStream.__aiter__>

          表现就是"结果全对，但退出时刷一屏红字"。
          用户真实跑冒烟脚本时撞上的就是这个。
        """
        if self._client_closed:
            return
        self._client_closed = True

        close = getattr(self.client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            print(f"[LLM] 关闭 HTTP 客户端时出错（已忽略）: {type(e).__name__}: {e}")

    async def __aenter__(self) -> "OpenAICompatProvider":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """
        发送对话请求（带超时与指数退避重试）

        可重试错误（超时 / 连接失败 / 限流 / 5xx）会按 1s、2s、4s… 退避重试，
        加重试抖动避免多个会话同时苏醒。不可重试错误（401 / 403 / 404 / 400）
        立刻失败并给出可操作的原因。

        Returns:
            LLMResponse: 永远返回响应对象，不抛异常（finish_reason=="error" 表示失败）
        """
        last_error: BaseException | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                return await self._request(messages, tools, model)
            except _RETRYABLE as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                # 指数退避 + 抖动，上限 8s
                delay = min(2 ** (attempt - 1) + random.uniform(0.0, 0.5), 8.0)
                print(
                    f"[LLM] {type(e).__name__}"
                    f"（第 {attempt}/{self.max_retries} 次），{delay:.1f}s 后重试"
                )
                await asyncio.sleep(delay)
            except Exception as e:
                # 不可重试 / 未预期异常：立刻返回，不浪费用户时间
                return self._error_response(e)

        return self._error_response(last_error)

    async def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
    ) -> LLMResponse:
        """
        真正发出一次请求并解析响应。

        异常不在这里处理，直接向上抛给 chat() 判断该重试还是该失败。

        动态构建请求参数：仅在 tools 存在时传递 tool_choice，
        避免硅基流动等 API 报 "tools must be specified when tool_choice is utilized" 错误。
        """
        request_params: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }

        # 仅在 tools 存在时添加工具相关参数
        if tools:
            request_params["tools"] = tools
            request_params["tool_choice"] = "auto"

        response = await self.client.chat.completions.create(**request_params)

        # 解析响应
        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason or "stop"

        # 提取 usage
        usage: dict[str, Any] = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # 获取 message 顶层的 reasoning_content（部分模型放在这里）
        message_reasoning = getattr(message, "reasoning_content", None)

        # 解析 tool_calls
        tool_calls: list[ToolCallRequest] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                # 解析 arguments JSON
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": tc.function.arguments}

                # 获取 tool_call 级别的 reasoning_content
                tc_reasoning = getattr(tc, "reasoning_content", None)

                # 合并 reasoning：优先 tool_call 级别，其次 message 级别
                reasoning_content = tc_reasoning or message_reasoning

                tool_calls.append(
                    ToolCallRequest(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                        reasoning_content=reasoning_content,
                    )
                )

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        on_delta: Any = None,
    ) -> LLMResponse:
        """
        流式对话请求。

        `on_delta` 每收到一段文本就被 await 一次，调用方可以立刻推给用户，
        而不是盯着空白等 30 秒。

        ★ 重试策略与 `chat()` 不同：
          只要已经开始往外吐字，就**不能再重试** —— 否则用户会看到前半段
          输出两遍。退避重试只发生在"一个字都还没出来"的时候。
        """
        last_error: BaseException | None = None

        for attempt in range(1, self.max_retries + 1):
            # ★ 用可变容器记录"是否已经吐过字"。
            #   之前写成局部变量 emitted=False 却从没赋值 —— 于是这个保护
            #   从来没生效过，中途断开时会重试并把前半段输出两遍。
            #   （和"熔断阈值不可达"是同一类错误：条件写了，但永远不会成立。）
            state = {"emitted": False}

            try:
                return await self._request_stream(
                    messages, tools, model, on_delta, state
                )
            except _RETRYABLE as e:
                last_error = e
                # 已经输出过内容 → 不能重试，直接报错给用户
                if state["emitted"] or attempt >= self.max_retries:
                    break
                delay = min(2 ** (attempt - 1) + random.uniform(0.0, 0.5), 8.0)
                print(
                    f"[LLM] 流式 {type(e).__name__}"
                    f"（第 {attempt}/{self.max_retries} 次），{delay:.1f}s 后重试"
                )
                await asyncio.sleep(delay)
            except Exception as e:
                return self._error_response(e)

        return self._error_response(last_error)

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        """显式关闭流式响应。

        ★ 必须显式关，不能交给 GC：
          httpx/httpcore 的响应体是异步生成器。如果只让它被垃圾回收，
          而事件循环已经关掉了，收尾时会抛
              RuntimeError: generator didn't stop after athrow()
          并刷一屏红字 —— 看着像崩溃，其实数据早就拿全了，只是没关干净。
          用户第一次跑冒烟脚本就撞上了这个。
        """
        if stream is None:
            return

        # AsyncStream 有 close()；裸异步生成器只有 aclose()
        for attr in ("close", "aclose"):
            closer = getattr(stream, attr, None)
            if closer is None:
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
                break
            except Exception as e:
                # 不静默：关闭失败意味着资源可能还挂着，必须留痕
                print(f"[LLM] 关闭流时出错（{attr}）: {type(e).__name__}: {e}")
                continue

        # AsyncStream 还持有底层 HTTP 响应，需要一并关掉
        response = getattr(stream, "response", None)
        if response is not None:
            for attr in ("aclose", "close"):
                closer = getattr(response, attr, None)
                if closer is None:
                    continue
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await result
                    break
                except Exception as e:
                    print(f"[LLM] 关闭响应时出错（{attr}）: {type(e).__name__}: {e}")
                    continue

    async def _request_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        on_delta: Any,
        state: dict[str, bool] | None = None,
    ) -> LLMResponse:
        """真正发一次流式请求并拼装响应。

        难点在 tool_calls：流式下函数的 name 和 arguments 都是**分片**到达的，
        必须按 index 累积拼接，最后才能得到完整 JSON。这是流式 Agent 的
        真实工程难点，也是最容易写错的地方。
        """
        request_params: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            # 让服务端在最后一帧带上 usage。部分 OpenAI 兼容服务不支持这个参数，
            # 下面会捕捉到并退回不带它的请求。
            "stream_options": {"include_usage": True},
        }
        if tools:
            request_params["tools"] = tools
            request_params["tool_choice"] = "auto"

        if state is None:
            state = {"emitted": False}

        stream: Any = None
        try:
            try:
                stream = await self.client.chat.completions.create(**request_params)
            except Exception as e:
                message = str(e).lower()
                if (
                    "stream_options" in message
                    or "unrecognized" in message
                    or "unknown" in message
                ):
                    # 服务端不认 stream_options，去掉再试一次（此时 usage 会缺失）
                    request_params.pop("stream_options", None)
                    stream = await self.client.chat.completions.create(
                        **request_params
                    )
                else:
                    raise

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_acc: dict[int, dict[str, str]] = {}
            usage: dict[str, Any] = {}
            finish_reason = "stop"

            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = {
                        "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(
                            chunk_usage, "completion_tokens", 0
                        )
                        or 0,
                        "total_tokens": getattr(chunk_usage, "total_tokens", 0) or 0,
                    }

                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue

                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    if on_delta is not None:
                        # ★ 先标记再回调：即使回调自己抛异常，也已经"吐过字"了，
                        #   之后就不能再重试（否则用户看到两遍前半段）
                        state["emitted"] = True
                        await on_delta(text)

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(reasoning)

                for tc in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tc, "index", 0) or 0
                    slot = tool_acc.setdefault(
                        index, {"id": "", "name": "", "args": ""}
                    )
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    function = getattr(tc, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            slot["name"] += function.name
                        if getattr(function, "arguments", None):
                            slot["args"] += function.arguments

                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason

            # 拼装 tool_calls（按 index 排序，保证与模型意图一致）
            tool_calls: list[ToolCallRequest] = []
            for index in sorted(tool_acc):
                slot = tool_acc[index]
                if not slot["name"]:
                    continue
                try:
                    arguments = json.loads(slot["args"]) if slot["args"] else {}
                except json.JSONDecodeError:
                    arguments = {"raw": slot["args"]}
                tool_calls.append(
                    ToolCallRequest(
                        id=slot["id"] or f"call_{index}",
                        name=slot["name"],
                        arguments=arguments,
                        reasoning_content="".join(reasoning_parts) or None,
                    )
                )

            return LLMResponse(
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )
        finally:
            # 无论成功、失败还是被取消，都要把流关掉，否则事件循环收尾时会抛
            # "generator didn't stop after athrow()" 刷一屏红字
            await self._close_stream(stream)

    @staticmethod
    def _error_response(e: BaseException | None) -> LLMResponse:
        """
        把异常翻译成"能定位"的错误响应。

        ★ 原实现是 `except Exception as e: return LLMResponse(content=f"错误: API 调用失败 - {e}")`，
          而 AgentLoop 只看到 finish_reason=="error" 就统一回一句"抱歉，发生了错误"。
          结果是超时、限流、密钥失效、余额不足、模型名写错，全部长得一模一样，
          排查时只能靠猜 —— 这和"静默失败是最坏的失败方式"是同一个毛病。
          现在按异常类型给出明确结论，并保留原始异常名和详情便于对照日志。
        """
        if e is None:
            return LLMResponse(content="模型调用失败：未知原因", finish_reason="error")

        name = type(e).__name__
        detail = str(e).strip()

        for exc_type, hint in _FATAL_HINTS:
            if isinstance(e, exc_type):
                return LLMResponse(
                    content=f"模型调用失败：{hint}（{name}: {detail}）",
                    finish_reason="error",
                )

        if _RETRYABLE and isinstance(e, _RETRYABLE):
            return LLMResponse(
                content=(
                    "模型调用失败：重试后仍然失败，服务可能不稳定或已限流"
                    f"（{name}: {detail}）"
                ),
                finish_reason="error",
            )

        return LLMResponse(
            content=f"模型调用失败（{name}: {detail}）",
            finish_reason="error",
        )
