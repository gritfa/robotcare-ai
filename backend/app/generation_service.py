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
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from time import perf_counter
from typing import Protocol

from sqlalchemy.orm import Session

from .knowledge_service import EmbeddingProvider, SearchResult, search_knowledge
from .llm_transport import (
    LLMTransportError,
    TimeoutPolicy,
    call_with_budget,
    is_retryable_status,
)
from .models import GenerationRecord
from .observability import current_trace_id, emit_json_log
from .safety import detect_unsafe_generated_answer

PROMPT_VERSION = "answer-v5"
REFUSE_TOKEN = "REFUSE"
MAX_SNIPPETS = 5

# 未显式注入配置时的兜底预算（脚本、评测等旁路入口）。默认值与 Settings 一致，
# 且必须小于反代 proxy_read_timeout；正式请求路径走 settings.llm_timeout_policy。
DEFAULT_GENERATION_TIMEOUT_POLICY = TimeoutPolicy(
    connect_seconds=5.0, read_seconds=40.0, budget_seconds=50.0, max_attempts=2
)

# v2（2026-08-04）：LLM 裁判评测实锤 11/34 条语义越界（docs/evidence/llm_judge_faithfulness.json），
# 两大模式针对性加约束：补片段没有的因果/机制解释；把其他故障条目的步骤挪用到当前问题
# v3（2026-08-04）：v2 复测剩 7 条不忠实中 2 条系"其余资料未提及"话术被滥用（片段其实有），
# 2 条系步骤后自加"以确保/以避免"式目的说明——改为禁断言资料未提及、禁自加目的状语
# v4（2026-08-05）：v3 复测剩 4 条不忠实（8 条 unsupported 论断）归到四种模式：
#   ① 自造后果："需确保前方 1.5 米空间"→"空间不足会影响回充"（片段只写要求，没写后果）
#   ② 自造分类与推广：把回充灯说成"光学感应组件"、把针对单个部件的禁忌概括成"回充相关部件均禁止接触液体"
#   ③ 强度升级：片段"用柔软干布"→回答"只能用干燥软布"
#   ④ 把用户问法与片段内容强行等同/建立因果："最后一次回充确认"＝短按回充键、
#      "无法清洗拖布"→"需检查清水箱过滤管"（片段只写日常清理，没写这条故障的因果）
# 四条对应约束逐条写入，其余 v2/v3 约束保持不变。
# v5（2026-08-05）：v4 复评 0.8529（29/34，v3 为 0.8824/30），FF-004 修好但新出 FF-005/FF-010。
# 剩余 5 条归到同一根因：**问题本身预设了片段没有的概念/关系**（"基站摆放对回充的影响"、
# "最后一次回充确认"、"第一步"、"回充部件"），模型顺着提问措辞造承接句和顺序判断，
# 裁判按事实论断核对必然判 unsupported。v5 只加两条：句子必须是片段内容的转述（禁概括/
# 承接/排序句），以及不得沿用提问里片段没有的说法。
SYSTEM_PROMPT = (
    "你是扫地机器人售后知识助手。只能依据提供的编号资料片段回答，"
    "禁止使用任何片段之外的知识。每个论断句末必须标注来源编号，如 [1] 或 [1][3]。"
    "只复述片段明确写出的事实和步骤：不要自行补充片段没有写的原因、机制或后果解释，"
    "也不要在步骤前后添加片段没有的目的说明（如'以确保…''以避免…''从而…'）；"
    "片段只写了要求或做法而没有写后果时，不要补写后果或风险"
    "（不要出现'否则…''不足会导致…''可能影响…'这类片段里没有的推断）；"
    "不要给部件发明分类或上位概念（如把某个部件称为'光学感应组件''回充相关部件'），"
    "针对某一个部件的规定不要推广成一类部件的通则；"
    "不要提高片段的语气强度：片段写'建议''可用'就不要写成'必须''只能''唯一''一律'；"
    "用户的说法与片段用词不一致时，不要断言两者是同一件事，也不要替片段建立它没写的因果关系，"
    "只陈述片段确实写了什么，并说明这是资料中与该问题相关的内容；"
    "每句话都必须是片段内容的直接转述：不要写概括句、承接句或顺序判断"
    "（如'…的影响体现在…''上述操作是第一步''首先／其次'），片段本身没有写顺序就不要排序；"
    "用户问题里出现片段没有的名词或说法时，回答中不要沿用该说法、也不要断言它成立，"
    "只用片段自己的用词陈述片段写了什么；"
    "片段中针对其他故障或其他场景的步骤，不要挪用到当前问题；"
    "片段只覆盖问题的一部分时，只回答覆盖到的部分，需要更多信息时建议联系官方售后，"
    "不要断言'资料未提及'某内容。"
    "如果片段完全不足以回答问题，只输出 REFUSE，不要在正常回答的末尾附加 REFUSE。"
    "绝不建议用户拆机、维修内部部件、短接触点或绕过安全保护；"
    "涉及冒烟、电池损坏、进水等危险情况一律建议联系官方售后。"
)

