"""
权限分级与人工确认（Human-in-the-loop）模块

为什么需要它
------------
原来的安全防护只有 `ExecTool` 里那张"危险命令正则黑名单"：命中就拒，
没命中就直接执行。也就是说 `write_file` / `exec` / `spawn_subagent` 这类
真正会改变系统状态的工具，只要不撞上黑名单，Agent 想跑就跑，用户全程不知情。

黑名单的本质是"列举坏事"，而坏事是列不完的 —— 换个写法、变量拼接、
`python -c "import shutil; shutil.rmtree(...)"`，都能绕过去。
正确的方向是反过来：**默认不信任，按风险分级，让"写"必须先经过人**。

三档
----
    auto     只读类，直接执行
             例：read_file / list_dir / web_search / web_fetch / 只读 MCP 工具
    confirm  有副作用，挂起等用户确认后才执行
             例：write_file / exec / spawn_subagent
    deny     命中危险命令黑名单，直接拒绝，不给确认机会
             例：rm -rf / mkfs / sudo / dd if= / curl|bash

为什么要落盘
------------
挂起状态必须持久化（`workspace/approvals/`）。只放内存的话进程一重启，
"待确认"就消失了，用户回来看到自己在确认一件不存在的事。
这也是"工作流状态持久化"最具体的一个场景。

超时
----
挂起超过 `pending_ttl_seconds`（默认 30 分钟）自动失效，
避免几天后突然冒出一句"确认"把陈年操作执行了。
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

# ---- 风险等级 ----
RISK_AUTO = "auto"        # 只读，直接执行
RISK_CONFIRM = "confirm"  # 需人工确认
RISK_DENY = "deny"        # 硬拒，不给确认机会

RISK_LABELS = {
    RISK_AUTO: "自动执行",
    RISK_CONFIRM: "需确认",
    RISK_DENY: "禁止",
}

# ---- 用户回复判定 ----
# 判定原则：**歧义时永远偏向"不执行"**。
# 批错一个动作的代价，远大于多问一次。
APPROVE_TOKENS = {
    "确认", "确定", "同意", "批准", "执行", "允许", "可以", "好的", "好", "行",
    "确认执行", "同意执行", "确认吧", "继续",
    "ok", "okay", "yes", "y", "confirm", "approve", "go", "allow", "proceed",
}
DENY_TOKENS = {
    "取消", "拒绝", "不要", "不用", "别", "停止", "不", "算了", "放弃",
    "取消执行", "拒绝执行", "别执行", "不要执行",
    "no", "n", "cancel", "deny", "reject", "abort", "stop",
}

DECISION_APPROVE = "approve"
DECISION_DENY = "deny"

# 超过这个长度就当"用户不是在回答确认"，不回 ACCEPT/DENY
MAX_DECISION_LENGTH = 12


def _normalize(text: str) -> str:
    """归一化用户回复：去空白、去首尾标点、转小写。"""
    cleaned = "".join(ch for ch in (text or "").strip() if not ch.isspace())
    return cleaned.strip("。.!！?？~～,，、;；:：").lower()


def parse_decision(text: str) -> str | None:
    """判断用户这句话是"确认"还是"拒绝"。

    Returns:
        DECISION_APPROVE / DECISION_DENY / None（既不是确认也不是拒绝）

    判定是**保守**的：
    - 只认短回复（≤ MAX_DECISION_LENGTH 字符），长句子当作用户在聊别的
    - 先判 DENY 再判 APPROVE："确认取消"这种同时含两类的，按拒绝处理
    - 都不匹配时返回 None，调用方会走"取消挂起"分支，绝不擅自执行
    """
    normalized = _normalize(text)
    if not normalized or len(normalized) > MAX_DECISION_LENGTH:
        return None

    # 先判拒绝：宁可少执行，不可错执行
    if normalized in DENY_TOKENS:
        return DECISION_DENY
    if any(normalized == token for token in APPROVE_TOKENS):
        return DECISION_APPROVE

    # 允许"确认执行"这类以关键字结尾的短语
    if any(normalized.endswith(token) for token in DENY_TOKENS if len(token) >= 2):
        return DECISION_DENY
    if any(normalized.endswith(token) for token in APPROVE_TOKENS if len(token) >= 2):
        return DECISION_APPROVE

    return None


@dataclass
class PendingAction:
    """一个等待用户确认的工具调用。"""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingAction":
        return cls(
            tool_call_id=data.get("tool_call_id", ""),
            tool_name=data.get("tool_name", ""),
            arguments=data.get("arguments") or {},
            reason=data.get("reason", ""),
            created_at=data.get("created_at", ""),
        )

    def describe(self) -> str:
        """给用户看的一句话摘要。"""
        args = json.dumps(self.arguments, ensure_ascii=False)
        if len(args) > 200:
            args = args[:200] + "..."
        return f"{self.tool_name}({args})"


class ApprovalStore:
    """待确认动作的持久化存储。

    一个会话同一时刻只有一批待确认动作（对应一轮 LLM 返回的多个工具调用）。
    文件位置：`<workspace>/approvals/<session_key>.json`
    """

    def __init__(self, base_dir: str, ttl_seconds: int = 1800) -> None:
        self.base_dir = os.path.abspath(base_dir)
        self.ttl_seconds = ttl_seconds
        os.makedirs(self.base_dir, exist_ok=True)

    def _path(self, session_key: str) -> str:
        return os.path.join(self.base_dir, quote(session_key, safe="") + ".json")

    def save(self, session_key: str, actions: list[PendingAction]) -> None:
        if not actions:
            self.clear(session_key)
            return
        payload = {
            "session_key": session_key,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "actions": [a.to_dict() for a in actions],
        }
        try:
            with open(self._path(session_key), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"警告: 保存待确认动作失败 - {e}")

    def load(self, session_key: str) -> list[PendingAction]:
        """读取待确认动作。过期则自动清除并返回空。"""
        path = self._path(session_key)
        if not os.path.isfile(path):
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            print(f"警告: 读取待确认动作失败 - {e}")
            return []

        raw_actions = payload.get("actions") or []
        if not raw_actions:
            return []

        # 过期检查
        saved_at = payload.get("saved_at", "")
        if self._is_expired(saved_at):
            print(f"[权限] 待确认动作已过期（超过 {self.ttl_seconds}s），自动失效")
            self.clear(session_key)
            return []

        return [PendingAction.from_dict(item) for item in raw_actions]

    def _is_expired(self, saved_at: str) -> bool:
        if not saved_at:
            return False
        try:
            saved = datetime.fromisoformat(saved_at)
        except ValueError:
            return False
        return (datetime.now() - saved).total_seconds() > self.ttl_seconds

    def clear(self, session_key: str) -> None:
        path = self._path(session_key)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"警告: 清除待确认动作失败 - {e}")


class PermissionGate:
    """风险分级闸门。

    职责很单一：给定一个工具调用，回答"它该自动跑、该问人、还是该拒绝"。
    执行与挂起由 AgentLoop 负责，这里不做副作用操作。
    """

    def __init__(
        self,
        enabled: bool = True,
        store: ApprovalStore | None = None,
    ) -> None:
        """
        Args:
            enabled: 是否启用确认环节。关闭后所有 confirm 级降为 auto
                （仅建议在自己调试时关，真实使用不要关）
            store: 待确认动作存储
        """
        self.enabled = enabled
        self.store = store

    def classify(self, tool: Any, arguments: dict[str, Any]) -> tuple[str, str]:
        """判定一次工具调用的风险等级。

        Returns:
            tuple[str, str]: (风险等级, 说明)。说明会展示给用户。
        """
        # ① 最高优先：工具自己能不能判定"这条绝对不行"
        #    例如 ExecTool 里有危险命令黑名单。这一步不受 enabled 影响 ——
        #    关掉确认环节不等于关掉硬拦截。
        checker = getattr(tool, "check_dangerous", None)
        if callable(checker):
            try:
                reason = checker(arguments)
            except Exception as e:  # 检查器自己炸了，按"要确认"处理，不放行
                return RISK_CONFIRM, f"危险检测失败（{e}），默认要求确认"
            if reason:
                return RISK_DENY, reason

        # ② 确认环节被关闭 → 一律放行（调试用）
        if not self.enabled:
            return RISK_AUTO, ""

        # ③ 按工具声明的等级
        #    没有声明的一律当 confirm：新工具默认不信任，这是 fail-safe 方向
        risk = getattr(tool, "risk_level", RISK_CONFIRM)
        if risk not in (RISK_AUTO, RISK_CONFIRM, RISK_DENY):
            return RISK_CONFIRM, f"未知风险等级 '{risk}'，默认要求确认"

        if risk == RISK_CONFIRM:
            return RISK_CONFIRM, f"{getattr(tool, 'name', '?')} 会修改系统状态"
        if risk == RISK_DENY:
            return RISK_DENY, f"{getattr(tool, 'name', '?')} 被标记为禁止操作"

        return RISK_AUTO, ""

    def build_confirm_prompt(self, actions: list[PendingAction]) -> str:
        """生成给用户看的确认提示文案。"""
        lines = ["⚠️ 以下操作会修改你的环境，需要你确认后才会执行：", ""]
        for i, action in enumerate(actions, 1):
            lines.append(f"  {i}. {action.describe()}")
            if action.reason:
                lines.append(f"     └ {action.reason}")
        lines += [
            "",
            "回复「确认」执行，回复「取消」放弃。",
            f"（{self.store.ttl_seconds // 60} 分钟内有效，超时自动失效）"
            if self.store
            else "",
        ]
        return "\n".join(line for line in lines if line is not None).rstrip()
