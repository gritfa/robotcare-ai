"""Domain routers split out of the former single-file app/api.py.

Each domain module owns an APIRouter(prefix="/api/v1"); this package exposes
an aggregated ``router`` that includes them all, so app.main only needs
``from .routers import router``.
"""

from fastapi import APIRouter

from . import admin, auth, catalog, conversations, devices, diagnostics, knowledge

router = APIRouter()
router.include_router(auth.router)
router.include_router(knowledge.router)
router.include_router(catalog.router)
router.include_router(admin.router)
router.include_router(devices.router)
router.include_router(diagnostics.router)
router.include_router(conversations.router)

__all__ = ["router", "admin", "auth", "catalog", "conversations", "devices", "diagnostics", "knowledge"]
