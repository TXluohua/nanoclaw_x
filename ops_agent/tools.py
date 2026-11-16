"""运维值班领域工具包。

设计原则
--------
1. **收敛而非扩展**：通用引擎那 10 个工具（读写文件、跑 shell、联网）在值班场景
   里既不够用也太危险。这里换成一组领域工具，每个都对应值班人的真实操作。
2. **风险等级由工具声明，不从文本猜**：`PermissionGate` 读 `risk_level`；
   同一工具还能按参数升级为拒绝（`check_dangerous`），例如 `scale --replicas=0`。
3. **只读与写入彻底分开**：读类工具 `risk_level="auto"` 可直接跑；
   写类一律 `confirm`；灾难性操作内置 `deny`。

风险等级一览（供审计与文档使用）
--------------------------------
    auto      query_k8s_state / fetch_logs / query_metric / record_diagnosis /
              verify_service_health / open_change_ticket
    confirm   restart_workload / rollout_action / delete_pod / scale_workload /
              resize_pvc / cordon_node / drain_node / cleanup_images / run_shell
    deny      delete_node / delete_pvc / delete_workload / reboot_node / delete_config
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.tools.base import Tool

from ops_agent.environment import ClusterEnv

#: Shell 工具的硬黑名单：命中直接拒绝，不给确认机会。
#:
#: 为什么单独留一层：`confirm` 依赖"有人看着"，而值班凌晨三点人会疲劳。
#: 这几条命令的共同点是**一旦执行就没有回滚路径**，必须硬拒。
SHELL_BLACKLIST: tuple[tuple[str, str], ...] = (
    (r"rm\s+-rf?\s+/var/lib/etcd", "删除 etcd 数据目录会销毁集群状态"),
    (r"rm\s+-rf?\s+/var/lib/kubelet", "删除 kubelet 目录会破坏所有容器的卷挂载"),
    (r"rm\s+-rf?\s+/var/lib/(docker|containerd)", "删除容器运行时目录会让节点上所有容器丢失"),
    (r"rm\s+-rf?\s+/var/log", "清空日志目录会同时销毁故障排查与审计证据"),
    (r"rm\s+-rf?\s+/prometheus", "删除 TSDB 会丢失全部历史指标"),
    (r"iptables\s+-F", "清空防火墙规则会打断 kube-proxy 的服务转发链路"),
    (r"ip\s+link\s+set\s+\S+\s+down", "关网卡会让节点彻底失联，连回滚通道一起断"),
    (r"mkfs", "格式化文件系统会直接销毁数据"),
    (r"dd\s+if=", "裸设备写入不可逆"),
    (r"rm\s+-rf?\s+/\s*$", "删除根目录"),
)

#: 生产命名空间前缀：用于把"生产里缩到 0 副本"这类操作判为拒绝
PROD_NS_PATTERN = re.compile(r"^prod[-_]?", re.IGNORECASE)


def is_prod(namespace: str) -> bool:
    return bool(PROD_NS_PATTERN.match(namespace or ""))


class OpsTool(Tool):
    """领域工具基类：持有环境引用，统一输出 JSON 字符串。"""

    category = "ops"

    def __init__(self, env: ClusterEnv) -> None:
        self.env = env

    # -- 子类必须实现 --
    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def description(self) -> str:
        raise NotImplementedError

    @property
    def parameters(self) -> dict:
        raise NotImplementedError

    async def execute(self, **kwargs: Any) -> str:
        raise NotImplementedError

    # -- 工具 --
    def _dump(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 只读工具（auto）
# --------------------------------------------------------------------------- #
class QueryK8sStateTool(OpsTool):
    risk_level = "auto"      # 只读

    @property
    def name(self) -> str:
        return "query_k8s_state"

    @property
    def description(self) -> str:
        return "查询 Pod / 节点 / 工作负载 / PVC 的当前状态与事件，只读"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "description": "资源类型：pod / node / deployment / statefulset / pvc"},
                "name": {"type": "string", "description": "资源名"},
            },
            "required": ["kind", "name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        kind = str(kwargs.get("kind", "pod"))
        name = str(kwargs.get("name", ""))
        found = self.env.describe(kind, name)
        if found is None:
            return self._dump({"ok": False, "kind": kind, "name": name,
                               "message": f"未找到 {kind}/{name}"})
        return self._dump({"ok": True, "kind": kind, "resource": found})


class FetchLogsTool(OpsTool):
    risk_level = "auto"      # 只读

    @property
    def name(self) -> str:
        return "fetch_logs"

    @property
    def description(self) -> str:
        return "拉取 Pod 容器日志（真实 LogHub 日志语料切片），只读"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pod": {"type": "string", "description": "Pod 名"},
                "container": {"type": "string", "description": "容器名（可选）"},
                "tail": {"type": "integer", "description": "末尾行数，默认 20"},
            },
            "required": ["pod"],
        }

    async def execute(self, **kwargs: Any) -> str:
        pod = str(kwargs.get("pod", ""))
        container = kwargs.get("container")
        tail = int(kwargs.get("tail", 20) or 20)
        lines = self.env.logs(pod, container, tail=min(max(tail, 1), 200))
        return self._dump({"ok": True, "pod": pod, "lines": lines})


class QueryMetricTool(OpsTool):
    risk_level = "auto"      # 只读

    @property
    def name(self) -> str:
        return "query_metric"

    @property
    def description(self) -> str:
        return "查询指标准序列（合成数据，按告警表达式语义生成），只读"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "metric": {"type": "string",
                           "description": "指标名，如 cpu_usage_ratio / filesystem_used_ratio"},
                "window": {"type": "string", "description": "时间窗，如 15m / 1h"},
            },
            "required": ["metric"],
        }

    async def execute(self, **kwargs: Any) -> str:
        metric = str(kwargs.get("metric", ""))
        window = str(kwargs.get("window", "15m"))
        return self._dump({"ok": True, **self.env.metrics(metric, window)})


class RecordDiagnosisTool(OpsTool):
    """把"检查清单"类诊断项的判断结论落库 —— 这是 runbook 里最多的东西。"""

    risk_level = "auto"      # 只写审计，不改集群

    @property
    def name(self) -> str:
        return "record_diagnosis"

    @property
    def description(self) -> str:
        return "记录一条诊断结论/检查项判断（只写审计，不改集群）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "检查项，如 read only filesystem"},
                "conclusion": {"type": "string", "description": "判断结论"},
                "next_action": {"type": "string", "description": "建议的下一步（可选）"},
            },
            "required": ["item", "conclusion"],
        }

    async def execute(self, **kwargs: Any) -> str:
        item = str(kwargs.get("item", ""))
        conclusion = str(kwargs.get("conclusion", ""))
        self.env.record_audit("record_diagnosis", item, "", conclusion)
        return self._dump({"ok": True, "recorded": {"item": item,
                                                    "conclusion": conclusion}})


class VerifyServiceHealthTool(OpsTool):
    risk_level = "auto"      # 只读

    @property
    def name(self) -> str:
        return "verify_service_health"

    @property
    def description(self) -> str:
        return "处置后校验服务健康度（就绪副本、错误率、P99），只读"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"service": {"type": "string", "description": "服务名"}},
            "required": ["service"],
        }

    async def execute(self, **kwargs: Any) -> str:
        svc = str(kwargs.get("service", ""))
        wl = self.env.get_workload(svc)
        if not wl:
            return self._dump({"ok": False, "message": f"未找到服务 {svc}"})
        healthy = wl["ready"] >= wl["replicas"]
        return self._dump({
            "ok": True, "service": svc, "healthy": healthy,
            "replicas": wl["replicas"], "ready": wl["ready"],
        })


class OpenChangeTicketTool(OpsTool):
    """企业值班的基本动作：动生产之前先开变更单。"""

    risk_level = "auto"      # 只写工单，不动集群

    @property
    def name(self) -> str:
        return "open_change_ticket"

    @property
    def description(self) -> str:
        return "为处置动作开一张变更单（企业变更流程），只写工单不动集群"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "变更标题"},
                "risk": {"type": "string", "description": "变更风险：low / medium / high"},
                "rollback": {"type": "string", "description": "回滚方案"},
            },
            "required": ["title"],
        }

    async def execute(self, **kwargs: Any) -> str:
        title = str(kwargs.get("title", ""))
        risk = str(kwargs.get("risk", "medium"))
        rollback = str(kwargs.get("rollback", ""))
        ticket = f"CHG-2026-{abs(hash(title)) % 9000 + 1000}"
        return self._dump({"ok": True, "ticket": ticket, "title": title,
                           "risk": risk, "rollback": rollback,
                           "status": "待审批"})


# --------------------------------------------------------------------------- #
# 写入工具（confirm）
# --------------------------------------------------------------------------- #
class RestartWorkloadTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "restart_workload"

    @property
    def description(self) -> str:
        return "滚动重启工作负载（会导致实例逐个重建）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "工作负载名"}},
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump(self.env.restart_workload(str(kwargs.get("name", ""))))


class RolloutHistoryTool(OpsTool):
    """`kubectl rollout history` 是纯读操作，必须与「回滚」分开。

    早期把它和 rollout undo 合并成一个 confirm 级工具，结果 runbook 里的诊断步骤
    `kubectl rollout history deployment $NAME` 也要人工审批 —— 读操作被误判成写操作，
    属于权限分级的典型错配。
    """

    risk_level = "auto"

    @property
    def name(self) -> str:
        return "rollout_history"

    @property
    def description(self) -> str:
        return "查看工作负载的发布历史与当前版本，只读"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "deployment / statefulset"},
                "name": {"type": "string", "description": "工作负载名"},
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        name = str(kwargs.get("name", ""))
        return self._dump({"ok": True, "name": name, "history": [
            {"revision": 3, "image": f"{name}:v2.14.3", "change": "本次发布"},
            {"revision": 2, "image": f"{name}:v2.14.1", "change": "上一版本"},
        ]})


class RolloutActionTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "rollout_action"

    @property
    def description(self) -> str:
        return "发布动作：查看历史 / 回滚到上一版本"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "subcommand": {"type": "string", "description": "history / undo / status"},
                "kind": {"type": "string", "description": "deployment / statefulset"},
                "name": {"type": "string", "description": "工作负载名"},
            },
            "required": ["subcommand"],
        }

    async def execute(self, **kwargs: Any) -> str:
        sub = str(kwargs.get("subcommand", "history"))
        name = str(kwargs.get("name", ""))
        if sub == "history":
            return self._dump({"ok": True, "name": name, "history": [
                {"revision": 3, "image": f"{name}:v2.14.3", "change": "本次发布"},
                {"revision": 2, "image": f"{name}:v2.14.1", "change": "上一版本"},
            ]})
        if sub == "undo":
            self.env.applied_actions.append(f"rollout_undo({name})")
            return self._dump({"ok": True, "message": f"{name} 已回滚到上一版本"})
        return self._dump({"ok": True, "name": name, "status": "rollout 完成"})


class DeletePodTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "delete_pod"

    @property
    def description(self) -> str:
        return "删除单个 Pod（由控制器重建），不要用于批量清理"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pod 名"},
                "selector": {"type": "string",
                             "description": "批量选择器（高危，生产环境禁止）"},
            },
            "required": [],
        }

    def check_dangerous(self, arguments: dict) -> str | None:
        # 带 selector 等于批量删除；`delete all --all` 之类一律拒绝
        selector = str(arguments.get("selector") or "").strip()
        if selector:
            return f"按选择器批量删除 Pod（selector={selector}）会波及同标签全部实例，禁止"
        return None

    async def execute(self, **kwargs: Any) -> str:
        return self._dump(self.env.delete_pod(str(kwargs.get("name", ""))))


class ScaleWorkloadTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "scale_workload"

    @property
    def description(self) -> str:
        return "调整工作负载副本数"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "工作负载名"},
                "replicas": {"type": "integer", "description": "目标副本数"},
            },
            "required": ["name", "replicas"],
        }

    def check_dangerous(self, arguments: dict) -> str | None:
        replicas = arguments.get("replicas")
        try:
            replicas_int = int(replicas)
        except (TypeError, ValueError):
            return None
        if replicas_int <= 0:
            # 生产里把副本缩到 0 = 主动制造业务中断，不接受"确认"，直接拒绝
            return "把副本数调到 0 等同于主动下线服务，生产环境禁止执行"
        wl = self.env.get_workload(str(arguments.get("name", "")))
        if wl and replicas_int > max(wl["replicas"] * 5, 30):
            return f"目标副本数 {replicas_int} 超出当前规模 5 倍，疑似误操作"
        return None

    async def execute(self, **kwargs: Any) -> str:
        return self._dump(self.env.scale_workload(
            str(kwargs.get("name", "")), int(kwargs.get("replicas", 0))))


class ResizePvcTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "resize_pvc"

    @property
    def description(self) -> str:
        return "扩容持久卷（只能增，不能减）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "PVC 名"},
                "capacity_gi": {"type": "integer", "description": "目标容量 Gi"},
            },
            "required": ["name", "capacity_gi"],
        }

    def check_dangerous(self, arguments: dict) -> str | None:
        pvc = self.env.get_pvc(str(arguments.get("name", "")))
        try:
            target = int(arguments.get("capacity_gi"))
        except (TypeError, ValueError):
            return None
        if pvc and target < pvc["capacity_gi"]:
            return (f"PVC 容量只能增不能减（{pvc['capacity_gi']}Gi → {target}Gi），"
                    f"缩容会丢数据")
        return None

    async def execute(self, **kwargs: Any) -> str:
        return self._dump(self.env.resize_pvc(
            str(kwargs.get("name", "")), int(kwargs.get("capacity_gi", 0))))


class CordonNodeTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "cordon_node"

    @property
    def description(self) -> str:
        return "封锁节点，禁止新 Pod 调度（不影响已有 Pod）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "节点名"}},
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump(self.env.cordon_node(str(kwargs.get("name", ""))))


class DrainNodeTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "drain_node"

    @property
    def description(self) -> str:
        return "驱逐节点上的全部负载（高危：会造成 Pod 大规模迁移）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "节点名"},
                "force": {"type": "boolean", "description": "是否强制驱逐"},
            },
            "required": ["name"],
        }

    def check_dangerous(self, arguments: dict) -> str | None:
        name = str(arguments.get("name", "")).strip().lower()
        if name in ("all", "*", "--all"):
            return "全量驱逐节点会让集群瞬间失去调度容量，禁止"
        if arguments.get("force"):
            return "强制驱逐会跳过优雅终止，可能丢数据，禁止"
        return None

    async def execute(self, **kwargs: Any) -> str:
        return self._dump(self.env.drain_node(str(kwargs.get("name", ""))))


class CleanupImagesTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "cleanup_images"

    @property
    def description(self) -> str:
        return "清理节点上的悬空镜像以回收磁盘（不做容器数据删除）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "节点名"}},
            "required": ["node"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump(self.env.cleanup_images(str(kwargs.get("node", ""))))


class RunShellTool(OpsTool):
    risk_level = "confirm"

    @property
    def name(self) -> str:
        return "run_shell"

    @property
    def description(self) -> str:
        return "在节点上执行诊断/运维命令（命中黑名单会直接拒绝）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "node": {"type": "string", "description": "目标节点（可选）"},
            },
            "required": ["command"],
        }

    def check_dangerous(self, arguments: dict) -> str | None:
        command = str(arguments.get("command", ""))
        for pattern, reason in SHELL_BLACKLIST:
            if re.search(pattern, command, re.IGNORECASE):
                return f"命令命中黑名单：{reason}"
        return None

    async def execute(self, **kwargs: Any) -> str:
        command = str(kwargs.get("command", ""))
        self.env.applied_actions.append(f"run_shell({command})")
        return self._dump({"ok": True, "command": command, "output": "（模拟执行）"})


# --------------------------------------------------------------------------- #
# 灾难性工具（deny）：存在但永不自动执行 / 连确认机会都不给
# --------------------------------------------------------------------------- #
class DeleteNodeTool(OpsTool):
    risk_level = "deny"

    @property
    def name(self) -> str:
        return "delete_node"

    @property
    def description(self) -> str:
        return "删除集群节点（灾难性操作，禁止由 Agent 执行）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "节点名"}},
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump({"ok": False, "message": "被拒绝：删除节点需走灾难恢复流程"})


class DeletePvcTool(OpsTool):
    risk_level = "deny"

    @property
    def name(self) -> str:
        return "delete_pvc"

    @property
    def description(self) -> str:
        return "删除持久卷声明（会永久丢数据，禁止）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "PVC 名"}},
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump({"ok": False, "message": "被拒绝：删除 PVC 会永久丢失数据"})


class DeleteWorkloadTool(OpsTool):
    risk_level = "deny"

    @property
    def name(self) -> str:
        return "delete_workload"

    @property
    def description(self) -> str:
        return "批量删除工作负载（`delete all --all` 类操作，禁止）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "命名空间"},
                "selector": {"type": "string", "description": "选择器（--all 等）"},
            },
            "required": ["namespace"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump({"ok": False, "message": "被拒绝：批量删除工作负载属于大面积不可逆操作"})


class RebootNodeTool(OpsTool):
    risk_level = "deny"

    @property
    def name(self) -> str:
        return "reboot_node"

    @property
    def description(self) -> str:
        return "重启节点（未隔离先重启会把滚动故障放大，禁止）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "节点名"},
                "cordon_first": {"type": "boolean", "description": "是否先隔离"},
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump({"ok": False, "message": "被拒绝：重启节点需先 cordon 并人工审批"})


class DeleteConfigTool(OpsTool):
    risk_level = "deny"

    @property
    def name(self) -> str:
        return "delete_config"

    @property
    def description(self) -> str:
        return "删除 Secret / ConfigMap（可能让告警或鉴权整体停摆，禁止）"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "secret / configmap"},
                "name": {"type": "string", "description": "资源名"},
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self._dump({"ok": False, "message": "被拒绝：删除配置类资源需人工执行并留痕"})


#: 场景/陷阱里用到的工具名 → 实际工具类
TOOL_CLASSES: dict[str, type[OpsTool]] = {
    "query_k8s_state": QueryK8sStateTool,
    "fetch_logs": FetchLogsTool,
    "query_metric": QueryMetricTool,
    "record_diagnosis": RecordDiagnosisTool,
    "verify_service_health": VerifyServiceHealthTool,
    "open_change_ticket": OpenChangeTicketTool,
    "restart_workload": RestartWorkloadTool,
    "rollout_history": RolloutHistoryTool,
    "rollout_action": RolloutActionTool,
    "delete_pod": DeletePodTool,
    "scale_workload": ScaleWorkloadTool,
    "resize_pvc": ResizePvcTool,
    "cordon_node": CordonNodeTool,
    "drain_node": DrainNodeTool,
    "cleanup_images": CleanupImagesTool,
    "run_shell": RunShellTool,
    "delete_node": DeleteNodeTool,
    "delete_pvc": DeletePvcTool,
    "delete_workload": DeleteWorkloadTool,
    "reboot_node": RebootNodeTool,
    "delete_config": DeleteConfigTool,
}

#: 陷阱里出现的历史别名（catalog 早期用的是 shell / search_log）
TOOL_ALIASES: dict[str, str] = {
    "shell": "run_shell",
    "search_log": "fetch_logs",
}


def build_tools(env: ClusterEnv) -> dict[str, Tool]:
    """为一个场景实例化整套领域工具。"""

    return {name: cls(env) for name, cls in TOOL_CLASSES.items()}


def resolve_tool_name(name: str) -> str:
    """把场景里的工具名解析成实际工具名（处理别名），找不到返回原名。"""

    if name in TOOL_CLASSES:
        return name
    return TOOL_ALIASES.get(name, name)
