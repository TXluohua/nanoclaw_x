"""单元测试：基础设施层

覆盖：消息总线背压、工具注册表、会话持久化、文件沙箱、长期记忆检索、新增工具。

★ 这里刻意不用 pytest-asyncio：
  异步测试统一写成同步函数 + `asyncio.run(...)`。好处是零插件依赖 ——
  在任何只有 pytest 的环境里都能跑，不会因为"少装一个插件"让整个测试集挂掉。
"""

import asyncio
import os


# --------------------------------------------------------------- 消息总线
class TestBoundedBus:
    def test_queue_has_capacity_limit(self):
        from bus.queue import MessageBus

        bus = MessageBus(maxsize=2)
        assert bus.inbound_queue.maxsize == 2

    def test_backpressure_raises_when_full(self):
        from bus.queue import BusFullError, InboundMessage, MessageBus

        async def scenario():
            bus = MessageBus(maxsize=2)
            for i in range(2):
                await bus.publish_inbound(
                    InboundMessage(
                        channel="t", sender_id=f"u{i}", chat_id="c", content="x"
                    )
                )
            try:
                await bus.publish_inbound(
                    InboundMessage(channel="t", sender_id="u9", chat_id="c", content="x")
                )
            except BusFullError:
                return bus
            return None

        bus = asyncio.run(scenario())
        assert bus is not None, "队列满时必须抛 BusFullError"
        stats = bus.stats()
        assert stats["inbound_size"] == 2
        assert stats["rejected_inbound"] == 1

    def test_outbound_backpressure(self):
        from bus.queue import BusFullError, MessageBus, OutboundMessage

        async def scenario():
            bus = MessageBus(maxsize=1)
            await bus.publish_outbound(
                OutboundMessage(channel="t", chat_id="c", content="1")
            )
            try:
                await bus.publish_outbound(
                    OutboundMessage(channel="t", chat_id="c", content="2")
                )
            except BusFullError:
                return bus
            return None

        bus = asyncio.run(scenario())
        assert bus is not None
        assert bus.rejected_outbound == 1


# --------------------------------------------------------------- 工具注册表
def _make_tool(name, timeout=None, raises=False, delay=0.0):
    from agent.tools.base import Tool

    class _T(Tool):
        def __init__(self):
            if timeout is not None:
                self.timeout = timeout

        @property
        def name(self):
            return name

        @property
        def description(self):
            return "t"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            if delay:
                await asyncio.sleep(delay)
            if raises:
                raise RuntimeError("boom")
            return f"{name}-ok"

    return _T()


class TestToolRegistry:
    def test_timeout_is_enforced(self):
        from agent.tools.registry import ToolRegistry

        async def scenario():
            reg = ToolRegistry(default_timeout=5)
            reg.register(_make_tool("slow", timeout=0.1, delay=2.0))
            return await reg.execute("slow", {})

        assert "超时" in asyncio.run(scenario())

    def test_exception_becomes_error_string(self):
        from agent.tools.registry import ToolRegistry

        async def scenario():
            reg = ToolRegistry()
            reg.register(_make_tool("boom", raises=True))
            return await reg.execute("boom", {})

        assert asyncio.run(scenario()).startswith("错误:")

    def test_unknown_tool(self):
        from agent.tools.registry import ToolRegistry

        assert "不存在" in asyncio.run(ToolRegistry().execute("nope", {}))

    def test_default_risk_is_fail_safe(self):
        """未声明等级的工具默认 confirm —— 忘了声明时多问一次，而不是静默执行。"""
        assert _make_tool("x").risk_level == "confirm"


