"""MCP 双传输 + 重连 + 探活 的手动验证（会真的起子进程）

用法： cd nanoclaw/nanoclaw && python _verify_mcp_runtime.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.tools.mcp import (  # noqa: E402
    TRANSPORT_HTTP,
    TRANSPORT_SSE,
    TRANSPORT_STDIO,
    MCPClientManager,
    _build_headers,
    _resolve_transport,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  [FAIL] {name}  {detail}")


CONFIG = {
    "poetry": {
        "type": "stdio",
        "command": sys.executable,
        "args": ["mcp_servers/poetry_server.py"],
        "description": "古诗词查询服务",
        "risk_level": "auto",
    }
}


async def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    print("\n=== 1. 传输类型推断 ===")
    cases = [
        ({"command": "python", "args": ["x.py"]}, TRANSPORT_STDIO),
        ({"type": "stdio", "command": "python"}, TRANSPORT_STDIO),
        ({"url": "http://localhost/mcp"}, TRANSPORT_HTTP),
        ({"type": "http", "url": "http://localhost/mcp"}, TRANSPORT_HTTP),
        ({"type": "streamable-http", "url": "http://x"}, TRANSPORT_HTTP),
        ({"type": "sse", "url": "http://x"}, TRANSPORT_SSE),
    ]
    for cfg, expected in cases:
        check(f"{cfg} → {expected}", _resolve_transport(cfg) == expected,
              _resolve_transport(cfg))

    print("\n=== 2. 请求头组装 ===")
    check(
        "bearer_token 生成 Authorization",
        _build_headers({"bearer_token": "abc"}) == {"Authorization": "Bearer abc"},
        str(_build_headers({"bearer_token": "abc"})),
    )
    headers = _build_headers(
        {"headers": {"X-Custom": "1"}, "bearer_token": "abc"}
    )
    check("显式 headers 与 bearer 共存", headers == {
        "X-Custom": "1", "Authorization": "Bearer abc"}, str(headers))
    explicit = _build_headers(
        {"headers": {"Authorization": "Basic zzz"}, "bearer_token": "abc"}
    )
    check("显式 Authorization 不被 bearer 覆盖",
          explicit["Authorization"] == "Basic zzz", str(explicit))

    print("\n=== 3. 真实连接 stdio MCP Server ===")
    manager = MCPClientManager(CONFIG, base_dir=base_dir, auto_reconnect=True)
    await manager.connect_all(timeout=30.0)

    tools = manager.get_tools()
    check("连上了并加载 3 个工具", len(tools) == 3, str(len(tools)))
    names = sorted(t.name for t in tools)
    check(
        "工具名带 server 前缀",
        all(n.startswith("poetry__") for n in names),
        str(names),
    )
    check("工具风险等级来自配置(auto)",
          all(getattr(t, "risk_level", None) == "auto" for t in tools))

    print("\n=== 4. 工具调用 ===")
    result = await manager.call_tool(
        "poetry", "search_poetry", {"keyword": "明月"}
    )
    check("搜索返回内容", isinstance(result, str) and len(result) > 5, result[:80])
    check("结果里出现「明月」", "明月" in result, result[:80])

    print("\n=== 5. 健康检查 ===")
    report = await manager.check_health()
    check("poetry 探活 ok", report.get("poetry") == "ok", str(report))

    print("\n=== 6. 断线重连（模拟连接丢失）===")
    # 直接关掉 server 的连接，模拟子进程崩溃/网络断开
    await manager._close_server("poetry")
    check("连接已被关闭", manager._servers.get("poetry") is None)
    check("关闭后工具条目仍保留（供注册表继续持有）", len(manager.get_tools()) == 3)

    # 此时 call_tool 应当自动重连并成功
    result2 = await manager.call_tool(
        "poetry", "random_poetry", {}
    )
    check(
        "★ 调用失败后自动重连并返回结果",
        isinstance(result2, str) and "错误" not in result2[:4],
        result2[:100],
    )
    check("重连后 server 恢复", manager._servers.get("poetry") is not None)

    print("\n=== 7. 未知 Server 的调用 ===")
    bad = await manager.call_tool("not_exist", "x", {})
    check("未知 server 返回错误而不是抛异常", bad.startswith("错误"), bad[:60])

    print("\n=== 8. 幂等关闭 ===")
    await manager.shutdown()
    check("shutdown 后无残留 server", manager._servers == {})
    await manager.shutdown()  # 第二次不应该炸
    check("shutdown 可重复调用（幂等）", True)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  失败: " + f)
        return 1
    print("MCP 运行时验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
