"""
NanoClaw 入口文件

提供命令行交互界面，组装 Agent 各组件并启动对话循环。

使用方法：
    python main.py

命令：
    /exit   - 退出程序
    /clear  - 清空对话历史
    /tools  - 查看可用工具列表
"""

import asyncio
import os
import sys
from urllib.parse import quote

from config import load_config
from providers.openai_compat import OpenAICompatProvider
from agent.tools.registry import ToolRegistry
from agent.tools.filesystem import ReadFileTool, WriteFileTool, ListDirTool
from agent.tools.editing import EditFileTool
from agent.tools.todo import TodoStore, TodoWriteTool
from agent.tools.shell import ExecTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.web_search import WebSearchTool
from agent.tools.spawn import SpawnSubagentTool
from agent.tools.mcp import MCPClientManager
from agent.skills import SkillsLoader
from agent.context import ContextBuilder
from agent.memory_store import MemoryRetriever
from agent.tools.memory_search import SearchMemoryTool
from agent.loop import AgentLoop
from agent.permissions import ApprovalStore, PermissionGate
from agent.tracing import CostEstimator, Tracer, UsageMeter
from session.manager import SessionManager
from agent.memory import MemoryConsolidator
from bus.queue import MessageBus
from channels.cli import CLIChannel
from channels.feishu import FeishuChannel
from channels.qq import QQChannel
from channels.web import WebChannel
from gateway import Gateway