# --------------------------------------------------------------- 会话持久化
class TestSessionManager:
    def test_roundtrip_with_underscore_keys(self, tmp_path):
        """飞书 open_id 全是下划线，旧实现会把 key 还原错。"""
        from session.manager import SessionManager

        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        keys = [
            "cli:local",
            "feishu:ou_6907752d04b41e541814fadb61cd47d7",
            "qq:9A1A7D1B42EC28DBD0081F23441E5B9F",
        ]
        for key in keys:
            mgr.save_message(key, {"role": "user", "content": f"hi {key}"})

        listed = set(mgr.list_sessions())
        for key in keys:
            assert key in listed, f"{key} 未被正确还原"
            history = mgr.get_history(key)
            assert len(history) == 1
            assert history[0]["content"] == f"hi {key}"

    def test_replace_tool_message(self, tmp_path):
        """人工确认批准后，占位消息要被替换而不是追加。"""
        from session.manager import SessionManager

        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        mgr.save_message("k", {"role": "tool", "tool_call_id": "c1", "content": "占位"})

        assert mgr.replace_tool_message("k", "c1", "真实结果") is True
        history = mgr.get_history("k")
        assert len(history) == 1, "替换不应该产生新消息"
        assert history[0]["content"] == "真实结果"

    def test_replace_missing_id_returns_false(self, tmp_path):
        from session.manager import SessionManager

        mgr = SessionManager(sessions_dir=str(tmp_path / "sessions"))
        mgr.save_message("k", {"role": "tool", "tool_call_id": "c1", "content": "x"})
        assert mgr.replace_tool_message("k", "nope", "y") is False


