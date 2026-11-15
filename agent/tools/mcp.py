"""
MCP (Model Context Protocol) 工具模块

提供 MCPClientManager 和 MCPTool，集成外部 MCP Server 到 NanoClaw。

支持的传输
----------
    stdio      本地子进程（command + args），最常见
    http       Streamable HTTP（url + headers），远程服务，可带鉴权
    sse        HTTP SSE（url + headers），老一代远程传输，仍有很多 Server 用

配置里不写 `type` 时自动推断：有 `url` 就是 http，有 `command` 就是 stdio。
这样老的配置（只有 command/args）不改也能跑。

自愈能力
--------
1. **调用失败自动重连一次**：Server 进程崩了 / 网络断了，先重建连接再重试
2. **调用超时**：单次 call_tool 有超时，不会把整个会话挂死
3. **健康检查**：`check_health()` 逐个探活；`health_loop()` 可后台周期跑
4. **幂等关闭**：`shutdown()` 可重复调用，不会二次 `__aexit__` 报错

关键约束（都是踩过的坑）
------------------------
- 所有 print 不使用 emoji（Windows GBK 会崩），用 [MCP]、[!] 代替
- `ClientSession` 必须手动 `__aenter__()`，否则内部消息循环不启动，
  `initialize()` 会永远卡死
- stdio 子进程必须指定 `cwd`，否则配置里的相对路径会按"启动时的目录"解析
- 退出顺序必须**逆序**：先关 ClientSession，再关 transport 上下文
"""

import asyncio
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.tools.base import Tool

# 传输类型常量
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORT_SSE = "sse"


def _resolve_transport(server_config: dict) -> str:
    """从配置推断传输类型。"""
    declared = str(server_config.get("type") or "").strip().lower()
    if declared in ("http", "streamable-http", "streamablehttp"):
        return TRANSPORT_HTTP
    if declared == "sse":
        return TRANSPORT_SSE
    if declared == "stdio":
        return TRANSPORT_STDIO
    # 没声明：按配置形状推断（兼容老配置）
    return TRANSPORT_HTTP if server_config.get("url") else TRANSPORT_STDIO


