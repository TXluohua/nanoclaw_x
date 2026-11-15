"""装配冒烟：验证 config 加载 + Agent 组装链路（不联网、不调模型）

用法： cd nanoclaw/nanoclaw && python _verify_boot.py
"""

import json
import os
import sys

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  [{'OK' if cond else 'FAIL'}]   {name}" + (f"  {detail}" if not cond else ""))


print("=== 1. 密钥是否已从 config.json 移除 ===")
with open("config.json", encoding="utf-8") as f:
    raw = json.load(f)
raw_text = json.dumps(raw)

check("config.json 里飞书 secret 为空", raw["feishu"]["app_secret"] == "", raw["feishu"])
check("config.json 里 QQ secret 为空", raw["qq"]["app_secret"] == "", raw["qq"])
check("config.json 里飞书 app_id 为空", raw["feishu"]["app_id"] == "", raw["feishu"])
check("config.json 里 QQ app_id 为空", raw["qq"]["app_id"] == "", raw["qq"])


def _env_values():
    """从 .env 里读出真实凭据值，用来反向校验 config.json 没残留。

    刻意不把密钥字面量写进本文件（哪怕是片段），否则这份可提交的脚本
    本身就成了泄漏点。
    """
    values = {}
    try:
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip("'\"")
                if v:
                    values[k.strip()] = v
    except FileNotFoundError:
        pass
    return values


secrets = {
    k: v
    for k, v in _env_values().items()
    if "SECRET" in k or "APP_ID" in k or "API_KEY" in k
}
check("从 .env 读到待比对的凭据项", len(secrets) >= 2, f"实际 {sorted(secrets)}")

leaked = [k for k, v in secrets.items() if v in raw_text]
check(
    "config.json 不含 .env 里的任何凭据值",
    not leaked,
    f"泄漏项: {leaked}",
)

print("\n=== 2. 密钥是否已从 .env 正确载入 ===")
from config import load_config

cfg = load_config()

check("飞书 app_id 已从 .env 载入", bool(cfg.feishu_app_id), cfg.feishu_app_id)
check("飞书 app_secret 已从 .env 载入", bool(cfg.feishu_app_secret))
check("QQ app_id 已从 .env 载入", bool(cfg.qq_app_id), cfg.qq_app_id)
check("QQ app_secret 已从 .env 载入", bool(cfg.qq_app_secret))
check("API Key 已载入", bool(cfg.api_key))
check("workspace 解析为绝对路径", os.path.isabs(cfg.workspace), cfg.workspace)
check("config_dir 已设置", os.path.isdir(cfg.config_dir), cfg.config_dir)

print("\n=== 3. Agent 组装（build_agent 不再重复 load_config）===")
import io
from contextlib import redirect_stdout

from main import build_agent

buf = io.StringIO()
with redirect_stdout(buf):
    agent1 = build_agent(cfg, "cli:local")
    agent2 = build_agent(cfg, "cli:local")

out = buf.getvalue()
# 修复 1.6 的判据：build_agent 内部不应再解析配置。
# config 在本脚本第 2 步已加载过（那次打印在 redirect 之外），
# 所以 redirect 块内出现 0 次才对；原实现会在这里再打印 2 次（每个会话一次）。
n_cfg_line = out.count("[配置] 配置文件:")
check(
    "build_agent 内部不再解析配置（原 bug 会每会话解析一次）",
    n_cfg_line == 0,
    f"块内出现 {n_cfg_line} 次",
)

check("Agent 组装成功", agent1 is not None)
check("工具已注册", len(agent1.tools.list_tools()) > 0, str(agent1.tools.list_tools()))
check("技能摘要非空", bool(agent1.context.skills_summary))
check("Token 压缩器已挂载", agent1.consolidator is not None)
check("Provider 超时已设置", getattr(agent1.provider, "timeout", None) is not None)
check("SDK 内部重试已关闭", getattr(agent1.provider.client, "max_retries", None) == 0)
check("熔断计数初始为空", agent1._tool_call_history == [])

print("\n=== 3b. 权限闸门是否已接入 ===")
check("permission_gate 已挂载", agent1.permission_gate is not None)
check("approval_store 已挂载", agent1.approval_store is not None)
check("顶层 Agent 的 interactive=True", agent1.interactive is True)
check("权限确认默认开启", agent1.permission_gate.enabled == cfg.permissions_enabled)
check("approvals 目录已就绪", os.path.isdir(agent1.approval_store.base_dir), agent1.approval_store.base_dir)

from agent.permissions import RISK_AUTO, RISK_CONFIRM, RISK_DENY

_risk_by_tool = {
    t: getattr(agent1.tools.get(t), "risk_level", "?") for t in agent1.tools.list_tools()
}
print("   工具风险等级：")
for name, risk in _risk_by_tool.items():
    print(f"     {name:26} {risk}")

check("read_file 是 auto", _risk_by_tool.get("read_file") == RISK_AUTO)
check("write_file 是 confirm", _risk_by_tool.get("write_file") == RISK_CONFIRM)
check("exec 是 confirm", _risk_by_tool.get("exec") == RISK_CONFIRM)
# MCP 工具只在本测试传入 mcp_manager 时才会注册；没注册就跳过这条断言
# （真实启动有 MCP，见启动日志里的 `poetry__search_poetry [auto]`）
if "poetry__search_poetry" in _risk_by_tool:
    check(
        "MCP 诗词工具按配置为 auto",
        _risk_by_tool.get("poetry__search_poetry") == RISK_AUTO,
        str(_risk_by_tool.get("poetry__search_poetry")),
    )