# --------------------------------------------------------------- 文件沙箱
class TestFileSandbox:
    def test_sector_prefix_bypass_is_blocked(self, tmp_path):
        """`../nano_secret` 曾被 startswith 判定为"在工作区内"。"""
        from agent.tools.filesystem import safe_join

        ws = tmp_path / "nano"
        evil = tmp_path / "nano_secret"
        ws.mkdir()
        evil.mkdir()

        assert safe_join(str(ws), "a/b.txt") is not None
        assert safe_join(str(ws), "../nano_secret/x.txt") is None
        assert safe_join(str(ws), str(evil / "y.txt")) is None
        assert safe_join(str(ws), "../../etc/passwd") is None

    def test_read_outside_workspace_refused(self, tmp_path):
        from agent.tools.filesystem import ReadFileTool

        ws = tmp_path / "ws"
        ws.mkdir()
        (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

        result = asyncio.run(
            ReadFileTool(str(ws)).execute(file_path="../outside.txt")
        )
        assert "拒绝" in result


# --------------------------------------------------------------- 长期记忆
class TestMemoryRetrieval:
    def _write(self, tmp_path, content):
        path = tmp_path / "MEMORY.md"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_split_entries(self):
        from agent.memory_store import split_memory_entries

        text = (
            "# 标题\n"
            "- 用户叫熊珂\n"
            "- 用户偏好中文\n"
            "  续行属于同一条\n"
            "## 另一组\n"
            "- 项目叫 NanoClaw\n"
        )
        entries = split_memory_entries(text)
        assert len(entries) == 3, entries
        assert entries[0] == "用户叫熊珂"
        assert "续行属于同一条" in entries[1]
        assert entries[2] == "项目叫 NanoClaw"

    def test_recall_is_relevant(self, tmp_path):
        from agent.memory_store import MemoryRetriever

        path = self._write(
            tmp_path,
            "- 用户的项目是 NanoClaw\n- 用户不喜欢表格\n- 用户要求中文回复\n",
        )
        retriever = MemoryRetriever(path, top_k=1)

        assert retriever.recall_sync("我的项目") == ["用户的项目是 NanoClaw"]
        assert retriever.recall_sync("喜欢什么格式") == ["用户不喜欢表格"]

    def test_unrelated_query_returns_nothing(self, tmp_path):
        """不相关的问题不该硬塞记忆 —— 那正是要避免的上下文污染。"""
        from agent.memory_store import MemoryRetriever

        path = self._write(tmp_path, "- 用户的项目是 NanoClaw\n")
        assert MemoryRetriever(path, top_k=5).recall_sync("今天天气怎么样") == []

    def test_index_refreshes_after_file_change(self, tmp_path):
        from agent.memory_store import MemoryRetriever

        path = self._write(tmp_path, "- 第一条\n")
        retriever = MemoryRetriever(path, top_k=5)
        assert retriever.size == 1

        with open(path, "a", encoding="utf-8") as f:
            f.write("- 第二条\n")
        retriever.refresh()
        assert retriever.size == 2

    def test_search_memory_tool(self, tmp_path):
        from agent.memory_store import MemoryRetriever
        from agent.tools.memory_search import SearchMemoryTool

        path = self._write(tmp_path, "- 用户的项目是 NanoClaw\n")
        tool = SearchMemoryTool(MemoryRetriever(path))
        assert "NanoClaw" in asyncio.run(tool.execute(query="项目"))


# --------------------------------------------------------------- 片段编辑
class TestEditFileTool:
    def test_replaces_unique_fragment(self, tmp_path):
        from agent.tools.editing import EditFileTool

        target = tmp_path / "a.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        tool = EditFileTool(str(tmp_path))

        result = asyncio.run(
            tool.execute(
                file_path="a.py", old_string="    return 1", new_string="    return 42"
            )
        )
        assert "成功" in result
        assert "return 42" in target.read_text(encoding="utf-8")

    def test_refuses_multiple_matches(self, tmp_path):
        """匹配到多处时宁可拒绝，也不要猜改哪一处。"""
        from agent.tools.editing import EditFileTool

        target = tmp_path / "a.py"
        target.write_text("return 1\nreturn 1\n", encoding="utf-8")
        tool = EditFileTool(str(tmp_path))

        result = asyncio.run(
            tool.execute(file_path="a.py", old_string="return 1", new_string="return 2")
        )
        assert "2 次" in result
        assert target.read_text(encoding="utf-8") == "return 1\nreturn 1\n"

    def test_missing_old_string(self, tmp_path):
        from agent.tools.editing import EditFileTool

        (tmp_path / "a.py").write_text("hello", encoding="utf-8")
        result = asyncio.run(
            EditFileTool(str(tmp_path)).execute(
                file_path="a.py", old_string="nope", new_string="x"
            )
        )
        assert "找不到" in result

    def test_cannot_escape_workspace(self, tmp_path):
        from agent.tools.editing import EditFileTool

        ws = tmp_path / "ws"
        ws.mkdir()
        result = asyncio.run(
            EditFileTool(str(ws)).execute(
                file_path="../oops.txt", old_string="a", new_string="b"
            )
        )
        assert "拒绝" in result

    def test_risk_is_confirm(self, tmp_path):
        from agent.tools.editing import EditFileTool

        assert EditFileTool(str(tmp_path)).risk_level == "confirm"


# --------------------------------------------------------------- 任务清单
class TestTodoTool:
    def test_write_and_render(self, tmp_path):
        from agent.tools.todo import TodoStore, TodoWriteTool

        store = TodoStore(str(tmp_path / "todos"))
        tool = TodoWriteTool(store, "s1")

        result = asyncio.run(
            tool.execute(
                todos=[
                    {"content": "改配置", "status": "completed"},
                    {"content": "跑测试", "status": "in_progress"},
                ]
            )
        )
        assert "1/2 已完成" in result
        assert len(store.load("s1")) == 2

    def test_rejects_bad_status(self, tmp_path):
        from agent.tools.todo import TodoStore, TodoWriteTool

        tool = TodoWriteTool(TodoStore(str(tmp_path / "todos")), "s1")
        result = asyncio.run(tool.execute(todos=[{"content": "x", "status": "done"}]))
        assert "非法" in result

    def test_full_list_replaces_previous(self, tmp_path):
        """提交的是完整清单，不是增量 —— 状态只有一份真相。"""
        from agent.tools.todo import TodoStore, TodoWriteTool

        store = TodoStore(str(tmp_path / "todos"))
        tool = TodoWriteTool(store, "s1")

        asyncio.run(tool.execute(todos=[{"content": "a", "status": "pending"}]))
        asyncio.run(tool.execute(todos=[{"content": "b", "status": "pending"}]))

        saved = store.load("s1")
        assert len(saved) == 1 and saved[0]["content"] == "b"

    def test_risk_is_auto(self, tmp_path):
        """只写自己的状态文件，不该每步都打断用户确认。"""
        from agent.tools.todo import TodoStore, TodoWriteTool

        assert TodoWriteTool(TodoStore(str(tmp_path / "todos")), "s1").risk_level == "auto"
