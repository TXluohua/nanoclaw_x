"""
LLM Provider 抽象基类模块

定义 LLM 调用的统一接口，支持多模型提供商切换。
ToolCallRequest 和 LLMResponse 封装了模型响应的标准化结构。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRequest:
    """
    工具调用请求

    当 LLM 决定调用工具时返回的结构，包含调用所需的全部信息。
    reasoning_content 字段保存支持"思考"功能的模型（如 Kimi-K2.5、DeepSeek-R1）
    在工具调用前的推理过程。

    Attributes:
        id: 工具调用的唯一标识，用于后续回传 tool message 时关联
        name: 要调用的工具名称
        arguments: 工具参数，已解析为字典
        reasoning_content: 模型的推理过程（可选），部分模型支持
    """

    id: str
    name: str
    arguments: dict[str, Any]
    reasoning_content: str | None = None


@dataclass
class LLMResponse:
    """
    LLM 响应结构

    标准化 LLM 返回结果，包含文本内容、工具调用、结束原因和用量统计。

    Attributes:
        content: 文本内容，无工具调用时有值
        tool_calls: 工具调用列表，模型决定调用工具时有值
        finish_reason: 结束原因，常见值：stop、tool_calls、length
        usage: 用量统计，包含 prompt_tokens、completion_tokens 等
    """

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        """判断响应是否包含工具调用"""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    LLM 提供商抽象基类

    定义与 LLM 交互的统一接口，所有提供商适配器必须实现 chat 方法。
    支持传入工具定义（OpenAI function calling 格式）和模型选择。

    使用示例：
        class OpenAIProvider(LLMProvider):
            def __init__(self, api_key: str):
                self.client = OpenAI(api_key=api_key)

            async def chat(
                self,
                messages: list[dict],
                tools: list[dict] | None = None,
                model: str | None = None
            ) -> LLMResponse:
                response = await self.client.chat.completions.create(
                    model=model or "gpt-4",
                    messages=messages,
                    tools=tools
                )
                # ... 解析响应
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """
        发送对话请求并获取响应

        Args:
            messages: 对话消息列表，格式为 OpenAI messages 格式
                [{"role": "user", "content": "..."}]
            tools: 工具定义列表，OpenAI function calling 格式
                [{"type": "function", "function": {...}}]
            model: 指定使用的模型，None 时使用默认模型

        Returns:
            LLMResponse: 标准化的 LLM 响应
        """
        ...

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        on_delta: Any = None,
    ) -> LLMResponse:
        """
        流式对话请求（可选实现）

        默认实现直接退回 `chat()`，然后把整段内容当作一次 delta 回调出去 ——
        这样"不支持流式的 Provider"（包括测试里的 StubProvider）不需要任何改动，
        调用方也可以无条件走 `chat_stream`。

        想真正流式的子类覆盖这个方法即可，约定：
        - 每产生一段新文本就 `await on_delta(text)`
        - 最终仍然返回完整的 `LLMResponse`（调用方靠它决策）

        Args:
            messages: 对话消息列表
            tools: 工具定义列表
            model: 模型名
            on_delta: 异步回调，签名 async (text: str) -> None

        Returns:
            LLMResponse: 完整响应
        """
        response = await self.chat(messages, tools, model)
        if on_delta is not None and response.content:
            await on_delta(response.content)
        return response

    async def close(self) -> None:
        """
        释放 Provider 持有的资源（可选实现）。

        ★ 为什么接口里要有这个方法：
          真实 Provider 内部通常持有 HTTP 客户端（带连接池）。
          不显式关闭的话，连接池只能等垃圾回收 —— 而 GC 经常发生在
          解释器退出、事件循环已关闭之后，此时异步生成器收不了尾，会抛
          `RuntimeError: generator didn't stop after athrow()`。
          表现就是"程序跑完一切正常，退出时刷一屏红字"。
          约定：谁创建 Provider，谁负责 await close()。

        默认什么都不做（无资源可释放，例如测试桩）。
        """
        return None