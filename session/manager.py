"""
会话管理器模块

SessionManager 负责持久化存储 Agent 的对话历史，包括：
- 按会话 key 分组存储消息（JSONL 格式）
- 加载历史对话记录
- 清除指定会话
- 列出所有已保存的会话

会话文件存储在 workspace/sessions 目录下，每个会话一个 .jsonl 文件。
文件名由 session_key 转换而来（":" 替换为 "_"）。

使用示例：
    manager = SessionManager()

    # 保存消息
    manager.save_message("cli:direct", {"role": "user", "content": "你好"})

    # 加载历史
    history = manager.get_history("cli:direct")

    # 清除会话
    manager.clear("cli:direct")
"""

import json
import os
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote


class SessionManager:
    """
    会话管理器

    持久化存储 Agent 的对话历史，支持多会话管理。

    所有数据文件存储在 workspace/ 目录下，避免污染项目根目录。
    """

    def __init__(self, sessions_dir: str = "workspace/sessions") -> None:
        """
        Args:
            sessions_dir: 会话文件存储目录，默认 "workspace/sessions"
        """
        self.sessions_dir = sessions_dir

        # 自动创建目录
        os.makedirs(self.sessions_dir, exist_ok=True)

    def _canonical_path(self, session_key: str) -> str:
        """规范文件名：session_key 做 URL 百分号编码。

        ★ 为什么不再用 `session_key.replace(":", "_")`（原实现的真实缺陷）：
          存的时候把 ":" 换成 "_"，读的时候把 "_" 全部还原成 ":"，这个映射
          **不可逆**。sender_id 本身含下划线时（飞书的 open_id 全是这种：
          `feishu_ou_6907752d04b41e541814fadb61cd47d7`），
          还原出来会变成 `feishu:ou:6907752d...` —— 四个冒号，原 key 完全对不上，
          会话再也定位不回来。
          quote() 是单射的，quoted 串里除了 %XX 不会再出现 "_" 或 ":"，
          所以 unquote() 一定能还原。
        """
        return os.path.join(self.sessions_dir, quote(session_key, safe="") + ".jsonl")

    def _legacy_path(self, session_key: str) -> str:
        """旧命名（冒号换下划线），仅用于兼容升级前已经落盘的会话文件。"""
        return os.path.join(
            self.sessions_dir, session_key.replace(":", "_") + ".jsonl"
        )

    def _get_session_path(self, session_key: str) -> str:
        """返回该会话实际应该读写的文件路径。

        规则：
        - 读写时优先复用【已存在】的文件（含旧命名的），避免历史被劈成两个文件；
        - 文件还不存在时，用规范命名新建。
        """
        canonical = self._canonical_path(session_key)
        legacy = self._legacy_path(session_key)

        if os.path.isfile(canonical):
            return canonical
        if os.path.isfile(legacy):
            return legacy
        return canonical

    def save_message(self, session_key: str, message: dict[str, Any]) -> None:
        """
        保存消息到会话文件

        给 message 加上 "timestamp" 字段（当前时间 ISO 格式），
        以 append 模式打开 JSONL 文件，写入一行 JSON。

        Args:
            session_key: 会话标识符
            message: 消息字典，格式为 {"role": "...", "content": "..."}
        """
        # 获取文件路径
        file_path = self._get_session_path(session_key)

        # 添加时间戳
        message_with_timestamp = message.copy()
        message_with_timestamp["timestamp"] = datetime.now().isoformat()

        # 写入 JSONL 文件（append 模式）
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                # ensure_ascii=False 支持中文
                json_line = json.dumps(message_with_timestamp, ensure_ascii=False)
                f.write(json_line + "\n")
        except Exception as e:
            # 写入失败不影响主流程，打印警告
            print(f"警告: 保存消息失败 - {e}")

    def replace_tool_message(
        self, session_key: str, tool_call_id: str, content: str
    ) -> bool:
        """把会话里最后一条指定 tool_call_id 的 tool 消息内容替换掉。

        用于"人工确认"流程：待确认的操作在历史里先落一条占位消息，
        用户批准后要把它换成真实的执行结果。

        ★ 必须替换而不是追加。OpenAI 协议要求每个 tool_call 有且只有
          一条对应的 tool 消息；如果占位一条、结果又追加一条，
          下一轮请求会因为 tool_call_id 重复直接返回 400。

        Returns:
            bool: 是否找到并替换成功
        """
        file_path = self._get_session_path(session_key)
        if not os.path.isfile(file_path):
            return False

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"警告: 读取会话失败 - {e}")
            return False

        target = -1
        for idx in range(len(lines) - 1, -1, -1):
            raw = lines[idx].strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                message.get("role") == "tool"
                and message.get("tool_call_id") == tool_call_id
            ):
                target = idx
                break

        if target < 0:
            return False

        try:
            message = json.loads(lines[target])
            message["content"] = content
            lines[target] = json.dumps(message, ensure_ascii=False) + "\n"

            # 先写临时文件再原子替换：避免写到一半崩掉把整个会话文件弄坏
            tmp_path = file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp_path, file_path)
        except Exception as e:
            print(f"警告: 替换会话消息失败 - {e}")
            return False

        return True

    def get_history(self, session_key: str) -> list[dict[str, Any]]:
        """
        加载会话历史

        读取对应的 JSONL 文件，逐行解析 JSON，跳过空行。
        返回消息列表（去掉 timestamp 字段）。

        Args:
            session_key: 会话标识符

        Returns:
            list[dict]: 消息列表，文件不存在时返回空列表
        """
        # 获取文件路径
        file_path = self._get_session_path(session_key)

        # 文件不存在则返回空列表
        if not os.path.isfile(file_path):
            return []

        # 读取并解析
        history: list[dict[str, Any]] = []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    # 跳过空行
                    if not line.strip():
                        continue

                    # 解析 JSON
                    try:
                        message = json.loads(line)

                        # 去掉 timestamp 字段（OpenAI API 不认识）
                        message_copy = message.copy()
                        message_copy.pop("timestamp", None)

                        history.append(message_copy)

                    except json.JSONDecodeError:
                        # 跳过无法解析的行
                        continue

        except Exception as e:
            print(f"警告: 加载历史失败 - {e}")
            return []

        return history

    def clear(self, session_key: str) -> None:
        """
        清除指定会话

        删除对应的 JSONL 文件（如果存在）。

        Args:
            session_key: 会话标识符
        """
        # 获取文件路径
        file_path = self._get_session_path(session_key)

        # 删除文件（如果存在）
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"警告: 清除会话失败 - {e}")

    def list_sessions(self) -> list[str]:
        """
        列出所有已保存的会话

        扫描 sessions_dir 下的所有 .jsonl 文件，
        返回 session_key 列表（文件名还原为 session_key）。

        Returns:
            list[str]: session_key 列表
        """
        sessions: list[str] = []

        # 检查目录是否存在
        if not os.path.isdir(self.sessions_dir):
            return sessions

        # 扫描 .jsonl 文件
        try:
            for filename in os.listdir(self.sessions_dir):
                # 只处理 .jsonl 文件
                if not filename.endswith(".jsonl"):
                    continue

                # ★ 用 unquote 还原，而不是把 "_" 全部换回 ":"。
                #   后者不可逆（见 _canonical_path 的说明）。
                #   旧命名（cli_local.jsonl 这类）不是 quote 出来的，unquote 会原样
                #   返回，于是得到一个"不含伪造冒号"的 key —— 有损，但不会再错乱。
                sessions.append(unquote(filename[: -len(".jsonl")]))

        except Exception:
            pass

        return sessions