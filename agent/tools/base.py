"""
Tool 抽象基类模块

定义所有工具的统一接口规范，子类必须实现 name、description、parameters
三个属性和 execute 异步方法。to_function_definition 方法自动生成 OpenAI
function calling 所需的 JSON 结构，无需手动拼装。
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """
    工具抽象基类

    所有可供 Agent 调用的工具都必须继承此类，并实现以下内容：
    - name: 工具的唯一标识名称，用于路由调用
    - description: 工具功能的自然语言描述，供 LLM 理解工具用途
    - parameters: 工具参数的 JSON Schema 描述，遵循 OpenAI function calling 规范
    - execute: 异步执行方法，接收关键字参数，返回字符串结果

    使用示例：
        class SearchTool(Tool):
            @property
            def name(self) -> str:
                return "search"

            @property
            def description(self) -> str:
                return "搜索互联网获取信息"

            @property
            def parameters(self) -> dict:
                return {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词"
                        }
                    },
                    "required": ["query"]
                }

            async def execute(self, **kwargs) -> str:
                query = kwargs["query"]
                return f"搜索结果: {query}"
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，唯一标识，用于匹配和路由调用"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能的自然语言描述，供 LLM 判断何时调用此工具"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """
        工具参数的 JSON Schema 定义

        格式遵循 OpenAI function calling 规范，示例：
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"}
            },
            "required": ["query"]
        }
        """
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """
        异步执行工具逻辑

        Args:
            **kwargs: 工具参数，与 parameters 中定义的 schema 对应

        Returns:
            str: 工具执行结果的字符串表示
        """
        ...

    # ---- 风险等级 ----
    # 由 agent/permissions.py 的 PermissionGate 读取，决定"自动执行 / 需确认 / 禁止"。
    #
    # 默认值是 "confirm"（需人工确认），这是刻意选的 fail-safe 方向：
    # 新写的工具如果忘了声明等级，宁可多问用户一次，也不要默默把有副作用的
    # 操作直接跑掉。声明为 "auto" 必须是想清楚"它真的只读"之后才写。
    #
    #   "auto"    只读、可重复执行、不改系统状态
    #   "confirm" 会改动文件 / 执行命令 / 消耗外部资源
    #   "deny"    永不自动执行（一般由 check_dangerous 动态判定）
    risk_level: str = "confirm"

    def check_dangerous(self, arguments: dict) -> str | None:
        """可选的动态危险判定。

        等级是静态的，但同一条命令有的安全有的致命（`ls` vs `rm -rf`）。
        工具可以实现本方法做参数级判定，返回非空字符串表示"直接拒绝"。
        PermissionGate 会在看 risk_level 之前先调用它。
        """
        return None

    def to_function_definition(self) -> dict:
        """
        将工具定义转换为 OpenAI function calling 格式

        自动将 name、description、parameters 组装为 OpenAI API 所需的
        tools JSON 结构，可直接传入 chat completion 请求的 tools 参数。

        Returns:
            dict: OpenAI 格式的工具定义，结构如下：
            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": {...}
                }
            }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
