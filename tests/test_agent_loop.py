"""集成测试：Agent 主循环的行为

覆盖：防爆熔断、工具并发与冲突降级、人工确认三端到端、追踪与预算、流式输出。
这些跑真实实现，只把 LLM 换成 StubProvider。
"""

import asyncio
import json
import os

from conftest import final_response, tool_call_response


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- 防爆熔断
class TestFuse:
    def test_counter_resets_each_run(self):
        """跨对话累计会误伤正常用户，必须每轮重置。"""
        from agent.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop._tool_call_history = []
        loop.FUSE_THRESHOLD = 20
        loop.WARNING_THRESHOLD = 10
        loop.WINDOW_SIZE = 30

        args = json.dumps({"file_path": "a.py"}, ensure_ascii=False)

        def call():
            return loop._check_tool_loop("read_file", args)[0]

        assert [call() for _ in range(10)] == ["pass"] * 10
        assert call() == "warn"

        loop._tool_call_history.clear()
        assert call() == "pass"

    def test_fuse_threshold_is_reachable(self):
        """告警后必须还能继续累加到熔断，否则 FUSE 是死代码。"""
        from agent.loop import AgentLoop

        loop = AgentLoop.__new__(AgentLoop)
        loop._tool_call_history = []
        loop.FUSE_THRESHOLD = 20
        loop.WARNING_THRESHOLD = 10
        loop.WINDOW_SIZE = 30

        args = json.dumps({"file_path": "a.py"}, ensure_ascii=False)
        levels = []
        for _ in range(25):
            levels.append(loop._check_tool_loop("read_file", args)[0])

        assert "fuse" in levels, f"从未熔断: {levels}"


# --------------------------------------------------------------- 工具并发
class TestToolConcurrency:
    def _calls(self):
        class TC:
            def __init__(self, name, arguments, tid):
                self.name = name
                self.arguments = arguments
                self.id = tid
                self.reasoning_content = None

        return TC

    def test_independent_calls_run_concurrently(self):
        from agent.loop import AgentLoop

        TC = self._calls()

        class Reg:
            def __init__(self):
                self.peak = 0
                self._inflight = 0

            async def execute(self, name, arguments):
                self._inflight += 1
                self.peak = max(self.peak, self._inflight)
                await asyncio.sleep(0.1)
                self._inflight -= 1
                return "ok"

        async def scenario():
            loop = AgentLoop.__new__(AgentLoop)
            loop.tools = Reg()
            calls = [
                TC("read_file", {"file_path": "a.py"}, "1"),
                TC("read_file", {"file_path": "b.py"}, "2"),
                TC("read_file", {"file_path": "c.py"}, "3"),
            ]
            result = await loop._execute_tool_calls(list(enumerate(calls)))
            return loop.tools.peak, result

        peak, result = run(scenario())
        assert peak == 3, f"应当并发执行，实际峰值 {peak}"
        assert set(result) == {0, 1, 2}

    def test_same_file_forces_serial(self):
        """write_file 与 edit_file 改同一个文件必须串行，否则互相覆盖。"""
        from agent.loop import AgentLoop

        TC = self._calls()

        class Reg:
            def __init__(self):
                self.peak = 0
                self._inflight = 0

            async def execute(self, name, arguments):
                self._inflight += 1
                self.peak = max(self.peak, self._inflight)
                await asyncio.sleep(0.05)
                self._inflight -= 1
                return "ok"

        async def scenario():
            loop = AgentLoop.__new__(AgentLoop)
            loop.tools = Reg()
            calls = [
                TC("write_file", {"file_path": "same.py"}, "1"),
                TC("edit_file", {"file_path": "same.py"}, "2"),
            ]
            await loop._execute_tool_calls(list(enumerate(calls)))
            return loop.tools.peak

        assert run(scenario()) == 1

    def test_exec_forces_serial(self):
        from agent.loop import AgentLoop

        TC = self._calls()

        class Reg:
            def __init__(self):
                self.peak = 0
                self._inflight = 0

            async def execute(self, name, arguments):
                self._inflight += 1
                self.peak = max(self.peak, self._inflight)
                await asyncio.sleep(0.05)
                self._inflight -= 1
                return "ok"

        async def scenario():
            loop = AgentLoop.__new__(AgentLoop)
            loop.tools = Reg()
            calls = [
                TC("exec", {"command": "echo 1"}, "1"),
                TC("read_file", {"file_path": "z.py"}, "2"),
            ]
            await loop._execute_tool_calls(list(enumerate(calls)))
            return loop.tools.peak

        assert run(scenario()) == 1


