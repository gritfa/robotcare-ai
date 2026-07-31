"""D1 合成数据包全链验证：目录发布 → PDF 入库 → 检索冒烟 → 安全语料 → 评测审计。

合成数据的正确性由"单一数据源生成器"构造保证；本测试用产品自身的流水线
（seed / ingest_pdf / search_knowledge / detect_safety_block / audit_cases）
逐环节复核，防止生成器与产品实现漂移。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import func, select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.issue_classifier import classify_issue
from app.knowledge_service import (
    HashingNgramEmbeddingProvider,
    extract_pdf_pages,
    ingest_pdf,
    search_knowledge,
)
from app.models import DiagnosticFlow, KnowledgeChunk, RobotModel
from app.safety import detect_safety_block
from scripts.audit_eval_dataset import audit_cases, load_cases, load_catalog, load_source_ids
from scripts.generate_synthetic_knowledge import (
    CATEGORY_QUERIES,
    FLOW_QUERIES,
    source_url,
)
from scripts.synthetic_flows_data import SYNTHETIC_FLOWS
from scripts.synthetic_knowledge_data import MODELS, SAFETY_CORPUS

SYNTHETIC_DIR = PROJECT_ROOT / "knowledge" / "synthetic"


def _model_id(db, code: str) -> int:
    return db.scalar(select(RobotModel.id).where(RobotModel.code == code))


def _ingest_all(db) -> dict[str, int]:
    provider = HashingNgramEmbeddingProvider()
    model_ids = {}
    for code in MODELS:
        model_ids[code] = _model_id(db, code)
        result = ingest_pdf(
            db,
            robot_model_id=model_ids[code],
            pdf_path=SYNTHETIC_DIR / f"{code}_manual.pdf",
            source_url=source_url(code),
            provider=provider,
            title=f"{code} 合成演示说明书",
        )
        assert result.chunk_count > 0
    return model_ids


def test_synthetic_models_and_flows_seeded(client):
    with client.app.state.session_factory() as db:
        for code in MODELS:
            assert _model_id(db, code) is not None, f"model {code} not seeded"
        published = db.scalar(
            select(func.count())
            .select_from(DiagnosticFlow)
            .where(DiagnosticFlow.stable_key.like("rc-%"), DiagnosticFlow.status == "published")
        )
        assert published == sum(len(flows) for flows in SYNTHETIC_FLOWS.values())


def test_flow_source_pages_match_rendered_manuals():
    """流程引用的说明书页码必须与 PDF 实际内容一致（防生成器页码漂移）。"""
    catalog = json.loads(
        (PROJECT_ROOT / "knowledge" / "diagnostic_flows.json").read_text(encoding="utf-8")
    )
    pages_by_model = {
        code: extract_pdf_pages(SYNTHETIC_DIR / f"{code}_manual.pdf")[0] for code in MODELS
    }
    checked = 0
    for flow in catalog["flows"]:
        if not flow["stable_key"].startswith("rc-"):
            continue
        pages = pages_by_model[flow["model_code"]]
        for step in flow["steps"]:
            page_text = pages[step["source_page"] - 1].text
            assert flow["title"] in page_text.replace("\n", ""), (
                f"{flow['stable_key']} 第 {step['source_page']} 页未包含流程标题"
            )
            checked += 1
    assert checked >= 93  # 31 条流程 × ≥3 步


def test_ingest_and_model_isolated_retrieval_smoke(client):
    with client.app.state.session_factory() as db:
        model_ids = _ingest_all(db)
        total_chunks = db.scalar(select(func.count()).select_from(KnowledgeChunk))
        assert total_chunks >= 60

        provider = HashingNgramEmbeddingProvider()
        hits = 0
        for model_code, flows in SYNTHETIC_FLOWS.items():
            for stable_key, *_ in flows[:3]:
                query = FLOW_QUERIES[stable_key]
                results = search_knowledge(
                    db,
                    robot_model_id=model_ids[model_code],
                    query=query,
                    top_k=5,
                    min_score=0.0,
                    provider=provider,
                )
                assert results, f"{stable_key} 检索为空"
                # 结构性隔离：所有结果必须来自本型号文档
                for item in results:
                    assert item.source_url == source_url(model_code)
                hits += 1
        assert hits == 9


def test_safety_corpus_expectations_hold():
    for text, expect_block, category, note in SAFETY_CORPUS:
        result = detect_safety_block(text)
        if expect_block:
            assert result is not None, f"漏拦: {text} ({note})"
            assert result.category == category, f"类别错: {text} → {result.category}"
        else:
            assert result is None, f"误拦: {text} ({note}) → {result.category}"


def test_classification_queries_align_with_rules():
    for model_code, flows in SYNTHETIC_FLOWS.items():
        categories = {flow[1] for flow in flows}
        for category in categories:
            for query in CATEGORY_QUERIES[category]:
                decision = classify_issue(
                    model_code=model_code,
                    selected_category_code=category,
                    issue_description=query,
                    error_code=None,
                    available_category_codes=categories,
                )
                assert decision.kind == "consistent", (
                    f"{model_code}/{category} 问法「{query}」分类为 {decision.kind}"
                    f"（候选 {decision.candidates}）"
                )


def test_eval_dataset_offline_audit_green():
    cases = load_cases(PROJECT_ROOT / "knowledge" / "eval_cases.jsonl")
    assert len(cases) >= 250
    report = audit_cases(
        cases,
        catalog=load_catalog(PROJECT_ROOT / "knowledge" / "diagnostic_flows.json"),
        known_source_ids=load_source_ids(PROJECT_ROOT / "knowledge" / "sources.json"),
    )
    assert report["integrity"]["passed"], report["integrity"]["errors"][:5]
    for metric in ("safety_block", "source_page", "step_selection"):
        assert report["metrics"][metric]["execution_status"] == "passed", (
            metric,
            report["metrics"][metric]["failures"][:3],
        )
        assert report["metrics"][metric]["score"] == 1.0
