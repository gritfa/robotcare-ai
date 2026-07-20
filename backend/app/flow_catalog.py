from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import DiagnosticFlow, DiagnosticStep, IssueCategory, RobotModel


class PublishedFlowMutationError(RuntimeError):
    pass


class FlowStepDefinition(BaseModel):
    stable_key: str = Field(min_length=3, max_length=120)
    position: int = Field(gt=0)
    title: str = Field(min_length=2, max_length=150)
    instruction: str = Field(min_length=5)
    source_label: str = Field(min_length=3, max_length=200)
    source_url: str = Field(min_length=10, max_length=2000)
    source_page: int = Field(gt=0)
    evidence_level: Literal["direct", "partial", "none"]
    evidence_basis: str = Field(min_length=5)
    policy_note: str | None = None


class FlowDefinition(BaseModel):
    stable_key: str = Field(min_length=3, max_length=120)
    version: int = Field(gt=0)
    status: Literal["draft", "published", "retired"]
    model_code: str
    model_name: str
    issue_category_code: str
    issue_category_name: str
    title: str = Field(min_length=3, max_length=150)
    reviewed_at: datetime | None = None
    reviewed_by: str | None = Field(default=None, max_length=120)
    review_note: str | None = None
    steps: list[FlowStepDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_release(self):
        positions = [step.position for step in self.steps]
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError("Flow steps must use contiguous positions starting at 1")
        if len({step.stable_key for step in self.steps}) != len(self.steps):
            raise ValueError("Flow step stable keys must be unique")
        if self.status == "published" and (self.reviewed_at is None or not self.reviewed_by):
            raise ValueError("Published flows require reviewed_at and reviewed_by")
        if self.status == "published" and any(step.evidence_level == "none" for step in self.steps):
            raise ValueError("Published flows cannot contain steps without source evidence")
        return self

    def content_sha256(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"status", "reviewed_at", "reviewed_by", "review_note"},
        )
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FlowCatalog(BaseModel):
    schema_version: Literal["2.0"]
    updated_at: str
    flows: list[FlowDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_versions(self):
        identities = [(flow.stable_key, flow.version) for flow in self.flows]
        if len(set(identities)) != len(identities):
            raise ValueError("Flow stable_key/version pairs must be unique")
        return self


def default_catalog_path() -> Path:
    return Path(__file__).resolve().parents[2] / "knowledge" / "diagnostic_flows.json"


def load_flow_catalog(path: str | Path | None = None) -> FlowCatalog:
    catalog_path = Path(path) if path else default_catalog_path()
    return FlowCatalog.model_validate_json(catalog_path.read_text(encoding="utf-8"))


def sync_flow_catalog(db: Session, catalog: FlowCatalog) -> None:
    for definition in catalog.flows:
        robot_model = db.scalar(select(RobotModel).where(RobotModel.code == definition.model_code))
        if robot_model is None:
            robot_model = RobotModel(code=definition.model_code, name=definition.model_name)
            db.add(robot_model)
            db.flush()

        category = db.scalar(
            select(IssueCategory).where(IssueCategory.code == definition.issue_category_code)
        )
        if category is None:
            category = IssueCategory(
                code=definition.issue_category_code,
                name=definition.issue_category_name,
            )
            db.add(category)
            db.flush()

        checksum = definition.content_sha256()
        flow = db.scalar(
            select(DiagnosticFlow)
            .options(selectinload(DiagnosticFlow.steps))
            .where(
                DiagnosticFlow.stable_key == definition.stable_key,
                DiagnosticFlow.version == definition.version,
            )
        )
        if flow is not None and flow.status in {"published", "retired"} and flow.content_sha256 != checksum:
            raise PublishedFlowMutationError(
                f"Released flow {definition.stable_key} v{definition.version} cannot be changed in place"
            )
        if flow is not None and flow.status == "retired" and definition.status == "published":
            raise PublishedFlowMutationError(
                f"Retired flow {definition.stable_key} v{definition.version} cannot be republished; create a new version"
            )

        if definition.status == "published":
            other_published = list(
                db.scalars(
                    select(DiagnosticFlow).where(
                        DiagnosticFlow.stable_key == definition.stable_key,
                        DiagnosticFlow.status == "published",
                        DiagnosticFlow.version != definition.version,
                    )
                )
            )
            for previous in other_published:
                previous.status = "retired"
                previous.active = False

        if flow is None:
            flow = DiagnosticFlow(
                stable_key=definition.stable_key,
                version=definition.version,
                robot_model_id=robot_model.id,
                issue_category_id=category.id,
                title=definition.title,
                status=definition.status,
                content_sha256=checksum,
                reviewed_at=definition.reviewed_at,
                reviewed_by=definition.reviewed_by,
                active=definition.status == "published",
            )
            db.add(flow)
            db.flush()
        elif flow.status == "draft":
            flow.robot_model_id = robot_model.id
            flow.issue_category_id = category.id
            flow.title = definition.title
            flow.status = definition.status
            flow.content_sha256 = checksum
            flow.reviewed_at = definition.reviewed_at
            flow.reviewed_by = definition.reviewed_by
            flow.active = definition.status == "published"
            flow.steps.clear()
            db.flush()
        elif flow.status == "published" and definition.status == "retired":
            flow.status = "retired"
            flow.active = False

        if not flow.steps:
            for step in definition.steps:
                db.add(
                    DiagnosticStep(
                        flow_id=flow.id,
                        stable_key=step.stable_key,
                        position=step.position,
                        title=step.title,
                        instruction=step.instruction,
                        source_label=step.source_label,
                        source_url=step.source_url,
                        source_page=step.source_page,
                        evidence_level=step.evidence_level,
                        evidence_basis=step.evidence_basis,
                        policy_note=step.policy_note,
                    )
                )
    db.commit()
