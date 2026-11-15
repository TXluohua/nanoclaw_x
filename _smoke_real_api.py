"""真实 API 冒烟测试（★ 会产生真实费用）

我（AI）全程用 StubProvider 做测试，**真实模型链路需要你自己跑一次确认**。
这个脚本只发 1~2 次请求，消耗通常在几百 token 量级，成本可以忽略，
但足以验证：流式真的能流、工具调用真的能被解析、权限闸门真的会拦。

用法：
    cd nanoclaw/nanoclaw
    python _smoke_real_api.py

它会：
1. 打印你的配置摘要（模型、流式开关、权限开关）
2. 让你确认后再发起真实请求
3. 跑一轮「读目录」的只读对话（不触发人工确认）
4. 打印 trace 时间线和本次花费

注意：脚本**不会**执行任何写操作，也不会碰你的文件。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.permissions import ApprovalStore, PermissionGate
from agent.tools.filesystem import ListDirTool, ReadFileTool
from agent.tools.registry import ToolRegistry
from agent.tracing import CostEstimator, Tracer, UsageMeter
from config import load_config
from providers.openai_compat import OpenAICompatProvider
from session.manager import SessionManager


async def main() -> int:
    config = load_config()

    print("=" * 62)
    print("  真实 API 冒烟测试")
    print("=" * 62)
    print(f"  模型      : {config.model}")
    print(f"  接口      : {config.base_url}")
    print(f"  API Key   : {config.api_key[:8]}…{config.api_key[-4:] if config.api_key else ''}")
    print(f"  流式输出  : {'开' if config.streaming_enabled else '关'}")
    print(f"  权限确认  : {'开' if config.permissions_enabled else '关'}")
    print(f"  记忆召回  : {'开' if config.memory_enabled else '关'}")
    print("=" * 62)

    if not config.api_key:
        print("✗ 没读到 API Key，请检查 .env 里的 NANOCLAW_API_KEY")
        return 1

    answer = input("\n这会发起真实请求（花费极小）。继续？[y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("已取消")
        return 0

    workspace = config.workspace
    tracer = Tracer(
        base_dir=os.path.join(workspace, "workspace", "traces"),
        session_key="smoke:real",
    )

    tools = ToolRegistry(default_timeout=30)
    tools.register(ReadFileTool(workspace))
    tools.register(ListDirTool(workspace))

    agent = AgentLoop(
        provider=OpenAICompatProvider(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
        ),
        tools=tools,
        context=ContextBuilder(
            workspace=workspace,
            identity_file="",
            skills_summary="",
        ),
        session_manager=SessionManager(
            sessions_dir=os.path.join(workspace, "workspace", "sessions_smoke")
        ),
        model=config.model,
        max_iterations=4,
        session_key="smoke:real",
        permission_gate=PermissionGate(
            enabled=config.permissions_enabled,
            store=ApprovalStore(
                base_dir=os.path.join(workspace, "workspace", "approvals")
            ),
        ),
        tracer=tracer,
        usage_meter=UsageMeter(
            path=os.path.join(workspace, "workspace", "traces", "sessions", "smoke.json"),
            estimator=CostEstimator(config.pricing),
            session_key="smoke:real",
        ),
        streaming=config.streaming_enabled,
    )

    # 装一个把增量直接打到终端的 sink
    first = {"done": False}

    async def sink(text: str) -> None:
        if not first["done"]:
            print("\n[流式输出] ", end="", flush=True)
            first["done"] = True
        print(text, end="", flush=True)

    agent.set_delta_sink(sink)

    print("\n--- 第 1 轮：只读任务（不应触发人工确认）---")
    try:
        # ★ 故意要求"分点说明"，让回复长一点。
        #   只说一句 13 个字的话，首字延迟和总耗时几乎相等，
        #   从数字上根本看不出流式有没有生效。
        out = await agent.run(
            "先用 list_dir 看一眼当前目录，然后用【5 个要点】简要说明这个项目目录的"
            "组织方式（每个要点一句话）。不要读写任何文件内容。"
        )
    except Exception as e:
        print(f"\n✗ 调用失败：{type(e).__name__}: {e}")
        return 1
    finally:
        # ★ 必须关掉 Provider 的 HTTP 客户端。
        #   不关的话连接池要等垃圾回收才释放，而 GC 往往在事件循环关闭之后，
        #   于是退出时刷一屏 `generator didn't stop after athrow()`。
        await agent.aclose()

    print(f"\n\n[最终回答] {out}")

    summary = tracer.last_run_summary() or {}
    print("\n" + "=" * 62)
    print("  本次运行摘要")
    print("=" * 62)
    print(f"  状态      : {summary.get('status')}")
    print(f"  耗时      : {summary.get('duration_ms')} ms")
    print(f"  模型调用  : {summary.get('llm_calls')} 次")
    print(f"  工具调用  : {summary.get('tool_calls')} 次")
    print(f"  token     : {summary.get('total_tokens')}"
          f"（入 {summary.get('prompt_tokens')} / 出 {summary.get('completion_tokens')}）")
    cost = summary.get("cost_usd") or 0.0
    print(f"  成本      : {'未配置价目表' if not config.pricing else f'${cost:.6f}'}")

    print("\n--- trace 时间线 ---")
    print(Tracer.format_timeline(tracer.last_run_timeline()))

    # 用数字判断流式是否真的生效，而不是靠"看起来像不像"
    llm_events = [
        e for e in tracer.last_run_timeline() if e["kind"] == "llm_response"
    ]
    streamed = [e for e in llm_events if e.get("first_delta_ms") is not None]
    print("\n" + "=" * 62)
    print("  流式自检")
    print("=" * 62)
    if streamed:
        ev = streamed[-1]
        first = ev.get("first_delta_ms") or 0
        total = ev.get("latency_ms") or 0
        print(f"  ✅ 首字延迟 {first}ms / 该轮总耗时 {total}ms")
        if total and first < total * 0.8:
            print("     首字明显早于完成 —— 用户确实提前看到内容了，流式生效")
        else:
            print("     ⚠ 首字接近总耗时：内容太短或服务端一次性返回，看不出流式效果")
    else:
        print("  ❌ 没有记录到 first_delta_ms —— 流式没走到")
        print("     检查 config.json 的 streaming.enabled，或该渠道是否支持流式")

    print("\n" + "=" * 62)
    print("  请人工确认以下几点")
    print("=" * 62)
    print("  1. 上面 [流式输出] 那段是不是【逐字冒出来】的（而不是一次性出现）")
    print("  2. 时间线里有 tool_result 吗（说明工具真的被调用了）")
    print("  3. 有没有出现 permission ... → auto（只读操作不该要你确认）")
    print("  4. 如果出现了 pending_saved，说明模型想做写操作 —— 这轮不该有")
    print("\n这个脚本没执行任何写操作，可以放心跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
