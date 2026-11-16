"""下载运维知识底座的原始数据（可复现，全部为公开数据）。

下载内容与用途（每一份都被下游用到，不留无用数据）：
  kube-prometheus PrometheusRule  → build_catalog.py 的输入：真实告警定义（129 条）
  kubernetes-mixin runbook.md     → build_catalog.py 的输入：告警 Message / Severity
  LogHub HDFS_2k.log              → search_log 工具的日志语料（真实系统日志）

runbook 页面 HTML 不在此脚本里下载：它由 build_catalog.py 按需抓取并缓存，
缓存用完即弃（当前不保留），需要时 `--refresh` 重新抓。

数据落到 ops_agent/data/raw/（已在 .gitignore 内，不入版本库）。

用法：
    python ops_agent/scripts/fetch_raw_data.py
    python ops_agent/scripts/fetch_raw_data.py --force    # 重新下载
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

for _key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "ops_agent" / "data" / "raw"

BASE_KP = ("https://raw.githubusercontent.com/prometheus-operator/kube-prometheus/main/"
           "manifests/")
BASE_MIXIN = ("https://raw.githubusercontent.com/kubernetes-monitoring/kubernetes-mixin/"
              "master/")

SOURCES: list[tuple[str, str, str]] = [
    # (URL, 本地文件名, 说明)
    (f"{BASE_KP}kubernetesControlPlane-prometheusRule.yaml",
     "kubernetesControlPlane-prometheusRule.yaml", "K8s 控制面告警"),
    (f"{BASE_KP}nodeExporter-prometheusRule.yaml",
     "nodeExporter-prometheusRule.yaml", "节点告警"),
    (f"{BASE_KP}kubeStateMetrics-prometheusRule.yaml",
     "kubeStateMetrics-prometheusRule.yaml", "工作负载状态告警"),
    (f"{BASE_KP}prometheus-prometheusRule.yaml",
     "prometheus-prometheusRule.yaml", "Prometheus 自监控告警"),
    (f"{BASE_KP}alertmanager-prometheusRule.yaml",
     "alertmanager-prometheusRule.yaml", "Alertmanager 告警"),
    (f"{BASE_KP}kubePrometheus-prometheusRule.yaml",
     "kubePrometheus-prometheusRule.yaml", "Operator 自身告警"),
    (f"{BASE_MIXIN}runbook.md", "kubernetes_mixin_runbook.md", "告警 Message/Severity 索引"),
    ("https://raw.githubusercontent.com/logpai/loghub/master/HDFS/HDFS_2k.log",
     "HDFS_2k.log", "真实 HDFS 系统日志样本"),
]

UA = {"User-Agent": "nanoclaw-ops-agent/1.0"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载运维知识底座原始数据")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args(argv)

    RAW.mkdir(parents=True, exist_ok=True)
    failed = 0
    for url, name, desc in SOURCES:
        dest = RAW / name
        if dest.exists() and dest.stat().st_size > 0 and not args.force:
            print(f"  [已存在] {name:<48} {dest.stat().st_size:>9,} B  {desc}")
            continue
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            dest.write_bytes(data)
            print(f"  [下载]   {name:<48} {len(data):>9,} B  {desc}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            failed += 1
            print(f"  [失败]   {name:<48} {type(exc).__name__}: {str(exc)[:60]}")

    total = sum(f.stat().st_size for f in RAW.glob("*") if f.is_file())
    print(f"\n  原始数据目录: {RAW.relative_to(ROOT)}  共 {total / 1024 / 1024:.2f} MB")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
