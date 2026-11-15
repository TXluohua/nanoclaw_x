"""
长期记忆的检索（"语义召回"）模块

原来是什么样
------------
`ContextBuilder._load_memory()` 每次把整个 `MEMORY.md` 原样塞进 system prompt。

记忆一多就出两个问题：
1. **吃满上下文** —— 几百条记忆全带进去，还没开始干活 token 就没了
2. **相关的不一定被记住，不相关的全被带进去** —— 模型在一堆无关信息里
   找线索，注意力被稀释

现在怎么做
----------
把 `MEMORY.md` 按行拆成"记忆条目"，建一个 **BM25 索引**，每轮只召回
与当前问题最相关的前 K 条注入 prompt。

★ 刻意不改 `MEMORY.md` 的格式：
  Agent 现在靠 "用 write_file 往 MEMORY.md 追加条目" 来记忆（见
  agent/context.py 的记忆管理指引）。只要格式不变，就不用迁移历史数据，
  也不用改 prompt —— 改动面越小，出问题的机会越少。
  索引是**派生数据**，随时可以从 MEMORY.md 重建。

检索方式：词汇召回 vs 语义召回
------------------------------
默认是 BM25（词汇匹配）。它没有真正的"语义"能力 —— 用户写"车"，记忆里
写"汽车"，词面不同就召不回。但它**零依赖、可离线、可测试、可解释**
（能说清楚为什么召回了这条）。

要真正语义召回，需要 embedding 模型。这里留了 `embedder` 钩子：
配置了就把 BM25 和向量分加权融合（hybrid），没配置就只用 BM25。
这样默认能跑，想要更强就填配置 —— 而不是一上来就强制依赖一个外部模型。
"""

import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

# BM25 参数（经验值，一般不用调）
BM25_K1 = 1.5
BM25_B = 0.75

# 单条记忆最长保留（字符）。超长的记忆多半是模型写歪了，截断避免污染索引
MAX_ENTRY_CHARS = 1000

# 没有 jieba 时的降级方案：中文按 2-gram 切
_CJK = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[A-Za-z0-9_]+")


class Embedder(Protocol):
    """向量化钩子。实现方需提供 async embed(texts) -> list[list[float]]。"""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class MemoryEntry:
    """一条记忆。"""

    index: int
    text: str
    tokens: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "text": self.text}


def tokenize(text: str) -> list[str]:
    """把文本切成检索用的 token。

    中文优先用 jieba（装了就用，效果好很多）；没装就退化成
    "中文连续段按 2-gram 切 + 英文/数字整体保留"。
    两种方式都小写化。
    """
    if not text:
        return []

    try:
        import jieba  # 可选依赖

        tokens = [t.strip().lower() for t in jieba.lcut(text)]
        tokens = [t for t in tokens if t and not t.isspace()]
        if tokens:
            return tokens
    except Exception:
        pass

    # 降级：中文 2-gram + 英文数字整体
    tokens = []
    for chunk in _TOKEN_RE.findall(text):
        lower = chunk.lower()
        if re.fullmatch(rf"[{_CJK}]+", chunk):
            if len(lower) == 1:
                tokens.append(lower)
            else:
                tokens.extend(lower[i : i + 2] for i in range(len(lower) - 1))
        else:
            tokens.append(lower)
    return tokens