def _build_headers(server_config: dict) -> dict[str, str]:
    """组装 HTTP 请求头，支持 headers 字典与 bearer_token 简写。"""
    headers = {
        str(k): str(v) for k, v in (server_config.get("headers") or {}).items()
    }
    token = server_config.get("bearer_token")
    if token and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class MCPTool(Tool):
    """
    MCP 工具包装器

    将 MCP Server 提供的工具包装为 NanoClaw 的 Tool 接口。
    工具名格式: {server_name}__{tool_name}，避免跨 server 冲突。
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        tool_description: str,
        input_schema: dict,
        client_manager: "MCPClientManager",
        risk_level: str = "confirm",
    ):
        """
        Args:
            server_name: MCP Server 名称（配置中的 key）
            tool_name: MCP Server 提供的工具名
            tool_description: 工具描述（从 MCP list_tools 获取）
            input_schema: 工具参数 schema（从 MCP list_tools 获取）
            client_manager: MCP 客户端管理器，用于执行工具调用
            risk_level: 风险等级。MCP 工具的能力由外部 Server 定义，引擎无法
                自动判断它会不会改数据，所以**默认 confirm**（要人工确认）。
                确定是只读查询的 Server，在 config.json 里显式写
                `"risk_level": "auto"` 或按工具粒度
                `"tools": {"search": {"risk_level": "auto"}}` 放宽。
        """
        self.server_name = server_name
        self.tool_name = tool_name
        self._description = tool_description
        self._input_schema = input_schema
        self.client_manager = client_manager
        self.risk_level = risk_level

    @property
    def name(self) -> str:
        """工具名称，格式: {server}__{tool}"""
        return f"{self.server_name}__{self.tool_name}"

    @property
    def description(self) -> str:
        """工具描述"""
        return self._description

    @property
    def parameters(self) -> dict:
        """工具参数 schema，直接使用 inputSchema"""
        return self._input_schema

    async def execute(self, **kwargs: Any) -> str:
        """
        执行 MCP 工具

        通过 client_manager 调用远程 MCP Server 的工具。

        Args:
            **kwargs: 工具参数

        Returns:
            str: 工具执行结果
        """
        return await self.client_manager.call_tool(
            self.server_name, self.tool_name, kwargs
        )


class MCPClientManager:
    """
    MCP 客户端管理器

    管理多个 MCP Server 的连接生命周期、工具调用、断线重连与健康检查。

    使用流程：
    1. 传入 mcp_config（格式同 Claude Desktop，扩展了 type / url / headers）
    2. `await connect_all()` 启动所有 server
    3. `get_tools()` 取回所有 MCPTool 并注册到 ToolRegistry
    4. `call_tool()` 执行工具
    5. `await shutdown()` 清理连接
    """

    # 单个 server 连接超时
    DEFAULT_CONNECT_TIMEOUT = 30.0
    # 单次工具调用超时（MCP 工具可能是远程的，必须兜底）
    DEFAULT_CALL_TIMEOUT = 60.0

    def __init__(
        self,
        mcp_config: dict,
        base_dir: str | None = None,
        connect_timeout: float | None = None,
        call_timeout: float | None = None,
        auto_reconnect: bool = True,
    ):
        """
        Args:
            mcp_config: MCP Server 配置字典
            base_dir: 相对路径的解析基准目录（通常是 config.json 所在目录）
            connect_timeout: 连接超时（秒）
            call_timeout: 单次工具调用超时（秒）
            auto_reconnect: 调用失败时是否自动重连一次

        ★ 为什么需要 base_dir（真实踩坑）：
          config.json 里写的是 "args": ["mcp_servers/poetry_server.py"]，
          这是个**相对路径**。不指定 cwd 的话，子进程会继承"你敲命令时所在的目录"。
          于是从别的目录启动 main.py 时，报的是：
              can't open file 'D:\\...\\nanoclaw\\mcp_servers\\poetry_server.py'
          —— 注意路径里少了一层 nanoclaw，因为它按父目录去找了。
          结果是 [MCP] 连接失败、工具列表里少了 poetry 的 3 个工具，
          但程序照样启动，只有一行容易看漏的 "[!] ... 连接失败，跳过"。
          指定 cwd 之后，MCP Server 的相对路径就固定以项目目录为基准了。
        """
        self.mcp_config = mcp_config or {}
        self.base_dir = base_dir
        self.connect_timeout = (
            self.DEFAULT_CONNECT_TIMEOUT
            if connect_timeout is None
            else connect_timeout
        )
        self.call_timeout = (
            self.DEFAULT_CALL_TIMEOUT if call_timeout is None else call_timeout
        )
        self.auto_reconnect = auto_reconnect

        # server_name -> {"config", "session", "entered", "transport", "tools"}
        self._servers: dict[str, dict[str, Any]] = {}
        self._tools: list[MCPTool] = []
        self._closed = False
        self._health_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    async def _enter_transport(
        self, server_name: str, server_config: dict
    ) -> tuple[Any, Any, str, list[Any]]:
        """进入传输层，返回 (read_stream, write_stream, 传输类型, 已进入的上下文列表)。"""
        transport = _resolve_transport(server_config)
        entered: list[Any] = []

        if transport == TRANSPORT_STDIO:
            if not server_config.get("command"):
                raise ValueError("stdio 传输缺少 command")
            # cwd：让子进程在项目目录下启动，这样 args 里的相对路径才找得到
            # （也可以在 config.json 里给单个 server 单独写 "cwd" 覆盖）
            server_cwd = server_config.get("cwd") or self.base_dir
            params = StdioServerParameters(
                command=server_config["command"],
                args=server_config.get("args", []),
                env=server_config.get("env", None),
                cwd=server_cwd,
            )
            if server_cwd:
                print(f"[MCP]   cwd = {server_cwd}")
            ctx = stdio_client(params)
            streams = await ctx.__aenter__()
            entered.append(ctx)

        elif transport == TRANSPORT_HTTP:
            from mcp.client.streamable_http import streamablehttp_client

            url = server_config.get("url")
            if not url:
                raise ValueError("http 传输缺少 url")
            headers = _build_headers(server_config)
            print(
                f"[MCP]   url = {url}"
                + (f"（携带 {len(headers)} 个请求头）" if headers else "")
            )
            ctx = streamablehttp_client(
                url,
                headers=headers or None,
                timeout=server_config.get("timeout", self.call_timeout),
                sse_read_timeout=server_config.get(
                    "sse_read_timeout", self.call_timeout * 5
                ),
            )
            streams = await ctx.__aenter__()
            entered.append(ctx)

        elif transport == TRANSPORT_SSE:
            from mcp.client.sse import sse_client

            url = server_config.get("url")
            if not url:
                raise ValueError("sse 传输缺少 url")
            headers = _build_headers(server_config)
            print(f"[MCP]   url = {url}（SSE 传输）")
            ctx = sse_client(url, headers=headers or None)
            streams = await ctx.__aenter__()
            entered.append(ctx)

        else:  # pragma: no cover - 防御
            raise ValueError(f"未知传输类型: {transport}")

        # 不同传输返回的元组长度不同（stdio/sse 是 2 元，http 是 3 元）
        return streams[0], streams[1], transport, entered

    async def _connect_one(self, server_name: str, server_config: dict) -> None:
        """连接单个 MCP Server（调用方负责超时）。"""
        print(f"[MCP] 正在连接 {server_name}...")

        read_stream, write_stream, transport, entered = await self._enter_transport(
            server_name, server_config
        )

        try:
            # ClientSession 也必须手动 __aenter__ 启动内部消息循环，
            # 否则 initialize() 会永远等待
            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            entered.append(session)

            await session.initialize()
            tools_response = await session.list_tools()
        except Exception:
            # 半途失败：把已经进入的上下文按逆序退掉，别泄漏子进程
            await self._close_entered(entered)
            raise

        # 风险等级：Server 级默认值 + 工具级覆盖
        default_risk = server_config.get("risk_level", "confirm")
        tool_risks = server_config.get("tools") or {}

        new_tools: list[MCPTool] = []
        for tool_info in tools_response.tools:
            tool_risk = default_risk
            override = tool_risks.get(tool_info.name)
            if isinstance(override, dict) and override.get("risk_level"):
                tool_risk = override["risk_level"]

            mcp_tool = MCPTool(
                server_name=server_name,
                tool_name=tool_info.name,
                tool_description=tool_info.description or "",
                input_schema=tool_info.inputSchema or {},
                client_manager=self,
                risk_level=tool_risk,
            )
            new_tools.append(mcp_tool)
            print(
                f"[MCP]   - 加载工具: {mcp_tool.name} "
                f"[{tool_risk}] ({tool_info.description[:40]}...)"
            )

        # 同一个 server 之前若已注册过工具，先按名字替换，避免重复
        existing_names = {t.name for t in new_tools}
        self._tools = [t for t in self._tools if t.name not in existing_names]
        self._tools.extend(new_tools)

        self._servers[server_name] = {
            "config": server_config,
            "session": session,
            "entered": entered,
            "transport": transport,
            "tools": new_tools,
        }
        self._closed = False

        print(
            f"[MCP] {server_name} 连接成功（{transport}），"
            f"加载 {len(tools_response.tools)} 个工具"
        )

    async def _close_entered(self, entered: list[Any]) -> None:
        """按【逆序】退出上下文管理器。

        顺序很重要：ClientSession 必须先退，transport 才能关；
        反过来关会让 session 的消息循环对着已关闭的流报错。
        """
        for ctx in reversed(entered):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as e:
                print(f"[!] 关闭上下文失败: {e}")

    async def connect_all(self, timeout: float | None = None) -> None:
        """
        连接所有 MCP Server

        单个 server 超时或异常则跳过并打印警告，不影响其它 server。

        Args:
            timeout: 单个 server 连接超时秒数
        """
        if not self.mcp_config:
            print("[MCP] 未配置 MCP Server，跳过")
            return

        limit = self.connect_timeout if timeout is None else timeout
        print(f"[MCP] 开始连接 {len(self.mcp_config)} 个 MCP Server...")

        for server_name, server_config in self.mcp_config.items():
            try:
                await asyncio.wait_for(
                    self._connect_one(server_name, server_config), timeout=limit
                )
            except asyncio.TimeoutError:
                print(f"[!] MCP Server {server_name} 连接超时（{limit}s），跳过")
            except Exception as e:
                print(f"[!] MCP Server {server_name} 连接失败: {e}，跳过")

    # ------------------------------------------------------------------
    # 重连与健康
    # ------------------------------------------------------------------
    async def _reconnect(self, server_name: str) -> bool:
        """重建某个 server 的连接。成功返回 True。

        ★ 为什么要自动重连：stdio 的 Server 是个子进程，它崩掉、被 kill、
          或者远程 HTTP 服务抖一下，之后所有工具调用都会永远返回
          "未连接"。用户看到的是"这个工具突然不能用了"，而重启整个进程
          才能恢复 —— 这在小工具上尤其讨厌。

        ★ 配置从哪里拿：从 `self.mcp_config` 取，而不是从 `self._servers`。
          因为连接失效时状态可能已经被 `_close_server` 清掉了，
          这时再回头找 `state["config"]` 会拿到 None —— 重连就永远不会成功。
        """
        config = self.mcp_config.get(server_name)
        if config is None:
            # 也兼容运行期动态加进来的 server
            state = self._servers.get(server_name)
            config = (state or {}).get("config")
        if config is None:
            return False

        print(f"[MCP] {server_name} 连接已失效，尝试重连...")
        await self._close_server(server_name)

        try:
            await asyncio.wait_for(
                self._connect_one(server_name, config),
                timeout=self.connect_timeout,
            )
            return True
        except asyncio.TimeoutError:
            print(f"[!] {server_name} 重连超时（{self.connect_timeout}s）")
        except Exception as e:
            print(f"[!] {server_name} 重连失败: {e}")
        return False

    async def _close_server(self, server_name: str) -> None:
        """关闭单个 server（幂等）。"""
        state = self._servers.pop(server_name, None)
        if state is None:
            return
        await self._close_entered(state.get("entered") or [])

    async def check_health(self) -> dict[str, str]:
        """逐个 Server 探活。

        Returns:
            dict[str, str]: server_name -> "ok" / "down: 原因"
        """
        report: dict[str, str] = {}
        for server_name, state in list(self._servers.items()):
            session = state.get("session")
            if session is None:
                report[server_name] = "down: 无会话"
                continue
            try:
                await asyncio.wait_for(
                    session.list_tools(), timeout=min(10.0, self.call_timeout)
                )
                report[server_name] = "ok"
            except Exception as e:
                report[server_name] = f"down: {type(e).__name__}"
        return report

    async def health_loop(self, interval: float = 60.0) -> None:
        """后台周期探活，发现 down 的尝试重连。用 asyncio.create_task 启动。"""
        while not self._closed:
            try:
                await asyncio.sleep(interval)
                if self._closed:
                    break
                report = await self.check_health()
                for server_name, status in report.items():
                    if status != "ok":
                        print(f"[MCP] 健康检查: {server_name} {status}")
                        if self.auto_reconnect:
                            await self._reconnect(server_name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[!] MCP 健康检查异常: {e}")

    def start_health_loop(self, interval: float = 60.0) -> None:
        """启动后台健康检查（需在事件循环内调用）。"""
        if self._health_task is not None and not self._health_task.done():
            return
        self._health_task = asyncio.create_task(self.health_loop(interval))
        print(f"[MCP] 已启动健康检查（每 {interval:.0f}s）")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def get_tools(self) -> list[Tool]:
        """获取所有 MCPTool。"""
        return list(self._tools)

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> str:
        """调用 MCP Server 的工具。

        失败时（未连接 / 超时 / 异常）若开启 auto_reconnect，会重建连接再重试一次。
        """
        state = self._servers.get(server_name)

        if state is None or state.get("session") is None:
            if not (self.auto_reconnect and await self._reconnect(server_name)):
                return f"错误: MCP Server '{server_name}' 未连接"
            state = self._servers.get(server_name)
            if state is None:
                return f"错误: MCP Server '{server_name}' 未连接"

        result = await self._invoke(state["session"], tool_name, arguments)
        if not isinstance(result, Exception):
            return result

        # 第一次失败：重连后重试一次
        if self.auto_reconnect and await self._reconnect(server_name):
            state = self._servers.get(server_name)
            if state is not None:
                retried = await self._invoke(
                    state["session"], tool_name, arguments
                )
                if not isinstance(retried, Exception):
                    return retried
                result = retried

        return f"错误: MCP 工具调用失败 - {result}"

    async def _invoke(
        self, session: Any, tool_name: str, arguments: dict
    ) -> str | Exception:
        """调用一次工具。成功返回文本，失败返回异常对象（不抛）。"""
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments), timeout=self.call_timeout
            )
        except asyncio.TimeoutError:
            return TimeoutError(f"调用超时（{self.call_timeout}s）")
        except Exception as e:
            return e

        if result.content:
            parts = []
            for item in result.content:
                if hasattr(item, "text"):
                    parts.append(item.text)
                elif hasattr(item, "data"):
                    parts.append(str(item.data))
            return "\n".join(parts) if parts else "（无文本内容）"
        return "（无返回内容）"

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    async def shutdown(self) -> None:
        """清理所有连接。可重复调用（幂等）。"""
        self._closed = True

        if self._health_task is not None and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except (asyncio.CancelledError, Exception):
                pass
        self._health_task = None

        if not self._servers:
            return

        print("[MCP] 正在关闭 MCP Server 连接...")
        for server_name in list(self._servers.keys()):
            try:
                await self._close_server(server_name)
            except Exception as e:
                print(f"[!] 关闭 {server_name} 失败: {e}")
        print("[MCP] MCP Server 连接已全部关闭")
