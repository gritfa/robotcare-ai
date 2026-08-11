from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Protocol

from pypdf import PdfReader
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import Float, bindparam, case, delete, distinct, func, select
from sqlalchemy.orm import Session

from .db_types import EMBEDDING_DIMENSION, normalize_embedding
from .llm_transport import (
    LLMTransportError,
    TimeoutPolicy,
    call_with_budget,
    is_retryable_exception,
    is_retryable_status,
)
from .models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeGapEvent,
    RobotModel,
)
from .observability import current_trace_id, emit_json_log

EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_BATCH_SIZE = 10

# 未显式注入配置时的兜底预算（CLI、脚本等旁路入口）；请求路径走
# settings.embedding_timeout_policy。embedding 是短请求，预算远小于生成。
DEFAULT_EMBEDDING_TIMEOUT_POLICY = TimeoutPolicy(
    connect_seconds=5.0, read_seconds=20.0, budget_seconds=30.0, max_attempts=2
)
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80


class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashingNgramEmbeddingProvider:
    """确定性本地嵌入：字符 2-gram 特征哈希到 256 维并 L2 归一化。

    仅提供词面（n-gram 重合）相似度，没有语义模型；用于合成演示数据入库、
    离线评测与 CI，绝不冒充 DashScope 语义向量。落库向量与真实语义向量
    不可混用——切换 Provider 时必须整库重建（release 流程本就整体替换）。
    """

    model_name = "hashing-ngram-v1"

    @staticmethod
    def _vector(text: str) -> list[float]:
        normalized = "".join(text.split()).lower()
        dims = [0.0] * EMBEDDING_DIMENSION
        if len(normalized) < 2:
            normalized = normalized + "□□"
        for index in range(len(normalized) - 1):
            gram = normalized[index : index + 2]
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSION
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            dims[slot] += sign
        norm = math.sqrt(sum(value * value for value in dims))
        if norm == 0:
            dims[0] = 1.0
            norm = 1.0
        return [value / norm for value in dims]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class DashScopeEmbeddingProvider:
    """DashScope TextEmbedding v4 adapter with the product's fixed settings."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_policy: TimeoutPolicy | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "").strip().rstrip("/") or None
        self.timeout_policy = timeout_policy or DEFAULT_EMBEDDING_TIMEOUT_POLICY

    def _embed(self, texts: Sequence[str], text_type: str) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > EMBEDDING_BATCH_SIZE:
            raise ValueError(f"DashScope embedding batch cannot exceed {EMBEDDING_BATCH_SIZE}")
        return call_with_budget(
            lambda read_timeout: self._embed_once(texts, text_type, read_timeout),
            self.timeout_policy,
            op_name="embedding.dashscope",
        )

    def _embed_once(
        self, texts: Sequence[str], text_type: str, read_timeout: float
    ) -> list[list[float]]:
        import dashscope

        if self.base_url:
            dashscope.base_http_api_url = self.base_url

        kwargs: dict[str, object] = {
            "model": EMBEDDING_MODEL,
            "input": list(texts),
            "dimension": EMBEDDING_DIMENSION,
            "text_type": text_type,
            # 显式覆盖 SDK 的 300 秒默认超时。
            "request_timeout": (self.timeout_policy.connect_seconds, read_timeout),
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        started_at = perf_counter()
        try:
            response = dashscope.TextEmbedding.call(**kwargs)
        except Exception as exc:
            emit_json_log(
                logging.ERROR,
                "embedding_call",
                trace_id=current_trace_id(),
                model=EMBEDDING_MODEL,
                text_type=text_type,
                item_count=len(texts),
                dimension=EMBEDDING_DIMENSION,
                outcome="error",
                error_type=type(exc).__name__,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
            # 超时/连接类异常标记为可重试，交给 call_with_budget 在预算内决定
            raise LLMTransportError(
                "DashScope embedding request failed",
                retryable=is_retryable_exception(exc),
            ) from exc
        if getattr(response, "status_code", None) != 200:
            emit_json_log(
                logging.ERROR,
                "embedding_call",
                trace_id=current_trace_id(),
                model=EMBEDDING_MODEL,
                text_type=text_type,
                item_count=len(texts),
                dimension=EMBEDDING_DIMENSION,
                outcome="error",
                provider_status=getattr(response, "status_code", None),
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
            status_code = getattr(response, "status_code", None)
            message = getattr(response, "message", "DashScope embedding request failed")
            raise LLMTransportError(
                str(message),
                retryable=isinstance(status_code, int) and is_retryable_status(status_code),
                status_code=status_code if isinstance(status_code, int) else None,
            )

        output = getattr(response, "output", None)
        if output is None and isinstance(response, dict):
            output = response.get("output")
        embeddings = output.get("embeddings", []) if isinstance(output, dict) else []
        ordered = sorted(embeddings, key=lambda item: item.get("text_index", 0))
        vectors = [list(map(float, item["embedding"])) for item in ordered]
        if len(vectors) != len(texts):
            emit_json_log(
                logging.ERROR,
                "embedding_call",
                trace_id=current_trace_id(),
                model=EMBEDDING_MODEL,
                text_type=text_type,
                item_count=len(texts),
                returned_count=len(vectors),
                dimension=EMBEDDING_DIMENSION,
                outcome="error",
                error_type="UnexpectedEmbeddingCount",
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
            raise RuntimeError("DashScope returned an unexpected embedding count")
        emit_json_log(
            logging.INFO,
            "embedding_call",
            trace_id=current_trace_id(),
            model=EMBEDDING_MODEL,
            text_type=text_type,
            item_count=len(texts),
            dimension=EMBEDDING_DIMENSION,
            outcome="success",
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
        )
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, text_type="document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], text_type="query")[0]


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str


@dataclass(frozen=True)
class ChunkText:
    chunk_index: int
    page_number: int
    content: str


@dataclass(frozen=True)
class IngestResult:
    document_id: int
    created: bool
    changed: bool
    chunk_count: int
    sha256: str


@dataclass(frozen=True)
class SearchResult:
    score: float
    content: str
    document_title: str
    document_sha256: str
    source_url: str
    page_number: int


@dataclass(frozen=True)
class KnowledgeStatus:
    robot_model_id: int
    model_code: str
    document_count: int
    chunk_count: int
    vector_count: int


@dataclass(frozen=True)
class KnowledgeModelHealth:
    robot_model_id: int
    model_code: str
    document_count: int
    chunk_count: int
    vector_count: int
    document_sha256s: list[str]
    ready: bool


def extract_pdf_pages(pdf_path: str | Path) -> tuple[list[PageText], str | None]:
    reader = PdfReader(str(pdf_path))
    metadata_title = reader.metadata.title if reader.metadata else None
    pages = [
        PageText(page_number=index, text=(page.extract_text() or "").strip())
        for index, page in enumerate(reader.pages, start=1)
    ]
    return pages, metadata_title


def split_pages(
    pages: Sequence[PageText], chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> list[ChunkText]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be between zero and chunk_size")

    chunks: list[ChunkText] = []
    step = chunk_size - overlap
    for page in pages:
        text = page.text.strip()
        if not text:
            continue
        for start in range(0, len(text), step):
            content = text[start : start + chunk_size].strip()
            if not content:
                continue
            chunks.append(
                ChunkText(
                    chunk_index=len(chunks),
                    page_number=page.page_number,
                    content=content,
                )
            )
            if start + chunk_size >= len(text):
                break
    return chunks


def _embed_in_batches(provider: EmbeddingProvider, texts: Sequence[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[start : start + EMBEDDING_BATCH_SIZE]
        batch_vectors = provider.embed_documents(batch)
        if len(batch_vectors) != len(batch):
            raise RuntimeError("Embedding provider returned an unexpected vector count")
        vectors.extend(batch_vectors)
    return vectors


def _append_document_version(
    db: Session,
    *,
    document: KnowledgeDocument,
    chunk_count: int,
    change_kind: str,
    note: str | None,
    actor_user_id: int | None,
) -> KnowledgeDocumentVersion:
    """追加一行版本历史，并保证版本号严格递增。

    历史文档没有任何版本行、补录场景又可能与现有行撞号，所以版本号以
    "已有最大版本 + 1" 为准，而不是盲信 document.version。
    """

    max_version = (
        db.scalar(
            select(func.max(KnowledgeDocumentVersion.version)).where(
                KnowledgeDocumentVersion.document_id == document.id
            )
        )
        or 0
    )
    if document.version is None or document.version <= max_version:
        document.version = max_version + 1
    entry = KnowledgeDocumentVersion(
        document_id=document.id,
        version=document.version,
        sha256=document.sha256,
        title=document.title,
        source_url=document.source_url,
        page_count=document.page_count,
        chunk_count=chunk_count,
        stored_filename=document.stored_filename,
        file_size=document.file_size,
        embedding_model=document.embedding_model or EMBEDDING_MODEL,
        change_kind=change_kind,
        note=note,
        created_by=actor_user_id,
    )
    db.add(entry)
    return entry


def ingest_pdf(
    db: Session,
    *,
    robot_model_id: int,
    pdf_path: str | Path,
    source_url: str,
    provider: EmbeddingProvider,
    title: str | None = None,
    commit: bool = True,
    stored_filename: str | None = None,
    file_size: int | None = None,
    actor_user_id: int | None = None,
    change_kind: str = "upload",
    note: str | None = None,
    force: bool = False,
) -> IngestResult:
    path = Path(pdf_path)
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.robot_model_id == robot_model_id,
            KnowledgeDocument.source_url == source_url,
        )
    )
    # force=True 用于重新向量化：内容没变但要重算向量（换 embedding 模型、
    # 修复损坏向量），此时不能走"sha 相同即跳过"的快捷路径。
    if existing is not None and existing.sha256 == file_hash and not force:
        count = db.scalar(
            select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.document_id == existing.id)
        )
        # 内容没变但此前没留存档（存档层上线之前入库的文档）：补录原件，
        # 这样它之后也能重建/回滚/下载，而不用等下一次内容变更。
        if stored_filename and not existing.stored_filename:
            existing.stored_filename = stored_filename
            existing.file_size = file_size
            _append_document_version(
                db,
                document=existing,
                chunk_count=count or 0,
                change_kind="upload",
                note="补录原始文件存档（内容未变）",
                actor_user_id=actor_user_id,
            )
            if commit:
                db.commit()
            else:
                db.flush()
        return IngestResult(existing.id, created=False, changed=False, chunk_count=count or 0, sha256=file_hash)

    if db.scalar(select(RobotModel.id).where(RobotModel.id == robot_model_id)) is None:
        raise ValueError(f"Robot model {robot_model_id} does not exist")

    pages, metadata_title = extract_pdf_pages(path)
    chunks = split_pages(pages)
    if not chunks:
        raise ValueError("PDF contains no extractable text")
    vectors = _embed_in_batches(provider, [chunk.content for chunk in chunks])

    created = existing is None
    try:
        if existing is None:
            document = KnowledgeDocument(
                robot_model_id=robot_model_id,
                title=title or metadata_title or path.stem,
                source_url=source_url,
                sha256=file_hash,
                page_count=len(pages),
                status="active",
                version=1,
                stored_filename=stored_filename,
                file_size=file_size,
                embedding_model=EMBEDDING_MODEL,
                uploaded_by=actor_user_id,
            )
            db.add(document)
            db.flush()
        else:
            document = existing
            db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
            document.title = title or metadata_title or path.stem
            document.sha256 = file_hash
            document.page_count = len(pages)
            document.version = (document.version or 1) + 1
            document.embedding_model = EMBEDDING_MODEL
            if stored_filename:
                document.stored_filename = stored_filename
                document.file_size = file_size

        db.add_all(
            [
                KnowledgeChunk(
                    document_id=document.id,
                    chunk_index=chunk.chunk_index,
                    page_number=chunk.page_number,
                    content=chunk.content,
                    embedding=normalize_embedding(vector),
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
        )
        _append_document_version(
            db,
            document=document,
            chunk_count=len(chunks),
            change_kind=change_kind,
            note=note,
            actor_user_id=actor_user_id,
        )
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(document)
    except Exception:
        db.rollback()
        raise

    return IngestResult(
        document_id=document.id,
        created=created,
        changed=not created,
        chunk_count=len(chunks),
        sha256=file_hash,
    )


def release_knowledge_package(
    db: Session,
    *,
    manifest_path: str | Path,
    provider: EmbeddingProvider,
) -> dict[str, object]:
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("release_version"), str)
        or manifest.get("embedding_model") != EMBEDDING_MODEL
        or manifest.get("embedding_dimension") != EMBEDDING_DIMENSION
        or not isinstance(manifest.get("created_at"), str)
        or not isinstance(manifest.get("documents"), list)
        or not manifest["documents"]
    ):
        raise ValueError("Invalid knowledge release manifest")

    results: list[dict[str, object]] = []
    try:
        for entry in manifest["documents"]:
            if not isinstance(entry, dict):
                raise ValueError("Knowledge release document entry must be an object")
            model_code = entry.get("model_code")
            relative_pdf = entry.get("pdf")
            expected_sha256 = entry.get("sha256")
            source_url = entry.get("source_url")
            if not all(
                isinstance(value, str) and value
                for value in (model_code, relative_pdf, expected_sha256, source_url)
            ):
                raise ValueError("Knowledge release document metadata is incomplete")
            pdf_path = (manifest_file.parent / relative_pdf).resolve()
            if not pdf_path.is_file():
                raise ValueError(f"Knowledge release PDF is missing: {relative_pdf}")
            actual_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            if not hmac.compare_digest(actual_sha256, expected_sha256.lower()):
                raise ValueError(f"Knowledge release SHA256 mismatch: {model_code}")
            robot_model = db.scalar(select(RobotModel).where(RobotModel.code == model_code))
            if robot_model is None:
                raise ValueError(f"Unknown robot model: {model_code}")
            result = ingest_pdf(
                db,
                robot_model_id=robot_model.id,
                pdf_path=pdf_path,
                source_url=source_url,
                title=entry.get("title") if isinstance(entry.get("title"), str) else None,
                provider=provider,
                commit=False,
            )
            results.append(
                {
                    "model_code": model_code,
                    "document_id": result.document_id,
                    "sha256": result.sha256,
                    "chunk_count": result.chunk_count,
                    "created": result.created,
                    "changed": result.changed,
                }
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "release_version": manifest["release_version"],
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimension": EMBEDDING_DIMENSION,
        "created_at": manifest["created_at"],
        "documents": results,
    }


def record_knowledge_gap_event(
    db: Session,
    *,
    robot_model_id: int,
    query_normalized: str,
    source: str,
    refusal_reason: str | None = None,
    trace_id: str | None = None,
) -> None:
    """内容缺口埋点：写入失败只记日志，绝不影响检索/回答主流程。

    该函数位于服务层，以便知识问答和聊天入口共用同一套缺口记录逻辑。
    下沉到这里是为了让生成层统一埋点，聊天、知识问答、诊断走的是同一条路。

    调用时机要求：此刻 Session 里不能有待提交的业务数据——本函数会 commit，
    否则会把调用方还没准备好的写入一并提交。生成层在 refuse() 开头调用，
    那时用户消息尚未落库（见 conversations._prepare_turn 的延迟落库）。
    """

    try:
        db.add(
            KnowledgeGapEvent(
                robot_model_id=robot_model_id,
                query_normalized=query_normalized,
                source=source,
                refusal_reason=refusal_reason,
            )
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 埋点失败不允许打断主流程
        db.rollback()
        emit_json_log(
            logging.ERROR,
            "knowledge_gap_event_write_failed",
            trace_id=trace_id,
            robot_model_id=robot_model_id,
            source=source,
            error_type=type(exc).__name__,
        )


def search_knowledge(
    db: Session,
    *,
    robot_model_id: int,
    query: str,
    top_k: int,
    min_score: float = 0.25,
    provider: EmbeddingProvider,
) -> list[SearchResult]:
    query_vector = normalize_embedding(provider.embed_query(query))
    if query_vector is None:
        return []
    return _search_knowledge_postgresql(
        db,
        robot_model_id=robot_model_id,
        query_vector=query_vector,
        top_k=top_k,
        min_score=min_score,
    )


def _search_knowledge_postgresql(
    db: Session,
    *,
    robot_model_id: int,
    query_vector: list[float],
    top_k: int,
    min_score: float,
) -> list[SearchResult]:
    statement = postgres_search_statement(
        robot_model_id=robot_model_id,
        query_vector=query_vector,
        top_k=top_k,
        min_score=min_score,
    )
    rows = db.execute(statement).all()
    return [
        SearchResult(
            score=max(-1.0, min(1.0, 1.0 - float(distance_value))),
            content=chunk.content,
            document_title=document.title,
            document_sha256=document.sha256,
            source_url=document.source_url,
            page_number=chunk.page_number,
        )
        for chunk, document, distance_value in rows
    ]


def postgres_search_statement(
    *, robot_model_id: int, query_vector: list[float], top_k: int, min_score: float
):
    query_parameter = bindparam(
        "query_embedding",
        value=query_vector,
        type_=VECTOR(EMBEDDING_DIMENSION),
    )
    # Keep the indexed column bare on the left side of <=>. Casting the column
    # itself would turn this into an expression and can prevent PostgreSQL from
    # using the HNSW index created by the migration.
    distance = KnowledgeChunk.embedding.op("<=>", return_type=Float)(query_parameter)
    return (
        select(KnowledgeChunk, KnowledgeDocument, distance.label("distance"))
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.robot_model_id == robot_model_id,
            # 停用的文档不参与检索——这是"停用"的唯一生效点，
            # 漏了这行整个开关就是摆设。
            KnowledgeDocument.status == "active",
            KnowledgeChunk.embedding.is_not(None),
            distance <= 1.0 - min_score,
        )
        .order_by(distance)
        .limit(top_k)
    )


def get_knowledge_status(db: Session) -> list[KnowledgeStatus]:
    rows = db.execute(
        select(
            RobotModel.id,
            RobotModel.code,
            func.count(distinct(KnowledgeDocument.id)),
            func.count(KnowledgeChunk.id),
            func.count(case((KnowledgeChunk.embedding.is_not(None), 1))),
        )
        .outerjoin(KnowledgeDocument, KnowledgeDocument.robot_model_id == RobotModel.id)
        .outerjoin(KnowledgeChunk, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .group_by(RobotModel.id, RobotModel.code)
        .order_by(RobotModel.code)
    ).all()
    return [
        KnowledgeStatus(
            robot_model_id=model_id,
            model_code=model_code,
            document_count=document_count,
            chunk_count=chunk_count,
            vector_count=vector_count,
        )
        for model_id, model_code, document_count, chunk_count, vector_count in rows
    ]


def get_knowledge_health(
    db: Session, required_model_codes: Sequence[str] = ("JH69U1", "VC35U1")
) -> list[KnowledgeModelHealth]:
    required = set(required_model_codes)
    statuses = get_knowledge_status(db)
    health: list[KnowledgeModelHealth] = []
    for item in statuses:
        if item.model_code not in required:
            continue
        # 健康度回答的是"这个型号现在能不能答"，所以只看启用中的文档：
        # 全部停用等于答不了，不能因为库里还躺着记录就报 ready。
        sha256s = list(
            db.scalars(
                select(KnowledgeDocument.sha256)
                .where(
                    KnowledgeDocument.robot_model_id == item.robot_model_id,
                    KnowledgeDocument.status == "active",
                )
                .order_by(KnowledgeDocument.id)
            )
        )
        ready = (
            item.document_count >= 1
            and item.chunk_count >= 1
            and item.vector_count == item.chunk_count
            and len(sha256s) == item.document_count
        )
        health.append(
            KnowledgeModelHealth(
                robot_model_id=item.robot_model_id,
                model_code=item.model_code,
                document_count=item.document_count,
                chunk_count=item.chunk_count,
                vector_count=item.vector_count,
                document_sha256s=sha256s,
                ready=ready,
            )
        )
    present = {item.model_code for item in health}
    for missing_code in sorted(required - present):
        health.append(
            KnowledgeModelHealth(
                robot_model_id=0,
                model_code=missing_code,
                document_count=0,
                chunk_count=0,
                vector_count=0,
                document_sha256s=[],
                ready=False,
            )
        )
    return sorted(health, key=lambda item: item.model_code)
