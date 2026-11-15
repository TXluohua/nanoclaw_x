"""
任务清单工具模块

为什么需要它
------------
长任务（"把这几个文件都改一遍，然后跑测试，最后更新文档"）最大的问题是
**中途丢掉全局视角**：模型做到第 4 步时，已经不记得第 1 步做完没有、
第 6 步是什么。表现就是重复做、漏做、或者做完一件就停下来等指令。

给模型一张显式的白板，让它每推进一步就更新一次，效果立竿见影 ——
这也是主流 Coding Agent（Claude Code、Codex 等）都有 todo 工具的原因。

存储
----
`<workspace>/workspace/todos/<session>.json`，按会话隔离。
落盘而不是只放内存，是为了进程重启后还能接着干。

语义
----
每次调用提交的是**完整清单**（替换旧的），不是增量。
这样状态永远只有一份真相，不会出现"我以为删了其实没删"。
"""

import json
import os
from typing import Any
from urllib.parse import quote

from agent.tools.base import Tool

VALID_STATUSES = ("pending", "in_progress", "completed")
STATUS_ICONS = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}

# 清单条数上限：防止模型把整个计划拆成 200 条，反而看不清
MAX_ITEMS = 30


class TodoStore:
    """按会话持久化任务清单。"""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def path(self, session_key: str) -> str:
        return os.path.join(self.base_dir, quote(session_key, safe="") + ".json")

    def load(self, session_key: str) -> list[dict[str, str]]:
        path = self.path(session_key)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("todos") if isinstance(data, dict) else None
            return items if isinstance(items, list) else []
        except Exception:
            return []

    def save(self, session_key: str, todos: list[dict[str, str]]) -> None:
        path = self.path(session_key)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"todos": todos}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            print(f"警告: 保存任务清单失败 - {e}")

    def clear(self, session_key: str) -> None:
        path = self.path(session_key)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass


def render_todos(todos: list[dict[str, str]]) -> str:
    """把清单渲染成可读文本。"""
    if not todos:
        return "（清单为空）"

    done = sum(1 for t in todos if t.get("status") == "completed")
    lines = [f"任务清单（{done}/{len(todos)} 已完成）："]
    for index, item in enumerate(todos, 1):
        status = item.get("status", "pending")
        icon = STATUS_ICONS.get(status, "⏳")
        lines.append(f"  {index}. {icon} [{status}] {item.get('content', '')}")
    return "\n".join(lines)


class TodoWriteTool(Tool):
    """更新当前会话的任务清单（提交完整清单）。"""

    # 只写自己的状态文件，不碰用户的代码/文件 → 不需要人工确认。
    # （如果把它设成 confirm，每推进一步都要用户点一次确认，没法用了。）
    risk_level = "auto"

    def __init__(self, store: TodoStore, session_key: str) -> None:
        self.store = store
        self.session_key = session_key

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "更新任务清单。用于多步骤任务的进度跟踪：开始前先把计划列出来，"
            "每完成一步就更新一次状态。每次调用必须提交【完整】清单（覆盖旧的），"
            "不是只提交变化的那一条。status 只能是 pending / in_progress / completed。"
            "同一时刻建议只有一条 in_progress。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": f"完整的任务清单，最多 {MAX_ITEMS} 条",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务描述，用祈使句（如「修改 config.py」）",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(VALID_STATUSES),
                                "description": "pending 待做 / in_progress 正在做 / completed 已完成",
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        }

    async def execute(self, **kwargs: Any) -> str:
        raw = kwargs.get("todos")
        if not isinstance(raw, list):
            return "错误: todos 必须是数组"
        if len(raw) > MAX_ITEMS:
            return f"错误: 清单最多 {MAX_ITEMS} 条，当前 {len(raw)} 条，请合并同类项"
        if not raw:
            # 空清单 = 清空（任务全部结束后合理）
            self.store.clear(self.session_key)
            return "任务清单已清空"

        cleaned: list[dict[str, str]] = []
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                return f"错误: 第 {index} 条不是对象"
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "pending").strip()
            if not content:
                return f"错误: 第 {index} 条缺少 content"
            if status not in VALID_STATUSES:
                return (
                    f"错误: 第 {index} 条 status='{status}' 非法，"
                    f"只能是 {', '.join(VALID_STATUSES)}"
                )
            cleaned.append({"content": content, "status": status})

        in_progress = [t for t in cleaned if t["status"] == "in_progress"]
        self.store.save(self.session_key, cleaned)

        note = ""
        if len(in_progress) > 1:
            note = f"\n  ⚠️ 有 {len(in_progress)} 条同时标记为进行中，建议只保留一条"

        return render_todos(cleaned) + note
