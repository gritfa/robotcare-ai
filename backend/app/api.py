"""Backward-compatible re-export layer for the former single-file API module.

The actual route handlers now live in the ``app.routers`` package (split by
business domain: auth / catalog / devices / knowledge / diagnostics / admin,
plus shared helpers in ``app.routers._shared``). This module re-exports the
complete public namespace the old ``app.api`` exposed, so existing imports and
tests keep working unchanged.

Patch propagation: several tests monkeypatch attributes on this module (e.g.
``monkeypatch.setattr(api_module, "get_settings", ...)``) and expect the route
handlers to observe the patched object. Handlers now resolve those names from
their own domain modules, so this module installs a custom ``__setattr__`` that
forwards any rebinding to every domain module whose current binding is the same
object being replaced. monkeypatch's teardown restores the original through the
same path, so patching and unpatching stay symmetric.
"""

import sys as _sys
import types as _types

import hashlib
import hmac
from io import BytesIO
import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4
import warnings

import jwt
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .auth_service import (
    REFRESH_COOKIE_NAME,
    acquire_login_attempt_lock,
    clear_login_failures,
    delete_refresh_cookie,
    enforce_login_rate_limit,
    issue_authentication,
    logout_refresh_token,
    normalize_email,
    record_login_failure,
    revoke_session,
    rotate_refresh_token,
    set_refresh_cookie,
)
from .config import Settings, get_settings
from .database import get_db
from .diagnostic_graph import feedback_decision_graph
from .issue_classifier import classify_issue
from .alerting import send_alert
from .generation_service import generate_answer
from .knowledge_service import get_knowledge_health, get_knowledge_status, search_knowledge
from .models import (
    Attachment,
    AuditLog,
    DiagnosticFlow,
    DiagnosticSession,
    DiagnosticStep,
    GenerationRecord,
    IssueCategory,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeGapEvent,
    PendingFileDeletion,
    RobotModel,
    SafetyBlockEvent,
    ServiceReport,
    StepExecution,
    User,
    UserDevice,
)
from .observability import emit_json_log, request_trace_id
from .attachment_service import create_attachment as persist_attachment
from .pdf_report import ensure_report_pdf as ensure_stored_report_pdf, report_pdf_path
from .report_service import get_or_create_service_report as persist_service_report
from .rate_limit_service import (
    aggregate_knowledge_version,
    enforce_business_rate_limit,
    enforce_embedding_rate_limit,
    enforce_registration_rate_limit,
    knowledge_cache_key,
    normalize_knowledge_query,
)
from .safety import detect_safety_block
from .schemas import (
    AnswerCitationRead,
    AttachmentRead,
    AdminAuditLogRead,
    AdminContentGapRead,
    AdminDiagnosticDetailRead,
    AdminDiagnosticSummary,
    AdminGenerationStatsRead,
    AdminKnowledgeUploadRead,
    AdminModelRead,
    AdminModelUpdate,
    AdminOverviewRead,
    AdminReportSummary,
    AdminRobotModelSummary,
    AdminSafetyBlockRead,
    AdminSafetyBlockDetailRead,
    AdminServiceReportDetailRead,
    AdminUnresolvedReportRead,
    AdminUserSummary,
    DeviceCreate,
    DeviceRead,
    DeviceUpdate,
    DiagnosticCreate,
    DiagnosticRead,
    DiagnosticOptionRead,
    FeedbackRequest,
    FeedbackResponse,
    LoginRequest,
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeHealthRead,
    KnowledgeModelHealthRead,
    KnowledgeStatusRead,
    ModelRead,
    RegisterRequest,
    ReportPdfRead,
    ReportRead,
    StepRead,
    TokenResponse,
    UserRead,
)
from .security import (
    DUMMY_PASSWORD_HASH,
    decode_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)

from .routers import router
from .routers import _shared as _shared_module
from .routers import (
    admin as _admin_module,
    auth as _auth_module,
    catalog as _catalog_module,
    devices as _devices_module,
    diagnostics as _diagnostics_module,
    knowledge as _knowledge_module,
)
from .routers._shared import (
    ALLOWED_IMAGE_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_DIAGNOSTIC,
    MAX_IMAGE_PIXELS,
    PIL_FORMAT_TO_CONTENT_TYPE,
    audit_sensitive_admin_read,
    current_step_for,
    enforce_registration_policy,
    mask_email,
    owned_device,
    owned_diagnostic,
    token_response,
    validate_image_content,
)
from .routers.auth import get_me, login, logout, refresh_authentication, register
from .routers.catalog import list_diagnostic_options, list_models
from .routers.devices import create_device, delete_device, get_device, list_devices, update_device
from .routers.knowledge import (
    knowledge_answer,
    knowledge_health,
    knowledge_search,
    knowledge_status,
    record_knowledge_gap_event,
)
from .routers.diagnostics import (
    create_diagnostic,
    create_report,
    create_report_pdf,
    delete_attachment,
    download_report_pdf,
    ensure_report_pdf,
    get_current_step,
    get_diagnostic,
    get_or_create_service_report,
    get_report,
    list_attachments,
    list_diagnostics,
    make_report,
    report_pdf_response,
    submit_feedback,
    upload_attachment,
)
from .routers.admin import (
    admin_audit_logs,
    admin_content_gaps,
    admin_diagnostic_detail,
    admin_knowledge_status,
    admin_knowledge_upload,
    admin_list_models,
    admin_overview,
    admin_report_detail,
    admin_safety_block_detail,
    admin_safety_blocks,
    admin_unresolved_reports,
    admin_update_model,
)

_MISSING = object()
_PATCH_PROPAGATION_TARGETS = (
    _shared_module,
    _admin_module,
    _auth_module,
    _catalog_module,
    _devices_module,
    _diagnostics_module,
    _knowledge_module,
)


class _BackCompatModule(_types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        previous = self.__dict__.get(name, _MISSING)
        super().__setattr__(name, value)
        if previous is _MISSING or previous is value:
            return
        for module in _PATCH_PROPAGATION_TARGETS:
            if module.__dict__.get(name, _MISSING) is previous:
                module.__dict__[name] = value


_sys.modules[__name__].__class__ = _BackCompatModule
