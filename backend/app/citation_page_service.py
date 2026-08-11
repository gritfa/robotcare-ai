"""引用原件按页抽取：缓存、内存闸与错误兜底。

问题背景：`/knowledge/citations/{sha}/pages/{n}` 每次请求都用
pypdf 把整本 PDF（海尔那份 27MB）解析进内存再抽一页，既无缓存也无并发上限，
且 pypdf 抛的解析异常没人接 —— 损坏文件直接 500。

三条防线：
1. **页缓存**：同一 (sha256, page) 的结果是不变的（文档按内容寻址，sha 变了
   就是另一份文件），可以放心缓存。按条数 + 总字节双上限做 LRU。
2. **并发解析闸**：缓存只挡得住重复请求，首次解析仍要吃整份文件。若 10 个用户
   同时点不同页，峰值就是 10 份 27MB。用信号量把同时解析数压住，拿不到就 503
   让客户端重试 —— 排队比 OOM 把整个进程拖死强。
3. **异常兜底**：解析失败转 422 并带可读原因，不再是裸 500。
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock
from typing import Iterator


class CitationPageCacheFull(RuntimeError):
    """并发解析闸已满（调用方应转 503）。"""


class CitationPageCache:
    def __init__(
        self,
        max_entries: int = 64,
        max_bytes: int = 32 * 1024 * 1024,
        max_concurrent_extractions: int = 4,
        acquire_timeout_seconds: float = 5.0,
    ) -> None:
        self.max_entries = max(1, max_entries)
        self.max_bytes = max(1, max_bytes)
        self.acquire_timeout_seconds = acquire_timeout_seconds
        self._guard = Lock()
        self._values: "OrderedDict[str, bytes]" = OrderedDict()
        self._total_bytes = 0
        self._semaphore = BoundedSemaphore(max(1, max_concurrent_extractions))

    @staticmethod
    def make_key(document_sha256: str, page_number: int) -> str:
        return f"{document_sha256.lower()}:{page_number}"

    def get(self, key: str) -> bytes | None:
        with self._guard:
            payload = self._values.get(key)
            if payload is None:
                return None
            self._values.move_to_end(key)
            return payload

    def set(self, key: str, payload: bytes) -> None:
        size = len(payload)
        if size > self.max_bytes:
            # 单页就超过整缓存预算：存了会把其他所有条目挤光，不如不存
            return
        with self._guard:
            existing = self._values.pop(key, None)
            if existing is not None:
                self._total_bytes -= len(existing)
            self._values[key] = payload
            self._total_bytes += size
            while self._values and (
                len(self._values) > self.max_entries or self._total_bytes > self.max_bytes
            ):
                _, evicted = self._values.popitem(last=False)
                self._total_bytes -= len(evicted)

    def stats(self) -> dict[str, int]:
        with self._guard:
            return {"entries": len(self._values), "bytes": self._total_bytes}

    @contextmanager
    def extraction_slot(self) -> Iterator[None]:
        acquired = self._semaphore.acquire(timeout=self.acquire_timeout_seconds)
        if not acquired:
            raise CitationPageCacheFull("citation page extraction is busy")
        try:
            yield
        finally:
            self._semaphore.release()
