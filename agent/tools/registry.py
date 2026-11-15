"""
工具注册表模块

提供 ToolRegistry 类，统一管理工具的注册、查询和执行。
Agent 通过注册表获取工具定义列表传给 LLM，并在收到调用请求时
通过注册表路由到对应工具执行。
"""

import asyncio

from agent.tools.base import Tool

# 工具默认超时（秒）。单个工具可以用 `timeout` 属性覆盖：
# Shell 60s、web_fetch 15s、spawn_subagent 更久。
DEFAULT_TOOL_TIMEOUT = 120.0


class ToolRegistry:
    """
    工具注册表

    集中管理所有已注册的 Tool 实例，提供以下能力：
    - register: 注册工具，以工具 name 为 key 存储
    - get_definitions: 批量获取所有工具的 OpenAI function calling JSON 定义
    - execute: 根据工具名查找并异步执行，自动处理工具不存在和执行异常
    - list_tools: 列出所有已注册的工具名称

    使用示例：
        registry = ToolRegistry()
        registry.register(SearchTool())
        registry.register(CalculatorTool())

        # 获取定义，传给 OpenAI API
        definitions = registry.get_definitions()

        # 执行工具
        result = await registry.execute("search", {"query": "天气"})
    """

    def __init__(self, default_timeout: float | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.default_timeout = (
            DEFAULT_TOOL_TIMEOUT if default_timeout is None else default_timeout
        )

    def register(self, tool: Tool) -> None:
        """
        注册一个工具

        以工具的 name 属性作为 key 存储。如果同名工具已存在则覆盖。

        Args:
            tool: 实现了 Tool 接口的工具实例
        """
        existing = self._tools.get(tool.name)
        if existing is not None and existing is not tool:
            # ★ 同名覆盖是个隐蔽的坑：后注册的会静默顶掉先注册的，
            #   表现成"这个工具怎么行为变了"。MCP 工具靠 {server}__{tool}
            #   前缀规避，这里再补一道显式提示。
            print(
                f"[工具] 警告：工具名 '{tool.name}' 被覆盖"
                f"（{type(existing).__name__} → {type(tool).__name__}）"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名称取工具实例（找不到返回 None）。

        PermissionGate 需要拿到工具对象来读它的 risk_level / check_dangerous，
        所以注册表要能被"看一眼但不动手"。
        """
        return self._tools.get(name)

    def get_definitions(self) -> list[dict]:
        """
        获取所有已注册工具的 OpenAI function calling 定义

        遍历注册表中的每个工具，调用其 to_function_definition() 方法，
        返回可直接传给 OpenAI chat completion API 的 tools 参数列表。

        Returns:
            list[dict]: 工具定义列表，每项格式为
                {"type": "function", "function": {...}}
        """
        return [tool.to_function_definition() for tool in self._tools.values()]

    async def execute(
        self, name: str, arguments: dict, timeout: float | None = None
    ) -> str:
        """
        根据工具名异步执行工具

        在注册表中查找指定名称的工具，并使用 arguments 字典中的参数调用其
        execute 方法。如果工具不存在、执行超时或执行过程中抛出异常，返回错误
        信息字符串而非抛出异常，确保调用方可以安全地将结果回传给 LLM。

        ★ 为什么要加超时（原实现直接 `await tool.execute(...)`）：
          ExecTool 自己有 60s 超时，但 MCP 工具、spawn_subagent 都没有。
          一个 MCP Server 假死，整个会话就永久挂在那里，用户只看到界面卡住，
          而网关那边（单消费者时）会连带阻塞所有其他用户。
          这里统一兜底：工具就是一个 async 函数，超时是该由调度方保证的。

        Args:
            name: 工具名称
            arguments: 工具参数字典，将作为 **kwargs 传入 execute
            timeout: 本次调用的超时秒数；不给则用工具自己的 timeout 属性，
                再退到 registry 的 default_timeout

        Returns:
            str: 工具执行结果；出错时返回 "错误: ..." 格式的错误信息
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"错误: 工具 '{name}' 不存在"

        limit = timeout
        if limit is None:
            # 约定：工具可以声明 `timeout`（秒）覆盖默认值。
            # 兼容历史写法 `TIMEOUT`（shell.py / web_fetch.py 用的是全大写常量）。
            limit = (
                getattr(tool, "timeout", None)
                or getattr(tool, "TIMEOUT", None)
                or self.default_timeout
            )

        try:
            return await asyncio.wait_for(tool.execute(**arguments), timeout=limit)
        except asyncio.TimeoutError:
            return (
                f"错误: 工具 '{name}' 执行超时（超过 {limit:g} 秒），已中止。"
                f"请换一种做法或缩小范围。"
            )
        except asyncio.CancelledError:
            # 上层取消（如用户中断）必须继续向上传播，不能吞掉
            raise
        except Exception as e:
            return f"错误: 执行工具 '{name}' 失败 - {e}"

    def list_tools(self) -> list[str]:
        """
        返回所有已注册工具的名称列表

        Returns:
            list[str]: 工具名称列表
        """
        return list(self._tools.keys())
