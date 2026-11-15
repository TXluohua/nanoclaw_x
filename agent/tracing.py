"""
全链路追踪与成本核算模块

解决两个问题
------------
1. **Agent 干了什么，事后说不清。**
   LLM 应用最难的排查是"它为什么这么回答"。如果只有一份对话记录，
   你只知道输入和输出，不知道中间调了几次模型、每次花了多少 token、
   哪个工具慢、哪一步被人拒绝 —— 排查只能靠猜。

2. **钱花在哪了，没人知道。**
   `providers/openai_compat.py` 每次都从响应里解析出了 `usage`，但这个字段
   在整个项目里**没有被任何地方使用**。跑一天下来花了多少钱，
   既没有累计也没有上限。

产出
----
    workspace/traces/YYYY-MM-DD.jsonl          每次运行的事件流水（逐条追加）
    workspace/traces/sessions/<session>.json   单会话的累计 token / 成本

为什么要逐条 append 而不是最后一次性写
--------------------------------------
进程崩溃 / 被 kill / 工具把进程搞死时，最后一次性写会丢掉全部现场。
逐条追加最多丢最后一个事件，前面的都还在——这正是排查崩溃时最需要的。

事件类型
--------
    run_start / run_end    一次 run() 的生命周期
    llm_request            发请求前（模型、消息数、工具数）
    llm_response           收到响应（耗时、token、成本、工具调用数）
    tool_call / tool_result 单个工具的调用与结果（含耗时）
    permission             风险分级结果
    pending_saved          挂起待确认
    approval_resolved      用户确认 / 拒绝 / 作废
    fuse                   防爆熔断
    budget_exceeded        超出预算
    error                  异常
"""

import json
import os
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote


def _now_iso() -> str:
    """本地时间，精确到毫秒。"""
    return datetime.now().isoformat(timespec="milliseconds")


def _truncate(value: Any, limit: int) -> str:
    """把任意值转成截断后的字符串，避免 trace 文件被长文本撑爆。"""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    return value if len(value) <= limit else value[:limit] + "…"


class CostEstimator:
    """按模型价目表把 token 换算成美元。

    价目表格式（USD / 每百万 token）：

        {
            "deepseek-v4-pro": {"input": 0.0, "output": 0.0},
            "deepseek-flash":  {"input": 0.0, "output": 0.0}
        }

    ★ 默认价目表是空的，此时只统计 token、不算钱，并明确显示
      "未配置价目表"。**故意不内置任何价格数字** —— 各家价格会变，
      写死一个过期的数字比不写更误导人。请按官方价目表自行填写。
    """

    def __init__(self, pricing: dict[str, Any] | None = None) -> None:
        self.pricing = pricing or {}

    def has_price(self, model: str) -> bool:
        entry = self.pricing.get(model) or {}
        return bool(entry.get("input") or entry.get("output"))

    def cost(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> float | None:
        """返回本次调用成本（美元），价目表未配置时返回 None。"""
        if not self.has_price(model):
            return None
        entry = self.pricing.get(model) or {}
        in_price = float(entry.get("input") or 0.0)
        out_price = float(entry.get("output") or 0.0)
        # 单位是"每百万 token"
        return (
            prompt_tokens / 1_000_000 * in_price
            + completion_tokens / 1_000_000 * out_price
        )


class UsageMeter:
    """单会话的 token 与成本累计，落盘保证重启后仍然连续。

    为什么按会话累计而不是全局：预算的意义是"控制单个用户/单个任务的花费"。
    全局累计只能事后看账单，不能当护栏用。
    """

    def __init__(
        self,
        path: str,
        estimator: CostEstimator,
        session_key: str = "",
    ) -> None:
        self.path = path
        self.estimator = estimator
        self.session_key = session_key
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        empty = {
            "session_key": self.session_key,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "llm_calls": 0,
            "tool_calls": 0,
            "runs": 0,
            "priced": False,
            "updated_at": "",
        }
        if not os.path.isfile(self.path):
            return empty
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                empty.update(loaded)
        except Exception as e:
            print(f"警告: 读取用量统计失败 - {e}")
        return empty

    def add_llm(
        self, model: str, usage: dict[str, Any] | None
    ) -> dict[str, Any]:
        """累计一次模型调用的用量与成本。"""
        usage = usage or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(
            usage.get("total_tokens") or (prompt_tokens + completion_tokens)
        )

        self.data["prompt_tokens"] += prompt_tokens
        self.data["completion_tokens"] += completion_tokens
        self.data["total_tokens"] += total_tokens
        self.data["llm_calls"] += 1

        cost = self.estimator.cost(model, prompt_tokens, completion_tokens)
        if cost is not None:
            self.data["cost_usd"] = round(self.data["cost_usd"] + cost, 8)
            self.data["priced"] = True

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": cost,
        }

    def add_tool(self) -> None:
        self.data["tool_calls"] += 1

    def add_run(self) -> None:
        self.data["runs"] += 1

    def save(self) -> None:
        self.data["updated_at"] = _now_iso()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"警告: 保存用量统计失败 - {e}")

    def cost_display(self) -> str:
        if not self.data.get("priced"):
            return "未配置价目表"
        return f"${self.data['cost_usd']:.4f}"

    def summary_line(self) -> str:
        return (
            f"token {self.data['total_tokens']}"
            f"（入 {self.data['prompt_tokens']} / 出 {self.data['completion_tokens']}）"
            f"  成本 {self.cost_display()}"
            f"  模型调用 {self.data['llm_calls']} 次"
            f"  工具调用 {self.data['tool_calls']} 次"
        )