class BM25Index:
    """一个最小实现的 BM25。

    为什么自己写而不是引 rank_bm25：只有 30 行，且能顺便把
    "为什么这条被召回"讲清楚（idf、tf、文档长度归一化）。
    """

    def __init__(self, documents: list[list[str]]) -> None:
        self.documents = documents
        self.doc_count = len(documents)
        self.doc_lengths = [len(doc) for doc in documents]
        self.avg_length = (
            sum(self.doc_lengths) / self.doc_count if self.doc_count else 0.0
        )

        # 每个词的文档频率
        self.doc_freq: Counter[str] = Counter()
        for doc in documents:
            for term in set(doc):
                self.doc_freq[term] += 1

        # 预先算 tf
        self.term_freqs = [Counter(doc) for doc in documents]

    def idf(self, term: str) -> float:
        df = self.doc_freq.get(term, 0)
        if df == 0:
            return 0.0
        # 加 1 防止负数（df 接近 doc_count 时）
        return math.log(1 + (self.doc_count - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: list[str], doc_index: int) -> float:
        if not query_tokens or self.doc_count == 0:
            return 0.0

        tf = self.term_freqs[doc_index]
        length = self.doc_lengths[doc_index] or 1
        total = 0.0

        for term in query_tokens:
            freq = tf.get(term, 0)
            if freq == 0:
                continue
            numerator = freq * (BM25_K1 + 1)
            denominator = freq + BM25_K1 * (
                1 - BM25_B + BM25_B * length / (self.avg_length or 1)
            )
            total += self.idf(term) * numerator / denominator

        return total

    def top_k(self, query_tokens: list[str], k: int) -> list[tuple[int, float]]:
        scored = [
            (index, self.score(query_tokens, index))
            for index in range(self.doc_count)
        ]
        scored = [(i, s) for i, s in scored if s > 0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]


def split_memory_entries(text: str) -> list[str]:
    """把 MEMORY.md 的正文拆成一条条记忆。

    支持的形状（真实文件里都出现过）：
        - 一条 bullet：`- 用户叫熊珂`
        - 多行 bullet：续行（缩进）归到同一条
        - 标题行：`## 偏好` 丢弃（它只是分组，不是记忆本身）
        - 空行：丢弃
    """
    entries: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        merged = " ".join(part.strip() for part in current).strip()
        if merged:
            entries.append(merged[:MAX_ENTRY_CHARS])
        current.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            flush()
            continue

        # Markdown 标题不是记忆
        if stripped.startswith("#"):
            flush()
            continue

        is_new_bullet = bool(re.match(r"^([-*+]|\d+\.)\s+", stripped))
        if is_new_bullet:
            flush()
            current.append(re.sub(r"^([-*+]|\d+\.)\s+", "", stripped))
            continue

        # 缩进续行 → 归到当前条目
        if line[:1].isspace() and current:
            current.append(stripped)
            continue

        # 普通段落行 → 自己算一条
        flush()
        current.append(stripped)

    flush()
    return entries


class MemoryRetriever:
    """长期记忆的检索器。

    特性：
    - 懒加载 + 按文件 mtime 自动重建索引（记忆被追加后立刻生效）
    - BM25 为主；注入了 embedder 时与向量分融合（hybrid）
    - 线程安全（网关里多个会话并发访问同一个 retriever）
    """

    def __init__(
        self,
        memory_path: str,
        top_k: int = 5,
        enabled: bool = True,
        embedder: Embedder | None = None,
        vector_weight: float = 0.5,
    ) -> None:
        self.memory_path = os.path.abspath(memory_path)
        self.top_k = top_k
        self.enabled = enabled
        self.embedder = embedder
        self.vector_weight = max(0.0, min(1.0, vector_weight))

        self._entries: list[MemoryEntry] = []
        self._index: BM25Index | None = None
        self._vectors: list[list[float]] | None = None
        self._loaded_mtime: float | None = None
        self._lock = threading.Lock()

    # ---- 索引 ----
    @property
    def size(self) -> int:
        self._ensure_loaded()
        return len(self._entries)

    def _file_mtime(self) -> float | None:
        try:
            return os.path.getmtime(self.memory_path)
        except OSError:
            return None

    def _ensure_loaded(self) -> None:
        mtime = self._file_mtime()
        with self._lock:
            if self._index is not None and mtime == self._loaded_mtime:
                return
            self._rebuild_locked(mtime)

    def _rebuild_locked(self, mtime: float | None) -> None:
        texts: list[str] = []
        if mtime is not None and os.path.isfile(self.memory_path):
            try:
                with open(self.memory_path, "r", encoding="utf-8") as f:
                    texts = split_memory_entries(f.read())
            except Exception as e:
                print(f"警告: 读取长期记忆失败 - {e}")
                texts = []

        self._entries = [
            MemoryEntry(index=i, text=t, tokens=tokenize(t))
            for i, t in enumerate(texts)
        ]
        self._index = BM25Index([e.tokens for e in self._entries])
        self._vectors = None  # 记忆变了，向量缓存失效
        self._loaded_mtime = mtime

    def refresh(self) -> None:
        """强制重建索引（新增记忆后想立刻生效时用）。"""
        with self._lock:
            self._rebuild_locked(self._file_mtime())

    def all_entries(self) -> list[str]:
        self._ensure_loaded()
        return [e.text for e in self._entries]

    # ---- 检索 ----
    def recall_sync(self, query: str, top_k: int | None = None) -> list[str]:
        """纯 BM25 同步召回（不依赖外部服务）。"""
        if not self.enabled:
            return []

        self._ensure_loaded()
        if self._index is None or self._index.doc_count == 0:
            return []

        limit = self.top_k if top_k is None else top_k
        if limit <= 0:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        hits = self._index.top_k(query_tokens, limit)
        return [self._entries[i].text for i, _ in hits]

    async def recall(self, query: str, top_k: int | None = None) -> list[str]:
        """召回。注入了 embedder 时走 hybrid（BM25 + 向量）。"""
        # 没有 embedder，或者 embedder 不可用 → 直接 BM25
        if self.embedder is None:
            return self.recall_sync(query, top_k)

        try:
            return await self._recall_hybrid(query, top_k)
        except Exception as e:
            # 向量侧任何问题都退回 BM25：召回质量降级，但功能不能挂
            print(f"警告: 向量召回失败（{type(e).__name__}: {e}），退回 BM25")
            return self.recall_sync(query, top_k)

    async def _recall_hybrid(self, query: str, top_k: int | None) -> list[str]:
        if not self.enabled:
            return []

        self._ensure_loaded()
        if not self._entries:
            return []

        limit = self.top_k if top_k is None else top_k
        if limit <= 0:
            return []

        query_tokens = tokenize(query)
        lexical = (
            self._index.top_k(query_tokens, len(self._entries))
            if query_tokens and self._index
            else []
        )
        lexical_scores = {i: s for i, s in lexical}
        lexical_max = max(lexical_scores.values(), default=0.0) or 1.0

        # 向量：懒计算并缓存
        if self._vectors is None:
            self._vectors = await self.embedder.embed(
                [e.text for e in self._entries]
            )
        query_vector = (await self.embedder.embed([query]))[0]

        vector_scores = {
            i: _cosine(query_vector, vec)
            for i, vec in enumerate(self._vectors)
            if vec
        }

        combined: list[tuple[int, float]] = []
        for i, entry in enumerate(self._entries):
            lex = lexical_scores.get(i, 0.0) / lexical_max
            vec = vector_scores.get(i, 0.0)
            fused = (1 - self.vector_weight) * lex + self.vector_weight * vec
            if fused > 0:
                combined.append((i, fused))

        combined.sort(key=lambda pair: pair[1], reverse=True)
        return [self._entries[i].text for i, _ in combined[:limit]]


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。任一为空返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
