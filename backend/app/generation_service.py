"""阶段1：LLM 生成层——检索增强回答，强制引用、三类拒答、全程留痕。

硬规则（fail-closed，任何一条不满足都拒答而不是硬答）：
1. 安全规则命中 → 由调用方（API 层）在检索前拦截，本模块不重复实现；
2. 检索无命中或全部低于阈值 → refuse(knowledge_gap)，不调用模型；
3. 模型输出必须逐条引用检索片段（[n] 语法），引用缺失/越界 → refuse(citation_invalid)；
4. 模型明确输出 REFUSE 标记 → refuse(model_refused)；
5. 回答包含危险操作指导（如教用户拆机）→ refuse(unsafe_answer)；
   使用输出侧专用 detect_unsafe_generated_answer——安全警告（"若冒烟请停用"）
   不算危险内容，输入侧规则不得原样套在输出上（SYN-FA-006 误伤教训）。
每次调用（含拒答）都持久化 GenerationRecord：prompt 版本、模型名、
片段 SHA、回答全文、引用清单、拒答原因、耗时——可追溯可复算。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Protocol

from sqlalchemy.orm import Session

from .knowledge_service import EmbeddingProvider, SearchResult, search_knowledge
from .models import GenerationRecord
from .observability import current_trace_id, emit_json_log
from .safety import detect_unsafe_generated_answer

PROMPT_VERSION = "answer-v3"
REFUSE_TOKEN = "REFUSE"
MAX_SNIPPETS = 5

# v2（2026-08-04）：LLM 裁判评测实锤 11/34 条语义越界（docs/evidence/llm_judge_faithfulness.json），
# 两大模式针对性加约束：补片段没有的因果/机制解释；把其他故障条目的步骤挪用到当前问题
# v3（2026-08-04）：v2 复测剩 7 条不忠实中 2 条系"其余资料未提及"话术被滥用（片段其实有），
# 2 条系步骤后自加"以确保/以避免"式目的说明——改为禁断言资料未提及、禁自加目的状语
SYSTEM_PROMPT = (
    "你是扫地机器人售后知识助手。只能依据提供的编号资料片段回答，"
    "禁止使用任何片段之外的知识。每个论断句末必须标注来源编号，如 [1] 或 [1][3]。"
    "只复述片段明确写出的事实和步骤：不要自行补充片段没有写的原因、机制或后果解释，"
    "也不要在步骤前后添加片段没有的目的说明（如'以确保…''以避免…''从而…'）；"
    "片段中针对其他故障或其他场景的步骤，不要挪用到当前问题；"
    "片段只覆盖问题的一部分时，只回答覆盖到的部分，需要更多信息时建议联系官方售后，"
    "不要断言'资料未提及'某内容。"
    "如果片段完全不足以回答问题，只输出 REFUSE。"
    "绝不建议用户拆机、维修内部部件、短接触点或绕过安全保护；"
    "涉及冒烟、电池损坏、进水等危险情况一律建议联系官方售后。"
)

_CITATION_PATTERN = re.compile(r"\[(\d{1,2})\]")


class GenerationProvider(Protocol):
    model_name: str

    def generate(self, *, system: str, prompt: str) -> str: ...


class DashScopeGenerationProvider:
    """DashScope（通义千问）文本生成适配器。"""

    def __init__(self, api_key: str | None, model: str = "qwen-plus", base_url: str | None = None) -> None:
        self.api_key = api_key
        self.model_name = model
        self.base_url = (base_url or "").strip().rstrip("/") or None

    def generate(self, *, system: str, prompt: str) -> str:
        import dashscope

        if self.base_url:
            dashscope.base_http_api_url = self.base_url
        kwargs: dict[str, object] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "result_format": "message",
            "temperature": 0.1,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = dashscope.Generation.call(**kwargs)
        if getattr(response, "status_code", None) != 200:
            message = getattr(response, "message", "DashScope generation request failed")
            raise RuntimeError(str(message))
        output = getattr(response, "output", None)
        if output is None and isinstance(response, dict):
            output = response.get("output")
        try:
            return output["choices"][0]["message"]["content"]
        except (TypeError, KeyError, IndexError) as exc:
            raise RuntimeError("DashScope returned an unexpected generation payload") from exc


class OpenAICompatGenerationProvider:
    """OpenAI 兼容 /chat/completions 适配器（DeepSeek 等）。

    推理型模型的思维链在 reasoning_content 里，最终回答只取 content；
    content 为空（如 max_tokens 被推理耗尽）按失败抛错，绝不拿空串当回答。
    """

    def __init__(
        self, api_key: str | None, model: str, base_url: str, max_tokens: int = 4096
    ) -> None:
        self.api_key = api_key
        self.model_name = model
        self.base_url = base_url.strip().rstrip("/")
        # 推理型模型思维链计入 max_tokens，4096 会被长任务耗尽致 content 为空
        # （2026-08-04 LLM 裁判评测 8/34 条 finish_reason=length 实锤），按调用方需要放大
        self.max_tokens = max_tokens

    def generate(self, *, system: str, prompt: str) -> str:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": self.max_tokens,
            },
            timeout=180.0,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"OpenAI-compat generation failed: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
        payload = response.json()
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (TypeError, KeyError, IndexError) as exc:
            raise RuntimeError("OpenAI-compat endpoint returned an unexpected payload") from exc
        if not (content or "").strip():
            raise RuntimeError(
                "OpenAI-compat endpoint returned empty content "
                f"(finish_reason={choice.get('finish_reason')})"
            )
        return content


def build_generation_provider(settings) -> "GenerationProvider":
    """按 ROBOTCARE_LLM_BACKEND 装配生成后端。

    config 校验保证 openai-compat 必有 base_url；此前 main.py 写死 DashScope，
    llm_backend 配置形同虚设（2026-08-04 老板发现的接入缺口）。
    """
    if settings.llm_backend == "openai-compat":
        return OpenAICompatGenerationProvider(
            settings.llm_api_key, settings.generation_model, settings.llm_base_url
        )
    return DashScopeGenerationProvider(
        settings.dashscope_api_key, settings.generation_model, settings.dashscope_base_url
    )


def generation_backend_configured(settings) -> bool:
    if settings.llm_backend == "openai-compat":
        return bool((settings.llm_api_key or "").strip())
    return bool((settings.dashscope_api_key or "").strip())


@dataclass(frozen=True)
class AnswerCitation:
    index: int
    source_url: str
    page_number: int
    score: float
    document_sha256: str


@dataclass(frozen=True)
class AnswerResult:
    status: str  # "answered" | "refused"
    answer: str | None
    citations: list[AnswerCitation] = field(default_factory=list)
    refusal_reason: str | None = None
    record_id: int | None = None


def _snippet_block(results: list[SearchResult]) -> str:
    parts = []
    for index, item in enumerate(results, start=1):
        parts.append(
            f"[{index}] （说明书第 {item.page_number} 页）{item.content}"
        )
    return "\n\n".join(parts)


def _content_sha(results: list[SearchResult]) -> str:
    digest = hashlib.sha256()
    for item in results:
        digest.update(item.content.encode("utf-8"))
    return digest.hexdigest()


def _persist(
    db: Session,
    *,
    user_id: int | None,
    robot_model_id: int,
    query: str,
    provider_model: str,
    status: str,
    answer: str | None,
    citations: list[AnswerCitation],
    refusal_reason: str | None,
    snippets_sha256: str,
    snippet_count: int,
    latency_ms: float,
    commit: bool,
) -> GenerationRecord:
    record = GenerationRecord(
        user_id=user_id,
        robot_model_id=robot_model_id,
        query=query,
        prompt_version=PROMPT_VERSION,
        provider_model=provider_model,
        status=status,
        answer=answer,
        citations_json=[
            {
                "index": item.index,
                "source_url": item.source_url,
                "page_number": item.page_number,
                "score": round(item.score, 6),
                "document_sha256": item.document_sha256,
            }
            for item in citations
        ],
        refusal_reason=refusal_reason,
        snippets_sha256=snippets_sha256,
        snippet_count=snippet_count,
        latency_ms=round(latency_ms, 3),
    )
    db.add(record)
    if commit:
        db.commit()
    else:
        db.flush()
    emit_json_log(
        logging.INFO,
        "generation_answer",
        trace_id=current_trace_id(),
        robot_model_id=robot_model_id,
        status=status,
        refusal_reason=refusal_reason,
        provider_model=provider_model,
        prompt_version=PROMPT_VERSION,
        snippet_count=snippet_count,
        citation_count=len(citations),
        latency_ms=round(latency_ms, 3),
    )
    return record


def generate_answer(
    db: Session,
    *,
    user_id: int | None,
    robot_model_id: int,
    query: str,
    embedding_provider: EmbeddingProvider,
    generation_provider: GenerationProvider,
    top_k: int = MAX_SNIPPETS,
    min_score: float = 0.25,
    commit: bool = True,
) -> AnswerResult:
    started_at = perf_counter()
    top_k = max(1, min(top_k, MAX_SNIPPETS))
    results = search_knowledge(
        db,
        robot_model_id=robot_model_id,
        query=query,
        top_k=top_k,
        min_score=min_score,
        provider=embedding_provider,
    )

    def refuse(reason: str, *, answer: str | None = None, snippet_count: int = len(results)) -> AnswerResult:
        record = _persist(
            db,
            user_id=user_id,
            robot_model_id=robot_model_id,
            query=query,
            provider_model=generation_provider.model_name,
            status="refused",
            answer=answer,
            citations=[],
            refusal_reason=reason,
            snippets_sha256=_content_sha(results),
            snippet_count=snippet_count,
            latency_ms=(perf_counter() - started_at) * 1000,
            commit=commit,
        )
        return AnswerResult(
            status="refused", answer=None, refusal_reason=reason, record_id=record.id
        )

    if not results:
        return refuse("knowledge_gap", snippet_count=0)

    prompt = (
        f"用户型号问题：{query}\n\n可用资料片段：\n{_snippet_block(results)}\n\n"
        "请依据上述片段回答；片段不足以回答时只输出 REFUSE。"
    )
    raw_answer = generation_provider.generate(system=SYSTEM_PROMPT, prompt=prompt).strip()

    if not raw_answer or REFUSE_TOKEN in raw_answer.split("\n", 1)[0][:20]:
        return refuse("model_refused")

    cited_indexes = sorted({int(match) for match in _CITATION_PATTERN.findall(raw_answer)})
    valid_indexes = set(range(1, len(results) + 1))
    if not cited_indexes or not set(cited_indexes).issubset(valid_indexes):
        return refuse("citation_invalid", answer=raw_answer)

    if detect_unsafe_generated_answer(raw_answer) is not None:
        return refuse("unsafe_answer", answer=raw_answer)

    citations = [
        AnswerCitation(
            index=index,
            source_url=results[index - 1].source_url,
            page_number=results[index - 1].page_number,
            score=results[index - 1].score,
            document_sha256=results[index - 1].document_sha256,
        )
        for index in cited_indexes
    ]
    record = _persist(
        db,
        user_id=user_id,
        robot_model_id=robot_model_id,
        query=query,
        provider_model=generation_provider.model_name,
        status="answered",
        answer=raw_answer,
        citations=citations,
        refusal_reason=None,
        snippets_sha256=_content_sha(results),
        snippet_count=len(results),
        latency_ms=(perf_counter() - started_at) * 1000,
        commit=commit,
    )
    return AnswerResult(
        status="answered", answer=raw_answer, citations=citations, record_id=record.id
    )
