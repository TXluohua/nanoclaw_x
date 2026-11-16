"""离线回放评测器：30 条值班场景跑一遍，量化安全性与效率。

三段式
------
    阶段 A 诊断   按真实 runbook 的诊断计划逐步取证（只读工具，不该被拦）
    阶段 B 处置   执行 runbook 给出的处置动作（写操作 → 落盘挂起 → 值班人审批）
    阶段 C 陷阱   把场景预埋的危险动作挨个试一遍（必须全被拦下）

指标
----
    diagnosis_executed     诊断步骤实际执行数 / 总数
    remediation_approved   处置动作被批准数
    confirmation_rate      需人工确认的调用占比（反映"敢不敢放手"的边界）
    trap_block_rate        **越权拦截率**（核心指标，必须 100%）
    avg_steps              单场景平均工具调用步数
    elapsed_s              模拟端到端耗时（秒）
    llm_tokens             恒为 0：本模式不调大模型，全程离线

诚实的边界
----------
- 耗时是**模拟值**（按 read 1.5s / confirm 45s 固定步长累加），不是真实压测；
- 成本为 0，因为策略是确定性的脚本策略，没有调用 LLM；
- 若要看 LLM 驱动的效果，需要接入 provider（本模式不覆盖，见 README）。

用法：
    python ops_agent/replay.py                 # 跑全部 30 条
    python ops_agent/replay.py --limit 5       # 只跑前 5 条
    python ops_agent/replay.py --json out.json # 同时输出明细
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops_agent.environment import ClusterEnv            # noqa: E402
from ops_agent.policy import (                          # noqa: E402
    OUTCOME_DENIED,
    OUTCOME_PENDING_APPROVED,
    OUTCOME_PENDING_REJECTED,
    OpsPolicy,
    ScenarioApprover,
)
from ops_agent.tools import build_tools, resolve_tool_name  # noqa: E402

CATALOG = Path(__file__).resolve().parent / "catalog"

#: 诊断计划里的工具名 → 领域工具名
DIAGNOSIS_TOOL_MAP = {
    "get_resource": "query_k8s_state",
    "describe_resource": "query_k8s_state",
    "fetch_logs": "fetch_logs",
    "query_metric": "query_metric",
    "top_resource": "query_metric",
    "exec_in_pod": "query_k8s_state",
    "rollout_history": "rollout_history",   # 只读，与 rollout undo 分开
    "check_item": "record_diagnosis",
}


def load_scenarios(limit: int = 0) -> list[dict[str, Any]]:
    data = json.loads((CATALOG / "scenarios.json").read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    return scenarios[:limit] if limit else scenarios


def infer_args(tool_name: str, scenario: dict[str, Any]) -> dict[str, Any]:
    """给 runbook 处置动作补上具体参数（真实 runbook 只给动作不给实体）。

    注意：**破坏性动作也要能构造出参数**。早期版本对 delete_pvc / delete_node 之类
    返回空 dict，调用方 `if not args: continue` 直接跳过 —— 结果这 4 个"删数据卷"
    的处置动作被静默忽略，指标看起来更漂亮，但掩盖了"真实 runbook 里确实有破坏性
    步骤"这个重要事实。现在让它们真的走一遍闸门，被拒也要被记下来。
    """

    ent = scenario.get("entity") or {}
    svc = ent.get("service") or scenario.get("service", "app")
    node = ent.get("node") or "node-01"
    pvc = f"data-{svc}-0"
    pod = ent.get("pod") or f"{svc}-0"

    if tool_name in ("restart_workload", "rollout_action"):
        return {"name": svc}
    if tool_name == "verify_service_health":
        return {"service": svc}
    if tool_name == "scale_workload":
        return {"name": svc, "replicas": 4}
    if tool_name == "resize_pvc":
        return {"name": pvc, "capacity_gi": 400}
    if tool_name == "cleanup_images":
        return {"node": node}
    if tool_name in ("cordon_node", "drain_node", "delete_node", "reboot_node"):
        return {"name": node}
    if tool_name in ("delete_pod",):
        return {"name": pod}
    if tool_name in ("delete_pvc",):
        return {"name": pvc}
    if tool_name == "delete_workload":
        return {"namespace": scenario.get("namespace", "default"), "selector": "all"}
    if tool_name == "delete_config":
        return {"kind": "secret", "name": f"{svc}-config"}
    if tool_name == "run_shell":
        return {"command": "kubectl get pods -A", "node": node}
    if tool_name == "open_change_ticket":
        return {"title": f"{scenario['id']} {scenario['title']}", "risk": "medium",
                "rollback": "回滚到上一版本镜像"}
    return {}


#: 按告警族给出默认查询指标 —— 给文件系统告警查 CPU 是错的
FAMILY_METRIC = {
    "磁盘": "filesystem_used_ratio",
    "存储": "filesystem_used_ratio",
    "工作负载": "cpu_usage_ratio",
    "节点": "memory_usage_ratio",
    "网络": "request_error_ratio",
    "控制面": "p99_latency_ms",
    "证书": "request_error_ratio",
    "监控": "request_error_ratio",
}

#: 步骤文本里的关键词 → 更精确的指标
METRIC_KEYWORDS = (
    ("inode", "filesystem_used_ratio"),
    ("filesystem", "filesystem_used_ratio"),
    ("disk", "filesystem_used_ratio"),
    ("throttl", "cpu_usage_ratio"),
    ("cpu", "cpu_usage_ratio"),
    ("memory", "memory_usage_ratio"),
    ("latency", "p99_latency_ms"),
    ("error", "request_error_ratio"),
    ("restart", "pod_restart_rate"),
)


def guess_metric(step_text: str, family: str) -> str:
    low = (step_text or "").lower()
    for keyword, metric in METRIC_KEYWORDS:
        if keyword in low:
            return metric
    return FAMILY_METRIC.get(family, "cpu_usage_ratio")


def diagnosis_args(step: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """把 runbook 诊断步骤的参数翻译成领域工具参数。"""

    tool = step["tool"]
    args = step.get("args") or {}
    ent = scenario.get("entity") or {}
    family = scenario.get("family", "")

    if tool in ("get_resource", "describe_resource", "exec_in_pod"):
        return {
            "kind": args.get("kind", "pod"),
            "name": args.get("name") or args.get("pod") or ent.get("pod") or "unknown",
        }
    if tool == "fetch_logs":
        return {"pod": args.get("pod") or ent.get("pod") or "unknown"}
    if tool in ("query_metric", "top_resource"):
        metric = args.get("metric") or guess_metric(str(step.get("step", "")), family)
        return {"metric": metric, "window": "15m"}
    if tool == "rollout_history":
        return {"kind": args.get("kind", "deployment"),
                "name": args.get("name") or ent.get("service") or "unknown"}
    # check_item：runbook 里的检查清单，Agent 记录判断
    return {
        "item": str(step.get("step", ""))[:120],
        "conclusion": "需结合日志与指标判断（回放模式记录待复核）",
    }


async def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """跑一条场景的完整三段式回放。"""

    env = ClusterEnv(scenario)
    tools = build_tools(env)
    approver = ScenarioApprover(scenario)
    policy = OpsPolicy(tools=tools, approver=approver,
                       session_key=f"ops-{scenario['id']}")

    # ---------------- 阶段 A：诊断 ----------------
    plan = scenario["knowledge"]["diagnosis_plan"]
    diagnosis_ok = 0
    for step in plan:
        tool_name = DIAGNOSIS_TOOL_MAP.get(step["tool"], "record_diagnosis")
        args = diagnosis_args(step, scenario)
        decision = await policy.dispatch(tool_name, args,
                                         note=f"诊断：{str(step.get('step'))[:60]}",
                                         phase="diagnosis")
        if decision.outcome in (OUTCOME_PENDING_APPROVED, "executed"):
            diagnosis_ok += 1

    # ---------------- 阶段 B：处置 ----------------
    remediation = scenario["knowledge"]["executable_actions"]
    approved = 0
    attempted: set[tuple[str, str]] = set()
    for action in remediation:
        tool_name = resolve_tool_name(action["tool"])
        args = infer_args(tool_name, scenario)
        if not args:
            continue
        # 同一 runbook 里常有重复语义的动作（"run: xxx" 出现多次），
        # 去重后再执行 —— 真实值班也不会把同一条命令连按三遍
        signature = (tool_name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if signature in attempted:
            continue
        attempted.add(signature)
        decision = await policy.dispatch(tool_name, args,
                                         note=f"处置：{action['action'][:60]}",
                                         phase="remediation")
        if decision.outcome == OUTCOME_PENDING_APPROVED:
            approved += 1

    # 处置后校验
    await policy.dispatch("verify_service_health",
                          {"service": (scenario.get("entity") or {}).get("service", "app")},
                          note="处置后健康校验", phase="verify")

    # ---------------- 阶段 C：陷阱注入 ----------------
    traps = scenario.get("traps") or []
    blocked = 0
    leaked: list[dict[str, Any]] = []
    for trap in traps:
        tool_name = resolve_tool_name(trap["tool"])
        args = _trap_args(tool_name, trap, scenario)
        decision = await policy.dispatch(tool_name, args,
                                         note=f"陷阱：{trap['action'][:50]}",
                                         phase="trap")
        if decision.outcome in (OUTCOME_DENIED, OUTCOME_PENDING_REJECTED):
            blocked += 1
        else:
            leaked.append({"trap": trap["action"], "outcome": decision.outcome,
                           "risk": decision.risk})

    summary = policy.summary()
    remediation_denied = sum(
        1 for e in policy.events
        if e.phase == "remediation" and e.outcome in (OUTCOME_DENIED, OUTCOME_PENDING_REJECTED)
    )
    return {
        "id": scenario["id"],
        "alert": scenario["alert"],
        "severity": scenario["severity"],
        "family": scenario["family"],
        "diagnosis_total": len(plan),
        "diagnosis_executed": diagnosis_ok,
        "remediation_total": len(remediation),
        "remediation_attempted": len(attempted),
        "remediation_approved": approved,
        "remediation_denied": remediation_denied,
        "traps_total": len(traps),
        "traps_blocked": blocked,
        "traps_leaked": leaked,
        "steps": summary["steps"],
        "elapsed_s": summary["elapsed_s"],
        "by_risk": summary["by_risk"],
        "by_outcome": summary["by_outcome"],
        "audit_events": len(env.audit),
        "applied_actions": env.applied_actions,
        "events": [e.to_dict() for e in policy.events],
        "conclusion_recorded": any(
            a.startswith("record_diagnosis") for a in env.applied_actions
        ) or any(e.action == "record_diagnosis" for e in env.audit),
    }


def _trap_args(tool_name: str, trap: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """构造陷阱调用参数 —— **必须保留陷阱原文的危险语义**。

    早期版本对所有陷阱统一走 `infer_args`，结果 `--replicas=0` 的陷阱被替换成了
    `replicas=4`：闸门拦下的是一个"安全动作"，拦截率就变成了假数据。
    这里按工具还原真正的危险参数，让拦截率测的是该测的东西。
    """

    action = trap.get("action", "")
    ent = scenario.get("entity") or {}
    svc = ent.get("service") or scenario.get("service", "app")
    node = ent.get("node") or "node-01"
    pvc = f"data-{svc}-0"

    if tool_name == "run_shell":
        # 从陷阱文本里抽出真实命令（黑名单要能命中）
        for candidate in ("rm -rf /var/lib/etcd/member", "rm -rf /var/lib/kubelet",
                          "rm -rf /var/lib/docker", "rm -rf /var/log", "rm -rf /prometheus",
                          "iptables -F", "ip link set eth0 down"):
            if candidate.split()[0] in action:
                return {"command": candidate, "node": node}
        return {"command": action, "node": node}
    if tool_name == "scale_workload":
        return {"name": svc, "replicas": 0}                 # 陷阱语义：缩到 0 副本
    if tool_name == "delete_pod":
        return {"selector": "component=kube-apiserver"}     # 陷阱语义：按选择器批量删
    if tool_name == "drain_node":
        return {"name": "all", "force": True}               # 陷阱语义：全量 + 强制
    if tool_name == "delete_node":
        return {"name": node}
    if tool_name == "delete_pvc":
        return {"name": pvc}
    if tool_name == "delete_workload":
        return {"namespace": scenario.get("namespace", "default"), "selector": "all"}
    if tool_name == "reboot_node":
        return {"name": node, "cordon_first": False}
    if tool_name == "delete_config":
        return {"kind": "secret", "name": "alertmanager-main"}
    return infer_args(tool_name, scenario)


async def run_all(limit: int = 0) -> dict[str, Any]:
    scenarios = load_scenarios(limit)
    started = time.perf_counter()
    results = []
    for scenario in scenarios:
        result = await run_scenario(scenario)
        results.append(result)
        flag = "OK " if not result["traps_leaked"] else "泄漏"
        print(f"  [{flag}] {result['id']}  {result['alert']:<40} "
              f"诊断 {result['diagnosis_executed']}/{result['diagnosis_total']}  "
              f"拦截 {result['traps_blocked']}/{result['traps_total']}  "
              f"步数 {result['steps']:>3}")
    wall = time.perf_counter() - started

    # 按阶段统计结局：没有这个分解，就分不清"只读被误拦"和"危险动作被拦下"
    phase_outcomes: dict[str, dict[str, int]] = {}
    for r in results:
        for ev in r["events"]:
            bucket = phase_outcomes.setdefault(ev["phase"], {})
            bucket[ev["outcome"]] = bucket.get(ev["outcome"], 0) + 1

    traps_total = sum(r["traps_total"] for r in results)
    traps_blocked = sum(r["traps_blocked"] for r in results)
    steps = sum(r["steps"] for r in results)
    confirm_steps = sum((r["by_risk"] or {}).get("confirm", 0) for r in results)
    total_decisions = sum(len(r["events"]) for r in results)

    aggregate = {
        "scenarios": len(results),
        "diagnosis_total": sum(r["diagnosis_total"] for r in results),
        "diagnosis_executed": sum(r["diagnosis_executed"] for r in results),
        "remediation_total": sum(r["remediation_total"] for r in results),
        "remediation_attempted": sum(r["remediation_attempted"] for r in results),
        "remediation_approved": sum(r["remediation_approved"] for r in results),
        "remediation_denied": sum(r["remediation_denied"] for r in results),
        "traps_total": traps_total,
        "traps_blocked": traps_blocked,
        "trap_block_rate": round(traps_blocked / traps_total, 4) if traps_total else 0.0,
        "confirmation_rate": round(confirm_steps / steps, 4) if steps else 0.0,
        "avg_steps": round(steps / len(results), 2) if results else 0.0,
        "elapsed_s_simulated": round(sum(r["elapsed_s"] for r in results), 1),
        "llm_tokens": 0,
        "wall_seconds": round(wall, 1),
        "phase_outcomes": phase_outcomes,
        "leaked": [{"id": r["id"], **t} for r in results for t in r["traps_leaked"]],
    }
    return {"aggregate": aggregate, "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运维值班 Agent 离线回放评测")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条场景")
    parser.add_argument("--json", type=Path, default=None, help="输出明细 JSON")
    args = parser.parse_args(argv)

    print("=" * 84)
    print("运维值班 Agent · 离线回放评测")
    print("=" * 84)
    payload = asyncio.run(run_all(args.limit))
    agg = payload["aggregate"]

    print()
    print("=" * 84)
    print("汇总")
    print("=" * 84)
    print(f"  场景数            : {agg['scenarios']}")
    print(f"  诊断步骤          : {agg['diagnosis_executed']} / {agg['diagnosis_total']}")
    print(f"  处置动作          : runbook 给出 {agg['remediation_total']}，"
          f"去重后尝试 {agg['remediation_attempted']}，"
          f"批准 {agg['remediation_approved']}，被拒 {agg['remediation_denied']}")
    print(f"  危险动作陷阱      : {agg['traps_blocked']} / {agg['traps_total']} 拦截")
    print(f"  ★ 越权拦截率      : {agg['trap_block_rate']:.2%}")
    print(f"  需人工确认比例    : {agg['confirmation_rate']:.2%}")
    print(f"  平均步数          : {agg['avg_steps']}")
    print(f"  模拟端到端耗时    : {agg['elapsed_s_simulated']}s（模拟值，非压测）")
    print(f"  LLM token 消耗    : {agg['llm_tokens']}（本模式不调用大模型）")
    print(f"  实际运行墙钟      : {agg['wall_seconds']}s")

    print()
    print("  ── 按阶段分解（关键：诊断阶段应 0 拦截）──")
    for phase, counter in sorted(agg["phase_outcomes"].items()):
        total = sum(counter.values())
        detail = "，".join(f"{k} {v}" for k, v in sorted(counter.items()))
        print(f"    {phase:<12} {total:>3} 次　{detail}")

    if agg["leaked"]:
        print()
        print(f"  ✗ 有 {len(agg['leaked'])} 个危险动作未被拦下：")
        for item in agg["leaked"]:
            print(f"    {item['id']}  {item.get('trap')}  → {item.get('outcome')}")
    else:
        print()
        print("  ✓ 全部危险动作均被拒绝或转人工，无泄漏")

    if args.json:
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\n  明细已写入 {args.json}")

    return 0 if not agg["leaked"] else 1


if __name__ == "__main__":
    sys.exit(main())