_CITATION_PATTERN = re.compile(r"\[(\d{1,2})\]")
# 模型偶尔在完整回答末尾附加 REFUSE 控制标记（v3 在线评测 FF-001 实锤：
# 回答正文完整，结尾多一个 " REFUSE" 被原样发给用户）。标记属于内部协议，
# 只在开头出现时才代表整体拒答；结尾残留必须剥掉，不能泄露到用户可见文本。
#
# 2026-08-05 生产栈实测：模型不止输出光秃秃的 REFUSE，还会附带说明，例如
# "REFUSE（注：资料未提供滤网单价）"。旧模式只匹配 REFUSE + 标点，这类变体
# 整条泄露给用户。REFUSE 是控制标记，正文里不会有以它开头的句子，
# 因此末尾"以 REFUSE 开头的最后一段"整体剥掉。
_TRAILING_REFUSE_PATTERN = re.compile(
    rf"\s*{REFUSE_TOKEN}\b[^\n]*(?:\n(?!\s*\n)[^\n]*)*\s*$"
)
_PUNCTUATION_ONLY = " \t\n。．.，,；;：:、'\"“”"


class GenerationProvider(Protocol):
    model_name: str

    def generate(self, *, system: str, prompt: str) -> str: ...


class StreamingGenerationProvider(GenerationProvider, Protocol):
    """支持增量输出的生成后端。

    不是所有后端都支持（测试桩、离线评测就不支持），所以调用方必须先用
    supports_streaming() 判断，取不到流式能力时回退整段生成——绝不能因为
    "想要流式"就让不支持的后端走上一条没测过的路径。
    """

    def generate_stream(self, *, system: str, prompt: str) -> Iterator[str]: ...


def supports_streaming(provider: GenerationProvider) -> bool:
    return callable(getattr(provider, "generate_stream", None))


# 句末边界：只有成句才过安全闸门。逐 token 检测没有意义——
# "拆开外壳"这四个字在到齐之前，任何检测都判不出危险。
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\n]")


