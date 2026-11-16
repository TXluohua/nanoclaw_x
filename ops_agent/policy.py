"""权限仲裁：把领域工具接进 NanoClaw 真实的三档权限闸门。

不改 `agent/permissions.py` 一行代码 —— 这里只是**适配层**：

    PermissionGate.classify(tool, args)  →  auto / confirm / deny
        ↓
    auto    直接执行
    confirm 落盘挂起（ApprovalStore）→ 值班人审批 → 批准才执行
    deny    硬拒，不给确认机会

为什么要接真闸门而不是自己写一套
--------------------------------
- 闸门的三条核心语义（工具没声明风险就当 confirm、`check_dangerous` 优先于风险等级、
  歧义回复按拒绝处理）是这个项目最值钱的部分，必须被回放测试真正覆盖；
- 挂起状态落盘复用的就是仓库里那套 `workspace/approvals/<session>.json` 机制，
  证明"进程重启后审批仍然可恢复"不是 PPT 说法。

值班人审批（Approver）
----------------------
回放时不可能真有人点确认，所以这里模拟一个**有原则的值班人**：

- `destructive` 一律拒绝（对应"这类动作本就该硬拒"）；
- `mutate` 只有当它与 runbook 给出的应做动作一致时才批准；
- 其余（无依据的改动）一律拒绝 —— 这正是"被诱导去做多余操作"时的真实反应。
"""

from __future__ import annotations

import itertools
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.permissions import (
    RISK_AUTO,
    RISK_CONFIRM,
    RISK_DENY,
    ApprovalStore,
    PendingAction,
    PermissionGate,
)
from agent.tools.base import Tool

#: 模拟时钟步长（秒）。真实值班是一步几十秒到几分钟，这里用固定值保证可复现。
#: 键必须与 permissions.py 的风险等级常量一致（auto / confirm / deny）——
#: 早期写成 read，导致 auto 路径 KeyError，属于"自造枚举与既有枚举不一致"的典型坑。
STEP_LATENCY_S = {RISK_AUTO: 1.5, RISK_CONFIRM: 45.0, RISK_DENY: 0.2}

OUTCOME_EXECUTED = "executed"
OUTCOME_DENIED = "denied"
OUTCOME_PENDING_APPROVED = "pending_approved"
OUTCOME_PENDING_REJECTED = "pending_rejected"


@dataclass
class TraceEvent:
    """一条值班时间线事件（供 replay_trace 渲染成中文复盘）。"""

    seq: int
    ts: float                      # 模拟秒
    kind: str                      # query / action / blocked / approval / note
    tool: str
    risk: str
    outcome: str
    detail: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    #: 归属阶段：diagnosis / remediation / trap / verify
    #: 没有它就分不清一次拦截是"诊断被误拦"还是"陷阱被拦下"，审计台账会失真
    phase: str = "ops"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": round(self.ts, 1),
            "phase": self.phase,
            "kind": self.kind,
            "tool": self.tool,
            "risk": self.risk,
            "outcome": self.outcome,
            "detail": self.detail,
            "args": self.args,
        }


@dataclass
class Decision:
    """一次工具调用的最终归宿。"""

    tool: str
    args: dict[str, Any]
    risk: str
    reason: str
    outcome: str
    result: str | None = None
    latency: float = 0.0

    @property
    def executed(self) -> bool:
        return self.outcome in (OUTCOME_EXECUTED, OUTCOME_PENDING_APPROVED)


class ScenarioApprover:
    """模拟值班人：只批准 runbook 依据之内的改动。"""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        #: 本场景 runbook 允许的处置工具（来自真实 mitigation 的映射）
        self.allowed_tools = {
            a["tool"] for a in scenario.get("knowledge", {}).get("executable_actions", [])
        }

    def decide(self, tool_name: str, risk: str, args: dict[str, Any]) -> tuple[bool, str]:
        if risk == RISK_DENY:
            return False, "值班人拒绝：该动作属灾难级，超出值班处置权限"
        if tool_name in self.allowed_tools:
            return True, "值班人批准：与 runbook 处置步骤一致，同意执行"
        return False, f"值班人拒绝：runbook 未给出 {tool_name} 这一步，先补充依据"


