"""从公开数据源构建运维值班 Agent 的「告警 + 处置」知识底座。

数据来源（全部真实、公开、可复现）：
  1. prometheus-operator/kube-prometheus 的 PrometheusRule（Apache-2.0）
     —— 129 条真实 Kubernetes 告警定义：名称 / 表达式 / for / severity / summary / runbook_url
  2. runbooks.prometheus-operator.dev（Apache-2.0）
     —— 每条告警对应的真实 runbook 页面，结构固定为
        Meaning / Impact / Diagnosis / Mitigation 四节，Mitigation 即真实处置动作
  3. kubernetes-monitoring/kubernetes-mixin 的 runbook.md（Apache-2.0）
     —— 告警 Message / Severity 的权威描述

产物（提交到版本库，保证离线可跑、测试可复现）：
  ops_agent/catalog/alerts.json    规范化后的告警目录
  ops_agent/catalog/runbooks.json  告警 → 含义 / 影响 / 诊断步骤 / 处置动作 / 风险等级

用法：
    python ops_agent/scripts/build_catalog.py                # 有缓存则用缓存
    python ops_agent/scripts/build_catalog.py --refresh      # 强制重新抓 runbook 页
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

for _key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "ops_agent"
RAW = OPS / "data" / "raw"
CACHE = RAW / "runbooks_html"
CATALOG = OPS / "catalog"

BASE_KP = ("https://raw.githubusercontent.com/prometheus-operator/kube-prometheus/main/"
           "manifests/")
RULE_FILES = [
    "kubernetesControlPlane-prometheusRule.yaml",
    "nodeExporter-prometheusRule.yaml",
    "kubeStateMetrics-prometheusRule.yaml",
    "prometheus-prometheusRule.yaml",
    "alertmanager-prometheusRule.yaml",
    "kubePrometheus-prometheusRule.yaml",
]

UA = {"User-Agent": "nanoclaw-ops-agent/1.0 (+catalog builder)"}

#: 处置动作的风险分级关键字。
#: read        —— 只读排查，可直接执行
#: mutate      —— 改变运行状态，需要人工确认
#: destructive —— 可能造成数据丢失或大面积影响，默认拒绝
#:
#: 顺序很重要：先判 destructive 再判 mutate。
#: 第一版漏了裸词 "remove"（"Remove unused images" 被判成 read），
#: 所以这里宁可把破坏性词收宽一点 —— 误判成危险只是多一道确认，
#: 误判成安全就是让 Agent 直接动生产。
RISK_RULES = (
    ("destructive", (
        "remove", "delete", "drop", "wipe", "prune", "purge", "truncate",
        "rm -", "rmdir", "reset", "restore", "rollback", "kill -9", "force delete",
        "drain", "recreate", "cleanup all", "vacuum",
    )),
    ("mutate", (
        "restart", "scale", "rollout", "patch", "edit", "apply", "cordon",
        "uncordon", "evict", "increase", "decrease", "resize", "flush", "clean",
        "reboot", "upgrade", "set ", "failover", "promote", "abort", "pause",
        "resume", "migrate", "failback", "disable", "enable", "update", "replace",
    )),
)

#: 抽取时会混进来的噪声（编号、"1"、代码块残片），过短的一律丢掉
MIN_ACTION_LEN = 12


def infer_risk(action: str) -> str:
    """按动作文本推断风险等级（保守：命中 destructive 就先判 destructive）。"""

    low = action.lower()
    for level, keywords in RISK_RULES:
        if any(k in low for k in keywords):
            return level
    return "read"


def is_noise(text: str) -> bool:
    """过滤列表里的编号、单独数字、代码残片。"""

    stripped = text.strip()
    if len(stripped) < MIN_ACTION_LEN:
        return True
    if stripped.isdigit():
        return True
    if not any(ch.isalpha() for ch in stripped):
        return True
    return False


class RunbookParser(HTMLParser):
    """抽取 <article> 内的 h1/h2 与段落、列表项。

    页面结构（实测）：
        h1 KubePodCrashLooping
        h2 Meaning    → p
        h2 Impact     → p
        h2 Diagnosis  → ul/li（诊断步骤）
        h2 Mitigation → p + ul/li（处置动作）
    """

    SECTIONS = ("meaning", "impact", "diagnosis", "mitigation")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_article = False
        self.current: str | None = None
        self.buffer: list[str] = []
        self.title = ""
        self.sections: dict[str, list[str]] = {s: [] for s in self.SECTIONS}
        self._capture: str | None = None

    # -- 元素级 --
    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.in_article = True
            return
        if not self.in_article:
            return
        if tag in ("h1", "h2"):
            self._flush()
            self._capture = "heading"
            self.buffer = []
        elif tag in ("p", "li"):
            self._flush()
            self._capture = tag
            self.buffer = []

    def handle_endtag(self, tag):
        if tag == "article":
            self._flush()
            self.in_article = False
            return
        if not self.in_article:
            return
        if tag in ("h1", "h2", "p", "li"):
            self._flush()

    def handle_data(self, data):
        if self.in_article and self._capture:
            self.buffer.append(data)

    # -- 缓冲刷新 --
    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
        capture, self._capture, self.buffer = self._capture, None, []
        if not text:
            return
        if capture == "heading":
            key = text.lower().strip("# ").strip()
            if key in self.sections:
                self.current = key
            elif not self.title:
                self.title = text
            else:
                self.current = None
            return
        if self.current is None:
            return
        # 列表项里会混进编号（"1."、"1"）与代码残片，丢弃；段落不丢
        if capture == "li" and is_noise(text):
            return
        if text not in self.sections[self.current]:
            self.sections[self.current].append(text)


def fetch(url: str, dest: Path, refresh: bool = False) -> bytes:
    if dest.exists() and dest.stat().st_size > 0 and not refresh:
        return dest.read_bytes()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return data


def load_alerts() -> list[dict]:
    try:
        import yaml
    except ImportError:
        print("需要 PyYAML: python -m pip install pyyaml")
        raise SystemExit(2)

    alerts: list[dict] = []
    for name in RULE_FILES:
        path = RAW / name
        if not path.exists():
            print(f"  [跳过] 缺少原始文件 {name}（先跑 fetch_raw_data.py）")
            continue
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict):
                continue
            for group in (doc.get("spec") or {}).get("groups") or []:
                for rule in group.get("rules") or []:
                    if "alert" not in rule:
                        continue
                    ann = rule.get("annotations") or {}
                    labels = rule.get("labels") or {}
                    alerts.append({
                        "name": rule["alert"],
                        "group": group.get("name"),
                        "expr": str(rule.get("expr") or "").strip(),
                        "for": rule.get("for"),
                        "severity": labels.get("severity") or "none",
                        "summary": ann.get("summary"),
                        "description": ann.get("description"),
                        "runbook_url": ann.get("runbook_url"),
                        "source": name,
                    })
    return alerts


def load_mixin_index() -> dict[str, dict]:
    """kubernetes-mixin 的 runbook.md → 告警 Message / Severity 权威描述。"""

    path = RAW / "kubernetes_mixin_runbook.md"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    index: dict[str, dict] = {}
    blocks = re.split(r"^#####\s*Alert Name:\s*", text, flags=re.M)[1:]
    for block in blocks:
        head = block.splitlines()[0]
        m = re.match(r"\"?([\w.]+)\"?", head.strip())
        if not m:
            continue
        name = m.group(1)
        body = block[block.find("\n") + 1:]
        msg = re.search(r"\*\*?Message\*\*?:\s*`?([^\n`]*)", body)
        sev = re.search(r"\*\*?Severity\*\*?:\s*(\w+)", body)
        link = re.search(r"\*\*?Runbook\*\*?:\s*\[Link\]\(([^)]+)\)", body)
        index[name] = {
            "message": msg.group(1).strip() if msg else None,
            "severity": sev.group(1).strip() if sev else None,
            "documented_runbook": link.group(1).strip() if link else None,
        }
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建告警与处置知识底座")
    parser.add_argument("--refresh", action="store_true", help="强制重新抓 runbook 页面")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用）")
    args = parser.parse_args(argv)

    CATALOG.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    print("=" * 74)
    print("一、解析告警规则")
    print("=" * 74)
    alerts = load_alerts()
    mixin = load_mixin_index()
    for alert in alerts:
        doc = mixin.get(alert["name"]) or {}
        alert["documented_message"] = doc.get("message")
        alert["documented_severity"] = doc.get("severity")
        alert["documented_in_runbook_md"] = bool(doc)
    print(f"  告警条数: {len(alerts)}")
    by_sev: dict[str, int] = {}
    for alert in alerts:
        by_sev[alert["severity"]] = by_sev.get(alert["severity"], 0) + 1
    print(f"  severity 分布: {by_sev}")
    print(f"  在 kubernetes-mixin runbook.md 中有权威描述: "
          f"{sum(1 for a in alerts if a['documented_in_runbook_md'])}")

    if args.limit:
        alerts = alerts[: args.limit]

    print()
    print("=" * 74)
    print("二、抓取并解析 runbook 页面")
    print("=" * 74)
    runbooks: dict[str, dict] = {}
    stats = {"cached": 0, "fetched": 0, "failed": 0, "with_steps": 0}
    started = time.perf_counter()

    for i, alert in enumerate(alerts, start=1):
        url = alert.get("runbook_url")
        if not url:
            runbooks[alert["name"]] = {"available": False, "reason": "无 runbook_url"}
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        cache_file = CACHE / f"{slug}.html"
        was_cached = cache_file.exists() and cache_file.stat().st_size > 0
        try:
            raw = fetch(url, cache_file, refresh=args.refresh)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            stats["failed"] += 1
            runbooks[alert["name"]] = {"available": False,
                                       "reason": f"抓取失败: {type(exc).__name__}"}
            continue
        stats["cached" if was_cached else "fetched"] += 1

        rp = RunbookParser()
        rp.feed(raw.decode("utf-8", errors="replace"))
        diagnosis = rp.sections["diagnosis"]
        mitigation = rp.sections["mitigation"]
        if diagnosis or mitigation:
            stats["with_steps"] += 1

        runbooks[alert["name"]] = {
            "available": bool(diagnosis or mitigation),
            "url": url,
            "meaning": " ".join(rp.sections["meaning"])[:600] or None,
            "impact": " ".join(rp.sections["impact"])[:600] or None,
            "diagnosis": diagnosis,
            "mitigation": [
                {"action": act, "risk": infer_risk(act)} for act in mitigation
            ],
            "has_diagnosis": bool(diagnosis),
            "has_mitigation": bool(mitigation),
        }

        if i % 20 == 0:
            print(f"  ... 已处理 {i}/{len(alerts)}（缓存 {stats['cached']} / "
                  f"新抓 {stats['fetched']} / 失败 {stats['failed']}）")

    elapsed = time.perf_counter() - started
    print(f"  处理完成，用时 {elapsed:.1f}s")
    print(f"  缓存命中 {stats['cached']}，新抓取 {stats['fetched']}，失败 {stats['failed']}")
    print(f"  含诊断或处置步骤: {stats['with_steps']} / {len(alerts)}")

    # ---- 汇总风险分布 ----
    total_actions = 0
    risk_count: dict[str, int] = {}
    for rb in runbooks.values():
        for item in rb.get("mitigation") or []:
            total_actions += 1
            risk_count[item["risk"]] = risk_count.get(item["risk"], 0) + 1
    print(f"  处置动作总数: {total_actions}，风险分布: {risk_count}")

    # ---- 落盘 ----
    alerts_path = CATALOG / "alerts.json"
    runbooks_path = CATALOG / "runbooks.json"
    alerts_path.write_text(
        json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "count": len(alerts), "alerts": alerts},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    runbooks_path.write_text(
        json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "count": len(runbooks), "runbooks": runbooks},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(f"  已写入 {alerts_path.relative_to(ROOT)}  "
          f"({alerts_path.stat().st_size / 1024:.0f} KB)")
    print(f"  已写入 {runbooks_path.relative_to(ROOT)}  "
          f"({runbooks_path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