class StreamSafetyGate:
    """流式下发的安全闸门。

    产品约束（本模块开头第 3~5 条）要求回答通过引用校验、REFUSE 标记检测和
    输出侧危险操作检测之后才能给用户看。逐 token 直发会打开一个"未校验内容
    已经在用户屏幕上"的窗口，对一个售后安全助手来说不可接受。

    折中口径：
    - 按句放行。累积到句末标点才检查、才下发。
    - **末尾残句永远不放行**，留给全文校验——模型把 REFUSE 标记附在结尾
      （2026-08-05 生产实测过）时，它绝不会被提前送出去。
    - 任一句触发怀疑就**关闸**：后续内容一律不再流式下发，改由全文判定决定
      是补齐还是撤回。关闸只是退化成非流式，不等于判定拒答——避免逐句检测
      的误伤（安全警告类句子单独看容易被误判）直接变成用户可见的拒答。
    """

    def __init__(self, snippet_count: int) -> None:
        self.snippet_count = snippet_count
        self.buffer = ""
        self.released = ""
        self.closed = False

    def _suspicious(self, text: str) -> bool:
        if REFUSE_TOKEN in text:
            return True
        indexes = {int(match) for match in _CITATION_PATTERN.findall(text)}
        if any(index < 1 or index > self.snippet_count for index in indexes):
            return True
        return detect_unsafe_generated_answer(text) is not None

    def feed(self, chunk: str) -> str:
        """吃进一段增量，返回本次可以安全下发的文本（可能为空）。"""
        if self.closed:
            return ""
        self.buffer += chunk
        boundaries = list(_SENTENCE_BOUNDARY.finditer(self.buffer))
        if not boundaries:
            return ""
        cut = boundaries[-1].end()
        candidate = self.buffer[:cut]
        if not self.released and REFUSE_TOKEN in candidate.split("\n", 1)[0][:20]:
            # 整体拒答的标记出现在开头，一个字都不该下发
            self.closed = True
            return ""
        if self._suspicious(candidate):
            self.closed = True
            return ""
        self.buffer = self.buffer[cut:]
        self.released += candidate
        return candidate