class Tracer:
    """一次运行的追踪器。

    生命周期：start_run() → 多次 event() → finish_run()。
    """

    def __init__(
        self,
        base_dir: str,
        session_key: str,
        estimator: CostEstimator | None = None,
        enabled: bool = True,
        max_preview: int = 300,
    ) -> None:
        self.base_dir = os.path.abspath(base_dir)
        self.session_key = session_key
        self.estimator = estimator or CostEstimator()
        self.enabled = enabled
        self.max_preview = max_preview

        self._run: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._last_run: dict[str, Any] | None = None

        if self.enabled:
            os.makedirs(self.base_dir, exist_ok=True)

    # ---- 文件路径 ----
    def _daily_path(self) -> str:
        return os.path.join(
            self.base_dir, datetime.now().strftime("%Y-%m-%d") + ".jsonl"
        )

    def _session_summary_path(self) -> str:
        return os.path.join(
            self.base_dir, "sessions", quote(self.session_key, safe="") + ".json"
        )

    # ---- 生命周期 ----
    def start_run(self, user_message: str) -> str:
        trace_id = uuid.uuid4().hex[:12]
        self._run = {
            "trace_id": trace_id,
            "started_at": _now_iso(),
            "session_key": self.session_key,
            "user_message": _truncate(user_message, self.max_preview),
            "_t0": time.perf_counter(),
        }
        self._events = []
        self.event("run_start", user_message=self._run["user_message"])
        return trace_id

    @property
    def trace_id(self) -> str:
        return self._run.get("trace_id", "")

    def event(self, kind: str, **fields: Any) -> None:
        """记录一个事件并立即追加落盘。"""
        if not self.enabled or not self._run:
            return

        record = {
            "trace_id": self._run["trace_id"],
            "ts": _now_iso(),
            "elapsed_ms": int(
                (time.perf_counter() - self._run["_t0"]) * 1000
            ),
            "kind": kind,
        }
        for key, value in fields.items():
            if isinstance(value, str) and len(value) > self.max_preview:
                record[key] = _truncate(value, self.max_preview)
            else:
                record[key] = value

        self._events.append(record)
        self._append_line(record)

    def finish_run(self, status: str, answer: str = "") -> dict[str, Any]:
        """结束本次运行，返回本次运行的摘要。"""
        if not self._run:
            return {}

        duration_ms = int((time.perf_counter() - self._run["_t0"]) * 1000)
        summary = {
            "trace_id": self._run["trace_id"],
            "session_key": self.session_key,
            "started_at": self._run["started_at"],
            "duration_ms": duration_ms,
            "status": status,
            "iterations": sum(
                1 for e in self._events if e["kind"] == "llm_response"
            ),
            "llm_calls": sum(1 for e in self._events if e["kind"] == "llm_response"),
            "tool_calls": sum(1 for e in self._events if e["kind"] == "tool_result"),
            "denied": sum(
                1
                for e in self._events
                if e["kind"] == "permission" and e.get("risk") == "deny"
            ),
            "pended": sum(
                e.get("count", 0)
                for e in self._events
                if e["kind"] == "pending_saved"
            ),
            "prompt_tokens": sum(
                (e.get("tokens") or {}).get("prompt_tokens", 0)
                for e in self._events
                if e["kind"] == "llm_response"
            ),
            "completion_tokens": sum(
                (e.get("tokens") or {}).get("completion_tokens", 0)
                for e in self._events
                if e["kind"] == "llm_response"
            ),
            "cost_usd": sum(
                e.get("cost_usd") or 0.0
                for e in self._events
                if e["kind"] == "llm_response"
            ),
            "answer_preview": _truncate(answer, self.max_preview),
        }
        summary["total_tokens"] = (
            summary["prompt_tokens"] + summary["completion_tokens"]
        )

        self.event("run_end", **summary)
        self._last_run = summary
        return summary

    # ---- 落盘 ----
    def _append_line(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            with open(self._daily_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"警告: 写入 trace 失败 - {e}")

    # ---- 展示 ----
    def last_run_summary(self) -> dict[str, Any] | None:
        return self._last_run

    def last_run_timeline(self) -> list[dict[str, Any]]:
        return list(self._events)

    @staticmethod
    def format_timeline(events: list[dict[str, Any]]) -> str:
        """把事件列表渲染成终端可读的时间线表格。"""
        if not events:
            return "（没有可显示的 trace 记录）"

        icons = {
            "run_start": "▶",
            "llm_request": "·",
            "llm_response": "◆",
            "tool_call": "·",
            "tool_result": "✓",
            "permission": "⚖",
            "pending_saved": "⏸",
            "approval_resolved": "✔",
            "fuse": "🚨",
            "budget_exceeded": "💸",
            "error": "✗",
            "run_end": "■",
        }

        lines = []
        for ev in events:
            kind = ev.get("kind", "?")
            icon = icons.get(kind, "·")
            elapsed = ev.get("elapsed_ms", 0)
            detail = ""

            if kind == "llm_response":
                tokens = ev.get("tokens") or {}
                cost = ev.get("cost_usd")
                cost_text = f"  ${cost:.6f}" if cost is not None else ""
                # 首字延迟才是用户真正感知的"快不快"：总耗时 7 秒但首字 0.8 秒，
                # 体验远好于总耗时 3 秒但 3 秒后才一起出现
                first = ev.get("first_delta_ms")
                first_text = f"  首字 {first}ms" if first is not None else ""
                detail = (
                    f"{ev.get('model', '?')}  {ev.get('latency_ms', 0)}ms"
                    f"  token {tokens.get('total_tokens', 0)}{cost_text}"
                    f"  tools={ev.get('n_tool_calls', 0)}{first_text}"
                )
            elif kind == "tool_call":
                detail = f"{ev.get('name', '?')}  {_truncate(ev.get('args', ''), 120)}"
            elif kind == "tool_result":
                ok = "ok" if ev.get("ok") else "FAIL"
                # 工具结果往往多行且很长，压成一行预览，否则时间线会被撑爆
                preview = (ev.get("preview") or "").replace("\n", " ⏎ ")
                preview = preview[:70] + ("…" if len(preview) > 70 else "")
                detail = (
                    f"{ev.get('name', '?')}  {ev.get('latency_ms', 0)}ms  {ok}"
                    f"  {ev.get('result_len', 0)}字符  {preview}"
                )
            elif kind == "permission":
                detail = f"{ev.get('tool', '?')} → {ev.get('risk', '?')}  {ev.get('reason', '')}"
            elif kind == "run_end":
                detail = (
                    f"status={ev.get('status')}  {ev.get('duration_ms', 0)}ms"
                    f"  模型{ev.get('llm_calls', 0)}次  工具{ev.get('tool_calls', 0)}次"
                    f"  token {ev.get('total_tokens', 0)}"
                )
            elif kind in ("run_start", "fuse", "budget_exceeded", "error"):
                detail = _truncate(
                    ev.get("user_message") or ev.get("message") or "", 120
                )
            elif kind == "pending_saved":
                detail = f"{ev.get('count', 0)} 个操作待确认"
            elif kind == "approval_resolved":
                detail = f"{ev.get('decision', '?')}  {ev.get('count', 0)} 个"
            else:
                detail = ""

            lines.append(f"  {elapsed:>6}ms {icon} {kind:<18} {detail}")

        return "\n".join(lines)
