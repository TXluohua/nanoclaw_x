"""NanoClaw 修复项冒烟验证

只验证本次改动的行为，不连网络、不调用真实模型。
用法： cd nanoclaw/nanoclaw && python _verify_fixes.py
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------- 0. 依赖
def check_deps():
    print("\n=== 0. 依赖 ===")
    for m in ["openai", "mcp", "lark_oapi", "botpy", "fastapi", "yaml", "httpx"]:
        ok = importlib.util.find_spec(m) is not None
        print(f"  {m:12} {'OK' if ok else 'MISSING（相关渠道会跳过，不影响下面测试）'}")


# ------------------------------------------------------- 1.3 文件沙箱
def test_sandbox():
    print("\n=== 1.3 文件沙箱：前缀绕过 ===")
    from agent.tools.filesystem import safe_join

    # 构造 workspace 与它同前缀的"兄弟"目录
    root = tempfile.mkdtemp(prefix="ncsbx_")
    ws = os.path.join(root, "nano")
    evil = os.path.join(root, "nano_secret")
    os.makedirs(ws)
    os.makedirs(evil)

    # ① 正常子路径必须放行
    inside = safe_join(ws, "a/b.txt")
    check("工作区内路径放行", inside is not None and inside.startswith(os.path.abspath(ws)))

    # ② 工作区自己放行
    check("工作区根放行", safe_join(ws, ".") is not None)

    # ③ 关键用例：../nano_secret 以前会被 startswith 放行
    escaped = safe_join(ws, "../nano_secret/x.txt")
    check(
        "越过 .. 到同前缀兄弟目录必须拒绝（原 bug）",
        escaped is None,
        f"实际返回 {escaped}",
    )

    # ④ 直接写绝对路径越界
    check(
        "绝对路径指向外部必须拒绝",
        safe_join(ws, os.path.join(evil, "y.txt")) is None,
    )

    # ⑤ 经典穿越
    check("../../ 穿越必须拒绝", safe_join(ws, "../../etc/passwd") is None)


# --------------------------------------------------- 1.5 会话 key 往返
def test_session_roundtrip():
    print("\n=== 1.5 session_key 往返一致性 ===")
    from session.manager import SessionManager

    tmp = tempfile.mkdtemp(prefix="ncsess_")
    mgr = SessionManager(sessions_dir=tmp)

    # 飞书 open_id 全是下划线，正是原实现会还原错的形状
    keys = [
        "cli:local",
        "feishu:ou_6907752d04b41e541814fadb61cd47d7",
        "qq:9A1A7D1B42EC28DBD0081F23441E5B9F",
        "web:112f5efa-64c3-4221-b5de-3201d11733b0",
    ]

    for key in keys:
        mgr.save_message(key, {"role": "user", "content": f"hello {key}"})

    listed = set(mgr.list_sessions())
    for key in keys:
        check(f"list_sessions 能还原 {key[:28]}...", key in listed, f"实际 {sorted(listed)}")
        hist = mgr.get_history(key)
        check(
            f"get_history 能读回 {key[:28]}...",
            len(hist) == 1 and hist[0]["content"] == f"hello {key}",
        )


# ------------------------------------------------------ 1.2 熔断跨轮
def test_fuse_reset():
    print("\n=== 1.2 熔断计数跨对话清零 + 熔断可达性 ===")
    from agent.loop import AgentLoop

    # 只测内部计数逻辑，不构造完整 Agent
    loop = AgentLoop.__new__(AgentLoop)
    loop._tool_call_history = []
    loop.FUSE_THRESHOLD = 20
    loop.WARNING_THRESHOLD = 10
    loop.WINDOW_SIZE = 30

    args = json.dumps({"file_path": "a.py"}, ensure_ascii=False)

    def call():
        return loop._check_tool_loop("read_file", args)[0]

    levels = [call() for _ in range(10)]
    check("前 10 次放行", all(lv == "pass" for lv in levels), str(levels))

    check("第 11 次触发告警", call() == "warn", "未告警")

    # ★ 原实现走到告警分支后就不再 append，计数钉死在 10，
    #   FUSE_THRESHOLD=20 永远到不了 —— 熔断是死代码。
    reached_fuse = False
    for _ in range(15):
        if call() == "fuse":
            reached_fuse = True
            break
    check("告警后可继续升级到熔断（原 bug：FUSE 不可达）", reached_fuse)

    # 模拟进入新一轮：run() 开头会 clear()
    loop._tool_call_history.clear()
    check("换轮后计数归零（原 bug：这里会继续累加到熔断）", call() == "pass")


# ------------------------------------------------ 1.8 工具超时 & 覆盖
def test_registry_timeout():
    print("\n=== 1.8 工具超时 + 同名覆盖告警 ===")
    from agent.tools.base import Tool
    from agent.tools.registry import ToolRegistry

    class SlowTool(Tool):
        timeout = 0.2

        @property
        def name(self):
            return "slow"

        @property
        def description(self):
            return "慢工具"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            await asyncio.sleep(5)
            return "迟到的结果"

    class BoomTool(Tool):
        @property
        def name(self):
            return "boom"

        @property
        def description(self):
            return "会抛异常的工具"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            raise RuntimeError("故意炸的")

    async def run():
        reg = ToolRegistry()
        reg.register(SlowTool())
        reg.register(BoomTool())

        t0 = time.monotonic()
        out = await reg.execute("slow", {})
        elapsed = time.monotonic() - t0
        check(
            "超时被中止且返回错误（原实现会永久挂起）",
            "超时" in out and elapsed < 2,
            f"elapsed={elapsed:.2f}s out={out!r}",
        )

        out = await reg.execute("boom", {})
        check("工具抛异常不影响调用方", out.startswith("错误:"))

        out = await reg.execute("not_exist", {})
        check("不存在的工具返回错误", "不存在" in out)

    asyncio.run(run())


# ------------------------------------------- 1.7 工具并发 & 冲突串行
def test_tool_concurrency():
    print("\n=== 1.7 工具并发执行 + 冲突降级 ===")
    from agent.loop import AgentLoop

    class _TC:
        def __init__(self, name, arguments, tid):
            self.name = name
            self.arguments = arguments
            self.id = tid
            self.reasoning_content = None

    class _Reg:
        def __init__(self):
            self.calls = 0
            self.peak = 0
            self._inflight = 0

        async def execute(self, name, arguments):
            self.calls += 1
            self._inflight += 1
            self.peak = max(self.peak, self._inflight)
            await asyncio.sleep(0.15)
            self._inflight -= 1
            return f"{name} done"

    async def run():
        loop = AgentLoop.__new__(AgentLoop)

        # ① 三个互不相关的工具 → 应并发（峰值 3，总耗时 ≈ 0.15s）
        loop.tools = _Reg()
        tcs = [
            _TC("read_file", {"file_path": "a.py"}, "1"),
            _TC("read_file", {"file_path": "b.py"}, "2"),
            _TC("read_file", {"file_path": "c.py"}, "3"),
        ]
        t0 = time.monotonic()
        res = await loop._execute_tool_calls(list(enumerate(tcs)))
        elapsed = time.monotonic() - t0
        check(
            "3 个独立调用并发执行",
            loop.tools.peak == 3 and elapsed < 0.3,
            f"peak={loop.tools.peak} elapsed={elapsed:.2f}s",
        )
        check("结果按下标对齐", res == {0: "read_file done", 1: "read_file done", 2: "read_file done"})

        # ② 两个调用碰同一个文件 → 必须串行
        loop.tools = _Reg()
        tcs = [
            _TC("write_file", {"file_path": "same.txt"}, "1"),
            _TC("write_file", {"file_path": "same.txt"}, "2"),
        ]
        await loop._execute_tool_calls(list(enumerate(tcs)))
        check(
            "同资源冲突降级为串行",
            loop.tools.peak == 1,
            f"peak={loop.tools.peak}",
        )

        # ③ 含 exec → 整批串行
        loop.tools = _Reg()
        tcs = [
            _TC("exec", {"command": "echo 1"}, "1"),
            _TC("read_file", {"file_path": "z.py"}, "2"),
        ]
        await loop._execute_tool_calls(list(enumerate(tcs)))
        check("含 exec 的批次强制串行", loop.tools.peak == 1, f"peak={loop.tools.peak}")

    asyncio.run(run())


# ---------------------------------------------------- 1.1 网关并发
def test_gateway_concurrency():
    print("\n=== 1.1 网关：跨会话并行 / 同会话保序 ===")
    from bus.queue import MessageBus, InboundMessage
    from gateway import Gateway

    class _FakeChannel:
        name = "test"

        def __init__(self, bus):
            self.bus = bus
            self.received = []

        async def start(self):
            await asyncio.sleep(3600)

        async def send(self, message):
            self.received.append(message.content)

        async def stop(self):
            pass

    inflight = {"now": 0, "peak": 0}
    order = []

    class _FakeAgent:
        def __init__(self, key):
            self.key = key

        async def run(self, content):
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
            order.append((self.key, content))
            await asyncio.sleep(0.2)
            inflight["now"] -= 1
            return f"{self.key}:{content}"

    def factory(key):
        return _FakeAgent(key)

    async def run():
        bus = MessageBus()
        ch = _FakeChannel(bus)
        gw = Gateway(bus=bus, channels=[ch], agent_factory=factory)

        # 三个不同会话同时发消息
        for user in ("u1", "u2", "u3"):
            await bus.publish_inbound(
                InboundMessage(channel="test", sender_id=user, chat_id=user, content="hi")
            )
        # 同一会话连发两条（必须保序）
        for text in ("first", "second"):
            await bus.publish_inbound(
                InboundMessage(channel="test", sender_id="u4", chat_id="u4", content=text)
            )

        inbound_task = asyncio.create_task(gw._process_inbound())
        dispatch_task = asyncio.create_task(gw._dispatch_outbound())

        deadline = time.monotonic() + 10
        while len(ch.received) < 5 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        inbound_task.cancel()
        dispatch_task.cancel()
        await asyncio.gather(
            inbound_task, dispatch_task, return_exceptions=True
        )

        check(
            "5 条消息全部有回复",
            len(ch.received) == 5,
            f"实际 {len(ch.received)} 条: {ch.received}",
        )
        check(
            "跨会话并发（原实现峰值只能是 1）",
            inflight["peak"] > 1,
            f"峰值并发={inflight['peak']}",
        )
        u4 = [c for k, c in order if k == "test:u4"]
        check("同会话保持顺序", u4 == ["first", "second"], f"实际 {u4}")
        check(
            "LRU 缓存里有 4 个 Agent",
            len(gw._agents) == 4,
            f"实际 {len(gw._agents)}",
        )

    asyncio.run(run())


# ------------------------------------------------- 1.4 错误分层（不联网）
def test_provider_error():
    print("\n=== 1.4 Provider 错误分层 ===")
    try:
        import openai
        from providers.openai_compat import OpenAICompatProvider
    except Exception as e:  # pragma: no cover
        print(f"  跳过（openai 未安装或导入失败: {e}）")
        return

    prov = OpenAICompatProvider(api_key="x", base_url="http://127.0.0.1:1", model="m")

    # 401 类错误 → 不可重试，必须给出可操作提示
    try:
        raise openai.AuthenticationError(
            "bad key", response=_Resp(401), body=None
        )
    except Exception as exc:
        resp = prov._error_response(exc)
        check(
            "401 给出密钥提示而不是笼统报错",
            "密钥" in resp.content and resp.finish_reason == "error",
            resp.content,
        )

    # 超时类 → 应被识别为可重试
    from providers.openai_compat import _FATAL_HINTS, _RETRYABLE

    retry_names = {c.__name__ for c in _RETRYABLE}
    check(
        "超时/连接/限流/5xx 都在可重试表里",
        {"APITimeoutError", "APIConnectionError", "RateLimitError"} <= retry_names,
        f"实际 {retry_names}",
    )
    check("致命分类表非空（401/403/404/400）", len(_FATAL_HINTS) >= 3)

    response = prov._error_response(ValueError("weird"))
    check(
        "未知异常也能透出类型和详情",
        "ValueError" in response.content and "weird" in response.content,
        response.content,
    )

    # 确认 SDK 自带重试已关闭（避免重试次数相乘）
    check("SDK 内部重试已关闭", getattr(prov.client, "max_retries", None) == 0)


class _Resp:
    """给 openai 异常构造用的最小 response 桩。"""

    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.request = None

    def json(self):
        return {}

    @property
    def text(self):
        return ""


# --------------------------------------------------------------- main
if __name__ == "__main__":
    check_deps()
    test_sandbox()
    test_session_roundtrip()
    test_fuse_reset()
    test_registry_timeout()
    test_tool_concurrency()
    test_gateway_concurrency()
    test_provider_error()

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  失败: " + f)
        sys.exit(1)
    print("全部通过")