else:
    print("  [SKIP] MCP 工具未注册（本测试未连 MCP Server），跳过 MCP 等级断言")

# 危险命令应被判定为 deny（不给确认机会）
_risk, _ = agent1.permission_gate.classify(
    agent1.tools.get("exec"), {"command": "rm -rf /"}
)
check("rm -rf 被判为 deny", _risk == RISK_DENY, _risk)

print("\n=== 3c. 追踪与成本核算是否已接入 ===")
check("tracer 已挂载", agent1.tracer is not None)
check("usage_meter 已挂载", agent1.usage_meter is not None)
check("budget 已传入", isinstance(agent1.budget, dict))
check("trace 目录已就绪", os.path.isdir(agent1.tracer.base_dir), agent1.tracer.base_dir)
check(
    "trace 目录在 <workspace>/workspace/traces 下（与会话/记忆同层）",
    os.path.basename(agent1.tracer.base_dir) == "traces"
    and os.path.basename(os.path.dirname(agent1.tracer.base_dir)) == "workspace",
    agent1.tracer.base_dir,
)
check(
    "approvals 目录在 <workspace>/workspace/approvals 下",
    os.path.basename(agent1.approval_store.base_dir) == "approvals"
    and os.path.basename(os.path.dirname(agent1.approval_store.base_dir)) == "workspace",
    agent1.approval_store.base_dir,
)
check(
    "用量统计文件路径在 traces/sessions 下",
    "sessions" in agent1.usage_meter.path,
    agent1.usage_meter.path,
)
check(
    "会话 key 在文件名里被编码",
    "%3A" in agent1.usage_meter.path or ":" not in os.path.basename(agent1.usage_meter.path),
    os.path.basename(agent1.usage_meter.path),
)
check("价目表已读入（可为空）", isinstance(cfg.pricing, dict))
check(
    "未配置价目表时成本显示为说明文字",
    agent1.usage_meter.cost_display() == "未配置价目表"
    if not cfg.pricing
    else agent1.usage_meter.cost_display().startswith("$"),
    agent1.usage_meter.cost_display(),
)
check("用量摘要行可生成", bool(agent1.usage_meter.summary_line()))

# /cost 与 /trace 依赖的可读接口
check("trace 时间线接口存在", callable(agent1.tracer.last_run_timeline))
check("trace 摘要接口存在", callable(agent1.tracer.last_run_summary))

print("\n=== 3d. /cost 与 /trace 的渲染 ===")
from main import render_cost_report, render_trace_timeline

_cost_text = render_cost_report(agent1)
check("/cost 能渲染", _cost_text.startswith("本会话累计"), _cost_text[:60])
# 断言格式而不是具体数值：会话用量是持久化的，跑过真实对话后 token 不为 0
check("含 token 统计", "token" in _cost_text and "入" in _cost_text, _cost_text[:80])
check("含成本字段", "成本" in _cost_text, _cost_text[:80])
check("含运行次数", "运行" in _cost_text, _cost_text[:80])

_trace_text = render_trace_timeline(agent1)
check(
    "/trace 在无运行时给出提示",
    "还没有运行记录" in _trace_text,
    _trace_text[:60],
)

# 手工灌几个事件，验证时间线渲染
agent1.tracer.start_run("测试问题")
agent1.tracer.event(
    "llm_response",
    model="stub",
    latency_ms=120,
    tokens={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    cost_usd=None,
    n_tool_calls=0,
)
agent1.tracer.event("tool_result", name="read_file", latency_ms=8, ok=True,
                    result_len=42, preview="内容")
agent1.tracer.finish_run("ok", "答案")

_trace_text2 = render_trace_timeline(agent1)
check("/trace 渲染出时间线", "llm_response" in _trace_text2, _trace_text2[:120])
check("/trace 带 trace_id 标题", "trace（" in _trace_text2)
check("/trace 显示耗时", "ms" in _trace_text2)

print("\n=== 3e. CLI 输入清洗（BOM / 零宽字符）===")
from channels.cli import normalize_input

check(
    "带 BOM 的输入被剥离（否则 /cost 会被当成问题发给模型）",
    normalize_input("\ufeff/cost") == "/cost",
    repr(normalize_input("\ufeff/cost")),
)
check("零宽空格被剥离", normalize_input("\u200b/tools") == "/tools")
check("零宽连接符被剥离", normalize_input("\u2060/exit") == "/exit")
check("普通输入不被破坏", normalize_input("  你好，帮我看看  ") == "你好，帮我看看")
check("空输入安全", normalize_input("") == "")
check("纯空白输入变空", normalize_input("   ") == "")

print("\n=== 4. 工具表清单 ===")
for name in agent1.tools.list_tools():
    print(f"   - {name}")

print("\n" + "=" * 56)
print(f"通过 {len(OK)} 项，失败 {len(BAD)} 项")
if BAD:
    for b in BAD:
        print("  失败: " + b)
    sys.exit(1)
print("装配冒烟全部通过")
