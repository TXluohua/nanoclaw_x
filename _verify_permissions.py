"""人工确认（Human-in-the-loop）端到端验证

只验证权限分级与挂起/恢复逻辑，不连网络、不调真实模型。
用 StubProvider 脚本化 LLM 的返回，所以整个流程可离线复现。

用法： cd nanoclaw/nanoclaw && python _verify_permissions.py
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
    """脚本化 Provider：按顺序吐出预设的 LLMResponse，不发任何网络请求。"""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    async def chat(self, messages, tools=None, model=None):
        self.calls.append(list(messages))
        if self.scripted:
            return self.scripted.pop(0)
        return LLMResponse(content="（默认最终回答）", tool_calls=[], finish_reason="stop")


def tool_call_response(tool_name, arguments, call_id):
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id=call_id,
                name=tool_name,
                arguments=arguments,
                reasoning_content=None,
            )
        ],
        finish_reason="tool_calls",
        usage={},
    )


def final_response(text):
    return LLMResponse(content=text, tool_calls=[], finish_reason="stop", usage={})


def build_agent(tmpdir, scripted, interactive=True, gate_enabled=True):
    """组装一个真实的 AgentLoop，只是 provider 被换成桩。"""
    from agent.context import ContextBuilder
    from agent.loop import AgentLoop
    from agent.permissions import ApprovalStore, PermissionGate
    from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
    from agent.tools.registry import ToolRegistry
    from agent.tools.shell import ExecTool
    from session.manager import SessionManager

    workspace = os.path.join(tmpdir, "ws")
    os.makedirs(workspace, exist_ok=True)

    tools = ToolRegistry(default_timeout=5)
    tools.register(ReadFileTool(workspace))
    tools.register(WriteFileTool(workspace))
    tools.register(ListDirTool(workspace))
    tools.register(ExecTool(workspace))

    provider = StubProvider(scripted)
    store = ApprovalStore(
        base_dir=os.path.join(workspace, "approvals"), ttl_seconds=1800
    )
    gate = PermissionGate(enabled=gate_enabled, store=store)

    agent = AgentLoop(
        provider=provider,
        tools=tools,
        context=ContextBuilder(workspace=workspace, identity_file=""),
        session_manager=SessionManager(
            sessions_dir=os.path.join(workspace, "sessions")
        ),
        model="stub",
        max_iterations=8,
        session_key="test:user",
        permission_gate=gate,
        interactive=interactive,
    )
    return agent, workspace, provider, store


def read_session_lines(workspace):
    path = os.path.join(workspace, "sessions", "test%3Auser.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ------------------------------------------- 1. 用户回复判定
def test_parse_decision():
    print("\n=== 1. 用户回复判定（歧义时偏向不执行）===")
    from agent.permissions import (
        DECISION_APPROVE,
        DECISION_DENY,
        parse_decision,
    )

    approves = ["确认", "确定", "同意", "执行", "可以", "好的", "ok", "YES", "确认执行", "确认。"]
    denies = ["取消", "拒绝", "不要", "算了", "no", "cancel", "取消执行"]

    for text in approves:
        check(f"『{text}』→ 确认", parse_decision(text) == DECISION_APPROVE, str(parse_decision(text)))
    for text in denies:
        check(f"『{text}』→ 拒绝", parse_decision(text) == DECISION_DENY, str(parse_decision(text)))

    # 「确认取消」同时含两类词 —— 按语义应为**拒绝**（用户明确说要取消），
    # 两种解读（拒绝 / 作废）的结果都是"不执行"，安全性一致，
    # 但显式判成 DENY 更符合用户本意，日志也更准确。
    check("『确认取消』→ 拒绝（先判否决）", parse_decision("确认取消") == DECISION_DENY)

    # 关键：长句与空串绝不判为"确认"，否则一次闲聊就能放行危险操作
    for text in ["帮我看看这个文件写了什么内容然后再决定要不要继续执行吧", "", "   ", "这个方案我觉得还行但是再想想"]:
        check(f"『{text[:12]}』→ 不判定", parse_decision(text) is None, str(parse_decision(text)))


# ------------------------------------------- 2. 工具风险等级
def test_risk_levels():
    print("\n=== 2. 工具风险等级声明 ===")
    from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
    from agent.tools.shell import ExecTool
    from agent.tools.spawn import SpawnSubagentTool
    from agent.tools.web_fetch import WebFetchTool
    from agent.tools.web_search import WebSearchTool

    tmp = tempfile.mkdtemp(prefix="ncrisk_")
    auto_expected = {
        "read_file": ReadFileTool(tmp),
        "list_dir": ListDirTool(tmp),
        "web_search": WebSearchTool(),
        "web_fetch": WebFetchTool(),
    }
    confirm_expected = {
        "write_file": WriteFileTool(tmp),
        "exec": ExecTool(tmp),
    }

    for name, tool in auto_expected.items():
        check(f"{name} → auto", tool.risk_level == "auto", tool.risk_level)
    for name, tool in confirm_expected.items():
        check(f"{name} → confirm", tool.risk_level == "confirm", tool.risk_level)

    # spawn 需要 provider_factory，这里只看类属性
    check(
        "spawn_subagent → confirm",
        SpawnSubagentTool.risk_level == "confirm",
        SpawnSubagentTool.risk_level,
    )

    # 未声明 risk_level 的自定义工具，默认必须是 confirm（fail-safe）
    from agent.tools.base import Tool

    class _NewTool(Tool):
        @property
        def name(self):
            return "brand_new"

        @property
        def description(self):
            return "新工具"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return "ok"

    check("未声明等级的工具默认 confirm（fail-safe）", _NewTool().risk_level == "confirm")

    # MCP 工具默认 confirm
    from agent.tools.mcp import MCPTool

    class _Mgr:
        async def call_tool(self, *a):
            return "x"

    mcp_tool = MCPTool("srv", "t", "d", {}, _Mgr())
    check("MCP 工具默认 confirm（能力由外部决定，不可信）", mcp_tool.risk_level == "confirm")
    mcp_auto = MCPTool("srv", "t", "d", {}, _Mgr(), risk_level="auto")
    check("MCP 工具可按配置放宽为 auto", mcp_auto.risk_level == "auto")


# ------------------------------------------- 3. 闸门判定
def test_gate_classify():
    print("\n=== 3. 闸门判定：deny / confirm / auto ===")
    from agent.permissions import PermissionGate, RISK_AUTO, RISK_CONFIRM, RISK_DENY
    from agent.tools.filesystem import ReadFileTool, WriteFileTool
    from agent.tools.shell import ExecTool

    tmp = tempfile.mkdtemp(prefix="ncgate_")
    gate = PermissionGate(enabled=True)
    exec_tool = ExecTool(tmp)

    risk, _ = gate.classify(ReadFileTool(tmp), {"file_path": "a.txt"})
    check("read_file 自动执行", risk == RISK_AUTO, risk)

    risk, reason = gate.classify(WriteFileTool(tmp), {"file_path": "a.txt"})
    check("write_file 需要确认", risk == RISK_CONFIRM, risk)
    check("确认理由非空", bool(reason), reason)

    risk, _ = gate.classify(exec_tool, {"command": "ls -la"})
    check("普通命令需要确认", risk == RISK_CONFIRM, risk)

    risk, reason = gate.classify(exec_tool, {"command": "rm -rf /"})
    check("rm -rf 直接禁止（不给确认机会）", risk == RISK_DENY, risk)
    check("禁止理由提到危险模式", "危险" in reason or "拦截" in reason, reason)

    risk, _ = gate.classify(exec_tool, {"command": "sudo apt install x"})
    check("sudo 直接禁止", risk == RISK_DENY, risk)

    # 关掉确认环节：确认级降为 auto，但"禁止"仍然生效
    gate_off = PermissionGate(enabled=False)
    risk, _ = gate_off.classify(WriteFileTool(tmp), {"file_path": "a.txt"})
    check("关闭确认后 write_file 放行", risk == RISK_AUTO, risk)
    risk, _ = gate_off.classify(exec_tool, {"command": "rm -rf /"})
    check("关闭确认后 rm -rf 仍然禁止（硬拦截不受开关影响）", risk == RISK_DENY, risk)


# ------------------------------------------- 4. 挂起状态持久化
def test_store():
    print("\n=== 4. 挂起动作的持久化与过期 ===")
    from agent.permissions import ApprovalStore, PendingAction

    tmp = tempfile.mkdtemp(prefix="ncstore_")
    store = ApprovalStore(base_dir=tmp, ttl_seconds=1800)

    actions = [
        PendingAction(tool_call_id="c1", tool_name="write_file", arguments={"file_path": "a"}),
        PendingAction(tool_call_id="c2", tool_name="exec", arguments={"command": "ls"}),
    ]
    store.save("test:user", actions)

    files = os.listdir(tmp)
    check("落盘文件已生成", len(files) == 1, str(files))
    check("文件名对 session_key 做了编码", "%3A" in files[0] or "_" not in files[0], files[0])

    loaded = store.load("test:user")
    check("能读回 2 条", len(loaded) == 2, str(len(loaded)))
    check("参数完整保留", loaded[1].arguments == {"command": "ls"}, str(loaded[1].arguments))

    store.clear("test:user")
    check("清除后为空", store.load("test:user") == [])

    # 过期：把 ttl 设成 0，立刻过期
    store0 = ApprovalStore(base_dir=tmp, ttl_seconds=0)
    store0.save("test:user", actions)
    check("超过 TTL 自动失效", store0.load("test:user") == [])
    check("失效后文件被清掉", not os.path.isfile(os.path.join(tmp, "test%3Auser.json")))


# ------------------------------------------- 5. 端到端：批准
def test_e2e_approve():
    print("\n=== 5. 端到端：写文件 → 挂起 → 用户确认 → 执行 ===")

    tmp = tempfile.mkdtemp(prefix="nce2e_")
    scripted = [
        tool_call_response(
            "write_file",
            {"file_path": "hello.txt", "content": "你好，NanoClaw"},
            "call_1",
        ),
        final_response("已经写好了 hello.txt"),
    ]
    agent, workspace, provider, store = build_agent(tmp, scripted)

    # --- 第 1 轮：应挂起，不落盘 ---
    out1 = asyncio.run(agent.run("帮我写个文件 hello.txt"))
    target = os.path.join(workspace, "hello.txt")

    check("第 1 轮返回确认提示", "确认" in out1, out1[:80])
    check("提示里列出了待确认操作", "write_file" in out1, out1[:120])
    check("★ 文件【没有】被写入", not os.path.isfile(target))
    check("挂起状态已落盘", len(store.load("test:user")) == 1)
    check("只调用了 1 次模型（未继续跑）", len(provider.calls) == 1, str(len(provider.calls)))

    # 历史里应是占位消息
    lines = read_session_lines(workspace)
    tool_msgs = [m for m in lines if m.get("role") == "tool"]
    check("历史里有 1 条 tool 占位消息", len(tool_msgs) == 1, str(len(tool_msgs)))
    check("占位内容含『尚未执行』", "尚未执行" in tool_msgs[0]["content"], tool_msgs[0]["content"][:40])

    # --- 第 2 轮：用户确认 ---
    out2 = asyncio.run(agent.run("确认"))

    check("★ 文件已被写入", os.path.isfile(target))
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            check("内容正确", f.read() == "你好，NanoClaw")
    check("第 2 轮返回了模型的最终回答", "写好了" in out2, out2[:60])
    check("挂起状态已清除", store.load("test:user") == [])
    check("调用了 2 次模型", len(provider.calls) == 2, str(len(provider.calls)))

    # ★ 最关键：同一个 tool_call_id 只能有一条 tool 消息
    lines = read_session_lines(workspace)
    tool_msgs = [m for m in lines if m.get("role") == "tool"]
    ids = [m.get("tool_call_id") for m in tool_msgs]
    check("tool_call_id 无重复（否则下一轮请求 400）", len(ids) == len(set(ids)), str(ids))
    check("占位已替换为真实结果（磁盘）", "尚未执行" not in tool_msgs[0]["content"], tool_msgs[0]["content"][:60])
    check("真实结果已落盘", "成功写入" in tool_msgs[0]["content"], tool_msgs[0]["content"][:60])


# ------------------------------------------- 6. 端到端：拒绝 / 歧义
def test_e2e_deny_and_ambiguous():
    print("\n=== 6. 端到端：拒绝 / 歧义回复都不执行 ===")

    for label, reply, expect_written in [
        ("拒绝", "取消", False),
        ("歧义", "顺便帮我看下今天天气怎么样呀", False),
    ]:
        tmp = tempfile.mkdtemp(prefix="ncdeny_")
        scripted = [
            tool_call_response(
                "write_file", {"file_path": "x.txt", "content": "data"}, "call_x"
            ),
            final_response(f"好的（{label}）"),
        ]
        agent, workspace, _, store = build_agent(tmp, scripted)
        target = os.path.join(workspace, "x.txt")

        asyncio.run(agent.run("写个文件"))
        check(f"[{label}] 第 1 轮未写文件", not os.path.isfile(target))

        out = asyncio.run(agent.run(reply))
        check(f"[{label}] 文件仍未写入", os.path.isfile(target) == expect_written)
        check(f"[{label}] 挂起已清除", store.load("test:user") == [])
        check(f"[{label}] 返回了回复", isinstance(out, str) and len(out) > 0)


# ------------------------------------------- 7. 子 Agent 无权确认
def test_subagent_denied():
    print("\n=== 7. 子 Agent（interactive=False）无权执行需确认操作 ===")

    tmp = tempfile.mkdtemp(prefix="ncchild_")
    scripted = [
        tool_call_response(
            "write_file", {"file_path": "c.txt", "content": "child"}, "call_c"
        ),
        final_response("我无法写文件，需要主 Agent 确认"),
    ]
    agent, workspace, provider, store = build_agent(tmp, scripted, interactive=False)
    target = os.path.join(workspace, "c.txt")

    asyncio.run(agent.run("写个文件"))

    check("子 Agent 未写文件", not os.path.isfile(target))
    check("子 Agent 不产生挂起动作", store.load("test:user") == [])
    check("子 Agent 继续跑完了（调用了 2 次模型）", len(provider.calls) == 2, str(len(provider.calls)))

    lines = read_session_lines(workspace)
    tool_msgs = [m for m in lines if m.get("role") == "tool"]
    check(
        "被拒绝且说明了原因",
        bool(tool_msgs) and "拒绝" in tool_msgs[0]["content"] and "人工确认" in tool_msgs[0]["content"],
        tool_msgs[0]["content"][:80] if tool_msgs else "无 tool 消息",
    )


# ------------------------------------------- 8. 只读操作不需要确认
def test_auto_no_confirm():
    print("\n=== 8. 只读操作直接执行，不打扰用户 ===")

    tmp = tempfile.mkdtemp(prefix="ncauto_")
    scripted = [
        tool_call_response("list_dir", {"dir_path": "."}, "call_l"),
        final_response("目录已列出"),
    ]
    agent, workspace, provider, store = build_agent(tmp, scripted)

    out = asyncio.run(agent.run("看看目录里有什么"))

    check("未产生挂起动作", store.load("test:user") == [])
    check("直接把结果交给了模型", "目录已列出" in out, out[:60])
    lines = read_session_lines(workspace)
    tool_msgs = [m for m in lines if m.get("role") == "tool"]
    check("tool 消息是真实结果而非占位", bool(tool_msgs) and "尚未执行" not in tool_msgs[0]["content"])


# ------------------------------------------- 9. deny 级不产生挂起
def test_deny_no_pending():
    print("\n=== 9. 危险命令直接拒绝，不产生待确认项 ===")

    tmp = tempfile.mkdtemp(prefix="ncdngr_")
    scripted = [
        tool_call_response("exec", {"command": "rm -rf /"}, "call_r"),
        final_response("已放弃该命令"),
    ]
    agent, workspace, provider, store = build_agent(tmp, scripted)

    asyncio.run(agent.run("清空根目录"))

    check("危险命令不产生挂起动作", store.load("test:user") == [])
    lines = read_session_lines(workspace)
    tool_msgs = [m for m in lines if m.get("role") == "tool"]
    check(
        "被直接拒绝",
        bool(tool_msgs) and "拒绝" in tool_msgs[0]["content"],
        tool_msgs[0]["content"][:80] if tool_msgs else "无",
    )


if __name__ == "__main__":
    test_parse_decision()
    test_risk_levels()
    test_gate_classify()
    test_store()
    test_e2e_approve()
    test_e2e_deny_and_ambiguous()
    test_subagent_denied()
    test_auto_no_confirm()
    test_deny_no_pending()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  失败: " + f)
        sys.exit(1)
    print("人工确认机制验证通过")
