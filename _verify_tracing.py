"""全链路追踪与成本核算验证

只验证 trace / 用量 / 预算逻辑，不连网络、不调真实模型。
用法： cd nanoclaw/nanoclaw && python _verify_tracing.py
"""

import asyncio
import json
import os
import sys
import tempfile

from providers.base import LLMResponse, ToolCallRequest

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name}  {detail}")


# --------------------------------------------------------------- 桩
class StubProvider:
    def __init__(self, scripted, model="stub-model"):
        self.scripted = list(scripted)
        self.calls = []
        self.model = model

    async def chat(self, messages, tools=None, model=None):
        self.calls.append(list(messages))
        if self.scripted:
            return self.scripted.pop(0)
        return LLMResponse(content="（默认最终回答）", tool_calls=[], finish_reason="stop")


def tool_call_response(tool_name, arguments, call_id, usage=None):
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id=call_id, name=tool_name, arguments=arguments, reasoning_content=None
            )
        ],
        finish_reason="tool_calls",
        usage=usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def final_response(text, usage=None):
    return LLMResponse(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=usage or {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250},
    )


def build(tmpdir, scripted, pricing=None, budget=None, trace_enabled=True):
    from agent.context import ContextBuilder
    from agent.loop import AgentLoop
    from agent.permissions import ApprovalStore, PermissionGate
    from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
    from agent.tools.registry import ToolRegistry
    from agent.tracing import CostEstimator, Tracer, UsageMeter
    from session.manager import SessionManager

    workspace = os.path.join(tmpdir, "ws")
    os.makedirs(workspace, exist_ok=True)

    tools = ToolRegistry(default_timeout=5)
    tools.register(ReadFileTool(workspace))
    tools.register(WriteFileTool(workspace))
    tools.register(ListDirTool(workspace))

    provider = StubProvider(scripted)
    estimator = CostEstimator(pricing or {})

    tracer = Tracer(
        base_dir=os.path.join(workspace, "traces"),
        session_key="test:user",
        estimator=estimator,
        enabled=trace_enabled,
    )
    meter = UsageMeter(
        path=os.path.join(workspace, "traces", "sessions", "test%3Auser.json"),
        estimator=estimator,
        session_key="test:user",
    )
    gate = PermissionGate(
        enabled=True,
        store=ApprovalStore(base_dir=os.path.join(workspace, "approvals")),
    )

    agent = AgentLoop(
        provider=provider,
        tools=tools,
        context=ContextBuilder(workspace=workspace, identity_file=""),
        session_manager=SessionManager(sessions_dir=os.path.join(workspace, "sessions")),
        model="stub-model",
        max_iterations=8,
        session_key="test:user",
        permission_gate=gate,
        interactive=True,
        tracer=tracer,
        usage_meter=meter,
        budget=budget or {},
    )
    return agent, workspace, provider, tracer, meter


def read_trace(workspace):
    trace_dir = os.path.join(workspace, "traces")
    if not os.path.isdir(trace_dir):
        return []
    records = []
    for name in os.listdir(trace_dir):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(trace_dir, name), encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
    return records


# ------------------------------------------------- 1. 成本估算
def test_cost_estimator():
    print("\n=== 1. 成本估算（价目表未配置时不算钱）===")
    from agent.tracing import CostEstimator

    empty = CostEstimator()
    check("空价目表：has_price 为假", not empty.has_price("gpt-x"))
    check("空价目表：cost 返回 None（不瞎算）", empty.cost("gpt-x", 1000, 500) is None)

    priced = CostEstimator(
        {"m1": {"input": 0.5, "output": 2.0}}  # USD / 1M tokens
    )
    check("有价目表：has_price 为真", priced.has_price("m1"))
    # 1,000,000 输入 + 1,000,000 输出 = 0.5 + 2.0
    cost = priced.cost("m1", 1_000_000, 1_000_000)
    check("百万 token 换算正确", abs(cost - 2.5) < 1e-9, str(cost))
    # 1000 输入 + 500 输出
    cost2 = priced.cost("m1", 1000, 500)
    check(
        "小额换算正确",
        abs(cost2 - (1000 / 1e6 * 0.5 + 500 / 1e6 * 2.0)) < 1e-12,
        str(cost2),
    )
    check("未列出的模型不算钱", priced.cost("m2", 1000, 1000) is None)