# --------------------------------------------------------------- 人工确认
class TestHumanInTheLoop:
    def test_write_is_pended_then_executed_on_approval(self, make_agent, tmp_path):
        agent, workspace, provider = make_agent(
            [
                tool_call_response(
                    "write_file", {"file_path": "hello.txt", "content": "hi"}, "c1"
                ),
                final_response("写好了"),
            ]
        )
        target = os.path.join(workspace, "hello.txt")

        out = run(agent.run("写个文件"))
        assert "确认" in out
        assert not os.path.isfile(target), "确认前绝不能落盘"

        run(agent.run("确认"))
        assert os.path.isfile(target)
        with open(target, encoding="utf-8") as f:
            assert f.read() == "hi"

    def test_tool_call_id_not_duplicated(self, make_agent):
        """占位消息必须被替换，不能追加 —— 否则下一轮请求 400。"""
        agent, workspace, _ = make_agent(
            [
                tool_call_response(
                    "write_file", {"file_path": "a.txt", "content": "x"}, "c1"
                ),
                final_response("ok"),
            ]
        )
        run(agent.run("写文件"))
        run(agent.run("确认"))

        history = agent.session_manager.get_history("test:user")
        tool_ids = [m.get("tool_call_id") for m in history if m.get("role") == "tool"]
        assert len(tool_ids) == len(set(tool_ids)), f"tool_call_id 重复: {tool_ids}"

    def test_deny_does_not_execute(self, make_agent):
        agent, workspace, _ = make_agent(
            [
                tool_call_response(
                    "write_file", {"file_path": "x.txt", "content": "d"}, "c1"
                ),
                final_response("好的"),
            ]
        )
        run(agent.run("写文件"))
        run(agent.run("取消"))
        assert not os.path.isfile(os.path.join(workspace, "x.txt"))

    def test_ambiguous_reply_is_treated_as_cancel(self, make_agent):
        """歧义回复永远偏向"不执行"。"""
        agent, workspace, _ = make_agent(
            [
                tool_call_response(
                    "write_file", {"file_path": "y.txt", "content": "d"}, "c1"
                ),
                final_response("好的"),
            ]
        )
        run(agent.run("写文件"))
        run(agent.run("顺便问一下今天上海天气如何呀"))
        assert not os.path.isfile(os.path.join(workspace, "y.txt"))

    def test_dangerous_command_is_denied_without_prompt(self, make_agent):
        """危险命令直接拒绝，连确认机会都不给。"""
        agent, workspace, _ = make_agent(
            [
                tool_call_response("exec", {"command": "rm -rf /"}, "c1"),
                final_response("放弃了"),
            ]
        )
        run(agent.run("清空根目录"))

        # 不该产生待确认项（deny 与 confirm 是两条路）
        assert agent.approval_store.load("test:user") == []

        # 拒绝结论写进了 tool 消息，模型据此放弃
        history = agent.session_manager.get_history("test:user")
        tool_msgs = [m for m in history if m.get("role") == "tool"]
        assert tool_msgs, "应当有一条 tool 消息记录拒绝结果"
        assert "拒绝" in tool_msgs[0]["content"], tool_msgs[0]["content"]

        # trace 里应能看到 deny 判定
        permissions = [
            e for e in agent.tracer.last_run_timeline() if e["kind"] == "permission"
        ]
        assert any(p.get("risk") == "deny" for p in permissions), permissions

    def test_subagent_cannot_confirm(self, make_agent):
        """子 Agent 没人可问，需确认的操作一律拒绝。"""
        agent, workspace, _ = make_agent(
            [
                tool_call_response(
                    "write_file", {"file_path": "c.txt", "content": "x"}, "c1"
                ),
                final_response("我做不了"),
            ],
            interactive=False,
        )
        run(agent.run("写文件"))
        assert not os.path.isfile(os.path.join(workspace, "c.txt"))
        assert agent.approval_store.load("test:user") == []

    def test_read_only_runs_without_asking(self, make_agent):
        agent, workspace, provider = make_agent(
            [
                tool_call_response("list_dir", {"dir_path": "."}, "c1"),
                final_response("列完了"),
            ]
        )
        out = run(agent.run("看看目录"))
        assert "列完了" in out
        assert agent.approval_store.load("test:user") == []