class OpsPolicy:
    """仲裁 + 执行 + 留痕。

    Args:
        tools: 工具名 → Tool 实例
        approver: 模拟值班人
        workspace_dir: 审批落盘目录（默认为临时目录，避免污染真实 workspace）
        gate: 可注入自定义闸门（测试用）
    """

    def __init__(
        self,
        tools: dict[str, Tool],
        approver: ScenarioApprover,
        workspace_dir: str | None = None,
        gate: PermissionGate | None = None,
        session_key: str = "ops-replay",
    ) -> None:
        self.tools = tools
        self.approver = approver
        self.session_key = session_key

        if workspace_dir is None:
            workspace_dir = tempfile.mkdtemp(prefix="ops-approvals-")
        self.workspace_dir = workspace_dir
        self.store = ApprovalStore(os.path.join(workspace_dir, "approvals"), ttl_seconds=1800)
        self.gate = gate or PermissionGate(enabled=True, store=self.store)

        self.clock = 0.0
        self.events: list[TraceEvent] = []
        self.decisions: list[Decision] = []
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ 内部
    def _emit(self, kind: str, tool: str, risk: str, outcome: str,
              detail: str = "", args: dict[str, Any] | None = None,
              phase: str = "ops") -> None:
        self.events.append(TraceEvent(
            seq=next(self._seq), ts=self.clock, kind=kind, tool=tool, risk=risk,
            outcome=outcome, detail=detail, args=args or {}, phase=phase,
        ))

    def _advance(self, risk: str) -> None:
        self.clock += STEP_LATENCY_S.get(risk, 1.0)

    # ------------------------------------------------------------------ 仲裁
    def arbitrate(self, tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        """只判定不执行 —— 便于单测与"预演"。"""

        tool = self.tools.get(tool_name)
        if tool is None:
            return RISK_DENY, f"工具 {tool_name} 未注册，拒绝执行未知动作"
        return self.gate.classify(tool, args)

    # ------------------------------------------------------------------ 执行
    async def dispatch(self, tool_name: str, args: dict[str, Any],
                       note: str = "", phase: str = "ops") -> Decision:
        """完整链路：仲裁 → 执行 / 拒绝 / 挂起审批 → 记录。"""

        tool = self.tools.get(tool_name)
        if tool is None:
            decision = Decision(tool=tool_name, args=args, risk=RISK_DENY,
                                reason=f"工具 {tool_name} 未注册", outcome=OUTCOME_DENIED)
            self._emit("blocked", tool_name, RISK_DENY, OUTCOME_DENIED,
                       f"未知工具：{tool_name}", args, phase)
            self._advance(RISK_DENY)
            self.decisions.append(decision)
            return decision

        risk, reason = self.gate.classify(tool, args)

        # ---- deny：硬拒 ----
        if risk == RISK_DENY:
            decision = Decision(tool=tool_name, args=args, risk=risk, reason=reason,
                                outcome=OUTCOME_DENIED, latency=STEP_LATENCY_S[RISK_DENY])
            self._emit("blocked", tool_name, risk, OUTCOME_DENIED, reason, args, phase)
            self._advance(risk)
            self.decisions.append(decision)
            return decision

        # ---- confirm：落盘 → 审批 → 执行 ----
        if risk == RISK_CONFIRM:
            action = PendingAction(
                tool_call_id=f"call_{next(self._seq)}",
                tool_name=tool_name,
                arguments=args,
                reason=reason,
            )
            self.store.save(self.session_key, [action])          # 真落盘
            persisted = self.store.load(self.session_key)         # 真读回
            approved, why = self.approver.decide(tool_name, risk, args)
            self._emit("approval", tool_name, risk,
                       OUTCOME_PENDING_APPROVED if approved else OUTCOME_PENDING_REJECTED,
                       f"{why}｜挂起已落盘 {len(persisted)} 条", args, phase)
            self._advance(risk)

            if not approved:
                self.store.clear(self.session_key)
                decision = Decision(tool=tool_name, args=args, risk=risk,
                                    reason=f"{reason}｜{why}",
                                    outcome=OUTCOME_PENDING_REJECTED,
                                    latency=STEP_LATENCY_S[RISK_CONFIRM])
                self.decisions.append(decision)
                return decision

            result = await tool.execute(**args)
            self.store.clear(self.session_key)
            decision = Decision(tool=tool_name, args=args, risk=risk,
                                reason=f"{reason}｜{why}",
                                outcome=OUTCOME_PENDING_APPROVED, result=result,
                                latency=STEP_LATENCY_S[RISK_CONFIRM])
            self._emit("action", tool_name, risk, OUTCOME_PENDING_APPROVED,
                       note or result or "", args, phase)
            self.decisions.append(decision)
            return decision

        # ---- auto：直接执行 ----
        result = await tool.execute(**args)
        self._emit("query" if risk == RISK_AUTO else "action", tool_name, risk,
                   OUTCOME_EXECUTED, note or str(result)[:160], args, phase)
        self._advance(risk)
        decision = Decision(tool=tool_name, args=args, risk=risk, reason=reason,
                            outcome=OUTCOME_EXECUTED, result=result,
                            latency=STEP_LATENCY_S[RISK_AUTO])
        self.decisions.append(decision)
        return decision

    # ------------------------------------------------------------------ 汇总
    def summary(self) -> dict[str, Any]:
        by_risk: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        for d in self.decisions:
            by_risk[d.risk] = by_risk.get(d.risk, 0) + 1
            by_outcome[d.outcome] = by_outcome.get(d.outcome, 0) + 1
        return {
            "steps": len(self.decisions),
            "by_risk": by_risk,
            "by_outcome": by_outcome,
            "elapsed_s": round(self.clock, 1),
            "trace_events": len(self.events),
        }
