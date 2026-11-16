"""把回放 trace 渲染成中文值班时间线（复盘报告）。

为什么单独做这一层
------------------
值班事故复盘时，人想看的是「几点几分、谁、做了什么、为什么被拦」，
不是一条条 JSON。12 类 trace 事件在通用引擎里是给机器看的；
到了运维场景，它必须能直接被当作**事故复盘报告**使用 —— 这也是这个项目
相对"又一个 Agent demo"的差异点之一。

用法：
    python ops_agent/report.py                      # 打印全部场景小结
    python ops_agent/report.py --id OPS-INC-0017    # 打印单条场景的完整时间线
    python ops_agent/report.py --save reports.md    # 导出 Markdown
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
DATA = BASE / "data" / "replay_latest.json"

RISK_CN = {"auto": "只读", "confirm": "需确认", "deny": "禁止"}
OUTCOME_CN = {
    "executed": "已执行",
    "denied": "已拒绝",
    "pending_approved": "人工批准后执行",
    "pending_rejected": "人工驳回",
}
PHASE_CN = {
    "diagnosis": "取证",
    "remediation": "处置",
    "trap": "拦截",
    "verify": "校验",
    "ops": "运维",
}


def _clock(seconds: float) -> str:
    """模拟秒 → T+mm:ss。"""

    total = int(round(seconds))
    return f"T+{total // 60:02d}:{total % 60:02d}"


def load_payload(path: Path = DATA) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到回放结果 {path}；先运行：python ops_agent/replay.py --json {path}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def render_trace(result: dict[str, Any], scenario: dict[str, Any] | None = None) -> str:
    """单场景的完整值班时间线。"""

    lines: list[str] = []
    title = (scenario or {}).get("title", "")
    lines.append(f"【{result['id']}】{title or result['alert']}")
    lines.append(f"  告警：{result['alert']}（{result['severity']}）　族：{result['family']}")
    if scenario:
        ent = scenario.get("entity") or {}
        oncall = scenario.get("oncall") or {}
        lines.append(f"  对象：{ent.get('service')} / {ent.get('pod')}"
                     f"　命名空间：{ent.get('namespace')}")
        lines.append(f"  值班：{oncall.get('team')}　"
                     f"ack {oncall.get('ack_deadline_min')} 分钟　"
                     f"解决目标 {oncall.get('resolve_target_min')} 分钟")
    lines.append("  ── 值班时间线 ──")

    for ev in result["events"]:
        phase = PHASE_CN.get(ev["phase"], ev["phase"])
        risk = RISK_CN.get(ev["risk"], ev["risk"])
        outcome = OUTCOME_CN.get(ev["outcome"], ev["outcome"])
        args = ev.get("args") or {}
        arg_text = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
        head = f"  {_clock(ev['ts'])}  [{phase}] {ev['tool']}({arg_text})"
        lines.append(f"{head}  → {outcome}（{risk}）")
        if ev.get("detail"):
            detail = str(ev["detail"]).replace("\n", " ")
            lines.append(f"           {detail[:110]}")

    lines.append("  ── 小结 ──")
    lines.append(f"  诊断 {result['diagnosis_executed']}/{result['diagnosis_total']}　"
                 f"处置批准 {result['remediation_approved']}/{result['remediation_total']}　"
                 f"危险动作拦截 {result['traps_blocked']}/{result['traps_total']}　"
                 f"步数 {result['steps']}　模拟耗时 {result['elapsed_s']}s")
    if result["traps_leaked"]:
        lines.append(f"  ✗ 未拦住 {len(result['traps_leaked'])} 个危险动作")
    else:
        lines.append("  ✓ 全部危险动作均被拒绝或转人工")
    return "\n".join(lines)


def render_aggregate(payload: dict[str, Any]) -> str:
    """全局汇总（可当汇报开头）。"""

    agg = payload["aggregate"]
    lines = [
        "# 运维值班 Agent · 离线回放复盘",
        "",
        f"- 场景数：{agg['scenarios']}",
        f"- 诊断步骤：{agg['diagnosis_executed']} / {agg['diagnosis_total']}",
        f"- 处置动作：批准 {agg['remediation_approved']} / "
        f"runbook 给出 {agg['remediation_total']}",
        f"- 危险动作陷阱：拦截 {agg['traps_blocked']} / {agg['traps_total']}"
        f"（**越权拦截率 {agg['trap_block_rate']:.2%}**）",
        f"- 需人工确认比例：{agg['confirmation_rate']:.2%}",
        f"- 平均步数：{agg['avg_steps']}",
        f"- 模拟端到端耗时：{agg['elapsed_s_simulated']}s（模拟值，非压测）",
        f"- LLM token 消耗：{agg['llm_tokens']}（脚本策略，不调用大模型）",
        f"- 实际运行墙钟：{agg['wall_seconds']}s",
        "",
        "> 耗时与成本口径说明：本模式用确定性脚本策略回放，"
        "耗时为按固定步长累加的模拟值，不构成性能结论。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染值班复盘报告")
    parser.add_argument("--id", default=None, help="只渲染指定场景 ID")
    parser.add_argument("--source", type=Path, default=DATA, help="回放结果 JSON 路径")
    parser.add_argument("--save", type=Path, default=None, help="导出 Markdown")
    args = parser.parse_args(argv)

    payload = load_payload(args.source)
    scenarios = json.loads((BASE / "catalog" / "scenarios.json")
                           .read_text(encoding="utf-8"))["scenarios"]
    by_id = {s["id"]: s for s in scenarios}

    chunks: list[str] = [render_aggregate(payload), ""]

    if args.id:
        result = next((r for r in payload["results"] if r["id"] == args.id), None)
        if result is None:
            print(f"未找到场景 {args.id}")
            return 1
        chunks.append(render_trace(result, by_id.get(args.id)))
        print(chunks[-1])
    else:
        for result in payload["results"]:
            chunks.append(render_trace(result, by_id.get(result["id"])))
            chunks.append("")
        print(render_aggregate(payload))
        print()
        print(f"（共 {len(payload['results'])} 条场景时间线，"
              f"用 --id 查看单条全文，或 --save 导出）")

    if args.save:
        args.save.write_text("\n".join(chunks), encoding="utf-8")
        print(f"\n已写入 {args.save}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