def build_agent(config, session_key: str, mcp_manager: MCPClientManager = None) -> AgentLoop:
    """
    组装 Agent 实例
    Args:
        config: 配置对象（由 load_config() 返回）
        session_key: 会话标识，格式为 "{channel}:{sender_id}"
        mcp_manager: MCP 客户端管理器（可选）
    加载配置、创建各组件并组装 AgentLoop：
    1. 加载配置文件
    2. 验证 API 密钥
    3. 创建 LLM Provider
    4. 注册文件系统工具
    5. 注册 MCP 工具（如果有）
    6. 创建上下文构建器
    7. 组装 AgentLoop

    Returns:
        AgentLoop: 组装完成的 Agent 实例
    """
    # ★ 配置由调用方传入，这里【不能】再 load_config()。
    #   原先这一行会把入参 config 整个覆盖掉，后果是每出现一个新会话（新用户 /
    #   新群聊）就重新解析一次 config.json，并且重复打印
    #   "[配置] 配置文件: ..."；更要紧的是启动后再改配置文件，会出现
    #   "老会话用旧配置、新会话用新配置"的不一致状态，排查时极难发现。
    #   现在统一在 async_main() 里加载一次，按引用传下去。

    # 验证 API 密钥
    if not config.api_key:
        print("错误: 未配置 API 密钥")
        print("请在 config.json 中设置 api_key，或设置环境变量 NANOCLAW_API_KEY")
        sys.exit(1)

    # 创建 LLM Provider
    provider = OpenAICompatProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )

    # 创建工具注册表并注册文件系统工具
    tools = ToolRegistry()
    tools.register(ReadFileTool(config.workspace))
    tools.register(WriteFileTool(config.workspace))
    tools.register(ListDirTool(config.workspace))
    tools.register(ExecTool(config.workspace))
    tools.register(WebSearchTool())
    tools.register(WebFetchTool())

    # 片段编辑：只改目标几行，不用整篇重写（大文件省 token，也不会改坏其它部分）
    tools.register(EditFileTool(config.workspace))

    # 任务清单：长任务的白板，按会话落盘
    tools.register(
        TodoWriteTool(
            store=TodoStore(os.path.join(config.workspace, "workspace", "todos")),
            session_key=session_key,
        )
    )

        # 注册子 Agent 工具
    def create_provider(model=None):
        """Provider 工厂函数，子 Agent 用。"""
        # 子 Agent 默认用 subagent 模型（更便宜）
        if model is None and config.models:
            model = config.models.get("subagent", config.model)
        return OpenAICompatProvider(
            api_key=config.api_key,
            base_url=config.base_url,
            model=model or config.model
        )

    tools.register(SpawnSubagentTool(
        provider_factory=create_provider,
        tools_registry=tools,
        workspace=config.workspace,
    ))

    # 注册 MCP 工具（如果有）
    if mcp_manager:
        mcp_tools = mcp_manager.get_tools()
        for mcp_tool in mcp_tools:
            tools.register(mcp_tool)
        print(f"[{session_key}] 已注册 MCP 工具: {len(mcp_tools)} 个")

    # 加载技能摘要
    skills_loader = SkillsLoader(
        skills_dir=os.path.join(config.workspace, "skills")
    )
    skills_summary = skills_loader.build_skills_summary()

    # 如果发现技能，打印数量
    if skills_summary:
        skills_list = skills_loader.list_skills()
        print(f"{session_key} 已发现技能: {len(skills_list)} 个")

    # 创建会话管理器
    session_manager = SessionManager(
        sessions_dir=os.path.join(config.workspace, "workspace", "sessions")
    )

    # session_key = "cli:direct"
    # 如果有历史，显示恢复提示
    existing_history = session_manager.get_history(session_key)
    if existing_history:
        print(f"[{session_key}] 已恢复 {len(existing_history)} 条历史消息")

    # ★ 这行必须放在【所有】tools.register 之后。
    #   之前它写在这里，而 search_memory 是在下面第 176 行才注册的，
    #   于是启动日志永远只报 9 个本地工具（真实是 10 个）——
    #   日志低报工具数是很容易误导人的：排查"某个工具为什么不可用"时，
    #   你会先怀疑注册失败，而实际上它已经注册了，只是没被打印出来。

    # 长期记忆检索器：把 MEMORY.md 按行索引，每轮只召回相关的几条
    memory_retriever = MemoryRetriever(
        memory_path=os.path.join(
            config.workspace, "workspace", "memory", "MEMORY.md"
        ),
        top_k=config.memory_top_k,
        enabled=config.memory_enabled,
    )

    # 创建上下文构建器
    context = ContextBuilder(
        workspace=config.workspace,
        identity_file=config.identity_file,
        skills_summary=skills_summary,
        memory_retriever=memory_retriever,
        memory_top_k=config.memory_top_k,
    )

    # 记忆检索工具：让 Agent 能自己主动多查几次
    tools.register(SearchMemoryTool(memory_retriever))

    # 打印已注册的工具（放在最后，确保是完整的最终清单）
    all_tools = tools.list_tools()
    local_tools = [name for name in all_tools if "__" not in name]
    print(
        f"[{session_key}] 已注册工具 {len(all_tools)} 个"
        f"（本地 {len(local_tools)} + MCP {len(all_tools) - len(local_tools)}）："
        f"{all_tools}"
    )

    # 创建 Token 压缩器
    consolidator = MemoryConsolidator(
        provider=provider,
        workspace=config.workspace,
        token_budget=16000
    )

    # 创建权限闸门（人工确认）
    # ★ 放在 <workspace>/workspace/approvals 而不是 <workspace>/approvals：
    #   本项目约定"运行期用户数据"统一放在 <workspace>/workspace/ 下
    #   （sessions / memory 都是这样，见 main.py 的 SessionManager 与
    #   ContextBuilder._load_memory），这里跟着保持一致，
    #   .gitignore 里的 workspace/approvals/ 才真正生效。
    approval_store = ApprovalStore(
        base_dir=os.path.join(config.workspace, "workspace", "approvals"),
        ttl_seconds=config.permissions_pending_ttl,
    )
    permission_gate = PermissionGate(
        enabled=config.permissions_enabled,
        store=approval_store,
    )

    # 全链路追踪 + 成本核算
    # trace 落 <workspace>/workspace/traces/<日期>.jsonl，
    # 会话累计落 <workspace>/workspace/traces/sessions/
    traces_dir = os.path.join(config.workspace, "workspace", "traces")
    estimator = CostEstimator(config.pricing)
    tracer = Tracer(
        base_dir=traces_dir,
        session_key=session_key,
        estimator=estimator,
        enabled=config.trace_enabled,
    )
    usage_meter = UsageMeter(
        path=os.path.join(
            traces_dir, "sessions", quote(session_key, safe="") + ".json"
        ),
        estimator=estimator,
        session_key=session_key,
    )

    # 组装 AgentLoop
    agent = AgentLoop(
        provider=provider,
        tools=tools,
        context=context,
        session_manager=session_manager,
        model=config.model,
        max_iterations=config.max_iterations,
        # session_key="cli:direct",
        session_key=session_key,
        permission_gate=permission_gate,
        interactive=True,
        tracer=tracer,
        usage_meter=usage_meter,
        budget=config.budget,
        streaming=config.streaming_enabled,
    )

    agent.consolidator = consolidator  # 挂载压缩器

    return agent


def render_cost_report(agent: AgentLoop) -> str:
    """生成 /cost 的文本报告。

    放在模块级而不是嵌在 async_main 里，是为了能被单独调用和测试 ——
    嵌套函数的代价就是改的时候方便、验证的时候没法单独测。
    """
    meter = getattr(agent, "usage_meter", None)
    if meter is None:
        return "未启用用量统计"

    data = meter.data
    return (
        f"本会话累计 {meter.summary_line()}\n"
        f"  运行 {data['runs']} 次  "
        f"最近更新 {data.get('updated_at') or '—'}"
    )


def render_trace_timeline(agent: AgentLoop) -> str:
    """生成 /trace 的文本时间线。"""
    tracer = getattr(agent, "tracer", None)
    if tracer is None:
        return "未启用追踪"

    events = tracer.last_run_timeline()
    if not events:
        return "还没有运行记录。先问一个问题再看。"

    header = f"最近一次运行 trace（{tracer.trace_id}）："
    return header + "\n" + Tracer.format_timeline(events)


