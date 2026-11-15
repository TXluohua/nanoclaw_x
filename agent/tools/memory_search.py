"""
长期记忆检索工具

让 Agent 能主动查自己的长期记忆，而不是被动等上下文注入。

为什么需要"主动查"这一层：
`ContextBuilder` 每轮会自动注入最相关的几条记忆，但那是**按当前问题**
召回的前 K 条。如果模型想看更早的、或者换几个角度查（"关于部署我说过什么"），
自动注入给不了它。给一个显式工具，它就能自己多查几次。

风险等级 auto：只读自己的记忆文件，不改任何东西。
"""

from typing import Any

from agent.memory_store import MemoryRetriever
from agent.tools.base import Tool


class SearchMemoryTool(Tool):
    """在长期记忆中按关键词/语义检索。"""

    risk_level = "auto"

    def __init__(self, retriever: MemoryRetriever) -> None:
        self.retriever = retriever

    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return (
            "检索长期记忆（MEMORY.md 里沉淀的用户偏好、项目信息、历史纠错）。"
            "当你不确定用户之前说过什么、或者需要回忆早前的约定时使用。"
            "参数 query 是检索意图，top_k 返回条数（默认 5）。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词或问题，例如「部署方式」「用户的技术偏好」",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数，默认 5，最多 20",
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "错误: query 不能为空"

        try:
            top_k = int(kwargs.get("top_k") or 5)
        except (TypeError, ValueError):
            top_k = 5
        top_k = max(1, min(top_k, 20))

        total = self.retriever.size
        if total == 0:
            return "长期记忆为空（MEMORY.md 还没有内容，或文件不存在）"

        hits = await self.retriever.recall(query, top_k=top_k)
        if not hits:
            return f"没有找到与「{query}」相关的记忆（当前共 {total} 条）"

        lines = [f"从 {total} 条记忆中找到 {len(hits)} 条与「{query}」相关："]
        for index, text in enumerate(hits, 1):
            lines.append(f"  {index}. {text}")
        return "\n".join(lines)