# ------------------------------------------------- 2. 用量累计
def test_usage_meter():
    print("\n=== 2. 用量累计与落盘（重启不丢）===")
    from agent.tracing import CostEstimator, UsageMeter

    tmp = tempfile.mkdtemp(prefix="ncmeter_")
    path = os.path.join(tmp, "sessions", "s.json")

    meter = UsageMeter(path, CostEstimator({"m": {"input": 1.0, "output": 3.0}}), "s")
    meter.add_llm("m", {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500})
    meter.add_llm("m", {"prompt_tokens": 2000, "completion_tokens": 1000, "total_tokens": 3000})
    meter.add_tool()
    meter.add_run()
    meter.save()

    check("token 累加正确", meter.data["total_tokens"] == 4500, str(meter.data["total_tokens"]))
    check("模型调用计数", meter.data["llm_calls"] == 2)
    check("工具调用计数", meter.data["tool_calls"] == 1)
    check("运行次数", meter.data["runs"] == 1)
    expected = 3000 / 1e6 * 1.0 + 1500 / 1e6 * 3.0
    check("成本累加正确", abs(meter.data["cost_usd"] - expected) < 1e-9, str(meter.data["cost_usd"]))
    check("cost_display 显示金额", meter.cost_display().startswith("$"), meter.cost_display())

    # 重新加载（模拟进程重启）
    reloaded = UsageMeter(path, CostEstimator({"m": {"input": 1.0, "output": 3.0}}), "s")
    check("重启后 token 仍在", reloaded.data["total_tokens"] == 4500)
    check("重启后成本仍在", abs(reloaded.data["cost_usd"] - expected) < 1e-9)

    # 没有价目表的会话
    meter2 = UsageMeter(os.path.join(tmp, "s2.json"), CostEstimator(), "s2")
    meter2.add_llm("unknown", {"total_tokens": 100})
    check("无价目表时显示『未配置价目表』", meter2.cost_display() == "未配置价目表", meter2.cost_display())
    check("无价目表仍统计 token", meter2.data["total_tokens"] == 100)


