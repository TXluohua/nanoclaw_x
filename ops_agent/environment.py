"""受控模拟环境：给运维工具提供可查询、可修改、可审计的集群状态。

为什么需要它
------------
值班 Agent 的工具必须能真的"查到东西"、也真的"改到东西"，否则权限闸门
无从验证。但项目不能连真实生产集群，所以这里做一个**确定性**的环境替身：

- 状态由场景（scenario）决定，同一场景 + 同一随机种子 → 完全可复现；
- 每次修改都会记录一条 audit 事件（谁、改了什么、改前改后），供复盘使用；
- 日志语料取自 LogHub 真实系统日志（HDFS_2k.log），指标按告警表达式语义合成。

边界说明（对外表述必须守住）
----------------------------
- 集群拓扑、Pod / 节点 / PVC 名称与状态 —— **模拟**
- 日志文本 —— **真实**（LogHub HDFS 日志，但来源是 HDFS，不是 K8s）
- 指标时序 —— **合成**（按 `rate(...)`、`histogram_quantile(...)` 等表达式形状生成）
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"
LOG_CORPUS = RAW_DIR / "HDFS_2k.log"


def _seed_of(text: str) -> int:
    """把场景 ID 变成稳定随机种子 —— 保证同一场景每次回放结果一致。"""

    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


@dataclass
class AuditEvent:
    """一次环境变更的审计记录。"""

    actor: str
    action: str
    target: str
    before: str
    after: str

    def to_dict(self) -> dict[str, str]:
        return {"actor": self.actor, "action": self.action, "target": self.target,
                "before": self.before, "after": self.after}


@dataclass
class ClusterEnv:
    """一个场景对应的一小片集群状态。

    Attributes:
        scenario: 场景定义（来自 catalog/scenarios.json）
        pods / nodes / pvcs / workloads: 模拟实体
        audit: 变更审计流水
        applied_actions: 已成功执行的动作（复盘中"到底做了什么"）
    """

    scenario: dict[str, Any]
    pods: dict[str, dict[str, Any]] = field(default_factory=dict)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    pvcs: dict[str, dict[str, Any]] = field(default_factory=dict)
    workloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    applied_actions: list[str] = field(default_factory=list)
    _rng: random.Random = field(init=False, repr=False)
    _log_lines: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(_seed_of(self.scenario["id"]))
        self._log_lines = self._load_logs()
        self._build_topology()

    # ------------------------------------------------------------------ 构建
    def _load_logs(self) -> list[str]:
        """加载真实日志语料；缺失时退化为极简占位（不影响其它功能）。"""

        if not LOG_CORPUS.is_file():
            return ["000000 000000 0 INFO fallback: log corpus not downloaded"]
        lines = [ln.rstrip() for ln in LOG_CORPUS.read_text(
            encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        return lines

    def _build_topology(self) -> None:
        ent = self.scenario.get("entity") or {}
        ns = ent.get("namespace") or self.scenario.get("namespace", "default")
        svc = ent.get("service") or self.scenario.get("service", "app")
        pod = ent.get("pod") or f"{svc}-0"
        node = ent.get("node") or "node-01"
        family = self.scenario.get("family", "")
        alert = self.scenario.get("alert", "")

        # 工作负载：副本数与就绪数按告警语义设置
        replicas, ready = 3, 3
        if alert in ("KubeDeploymentReplicasMismatch", "KubeStatefulSetReplicasMismatch"):
            replicas, ready = 3, 2
        elif alert == "KubePodCrashLooping":
            replicas, ready = 3, 2
        elif alert in ("KubePodNotReady", "KubeContainerWaiting"):
            replicas, ready = 3, 2
        elif alert == "KubeQuotaExceeded":
            replicas, ready = 3, 1

        self.workloads[svc] = {
            "name": svc, "namespace": ns, "kind": "Deployment",
            "replicas": replicas, "ready": ready,
            "image": f"registry.corp.local/prod/{svc}:v2.14.3",
        }
        self.pods[pod] = {
            "name": pod, "namespace": ns, "workload": svc, "node": node,
            "phase": "Running",
            "ready": alert != "KubePodNotReady",
            "restarts": 7 if alert == "KubePodCrashLooping" else 0,
            "reason": "CrashLoopBackOff" if alert == "KubePodCrashLooping" else "",
            "container_waiting": alert == "KubeContainerWaiting",
            "cpu_throttled": alert == "CPUThrottlingHigh",
        }

        # 节点：把告警族映射成可观察的状态
        self.nodes[node] = {
            "name": node,
            "ready": alert not in ("KubeNodeNotReady", "KubeNodeUnreachable",
                                   "KubeletDown", "KubeletInstanceUnreachable"),
            "unreachable": alert in ("KubeNodeUnreachable", "KubeletDown"),
            "flapping": alert == "KubeNodeReadinessFlapping",
            "raid_degraded": alert == "NodeRAIDDegraded",
            "filesystem_full_pct": 96.4 if alert.startswith("NodeFilesystem") else 41.0,
            "fd_used_pct": 94.2 if alert == "NodeFileDescriptorLimit" else 18.0,
            "clock_offset_ms": 1240 if alert == "NodeClockSkewDetected" else 3,
            "conntrack_used_pct": 91.5 if alert == "NodeHighNumberConntrackEntriesUsed" else 22.0,
            "cordoned": False,
        }

        # 持久卷：容量按告警语义设置
        pvc = f"data-{svc}-0"
        used = 92.7 if alert == "KubePersistentVolumeFillingUp" else 38.5
        self.pvcs[pvc] = {
            "name": pvc, "namespace": ns, "capacity_gi": 200,
            "used_pct": used, "phase": "Bound",
            "erroring": alert == "KubePersistentVolumeErrors",
            "storage_class": "fast-ssd",
        }
        # 场景里提到但不在拓扑表中的实体也能被查到（返回"不存在"）
        self._family = family

    # ------------------------------------------------------------------ 查询
    def get_pod(self, name: str) -> dict[str, Any] | None:
        return self.pods.get(name)

    def get_node(self, name: str) -> dict[str, Any] | None:
        return self.nodes.get(name)

    def get_workload(self, name: str) -> dict[str, Any] | None:
        return self.workloads.get(name)

    def get_pvc(self, name: str) -> dict[str, Any] | None:
        return self.pvcs.get(name)

    def describe(self, kind: str, name: str) -> dict[str, Any] | None:
        lookup = {
            "pod": self.get_pod, "pods": self.get_pod,
            "node": self.get_node, "nodes": self.get_node,
            "deployment": self.get_workload, "statefulset": self.get_workload,
            "pvc": self.get_pvc, "persistentvolumeclaim": self.get_pvc,
        }
        fn = lookup.get(kind.lower())
        return fn(name) if fn else None

    def logs(self, pod: str, container: str | None = None, tail: int = 20) -> list[str]:
        """返回真实日志切片。

        真实日志来自 HDFS，与 K8s Pod 无关 —— 因此在前面拼一条明显的来源标注，
        避免演示时被误读成"这是该 Pod 的真实日志"。
        """

        start = self._rng.randrange(0, max(len(self._log_lines) - tail, 1))
        picked = self._log_lines[start:start + tail]
        header = [f"# 来源：LogHub HDFS_2k.log（真实系统日志样本，非本 Pod 原始日志）",
                  f"# 目标：pod={pod} container={container or 'default'}"]
        return header + picked

    def metrics(self, metric: str, window: str = "15m") -> dict[str, Any]:
        """按告警表达式形状合成指标序列（不是真实监控数据）。"""

        points = 12
        base = {"cpu_usage_ratio": 0.82, "memory_usage_ratio": 0.71,
                "filesystem_used_ratio": 0.93, "pod_restart_rate": 0.35,
                "request_error_ratio": 0.041, "p99_latency_ms": 840.0}.get(metric)
        if base is None:
            base = 0.5
        series = []
        for i in range(points):
            drift = math.sin(i / 2.0) * 0.06 + self._rng.uniform(-0.02, 0.02)
            series.append(round(max(base + drift, 0.0), 4))
        return {
            "metric": metric, "window": window, "points": points,
            "series": series,
            "note": "合成数据：按表达式语义生成，非真实监控",
        }

    # ------------------------------------------------------------------ 变更
    def _audit(self, action: str, target: str, before: str, after: str) -> None:
        self.audit.append(AuditEvent(actor="ops-agent", action=action,
                                     target=target, before=before, after=after))

    def record_audit(self, action: str, target: str, before: str, after: str,
                     actor: str = "ops-agent") -> None:
        """公开的审计入口：工具层用它记录不可变更的操作（如诊断结论）。"""

        self.audit.append(AuditEvent(actor=actor, action=action, target=target,
                                     before=before, after=after))

    def restart_workload(self, name: str) -> dict[str, Any]:
        wl = self.workloads.get(name)
        if not wl:
            return {"ok": False, "message": f"未找到工作负载 {name}"}
        self._audit("restart_workload", name, f"replicas={wl['replicas']}",
                    f"replicas={wl['replicas']} (rollout restart)")
        self.applied_actions.append(f"restart_workload({name})")
        for pod in self.pods.values():
            if pod["workload"] == name:
                pod["restarts"] = 0
                pod["reason"] = ""
        return {"ok": True, "message": f"已触发 {name} 滚动重启"}

    def scale_workload(self, name: str, replicas: int) -> dict[str, Any]:
        wl = self.workloads.get(name)
        if not wl:
            return {"ok": False, "message": f"未找到工作负载 {name}"}
        before = wl["replicas"]
        wl["replicas"] = replicas
        self._audit("scale_workload", name, f"replicas={before}", f"replicas={replicas}")
        self.applied_actions.append(f"scale_workload({name},{replicas})")
        return {"ok": True, "message": f"{name} 副本 {before} → {replicas}"}

    def delete_pod(self, name: str) -> dict[str, Any]:
        pod = self.pods.get(name)
        if not pod:
            return {"ok": False, "message": f"未找到 Pod {name}"}
        self._audit("delete_pod", name, "phase=Running", "deleted")
        self.applied_actions.append(f"delete_pod({name})")
        pod["phase"] = "Terminating"
        return {"ok": True, "message": f"已删除 Pod {name}（由控制器重建）"}

    def drain_node(self, name: str) -> dict[str, Any]:
        node = self.nodes.get(name)
        if not node:
            return {"ok": False, "message": f"未找到节点 {name}"}
        self._audit("drain_node", name, f"cordoned={node['cordoned']}", "cordoned=True,drained=True")
        self.applied_actions.append(f"drain_node({name})")
        node["cordoned"] = True
        return {"ok": True, "message": f"已驱逐节点 {name} 上全部负载"}

    def cordon_node(self, name: str) -> dict[str, Any]:
        node = self.nodes.get(name)
        if not node:
            return {"ok": False, "message": f"未找到节点 {name}"}
        self._audit("cordon_node", name, "cordoned=False", "cordoned=True")
        self.applied_actions.append(f"cordon_node({name})")
        node["cordoned"] = True
        return {"ok": True, "message": f"已封锁节点 {name}，不再调度新 Pod"}

    def resize_pvc(self, name: str, capacity_gi: int) -> dict[str, Any]:
        pvc = self.pvcs.get(name)
        if not pvc:
            return {"ok": False, "message": f"未找到 PVC {name}"}
        before = pvc["capacity_gi"]
        pvc["capacity_gi"] = capacity_gi
        pvc["used_pct"] = round(pvc["used_pct"] * before / capacity_gi, 1)
        self._audit("resize_pvc", name, f"capacity={before}Gi", f"capacity={capacity_gi}Gi")
        self.applied_actions.append(f"resize_pvc({name},{capacity_gi})")
        return {"ok": True, "message": f"{name} 容量 {before}Gi → {capacity_gi}Gi"}

    def cleanup_images(self, node: str) -> dict[str, Any]:
        node_info = self.nodes.get(node)
        if not node_info:
            return {"ok": False, "message": f"未找到节点 {node}"}
        before = node_info["filesystem_full_pct"]
        node_info["filesystem_full_pct"] = round(max(before - 12.5, 5.0), 1)
        self._audit("cleanup_images", node, f"disk={before}%",
                    f"disk={node_info['filesystem_full_pct']}%")
        self.applied_actions.append(f"cleanup_images({node})")
        return {"ok": True, "message": f"{node} 清理悬空镜像，磁盘占用 {before}% → "
                                       f"{node_info['filesystem_full_pct']}%"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "workloads": json.loads(json.dumps(self.workloads)),
            "pods": json.loads(json.dumps(self.pods)),
            "nodes": json.loads(json.dumps(self.nodes)),
            "pvcs": json.loads(json.dumps(self.pvcs)),
            "audit": [e.to_dict() for e in self.audit],
            "applied_actions": list(self.applied_actions),
        }