async def async_main() -> None:
    """
    异步主入口

    启动 MCP Server、组装 Agent、启动 Gateway。
    """
    # 加载配置
    config = load_config()

    # 启动 banner
    print("=" * 50)
    print("  NanoClaw - 智能代码助手")
    print(f"  模型: {config.model} ({config.base_url})")
    # ★ 把"工作区"打出来。它是文件工具的沙箱根目录，也是 identity.md /
    #   skills / MEMORY.md / sessions 的查找起点 —— 一旦解析错了，
    #   人设和技能会静默失效，看不出来。显式打印才查得动。
    print(f"  工作区: {config.workspace}")
    print(
        f"  权限确认: {'已启用（写操作需你确认）' if config.permissions_enabled else '★ 已关闭（危险）'}"
    )
    print("=" * 50)

    bus = MessageBus(maxsize=config.bus_maxsize)

    # 启动 MCP Server（如果有配置）
    mcp_manager = None
    if config.mcp_servers:
        # ★ 传 config_dir：MCP 配置里的 "mcp_servers/poetry_server.py" 是相对路径，
        #   不给基准目录的话它会按"当前工作目录"去找，换个目录启动就找不到
        mcp_manager = MCPClientManager(config.mcp_servers, base_dir=config.config_dir)
        await mcp_manager.connect_all(timeout=30.0)

        # 后台健康检查：Server 子进程崩了 / 远程服务抖了，自动探测并重连，
        # 而不是等到用户调用工具时才报"未连接"
        mcp_manager.start_health_loop(interval=60.0)

        health = await mcp_manager.check_health()
        for name, status in health.items():
            print(f"[MCP] 初始健康状态 {name}: {status}")

    # 定义 Agent 工厂函数
    def create_agent(session_key: str) -> AgentLoop:
        return build_agent(config, session_key, mcp_manager)

    # 注册渠道
    cli_channel = CLIChannel(bus)
    channels = [cli_channel]

    # 飞书渠道（如果配置了 feishu app_id / app_secret 就自动启用）
    if config.feishu_app_id and config.feishu_app_secret:
        feishu_channel = FeishuChannel(bus, config.feishu_app_id, config.feishu_app_secret)
        channels.append(feishu_channel)
        print("[启动] 已启用飞书渠道")
    else:
        print("[启动] 未配置飞书，跳过飞书渠道")

    # QQ 渠道（如果配置了 qq app_id / app_secret 就自动启用）
    if config.qq_app_id and config.qq_app_secret:
        qq_channel = QQChannel(bus, config.qq_app_id, config.qq_app_secret)
        channels.append(qq_channel)
        print("[启动] 已启用 QQ 渠道")
    else:
        print("[启动] 未配置 QQ，跳过 QQ 渠道")

    # Web 渠道（默认启用，不需要外部凭证）
    if config.web_enabled:
        web_channel = WebChannel(bus, host=config.web_host, port=config.web_port)
        channels.append(web_channel)
        print(f"[启动] 已启用 Web 渠道: http://{config.web_host}:{config.web_port}")
    else:
        print("[启动] Web 渠道已禁用")

    # 启动网关
    gateway = Gateway(bus, channels, create_agent)

    # 预创建 CLI Agent，让初始化信息在启动时就显示
    cli_session_key = "cli:local"
    agent = create_agent(cli_session_key)
    gateway._agents[cli_session_key] = agent

    # 注入工具列表和清空回调给 CLI 渠道
    cli_channel.tool_names = agent.tools.list_tools()
    cli_channel._clear_callback = lambda: agent.clear_history()

    # 注入 /cost 与 /trace 回调（可观测性）
    cli_channel._cost_callback = lambda: render_cost_report(agent)
    cli_channel._trace_callback = lambda: render_trace_timeline(agent)

    try:
        await gateway.run()
    except KeyboardInterrupt:
        print("\n[NanoClaw] 正在退出...")
    finally:
        # 清理 MCP 连接
        if mcp_manager:
            await mcp_manager.shutdown()

        # 关闭所有 Agent 的 Provider HTTP 客户端（释放连接池）。
        # ★ 不做这一步，连接池会等垃圾回收才释放，而 GC 常发生在
        #   事件循环关闭之后，退出时会刷一屏
        #   `generator didn't stop after athrow()` —— 看着像崩溃，
        #   其实只是资源没关干净。
        closed = 0
        for session_key, cached_agent in list(gateway._agents.items()):
            closer = getattr(cached_agent, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
                closed += 1
            except Exception as e:
                print(f"警告: 关闭会话 {session_key} 的 Provider 失败 - {e}")
        if closed:
            print(f"[NanoClaw] 已关闭 {closed} 个会话的 HTTP 客户端")


def main() -> None:
    """
    主入口（同步包装）

    打印启动 banner，组装 Agent 并启动交互循环。
    """
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n[NanoClaw] 正在退出...")
        import os
        os._exit(0)  # 强制退出，杀掉残留的 WebSocket 子线程


if __name__ == "__main__":
    main()