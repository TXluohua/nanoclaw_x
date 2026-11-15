"""Agent 主循环模块"""

import asyncio
import inspect
import json
import time
from typing import Any

from providers.base import LLMProvider, LLMResponse
from agent.tools.registry import ToolRegistry
from agent.context import ContextBuilder
from agent.permissions import (
    DECISION_APPROVE,
    PendingAction,
    PermissionGate,
    RISK_AUTO,
    RISK_CONFIRM,
    RISK_DENY,
    parse_decision,
)
from agent.tracing import Tracer, UsageMeter
from session.manager import SessionManager

# 防爆检测的三种判定结果
LOOP_PASS = "pass"   # 放行，正常执行
LOOP_WARN = "warn"   # 告警，本轮跳过执行、回一条系统警告给模型
LOOP_FUSE = "fuse"   # 熔断，立即终止整个任务

# 待确认工具在历史里的占位文本。批准后会整条替换成真实结果。
PENDING_PLACEHOLDER = "⏸ 该操作需要你确认后才会执行，当前尚未执行。"

class AgentLoop:
    """Agent 执行循环

    管理 LLM 与工具之间的交互循环，包含防爆检测机制。
    """

    # 防爆阈值常量
    FUSE_THRESHOLD = 20  # 熔断阈值
    WARNING_THRESHOLD = 10  # 警告阈值
    WINDOW_SIZE = 30  # 滑动窗口大小

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ContextBuilder,
        session_manager: SessionManager,
        model: str | None = None,
        max_iterations: int = 32,
        session_key: str = "cli:direct",
        permission_gate: PermissionGate | None = None,
        interactive: bool = True,
        tracer: Tracer | None = None,
        usage_meter: UsageMeter | None = None,
        budget: dict[str, Any] | None = None,
        streaming: bool = False,
    ):
        """初始化 AgentLoop

        Args:
            provider: LLM 提供方实例
            tools: 工具注册表
            context: 上下文构建器
            session_manager: 会话管理器
            model: 模型名称（可选）
            max_iterations: 最大迭代次数
            session_key: 会话标识符，默认 "cli:direct"
            permission_gate: 权限闸门，决定工具是"自动执行/需确认/禁止"。
                为 None 时不做确认（等价于全部放行），仅用于测试；
                真实运行必须接上。
            interactive: 对面是否有真人在等。True=顶层会话（可以问人确认）；
                False=子 Agent（没有人盯着它的中间步骤，需确认的操作一律拒绝，
                否则 spawn 就成了绕过人工确认的后门）。
            tracer: 全链路追踪器，为 None 时不记录
            usage_meter: 用量/成本计数器，为 None 时不累计
            budget: 预算护栏 {"session_tokens": int, "session_cost_usd": float}，
                0 或缺失表示不限制
        """
        self.provider = provider
        self.tools = tools
        self.context = context
        self.session_manager = session_manager
        self.session_key = session_key
        self.model = model
        self.max_iterations = max_iterations
        self.consolidator = None  # 在整合压缩器之前先设为 None

        # 权限分级
        self.permission_gate = permission_gate
        self.interactive = interactive
        self.approval_store = (
            permission_gate.store if permission_gate is not None else None
        )

        # 可观测性与成本
        self.tracer = tracer
        self.usage_meter = usage_meter
        self.budget = budget or {}
        self._tool_timings: dict[int, int] = {}  # 本轮各工具耗时（毫秒）

        # 流式输出
        # streaming=True 只是"允许流式"；真正能不能流还取决于两点：
        #   1. provider 有没有 chat_stream（基类有默认实现，会自动退化）
        #   2. 网关有没有给这个渠道装上 delta_sink（飞书/QQ 不发增量）
        self.streaming = streaming
        self._delta_sink: Any = None

        # 内部状态
        self._tool_call_history: list[str] = []  # 滑动窗口
        # 从会话管理器恢复历史
        self._session_history: list[dict] = session_manager.get_history(session_key)

    def set_delta_sink(self, sink: Any) -> None:
        """设置流式增量出口（async callable，签名 (text: str) -> None）。

        由网关在每次处理消息前按渠道能力装/卸，所以同一个 Agent 实例
        在不同渠道下行为不同（CLI 流式、飞书不流式）。
        """
        self._delta_sink = sink

    async def aclose(self) -> None:
        """释放该 Agent 持有的资源（主要是 Provider 的 HTTP 客户端）。

        ★ 不关会怎样：AsyncOpenAI 内部的连接池要等垃圾回收才释放，
          而 GC 常常发生在事件循环关闭之后，于是异步生成器收不了尾，
          退出时刷一屏 `generator didn't stop after athrow()`。
          谁创建 Agent，谁负责在退出前 await 这个方法。
        """
        closer = getattr(self.provider, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            print(f"警告: 关闭 Provider 失败 - {type(e).__name__}: {e}")

    async def run(self, user_message: str) -> str:
        """执行 Agent 主循环（外层：追踪与记账的收尾）

        Args:
            user_message: 用户输入消息

        Returns:
            str: Agent 的最终响应
        """
        # 0. ★ 每轮对话重新开始统计工具调用频率
        #    _tool_call_history 是【防爆检测】的滑动窗口，以前只在 clear_history()
        #    里清空 —— 也就是跨对话累加。真实后果：
        #      第一轮读了 10 次文件，第二轮一开局计数就是 10，
        #      再读一次就触发"警告"（工具被跳过不执行），到 20 次直接熔断，
        #      用户看到的是"我什么都没干，它突然说我在循环调用"。
        #    防爆的本意是防止【单轮内】模型打转，跨对话累计毫无意义。
        self._tool_call_history.clear()

        if self.tracer is not None:
            self.tracer.start_run(user_message)

        answer = ""
        status = "ok"
        try:
            answer = await self._run_loop(user_message)
            return answer
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            if self.tracer is not None:
                self.tracer.event("error", message=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            # ★ 放在 finally 里：熔断、挂起、异常、正常返回，四条路径都要收尾，
            #   否则 trace 会缺 run_end，用量也不会落盘。
            self._finish_trace(status, answer)

    async def _run_loop(self, user_message: str) -> str:
        """真正的主循环。追踪生命周期由 run() 包在外层，这里只管干活。"""
        # 0.5 ★ 如果上一轮留下了"待用户确认"的操作，先处理它。
        #     必须在 build_messages 之前完成：恢复会把历史里的占位 tool 消息
        #     替换成真实执行结果，历史必须先是"对的"，再拿去拼 prompt。
        await self._resolve_pending_action(user_message)

        # 1. 构建初始 messages
        #    async：记忆召回可能要走向量化服务（本轮问题作为召回 query）
        messages = await self.context.build_messages(
            history=self._session_history,
            current_message=user_message
        )

        # 保存用户消息
        user_msg = {"role": "user", "content": user_message}
        self.session_manager.save_message(self.session_key, user_msg)

        # 2. 循环处理
        for iteration in range(self.max_iterations):
            if hasattr(self, 'consolidator') and self.consolidator:
                messages = await self.consolidator.maybe_consolidate(messages)

            # 预算护栏：超额就停，不要"跑完再告诉你花了多少钱"。
            # 护栏要放在发请求之前，否则这一轮的请求已经花出去了才拦。
            budget_message = self._check_budget()
            if budget_message:
                print(f"\n  💸 {budget_message}")
                if self.tracer is not None:
                    self.tracer.event("budget_exceeded", message=budget_message)
                return budget_message

            # a. 调用 LLM（打印思考提示）
            tool_definitions = self.tools.get_definitions()
            if self.tracer is not None:
                self.tracer.event(
                    "llm_request",
                    iteration=iteration + 1,
                    model=self.model or "default",
                    n_messages=len(messages),
                    n_tools=len(tool_definitions),
                )

            print(f"  💭 思考中... (第 {iteration + 1} 轮)", end="", flush=True)
            llm_started = time.perf_counter()
            first_delta_ms: int | None = None

            if self.streaming and self._delta_sink is not None:
                # 流式：模型每吐一段就转给渠道，用户不用盯着空白等
                async def _forward(text: str) -> None:
                    nonlocal first_delta_ms
                    if first_delta_ms is None:
                        first_delta_ms = int(
                            (time.perf_counter() - llm_started) * 1000
                        )
                    try:
                        await self._delta_sink(text)
                    except Exception as forward_error:
                        # 推不出去不影响任务：最终结果仍会以 final 消息送到
                        print(f"  [!] 流式片段推送失败: {forward_error}")

                response = await self.provider.chat_stream(
                    messages=messages,
                    tools=tool_definitions,
                    model=self.model,
                    on_delta=_forward,
                )
            else:
                response = await self.provider.chat(
                    messages=messages,
                    tools=tool_definitions,
                    model=self.model
                )
            llm_latency_ms = int((time.perf_counter() - llm_started) * 1000)
            if first_delta_ms is not None:
                # 流式内容已经把当前行占了，先换行再打完成标记
                print()
            print(" ✓")  # 思考完成

            # 记账 + 追踪：这里终于用上了 provider 早就解析出来的 usage
            used_model = self.model or getattr(self.provider, "model", "") or "unknown"
            cost_usd = None
            if self.usage_meter is not None:
                cost_usd = self.usage_meter.add_llm(
                    used_model, response.usage
                ).get("cost_usd")
            if self.tracer is not None:
                self.tracer.event(
                    "llm_response",
                    iteration=iteration + 1,
                    model=used_model,
                    latency_ms=llm_latency_ms,
                    finish_reason=response.finish_reason,
                    n_tool_calls=len(response.tool_calls),
                    tokens=response.usage or {},
                    cost_usd=cost_usd,
                    # 首字延迟：流式下这才是用户真正感知的"快不快"
                    first_delta_ms=first_delta_ms,
                )

            # 打印 reasoning_content（思考过程）
            if response.tool_calls and response.tool_calls[0].reasoning_content:
                reasoning = response.tool_calls[0].reasoning_content
                # 截断显示，避免刷屏
                preview = reasoning[:300] + "..." if len(reasoning) > 300 else reasoning
                print(f"  🧠 思考过程:\n{preview}")

            # b. 检查错误
            if response.finish_reason == "error":
                # ★ 透传具体原因。超时 / 限流 / 密钥失效 / 余额不足 长得完全不一样，
                #   以前一律折叠成"抱歉，发生了错误"，等于把排查线索全扔了。
                #   provider 已经把错误分类整理进了 content，这里直接用。
                return response.content or "抱歉，调用模型失败，请稍后重试。"

            # c. 处理工具调用
            if response.has_tool_calls:
                # 构造 assistant 消息（含 tool_calls）
                assistant_msg = self._build_assistant_message(response)
                messages.append(assistant_msg)

                # 保存 assistant 消息（含 tool_calls）
                self.session_manager.save_message(self.session_key, assistant_msg)

                # ---- 第一步：先按模型给出的顺序，把每个 tool_call 都判一遍 ----
                # 必须"先全判完、再执行"：否则并发跑的时候会出现
                # "前几个工具已经改了文件，后面那个才熔断"的半完成状态，很难收拾。
                decisions: list[tuple[Any, str, str]] = []  # (tool_call, 级别, 说明)
                fuse_message = ""
                for tc in response.tool_calls:
                    args_json = json.dumps(tc.arguments, ensure_ascii=False)

                    # ★ 打印工具调用过程，让用户看到 Agent 在干什么
                    print(f"\n  🛠️  工具: {tc.name}({args_json})")

                    level, message = self._check_tool_loop(tc.name, args_json)
                    decisions.append((tc, level, message))

                    if level == LOOP_FUSE:
                        fuse_message = message
                        break

                if fuse_message:
                    # 熔断 - 直接返回，本次任务终止
                    print(f"  🚨 {fuse_message}")
                    if self.tracer is not None:
                        self.tracer.event("fuse", message=fuse_message)
                    return fuse_message

                # ---- 第二步：风险分级（auto / confirm / deny）----
                plans: list[dict] = []
                pending_actions: list[PendingAction] = []

                for idx, (tc, level, message) in enumerate(decisions):
                    plan: dict[str, Any] = {
                        "idx": idx,
                        "tc": tc,
                        "level": level,
                        "message": message,
                    }

                    if level != LOOP_PASS:
                        # 防爆告警：本轮不执行
                        plan["outcome"] = "skip"
                        plans.append(plan)
                        continue

                    risk, reason = RISK_AUTO, ""
                    tool = self.tools.get(tc.name)
                    if tool is None:
                        # 模型幻觉出不存在的工具名：直接拒，别让用户去确认
                        # 一个根本执行不了的操作。
                        plan["outcome"] = "deny"
                        plan["reason"] = f"工具 '{tc.name}' 不存在"
                        plans.append(plan)
                        continue

                    if self.permission_gate is not None:
                        risk, reason = self.permission_gate.classify(
                            tool, tc.arguments
                        )
                    # permission_gate 为 None 时保持旧行为（全部放行），仅测试用

                    if risk == RISK_DENY:
                        plan["outcome"] = "deny"
                        plan["reason"] = reason

                    elif risk == RISK_CONFIRM:
                        if self.interactive:
                            plan["outcome"] = "confirm"
                            pending_actions.append(
                                PendingAction(
                                    tool_call_id=tc.id,
                                    tool_name=tc.name,
                                    arguments=tc.arguments,
                                    reason=reason,
                                )
                            )
                        else:
                            # 子 Agent 没有人可以问 → 直接拒绝，
                            # 让它把"我想做这件事"写进结论交回主 Agent
                            plan["outcome"] = "deny"
                            plan["reason"] = (
                                "该操作需要人工确认，但子 Agent 没有可确认的对象，"
                                "故不予执行。请在最终结论中说明你需要执行的操作。"
                            )

                    else:
                        plan["outcome"] = "auto"

                    if self.tracer is not None:
                        self.tracer.event(
                            "permission",
                            tool=tc.name,
                            risk=risk,
                            reason=reason,
                            outcome=plan["outcome"],
                        )

                    plans.append(plan)

                # ---- 第三步：执行 auto 级（互不冲突的并发跑）----
                to_run = [(p["idx"], p["tc"]) for p in plans if p["outcome"] == "auto"]
                if self.tracer is not None:
                    for _idx, _tc in to_run:
                        self.tracer.event(
                            "tool_call",
                            name=_tc.name,
                            args=json.dumps(_tc.arguments, ensure_ascii=False),
                        )
                results = await self._execute_tool_calls(to_run)

                # ---- 第四步：按【原顺序】回写 tool 消息 ----
                # 顺序必须与 tool_call_id 严格对应，打乱了下一轮请求会直接 400。
                for plan in plans:
                    tc = plan["tc"]
                    outcome = plan["outcome"]

                    if plan["level"] == LOOP_WARN:
                        # 警告 - 本轮跳过执行，回一条系统警告让模型自己纠正
                        print(f"  ⚠️  {plan['message']}")
                        content = f"系统警告：{plan['message']}"
                    elif outcome == "deny":
                        print(f"  🚫 {tc.name} 已拒绝：{plan['reason']}")
                        content = f"已拒绝执行：{plan['reason']}"
                    elif outcome == "confirm":
                        print(f"  ⏸  {tc.name} 待用户确认")
                        content = PENDING_PLACEHOLDER
                    else:
                        content = results.get(plan["idx"], "错误: 工具未执行")
                        # 打印工具结果（截断显示，避免刷屏）
                        preview = (
                            content[:200] + "..." if len(content) > 200 else content
                        )
                        print(f"  ✅ {tc.name} 结果: {preview}")

                    # 工具执行情况的追踪：只对真正跑过的记 tool_result
                    if self.tracer is not None and outcome == "auto":
                        if self.usage_meter is not None:
                            self.usage_meter.add_tool()
                        content_text = content if isinstance(content, str) else str(content)
                        self.tracer.event(
                            "tool_result",
                            name=tc.name,
                            latency_ms=self._tool_timings.get(plan["idx"], 0),
                            ok=not content_text.startswith(("错误", "已拒绝")),
                            result_len=len(content_text),
                            preview=content_text,
                        )

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content,
                    }
                    messages.append(tool_msg)
                    self.session_manager.save_message(self.session_key, tool_msg)

                # ---- 第五步：有挂起操作就结束本轮，等用户确认 ----
                # 注意这里必须【结束本轮】而不是继续循环：
                # 继续跑等于把"待确认"的操作留给模型自己去想办法绕过。
                if pending_actions and self.approval_store is not None:
                    self.approval_store.save(self.session_key, pending_actions)
                    if self.tracer is not None:
                        self.tracer.event(
                            "pending_saved",
                            count=len(pending_actions),
                            tools=[a.tool_name for a in pending_actions],
                        )
                    prompt = self.permission_gate.build_confirm_prompt(pending_actions)
                    prompt_msg = {"role": "assistant", "content": prompt}
                    messages.append(prompt_msg)
                    self.session_manager.save_message(self.session_key, prompt_msg)
                    self._save_to_history(messages)
                    return prompt

                # continue 回到循环顶部
                continue

            # d. 没有工具调用 - 保存历史并返回
            self._save_to_history(messages)

            # 保存最终的 assistant 消息（不含 tool_calls）
            if response.content:
                assistant_final_msg = {"role": "assistant", "content": response.content}
                self.session_manager.save_message(self.session_key, assistant_final_msg)

            return response.content or ""

        # 3. for-else: 超过最大次数
        return "已达到最大迭代次数，任务未完成。"

    async def _resolve_pending_action(self, user_message: str) -> bool:
        """处理上一轮挂起的"待用户确认"操作。

        三种结局：
        - 用户明确确认 → 真正执行，把历史里的占位消息换成真实结果
        - 用户明确拒绝 → 不执行，占位消息换成"用户拒绝执行"
        - 用户说了别的话 → 视为放弃挂起，占位消息换成"已作废"

        无论哪种结局都会清除挂起状态，保证同一批操作不会被重复确认。

        Returns:
            bool: 是否处理了挂起操作
        """
        store = self.approval_store
        if store is None:
            return False

        pending = store.load(self.session_key)
        if not pending:
            return False

        decision = parse_decision(user_message)

        if decision == DECISION_APPROVE:
            print(f"\n  ✅ 用户已确认，执行 {len(pending)} 个挂起操作")
            contents = await self._execute_pending(pending)
            resolved_as = "approved"
        elif decision is not None:
            print(f"\n  🚫 用户拒绝，放弃 {len(pending)} 个挂起操作")
            contents = {
                action.tool_call_id: "用户拒绝执行该操作，命令未运行。"
                for action in pending
            }
            resolved_as = "denied"
        else:
            # ★ 用户回了别的：默认作废，绝不擅自执行。
            #   "歧义时偏向不执行"是这一整套机制的基本立场。
            print("\n  ↩️ 用户未确认（回复了其他内容），挂起操作已作废")
            contents = {
                action.tool_call_id: (
                    "用户未确认该操作（回复了其他内容），操作已作废，未执行。"
                )
                for action in pending
            }
            resolved_as = "cancelled"

        if self.tracer is not None:
            self.tracer.event(
                "approval_resolved",
                decision=resolved_as,
                count=len(pending),
                tools=[action.tool_name for action in pending],
            )

        for tool_call_id, content in contents.items():
            self._replace_tool_message(tool_call_id, content)

        store.clear(self.session_key)
        return True

    async def _execute_pending(
        self, pending: list[PendingAction]
    ) -> dict[str, str]:
        """执行一批已获批准的操作，返回 {tool_call_id: 结果}。"""
        contents: dict[str, str] = {}
        for action in pending:
            result = await self.tools.execute(action.tool_name, action.arguments)
            preview = result[:200] + "..." if len(result) > 200 else result
            print(f"  ✅ [已确认] {action.tool_name} 结果: {preview}")
            contents[action.tool_call_id] = result
        return contents

    def _replace_tool_message(self, tool_call_id: str, content: str) -> None:
        """把一个 tool 占位消息替换成真实内容（内存与磁盘同步）。

        为什么要"替换"而不是"再追加一条"：
        OpenAI 协议要求每个 tool_call 有且只有一条对应的 tool 消息。
        如果占位一条、真实结果又追加一条，下一轮请求会因为
        tool_call_id 重复直接 400。
        """
        replaced_in_memory = False
        for msg in reversed(self._session_history):
            if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                msg["content"] = content
                replaced_in_memory = True
                break

        if not replaced_in_memory:
            print(f"  [!] 历史里没找到 tool_call_id={tool_call_id} 的占位消息")

        self.session_manager.replace_tool_message(
            self.session_key, tool_call_id, content
        )

    def _check_budget(self) -> str:
        """预算护栏。超出上限返回提示语，未超出返回空串。

        护栏放在"发请求之前"，因为钱是随请求花出去的：
        跑完再检查，这一轮已经付过账了。
        """
        if self.usage_meter is None:
            return ""

        data = self.usage_meter.data

        token_cap = int(self.budget.get("session_tokens") or 0)
        if token_cap and data["total_tokens"] >= token_cap:
            return (
                f"本次会话已消耗 {data['total_tokens']} token，达到上限 {token_cap}。"
                f"如需继续，请调高 config.json 里的 budget.session_tokens；"
                f"或用 /clear 开一个新会话重新计数。"
            )

        cost_cap = float(self.budget.get("session_cost_usd") or 0)
        if cost_cap and data.get("priced") and data["cost_usd"] >= cost_cap:
            return (
                f"本次会话成本已达 ${data['cost_usd']:.4f}，超过上限 ${cost_cap:.4f}。"
                f"如需继续，请调高 config.json 里的 budget.session_cost_usd。"
            )

        return ""

    def _finish_trace(self, status: str, answer: str) -> None:
        """收尾：落盘用量、结束 trace、打印一行摘要。"""
        if self.usage_meter is not None:
            self.usage_meter.add_run()
            self.usage_meter.save()

        if self.tracer is None:
            return

        summary = self.tracer.finish_run(status, answer)
        if not summary:
            return

        print(
            f"  📊 本次 {summary['duration_ms']}ms"
            f"  token {summary['total_tokens']}"
            f"  模型 {summary['llm_calls']} 次"
            f"  工具 {summary['tool_calls']} 次"
        )
        if self.usage_meter is not None:
            print(f"  📈 会话累计 {self.usage_meter.summary_line()}")

    def _build_assistant_message(self, response: LLMResponse) -> dict:
        """构建 assistant 消息（含 tool_calls）

        严格按照 OpenAI 格式构建，reasoning_content 放在顶层。

        Args:
            response: LLM 响应

        Returns:
            dict: assistant 消息字典
        """
        assistant_msg: dict = {
            "role": "assistant",
            "content": response.content
        }

        # 构建 tool_calls 数组
        tool_calls = []
        for tc in response.tool_calls:
            tool_call_item = {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                }
            }
            tool_calls.append(tool_call_item)

        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls

        # reasoning_content 放在顶层（不能放在 tool_calls 里）
        if response.tool_calls and response.tool_calls[0].reasoning_content:
            assistant_msg["reasoning_content"] = response.tool_calls[0].reasoning_content

        return assistant_msg

    # 这些工具一旦出现在同一批次里，整批退化为串行（Shell 副作用不可预测）
    _SERIAL_ONLY_TOOLS = frozenset({"exec"})

    @staticmethod
    def _resource_key(tc: Any) -> str:
        """提取一次工具调用会碰到的"资源"，用来判断两次调用能不能并行。

        两个调用碰同一个文件 / 同一个 URL / 同一条命令 → 视为冲突，不能并发，
        否则可能出现两个写操作同时改同一个文件、后写覆盖先写的竞争。

        ★ 资源键【不包含工具名】：`write_file(a.py)` 与 `edit_file(a.py)`
          碰的是同一个文件，必须算冲突。早先版本用 f"{tool}:{path}"，
          会让这两个并发跑 —— 那恰恰是最危险的一种组合。
        """
        args = getattr(tc, "arguments", None) or {}
        for key, prefix in (
            ("file_path", "file"),
            ("dir_path", "dir"),
            ("url", "url"),
            ("command", "cmd"),
        ):
            if key in args:
                return f"{prefix}:{args[key]}"
        return f"tool:{tc.name}"

    async def _execute_tool_calls(
        self, calls: list[tuple[int, Any]]
    ) -> dict[int, str]:
        """执行本轮放行的工具调用，返回 {decisions 下标: 结果字符串}。

        并发策略（保守优先，宁可慢也不出错）：
        - 只有 1 个调用          → 直接跑
        - 出现重复的资源键        → 整批退化为串行
        - 批次里含 exec          → 整批退化为串行
        - 其余情况               → asyncio.gather 并发

        为什么值得并发：模型一次返回多个互不相关的工具调用很常见
        （"同时看这三个文件"），原先的串行 for 循环让总耗时等于各次之和；
        只要其中一个是不相关的 15 秒网页抓取，其余的都在干等。
        """
        if not calls:
            return {}

        # 每次批量执行前清空耗时表，避免上一轮的数值混进来
        self._tool_timings = {}

        async def _timed(idx: int, tc: Any) -> str:
            """执行单个工具并记录耗时（毫秒）。"""
            started = time.perf_counter()
            result = await self.tools.execute(tc.name, tc.arguments)
            self._tool_timings[idx] = int((time.perf_counter() - started) * 1000)
            return result

        if len(calls) > 1:
            keys = [self._resource_key(tc) for _, tc in calls]
            has_conflict = len(set(keys)) != len(keys)
            has_serial_tool = any(
                tc.name in self._SERIAL_ONLY_TOOLS for _, tc in calls
            )

            if not has_conflict and not has_serial_tool:
                results = await asyncio.gather(
                    *(_timed(idx, tc) for idx, tc in calls)
                )
                return {
                    idx: (res if isinstance(res, str) else str(res))
                    for (idx, _), res in zip(calls, results)
                }

        # 兜底：串行
        serial_results: dict[int, str] = {}
        for idx, tc in calls:
            serial_results[idx] = await _timed(idx, tc)
        return serial_results

    def _check_tool_loop(
        self, tool_name: str, tool_args_json: str
    ) -> tuple[str, str]:
        """防爆检测

        以「工具名 + 参数 JSON」为签名，统计滑动窗口内的重复次数，防止模型打转。

        ★ 返回结构化级别而不是字符串，是因为原先靠 `"熔断" in result` 做子串
          判断来决定分支 —— 只要改一个字面量就静默失效，太脆。

        Args:
            tool_name: 工具名称
            tool_args_json: 工具参数的 JSON 字符串

        Returns:
            tuple[str, str]: (级别, 说明)。
                级别为 LOOP_PASS 放行 / LOOP_WARN 本轮不执行 / LOOP_FUSE 终止任务
        """
        signature = f"{tool_name}:{tool_args_json}"

        # 统计签名出现次数
        count = self._tool_call_history.count(signature)

        if count >= self.FUSE_THRESHOLD:
            return (
                LOOP_FUSE,
                f"检测到工具 '{tool_name}' 重复调用已达熔断阈值"
                f"（{self.FUSE_THRESHOLD}次），已自动终止执行。请尝试其他方案。",
            )

        # ★ 无论是"放行"还是"告警"，都要把这次调用记进窗口。
        #   原实现只有在放行分支才 append —— 一旦计数摸到 WARNING_THRESHOLD，
        #   之后就永远走告警分支、再也不 append，计数被钉死在 10 上，
        #   FUSE_THRESHOLD=20 变成不可达的死代码，熔断形同不存在。
        #   （这不是设计意图：阈值的本意就是"先警告、再熔断"两级。）
        self._tool_call_history.append(signature)

        # 维护滑动窗口大小
        if len(self._tool_call_history) > self.WINDOW_SIZE:
            self._tool_call_history.pop(0)

        if count >= self.WARNING_THRESHOLD:
            return (
                LOOP_WARN,
                f"警告：工具 '{tool_name}' 重复调用已达 "
                f"{self.WARNING_THRESHOLD} 次，请检查是否有循环调用问题。",
            )

        return (LOOP_PASS, "")

    def _save_to_history(self, messages_snapshot: list[dict]) -> None:
        """保存本轮新增消息到会话历史

        注意：消息已通过 session_manager.save_message 持久化，
        此方法仅更新内存中的 _session_history，供下次对话使用。

        Args:
            messages_snapshot: 当前完整的消息列表
        """
        # 计算新增消息（跳过 system 消息）
        # 新消息数量 = 总消息数 - 已有历史消息数 - 1 (system)
        existing_count = len(self._session_history)

        # 新增的消息（不包含 system）
        new_messages = messages_snapshot[1 + existing_count:]

        # 添加到历史（仅更新内存状态）
        self._session_history.extend(new_messages)

    def clear_history(self) -> None:
        """清空对话历史和工具调用历史"""
        self._tool_call_history.clear()
        self._session_history.clear()
        # 清空会话文件
        self.session_manager.clear(self.session_key)