class DashScopeGenerationProvider:
    """DashScope（通义千问）文本生成适配器。"""

    def __init__(
        self,
        api_key: str | None,
        model: str = "qwen-plus",
        base_url: str | None = None,
        timeout_policy: TimeoutPolicy | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_name = model
        self.base_url = (base_url or "").strip().rstrip("/") or None
        self.timeout_policy = timeout_policy or DEFAULT_GENERATION_TIMEOUT_POLICY

    def generate(self, *, system: str, prompt: str) -> str:
        return call_with_budget(
            lambda read_timeout: self._generate_once(
                system=system, prompt=prompt, read_timeout=read_timeout
            ),
            self.timeout_policy,
            op_name="generation.dashscope",
        )

    def generate_stream(self, *, system: str, prompt: str) -> Iterator[str]:
        """增量输出。DashScope 的 incremental_output 直接给增量片段。

        流式无法整体重试（已下发内容不能收回），所以这里只有一次尝试；
        失败时由调用方决定回退整段生成还是报错。
        """
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
            "stream": True,
            "incremental_output": True,
            "request_timeout": (
                self.timeout_policy.connect_seconds,
                self.timeout_policy.read_seconds,
            ),
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        for response in dashscope.Generation.call(**kwargs):
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                message = getattr(response, "message", "DashScope streaming failed")
                raise LLMTransportError(
                    str(message),
                    retryable=False,  # 已经开始下发，不能重试
                    status_code=status_code if isinstance(status_code, int) else None,
                )
            output = getattr(response, "output", None)
            if output is None and isinstance(response, dict):
                output = response.get("output")
            try:
                piece = output["choices"][0]["message"]["content"]
            except (TypeError, KeyError, IndexError):
                continue
            if piece:
                yield piece

    def _generate_once(self, *, system: str, prompt: str, read_timeout: float) -> str:
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
            # SDK 默认 300s（common/constants.py DEFAULT_REQUEST_TIMEOUT_SECONDS），
            # 远超反代的 60s，不显式传等于没有超时
            "request_timeout": (self.timeout_policy.connect_seconds, read_timeout),
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = dashscope.Generation.call(**kwargs)
        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            message = getattr(response, "message", "DashScope generation request failed")
            raise LLMTransportError(
                str(message),
                retryable=isinstance(status_code, int) and is_retryable_status(status_code),
                status_code=status_code if isinstance(status_code, int) else None,
            )
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
        self,
        api_key: str | None,
        model: str,
        base_url: str,
        max_tokens: int = 4096,
        timeout_policy: TimeoutPolicy | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_name = model
        self.base_url = base_url.strip().rstrip("/")
        # 推理型模型思维链计入 max_tokens，4096 会被长任务耗尽致 content 为空
        # （2026-08-04 LLM 裁判评测 8/34 条 finish_reason=length 实锤），按调用方需要放大
        self.max_tokens = max_tokens
        self.timeout_policy = timeout_policy or DEFAULT_GENERATION_TIMEOUT_POLICY

    def generate(self, *, system: str, prompt: str) -> str:
        return call_with_budget(
            lambda read_timeout: self._generate_once(
                system=system, prompt=prompt, read_timeout=read_timeout
            ),
            self.timeout_policy,
            op_name="generation.openai_compat",
        )

    def generate_stream(self, *, system: str, prompt: str) -> Iterator[str]:
        """OpenAI 兼容 SSE 增量输出。只取 content，忽略 reasoning_content。"""
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with httpx.stream(
            "POST",
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
                "stream": True,
            },
            timeout=httpx.Timeout(
                self.timeout_policy.read_seconds,
                connect=self.timeout_policy.connect_seconds,
            ),
        ) as response:
            if response.status_code != 200:
                response.read()
                raise LLMTransportError(
                    f"OpenAI-compat streaming failed: HTTP {response.status_code}",
                    retryable=False,  # 已经开始下发，不能重试
                    status_code=response.status_code,
                )
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    delta = json.loads(data)["choices"][0]["delta"]
                except (ValueError, KeyError, IndexError):
                    continue
                piece = delta.get("content")
                if piece:
                    yield piece

    def _generate_once(self, *, system: str, prompt: str, read_timeout: float) -> str:
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
            # 旧值写死 180s，超过反代 60s：用户收 504 后这里还在跑并照常计费
            timeout=httpx.Timeout(
                read_timeout,
                connect=self.timeout_policy.connect_seconds,
            ),
        )
        if response.status_code != 200:
            raise LLMTransportError(
                f"OpenAI-compat generation failed: HTTP {response.status_code} "
                f"{response.text[:200]}",
                retryable=is_retryable_status(response.status_code),
                status_code=response.status_code,
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
    # 评测脚本等旁路入口可能传入简易 settings 对象；取不到就用模块默认预算，
    # 但绝不退回"无超时"
    policy = getattr(settings, "llm_timeout_policy", None) or DEFAULT_GENERATION_TIMEOUT_POLICY
    if settings.llm_backend == "openai-compat":
        return OpenAICompatGenerationProvider(
            settings.llm_api_key,
            settings.generation_model,
            settings.llm_base_url,
            timeout_policy=policy,
        )
    return DashScopeGenerationProvider(
        settings.dashscope_api_key,
        settings.generation_model,
        settings.dashscope_base_url,
        timeout_policy=policy,
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
    # 证据可核验所需：不带原文的引用只是个不可查证的角标，用户无法判断
    # "第 15 页"到底支持不支持这句话（2026-08-05 老板提的核验缺口）
    snippet: str = ""
    document_title: str = ""


@dataclass(frozen=True)
class AnswerResult:
    status: str  # "answered" | "refused"
    answer: str | None
    citations: list[AnswerCitation] = field(default_factory=list)
    refusal_reason: str | None = None
    record_id: int | None = None
    # 流式期间已经通过安全闸门送到用户屏幕上的文本。调用方据此决定
    # 补发剩余部分（answered）还是让前端撤回（refused）。
    streamed_text: str = ""


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


MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 500


def _history_block(history: list[dict]) -> str:
    role_names = {"user": "用户", "assistant": "助手"}
    lines = [
        f"{role_names.get(m['role'], m['role'])}：{m['content'][:MAX_HISTORY_CHARS]}"
        for m in history[-MAX_HISTORY_MESSAGES:]
    ]
    return "\n".join(lines)


def _retrieval_query(query: str, history: list[dict] | None) -> str:
    """追问往往缺主语（'那第二步怎么做'），拼上最近一条用户问题补语境做检索。"""
    if not history:
        return query
    last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
    if not last_user:
        return query
    return f"{last_user[:200]} {query}"[:500]


def answer_events(
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
    history: list[dict] | None = None,
    streaming: bool = False,
) -> Iterator[tuple[str, str]]:
    """检索增强作答，以事件流的形式产出过程、以返回值产出结果。

    产出的事件：
    - ("stage", retrieving | retrieved | generating)：真实阶段，让前端不再
      只显示一句不变的"正在检索…"。
    - ("delta", 文本)：增量正文。**只有通过 StreamSafetyGate 的成句才会产出**，
      末尾残句一律留到全文校验之后，由调用方补发。

    用生成器而不是回调，是因为回调没法把事件"当场"送出 SSE——攒进列表等函数
    返回再发，首字延迟依旧等于完整生成延迟，等于没做流式。用生成器委托则
    既不需要后台线程，也不会让 Session 跨线程。

    最终结果通过 StopIteration.value 返回；同步调用方用 generate_answer()。
    """
    started_at = perf_counter()
    top_k = max(1, min(top_k, MAX_SNIPPETS))
    yield ("stage", "retrieving")
    results = search_knowledge(
        db,
        robot_model_id=robot_model_id,
        query=_retrieval_query(query, history),
        top_k=top_k,
        min_score=min_score,
        provider=embedding_provider,
    )
    yield ("stage", "retrieved")

    gate: StreamSafetyGate | None = None

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
            status="refused",
            answer=None,
            refusal_reason=reason,
            record_id=record.id,
            streamed_text=gate.released if gate else "",
        )

    if not results:
        return refuse("knowledge_gap", snippet_count=0)

    history_section = (
        f"此前对话（仅供理解语境，回答依据只能来自资料片段）：\n{_history_block(history)}\n\n"
        if history
        else ""
    )
    prompt = (
        f"{history_section}用户型号问题：{query}\n\n可用资料片段：\n{_snippet_block(results)}\n\n"
        "请依据上述片段回答；片段不足以回答时只输出 REFUSE。"
    )
    yield ("stage", "generating")

    if streaming and supports_streaming(generation_provider):
        # 真流式：成句即过闸下发。首字延迟不再等于完整生成延迟。
        gate = StreamSafetyGate(snippet_count=len(results))
        pieces: list[str] = []
        for piece in generation_provider.generate_stream(system=SYSTEM_PROMPT, prompt=prompt):
            pieces.append(piece)
            releasable = gate.feed(piece)
            if releasable:
                yield ("delta", releasable)
        raw_answer = "".join(pieces).strip()
    else:
        raw_answer = generation_provider.generate(system=SYSTEM_PROMPT, prompt=prompt).strip()

    if not raw_answer or REFUSE_TOKEN in raw_answer.split("\n", 1)[0][:20]:
        return refuse("model_refused")

    raw_answer = _TRAILING_REFUSE_PATTERN.sub("", raw_answer).strip()
    if not raw_answer.strip(_PUNCTUATION_ONLY):
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
            snippet=results[index - 1].content,
            document_title=results[index - 1].document_title,
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
        status="answered",
        answer=raw_answer,
        citations=citations,
        record_id=record.id,
        # 已下发部分可能与最终答案有差异（末尾 REFUSE 标记被剥离等），
        # 调用方按前缀关系补发剩余，不重复下发
        streamed_text=gate.released if gate else "",
    )


def generate_answer(db: Session, **kwargs) -> AnswerResult:
    """同步作答：消费掉过程事件，只要最终结果。

    非流式调用方（同步接口、评测脚本、CLI）沿用这个入口，行为与改造前一致。
    """
    kwargs.pop("streaming", None)
    events = answer_events(db, **kwargs)
    while True:
        try:
            next(events)
        except StopIteration as stop:
            return stop.value