# --------------------------------------------------------------- 追踪与预算
class TestTracingAndBudget:
    def test_full_trace_chain(self, make_agent):
        agent, workspace, _ = make_agent(
            [
                tool_call_response("list_dir", {"dir_path": "."}, "c1"),
                final_response("好了"),
            ]
        )
        run(agent.run("看看目录"))

        events = agent.tracer.last_run_timeline()
        kinds = [e["kind"] for e in events]

        assert kinds[0] == "run_start"
        assert kinds[-1] == "run_end"
        assert "llm_request" in kinds
        assert kinds.count("llm_response") == 2
        assert "tool_result" in kinds
        assert "permission" in kinds

        summary = agent.tracer.last_run_summary()
        assert summary["llm_calls"] == 2
        assert summary["tool_calls"] == 1
        assert summary["total_tokens"] == 240  # 120 + 120

    def test_budget_blocks_before_spending(self, make_agent):
        agent, _, provider = make_agent(
            [
                tool_call_response("list_dir", {"dir_path": "."}, "c1"),
                final_response("不该到这"),
            ],
            budget={"session_tokens": 1},
        )
        out = run(agent.run("看看目录"))

        assert "上限" in out
        assert len(provider.calls) == 1, "超预算后不该再发请求"

        kinds = [e["kind"] for e in agent.tracer.last_run_timeline()]
        assert "budget_exceeded" in kinds

    def test_pending_and_resolution_are_traced(self, make_agent):
        agent, _, _ = make_agent(
            [
                tool_call_response(
                    "write_file", {"file_path": "t.txt", "content": "x"}, "c1"
                ),
                final_response("ok"),
            ]
        )
        run(agent.run("写文件"))
        kinds = [e["kind"] for e in agent.tracer.last_run_timeline()]
        assert "pending_saved" in kinds

        run(agent.run("取消"))
        kinds = [e["kind"] for e in agent.tracer.last_run_timeline()]
        assert "approval_resolved" in kinds


# --------------------------------------------------------------- 流式
class TestStreaming:
    def test_deltas_are_forwarded(self, make_agent):
        agent, _, provider = make_agent(
            [final_response("第一段第二段")], streaming=True
        )
        received = []

        async def sink(text):
            received.append(text)

        agent.set_delta_sink(sink)
        answer = run(agent.run("说话"))

        assert provider.stream_calls == 1, "应当走 chat_stream"
        assert received == ["第一段", "第二段"], received
        assert answer == "第一段第二段"

    def test_no_sink_falls_back_to_non_stream(self, make_agent):
        agent, _, provider = make_agent([final_response("ok")], streaming=True)
        run(agent.run("hi"))
        assert provider.stream_calls == 0
        assert len(provider.calls) == 1

    def test_streaming_disabled(self, make_agent):
        agent, _, provider = make_agent([final_response("ok")], streaming=False)

        async def sink(text):
            pass

        agent.set_delta_sink(sink)
        run(agent.run("hi"))
        assert provider.stream_calls == 0

    def test_first_delta_latency_recorded(self, make_agent):
        agent, _, _ = make_agent([final_response("ab")], streaming=True)

        async def sink(text):
            pass

        agent.set_delta_sink(sink)
        run(agent.run("hi"))

        events = [e for e in agent.tracer.last_run_timeline() if e["kind"] == "llm_response"]
        assert events[0].get("first_delta_ms") is not None


# --------------------------------------------------------------- 网关
class TestGateway:
    def test_session_lock_keeps_order_and_concurrency(self):
        from bus.queue import InboundMessage, MessageBus
        from gateway import Gateway

        class FakeChannel:
            name = "test"
            supports_streaming = False

            def __init__(self, bus):
                self.bus = bus
                self.received = []

            async def start(self):
                await asyncio.sleep(3600)

            async def send(self, message):
                self.received.append(message.content)

            async def stop(self):
                pass

        state = {"now": 0, "peak": 0}
        order = []

        class FakeAgent:
            def __init__(self, key):
                self.key = key

            def set_delta_sink(self, sink):
                pass

            async def run(self, content):
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
                order.append((self.key, content))
                await asyncio.sleep(0.1)
                state["now"] -= 1
                return f"{self.key}:{content}"

        async def scenario():
            bus = MessageBus()
            channel = FakeChannel(bus)
            gw = Gateway(bus=bus, channels=[channel], agent_factory=FakeAgent)

            for user in ("u1", "u2", "u3"):
                await bus.publish_inbound(
                    InboundMessage(channel="test", sender_id=user, chat_id=user, content="hi")
                )
            for text in ("first", "second"):
                await bus.publish_inbound(
                    InboundMessage(channel="test", sender_id="u4", chat_id="u4", content=text)
                )

            t1 = asyncio.create_task(gw._process_inbound())
            t2 = asyncio.create_task(gw._dispatch_outbound())
            for _ in range(100):
                if len(channel.received) >= 5:
                    break
                await asyncio.sleep(0.05)
            t1.cancel()
            t2.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)
            return channel.received, state["peak"], order

        received, peak, order = run(scenario())
        assert len(received) == 5, received
        assert peak > 1, f"应当跨会话并发，实际峰值 {peak}"
        assert [c for k, c in order if k == "test:u4"] == ["first", "second"]

    def test_lru_evicts_old_sessions(self):
        from bus.queue import MessageBus
        from gateway import Gateway

        class FakeAgent:
            def __init__(self, key):
                self.key = key

        bus = MessageBus()
        gw = Gateway(
            bus=bus,
            channels=[],
            agent_factory=FakeAgent,
            max_cached_agents=2,
        )
        for key in ("a", "b", "c"):
            gw._get_or_create_agent(key)

        assert len(gw._agents) == 2
        assert "a" not in gw._agents, "最久未用的应被淘汰"
