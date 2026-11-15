"""
文件片段编辑工具模块

为什么需要它
------------
原来只有 `write_file`——全文覆盖。对一个会改代码的 Agent 来说这很危险：

- 模型想改 3 行，得重写整个文件；一旦它记错了某段原文，整个文件就被改坏
- 大文件（几千行）重写一次就是几千 token，又慢又贵还容易截断
- 改完没有任何"只动了这一处"的证据，人看不出改了什么

`edit_file` 只做"把这段换成那段"：
- 找不到 `old_string` → 直接失败，不会误写
- `old_string` 出现多次 → 也失败，要求给更多上下文（避免改错位置）
- 只改动目标片段，其余字节不动

这跟 `write_file` 是互补关系，不是替换：
新建文件 / 整篇重写用 `write_file`，局部修改用 `edit_file`。
"""

import os
from typing import Any

from agent.tools.base import Tool
from agent.tools.filesystem import safe_join

# 单次编辑的上下文预览长度
_PREVIEW = 120


class EditFileTool(Tool):
    """把文件里的一段文本替换成另一段。

    安全边界与文件工具一致：只允许操作 workspace 内的路径。
    风险等级 confirm：它确实会改文件内容。
    """

    risk_level = "confirm"

    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "在文件中精确替换一段文本。参数 old_string 是被替换的原文（必须与文件内容"
            "完全一致，包含缩进），new_string 是新内容。只改这一处，其余内容不动。"
            "若 old_string 在文件中出现多次，会拒绝执行——请提供更多上下文让它唯一。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要修改的文件路径，相对于工作区根目录",
                },
                "old_string": {
                    "type": "string",
                    "description": "要被替换的原文，必须与文件中的内容逐字符一致",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的新内容；传空字符串表示删除这段",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "为 true 时替换所有匹配（默认 false，要求唯一匹配）",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        }

    async def execute(self, **kwargs: Any) -> str:
        file_path = kwargs.get("file_path", "")
        old_string = kwargs.get("old_string", "")
        new_string = kwargs.get("new_string", "")
        replace_all = bool(kwargs.get("replace_all", False))

        if not file_path:
            return "错误: 缺少 file_path"
        if old_string == "":
            return "错误: old_string 不能为空（新建内容请用 write_file）"
        if old_string == new_string:
            return "错误: old_string 与 new_string 相同，没有任何改动"

        absolute_path = safe_join(self.workspace, file_path)
        if absolute_path is None:
            return "错误: 拒绝访问工作区外的路径"

        if not os.path.isfile(absolute_path):
            return f"错误: 文件不存在: {file_path}"

        try:
            with open(absolute_path, "r", encoding="utf-8") as f:
                original = f.read()
        except UnicodeDecodeError:
            return "错误: 无法解码文件内容，可能不是文本文件"
        except Exception as e:
            return f"错误: 读取文件失败 - {e}"

        occurrences = original.count(old_string)

        if occurrences == 0:
            return (
                f"错误: 在 {file_path} 中找不到 old_string。"
                f"请先 read_file 确认原文（注意缩进、空格、换行必须完全一致）。"
            )

        if occurrences > 1 and not replace_all:
            # 这是这个工具最重要的保护：宁可拒绝，也不要猜哪一处
            return (
                f"错误: old_string 在 {file_path} 中出现了 {occurrences} 次，"
                f"无法确定要改哪一处。请提供更多上下文使其唯一，"
                f"或显式传 replace_all=true 修改全部。"
            )

        updated = original.replace(old_string, new_string)
        if updated == original:
            return "错误: 替换后内容未变化"

        try:
            # 先写临时文件再原子替换，避免写一半崩掉把源文件弄坏
            tmp_path = absolute_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(updated)
            os.replace(tmp_path, absolute_path)
        except PermissionError:
            return f"错误: 无权限写入文件: {file_path}"
        except Exception as e:
            return f"错误: 写入文件失败 - {e}"

        replaced_count = occurrences if replace_all else 1
        old_lines = original.count("\n") + 1
        new_lines = updated.count("\n") + 1
        delta = new_lines - old_lines
        delta_text = f"{delta:+d}" if delta else "0"

        return (
            f"成功编辑 {file_path}：替换 {replaced_count} 处，"
            f"行数 {old_lines} → {new_lines}（{delta_text}）\n"
            f"  被替换: {_preview(old_string)}\n"
            f"  替换为: {_preview(new_string)}"
        )


def _preview(text: str) -> str:
    """把多行文本压成一行预览。"""
    flat = text.replace("\n", "\\n")
    return flat if len(flat) <= _PREVIEW else flat[:_PREVIEW] + "…"