# ------------------------------------------------- 3. Tracer 基本行为
def test_tracer():
    print("\n=== 3. Tracer：逐条落盘 + 摘要统计 ===")
    from agent.tracing import Tracer

    tmp = tempfile.mkdtemp(prefix="nctrace_")
    tracer = Tracer(base_dir=tmp, session_key="s:1", enabled=True)

    tid = tracer.start_run("你好")
    check("trace_id 已生成", len(tid) == 12, tid)
    check("trace_id 可读取", tracer.trace_id == tid)

    tracer.event("llm_response", model="m", latency_ms=100,
                 tokens={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                 cost_usd=0.001, n_tool_calls=1)
    tracer.event("tool_result", name="read_file", latency_ms=20, ok=True,
                 result_len=50, preview="内容")
    tracer.event("permission", tool="exec", risk="deny", reason="x", outcome="deny")

    summary = tracer.finish_run("ok", "答案内容")

    check("摘要 iteration/llm_calls 计数", summary["llm_calls"] == 1, str(summary["llm_calls"]))
    check("摘要 tool_calls 计数", summary["tool_calls"] == 1)
    check("摘要 denied 计数", summary["denied"] == 1)
    check("摘要 prompt_tokens 累加", summary["prompt_tokens"] == 10)
    check("摘要 total_tokens = 入+出", summary["total_tokens"] == 15, str(summary["total_tokens"]))
    check("摘要 cost 累加", abs(summary["cost_usd"] - 0.001) < 1e-9)
    check("摘要含 status", summary["status"] == "ok")

    # 逐条落盘
    files = [f for f in os.listdir(tmp) if f.endswith(".jsonl")]
    check("已生成按日期的 trace 文件", len(files) == 1, str(files))
    lines = [json.loads(line) for line in open(os.path.join(tmp, files[0]), encoding="utf-8") if line.strip()]
    kinds = [r["kind"] for r in lines]
    check("run_start 已落盘", kinds[0] == "run_start", str(kinds[:3]))
    check("run_end 已落盘", kinds[-1] == "run_end", str(kinds[-3:]))
    check("每条都带 trace_id", all(r.get("trace_id") == tid for r in lines))
    check("每条都带 elapsed_ms", all("elapsed_ms" in r for r in lines))

    # 时间线渲染
    text = Tracer.format_timeline(tracer.last_run_timeline())
    check("时间线可渲染", "run_start" in text and "run_end" in text)

    # 关闭时不写文件
    tmp2 = tempfile.mkdtemp(prefix="nctraceoff_")
    off = Tracer(base_dir=tmp2, session_key="s", enabled=False)
    off.start_run("x")
    off.event("llm_response")
    off.finish_run("ok")
    check("trace 关闭时不落盘", not os.path.isdir(tmp2) or not os.listdir(tmp2), str(os.listdir(tmp2)))


# ------------------------------------------------- 4. 端到端
def test_e2e_trace():
    print("\n=== 4. 端到端：一次真实运行留下完整链路 ===")

    tmp = tempfile.mkdtemp(prefix="nce2etr_")
    scripted = [
        tool_call_response("list_dir", {"dir_path": "."}, "c1"),
        final_response("看完了"),
    ]
    agent, workspace, provider, tracer, meter = build(tmp, scripted)

    out = asyncio.run(agent.run("看看目录"))

    records = read_trace(workspace)
    kinds = [r["kind"] for r in records]

    check("有 run_start", "run_start" in kinds)
    check("有 llm_request", "llm_request" in kinds)
    check("有 2 次 llm_response", kinds.count("llm_response") == 2, str(kinds.count("llm_response")))
    check("有 tool_call", "tool_call" in kinds)
    check("有 tool_result", "tool_result" in kinds)
    check("有 permission 分级记录", "permission" in kinds)
    check("有 run_end", "run_end" in kinds)
    check("事件按时间顺序（run_start 在最前）", kinds[0] == "run_start", kinds[0])
    check("run_end 在最后", kinds[-1] == "run_end", kinds[-1])

    run_end = [r for r in records if r["kind"] == "run_end"][0]
    check("run_end 记录了 2 次模型调用", run_end["llm_calls"] == 2, str(run_end["llm_calls"]))
    check("run_end 记录了 1 次工具调用", run_end["tool_calls"] == 1, str(run_end["tool_calls"]))
    check("run_end token = 120+250", run_end["total_tokens"] == 370, str(run_end["total_tokens"]))
    check("run_end 耗时为正", run_end["duration_ms"] >= 0)

    # 工具耗时被记录
    tr = [r for r in records if r["kind"] == "tool_result"][0]
    check("tool_result 带耗时", "latency_ms" in tr and tr["latency_ms"] >= 0, str(tr.get("latency_ms")))
    check("tool_result 标记成功", tr["ok"] is True)

    # 用量累计
    check("会话 token 已累计", meter.data["total_tokens"] == 370, str(meter.data["total_tokens"]))
    check("会话 llm_calls 已累计", meter.data["llm_calls"] == 2)
    check("会话 tool_calls 已累计", meter.data["tool_calls"] == 1)
    check("会话 runs 已累计", meter.data["runs"] == 1)
    check("用量已落盘", os.path.isfile(meter.path), meter.path)

    check("用户收到了回答", "看完了" in out, out[:40])


# ------------------------------------------------- 5. 预算护栏
def test_budget():
    print("\n=== 5. 预算护栏：在花钱之前拦住 ===")

    tmp = tempfile.mkdtemp(prefix="ncbudget_")
    scripted = [
        tool_call_response("list_dir", {"dir_path": "."}, "c1"),
        final_response("不该走到这里"),
    ]
    # 第一次调用就用掉 120 token，上限设为 1 → 第二轮必须被拦
    agent, workspace, provider, tracer, meter = build(
        tmp, scripted, budget={"session_tokens": 1}
    )

    out = asyncio.run(agent.run("看看目录"))

    check("返回了预算提示", "上限" in out or "预算" in out, out[:80])
    check("只调用了 1 次模型（第二轮被拦）", len(provider.calls) == 1, str(len(provider.calls)))

    records = read_trace(workspace)
    kinds = [r["kind"] for r in records]
    check("trace 记录了 budget_exceeded", "budget_exceeded" in kinds, str(kinds))
    check("被拦时仍有 run_end 收尾", kinds[-1] == "run_end", kinds[-1])

    # 成本上限
    tmp2 = tempfile.mkdtemp(prefix="ncbudget2_")
    scripted2 = [
        tool_call_response("list_dir", {"dir_path": "."}, "c1"),
        final_response("不该走到这里"),
    ]
    agent2, ws2, prov2, _, meter2 = build(
        tmp2,
        scripted2,
        pricing={"stub-model": {"input": 100.0, "output": 100.0}},
        budget={"session_cost_usd": 0.0001},
    )
    out2 = asyncio.run(agent2.run("看看目录"))
    check("成本上限也会拦", "上限" in out2 or "成本" in out2, out2[:80])
    check("成本上限下只调用 1 次", len(prov2.calls) == 1, str(len(prov2.calls)))


# ------------------------------------------------- 6. 多轮累计跨 run
def test_multi_run_accumulation():
    print("\n=== 6. 多轮对话的成本连续累计 ===")

    tmp = tempfile.mkdtemp(prefix="ncmulti_")
    scripted = [
        final_response("第一次回答"),
        final_response("第二次回答"),
    ]
    agent, workspace, provider, tracer, meter = build(tmp, scripted)

    asyncio.run(agent.run("问题一"))
    after_first = meter.data["total_tokens"]
    asyncio.run(agent.run("问题二"))

    check("第一轮累计 250 token", after_first == 250, str(after_first))
    check("第二轮继续累加到 500", meter.data["total_tokens"] == 500, str(meter.data["total_tokens"]))
    check("runs 计到 2", meter.data["runs"] == 2, str(meter.data["runs"]))

    # trace 文件里应有 2 个 run_start
    records = read_trace(workspace)
    check(
        "trace 有 2 次 run_start（每次 run 独立）",
        sum(1 for r in records if r["kind"] == "run_start") == 2,
    )
    check(
        "两次 run 的 trace_id 不同",
        len({r["trace_id"] for r in records}) == 2,
        str(len({r["trace_id"] for r in records})),
    )


# ------------------------------------------------- 7. 挂起路径也有 trace
def test_pending_traced():
    print("\n=== 7. 人工确认路径同样被追踪 ===")

    tmp = tempfile.mkdtemp(prefix="ncpend_")
    scripted = [
        tool_call_response("write_file", {"file_path": "a.txt", "content": "x"}, "c1"),
        final_response("已写"),
    ]
    agent, workspace, provider, tracer, meter = build(tmp, scripted)

    asyncio.run(agent.run("写个文件"))

    records = read_trace(workspace)
    kinds = [r["kind"] for r in records]

    check("trace 记录了 pending_saved", "pending_saved" in kinds, str(kinds))
    pend = [r for r in records if r["kind"] == "pending_saved"][0]
    check("pending_saved 带数量和工具名", pend["count"] == 1 and pend["tools"] == ["write_file"])

    # 第二次运行：拒绝
    scripted2 = [final_response("好的")]
    agent.provider.scripted = scripted2
    asyncio.run(agent.run("取消"))

    records = read_trace(workspace)
    kinds = [r["kind"] for r in records]
    check("trace 记录了 approval_resolved", "approval_resolved" in kinds, str(kinds))
    resolved = [r for r in records if r["kind"] == "approval_resolved"][0]
    check("approval_resolved 决策为 denied", resolved["decision"] == "denied", resolved["decision"])


# ------------------------------------------------- 8. CLI 命令分发
def test_cli_commands():
    print("\n=== 8. CLI 的 /cost 与 /trace 真的能用 ===")
    import builtins
    import io
    from contextlib import redirect_stdout

    from bus.queue import MessageBus
    from channels.cli import CLIChannel
    from main import render_cost_report, render_trace_timeline

    tmp = tempfile.mkdtemp(prefix="nccli_")
    scripted = [final_response("回答完毕")]
    agent, workspace, provider, tracer, meter = build(tmp, scripted)

    # 先跑一次，让 trace / 用量有内容
    asyncio.run(agent.run("随便问一句"))

    bus = MessageBus()
    channel = CLIChannel(bus)
    channel.tool_names = agent.tools.list_tools()
    channel._cost_callback = lambda: render_cost_report(agent)
    channel._trace_callback = lambda: render_trace_timeline(agent)

    # 喂命令序列（含一个带 BOM 的命令，验证清洗生效）
    commands = iter(["/cost", "\ufeff/trace", "/tools", "/exit"])
    original_input = builtins.input
    builtins.input = lambda prompt="": next(commands)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            asyncio.run(channel.start())
    finally:
        builtins.input = original_input

    out = buf.getvalue()

    check("/cost 打印了用量", "本会话累计" in out, out[:120])
    check("/cost 含 token", "token" in out)
    check("/trace 打印了时间线", "llm_response" in out, out[:200])
    check("带 BOM 的 /trace 也被正确识别", "trace（" in out, out[:200])
    check("/tools 打印了工具列表", "可用工具" in out)
    check("/exit 正常退出", "再见" in out)
    check(
        "带 BOM 的命令【没有】被当成问题发给模型",
        len(provider.calls) == 1,
        f"模型被调用了 {len(provider.calls)} 次",
    )


if __name__ == "__main__":
    test_cost_estimator()
    test_usage_meter()
    test_tracer()
    test_e2e_trace()
    test_budget()
    test_multi_run_accumulation()
    test_pending_traced()
    test_cli_commands()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  失败: " + f)
        sys.exit(1)
    print("追踪与成本核算验证通过")
