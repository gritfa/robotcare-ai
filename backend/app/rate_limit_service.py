from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
from threading import Lock
from time import monotonic
from typing import Iterator, Sequence, TypeVar

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from .auth_service import client_ip, normalize_email
from .config import Settings, get_settings
from .models import ApiRateLimit


T = TypeVar("T")


@dataclass(frozen=True)
class RateQuota:
    action: str
    scope: str
    principal: str
    window_kind: str
    limit: int


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _digest(settings: Settings, namespace: str, value: str) -> str:
    # 独立密钥：轮换它只清掉当前限流窗口，不会把所有用户踢下线
    return hmac.new(
        settings.effective_rate_limit_hmac_secret.encode("utf-8"),
        f"{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _window(now: datetime, kind: str) -> tuple[datetime, datetime]:
    if kind == "minute":
        started = now.replace(second=0, microsecond=0)
        return started, started + timedelta(minutes=1)
    if kind == "day":
        started = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return started, started + timedelta(days=1)
    raise ValueError(f"Unsupported rate-limit window: {kind}")


def _retry_after_seconds(expires_at: datetime, now: datetime) -> int:
    return max(1, math.ceil((expires_at - now).total_seconds()))


def consume_rate_quotas(
    db: Session,
    quotas: Sequence[RateQuota],
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> None:
    """Atomically reserve every fixed-window quota or reserve none of them."""

    resolved_settings = settings or get_settings()
    resolved_now = now or utcnow()
    insert_factory = postgresql_insert

    blocked: tuple[RateQuota, datetime] | None = None
    for quota in quotas:
        started_at, expires_at = _window(resolved_now, quota.window_kind)
        principal_hash = _digest(
            resolved_settings,
            f"api-rate-principal-{quota.scope}",
            quota.principal,
        )
        key_hash = _digest(
            resolved_settings,
            "api-rate-key",
            "\0".join(
                (
                    quota.action,
                    quota.scope,
                    principal_hash,
                    quota.window_kind,
                    started_at.isoformat(),
                )
            ),
        )
        statement = (
            insert_factory(ApiRateLimit)
            .values(
                key_hash=key_hash,
                action=quota.action,
                scope=quota.scope,
                principal_hash=principal_hash,
                window_kind=quota.window_kind,
                window_started_at=started_at,
                expires_at=expires_at,
                request_count=1,
                updated_at=resolved_now,
            )
            .on_conflict_do_update(
                index_elements=[ApiRateLimit.key_hash],
                set_={
                    "request_count": ApiRateLimit.request_count + 1,
                    "updated_at": resolved_now,
                },
                where=ApiRateLimit.request_count < quota.limit,
            )
            .returning(ApiRateLimit.request_count)
        )
        if db.execute(statement).scalar_one_or_none() is None:
            blocked = quota, expires_at
            break

    if blocked is not None:
        db.rollback()
        quota, expires_at = blocked
        retry_after = _retry_after_seconds(expires_at, resolved_now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMITED",
                "action": quota.action,
                "scope": quota.scope,
                "window_kind": quota.window_kind,
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )
    db.commit()


def enforce_registration_rate_limit(
    db: Session,
    request: Request,
    email: str,
    settings: Settings | None = None,
) -> None:
    resolved = settings or get_settings()
    consume_rate_quotas(
        db,
        (
            RateQuota(
                "registration",
                "email",
                normalize_email(email),
                "minute",
                resolved.registration_email_per_minute,
            ),
            RateQuota(
                "registration",
                "ip",
                client_ip(request, resolved),
                "minute",
                resolved.registration_ip_per_minute,
            ),
        ),
        resolved,
    )


def enforce_login_success_rate_limit(
    db: Session,
    email: str,
    request: Request,
    settings: Settings | None = None,
) -> None:
    # 参数顺序刻意与同文件的 enforce_login_rate_limit 保持一致（db, email, request）。
    # registration 那个是 (db, request, email)，两者在同一文件里很容易记混。
    """成功登录也要计费。

    失败计数（enforce_login_rate_limit）只拦得住猜密码的人。拿着一副有效凭据
    的脚本可以无限次登录：每次都签发一对 token、写一行 refresh_tokens、跑一次
    Argon2 校验，因此成功登录路径同样需要速率约束。
    """

    resolved = settings or get_settings()
    consume_rate_quotas(
        db,
        (
            RateQuota(
                "login_success",
                "email",
                normalize_email(email),
                "minute",
                resolved.login_success_email_per_minute,
            ),
            RateQuota(
                "login_success",
                "ip",
                client_ip(request, resolved),
                "minute",
                resolved.login_success_ip_per_minute,
            ),
        ),
        resolved,
    )


def enforce_business_rate_limit(
    db: Session,
    request: Request,
    *,
    action: str,
    user_id: int,
    settings: Settings | None = None,
) -> None:
    resolved = settings or get_settings()
    user_limit, ip_limit = resolved.business_rate_limits(action)
    consume_rate_quotas(
        db,
        (
            RateQuota(action, "user", str(user_id), "minute", user_limit),
            RateQuota(
                action,
                "ip",
                client_ip(request, resolved),
                "minute",
                ip_limit,
            ),
        ),
        resolved,
    )


def enforce_embedding_rate_limit(
    db: Session,
    request: Request,
    *,
    user_id: int,
    settings: Settings | None = None,
) -> None:
    resolved = settings or get_settings()
    source_ip = client_ip(request, resolved)
    consume_rate_quotas(
        db,
        (
            RateQuota(
                "knowledge_embedding",
                "user",
                str(user_id),
                "minute",
                resolved.embedding_user_per_minute,
            ),
            RateQuota(
                "knowledge_embedding",
                "ip",
                source_ip,
                "minute",
                resolved.embedding_ip_per_minute,
            ),
            RateQuota(
                "knowledge_embedding",
                "user",
                str(user_id),
                "day",
                resolved.embedding_user_per_day,
            ),
            RateQuota(
                "knowledge_embedding",
                "ip",
                source_ip,
                "day",
                resolved.embedding_ip_per_day,
            ),
        ),
        resolved,
    )


def cleanup_expired_api_rate_limits(
    db: Session, *, now: datetime | None = None
) -> int:
    result = db.execute(delete(ApiRateLimit).where(ApiRateLimit.expires_at <= (now or utcnow())))
    db.commit()
    return result.rowcount or 0


def normalize_knowledge_query(query: str) -> str:
    return " ".join(query.casefold().split())


def aggregate_knowledge_version(document_shas: Sequence[str]) -> str:
    canonical = "\n".join(sorted(document_shas))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def knowledge_cache_key(
    settings: Settings,
    *,
    model_code: str,
    normalized_query: str,
    knowledge_version: str,
    top_k: int,
    min_score: float,
) -> str:
    payload = json.dumps(
        {
            "model": model_code,
            "query": normalized_query,
            "knowledge_version": knowledge_version,
            "top_k": top_k,
            "min_score": min_score,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest(settings, "knowledge-search-cache", payload)


class KnowledgeSearchCache:
    """Short-lived process-local cache with per-key singleflight.

    The database quotas remain correct across instances. Cache contents and
    singleflight locks intentionally do not: multiple Web processes may each
    perform one Embedding call for the same miss.
    """

    def __init__(self, ttl_seconds: int, max_entries: int = 512):
        self.ttl_seconds = ttl_seconds
        # 容量上限：命中缓存的前提是"同一问题短时间被重复问"，长尾查询本来
        # 就不会二次命中，却会永久占住内存直到进程 OOM。超限按 LRU 淘汰。
        self.max_entries = max(1, max_entries)
        self._guard = Lock()
        self._values: "OrderedDict[str, tuple[float, tuple[object, ...]]]" = OrderedDict()
        self._locks: dict[str, tuple[Lock, int]] = {}

    def _purge_expired_locked(self, now: float) -> None:
        """清扫已过期条目。

        只靠 get() 顺带删除是不够的：过期后再没人查的键永远等不到那次 get，
        条目会一直挂在字典里。所以每次写入时主动扫一遍。
        """
        expired = [key for key, (expires_at, _) in self._values.items() if expires_at <= now]
        for key in expired:
            self._values.pop(key, None)

    def get(self, key: str) -> list[object] | None:
        with self._guard:
            cached = self._values.get(key)
            if cached is None:
                return None
            expires_at, values = cached
            if expires_at <= monotonic():
                self._values.pop(key, None)
                return None
            self._values.move_to_end(key)
            return list(values)

    def set(self, key: str, values: Sequence[object]) -> None:
        now = monotonic()
        with self._guard:
            self._purge_expired_locked(now)
            self._values[key] = (now + self.ttl_seconds, tuple(values))
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                # popitem(last=False) 取最久未使用的那条
                self._values.popitem(last=False)

    def size(self) -> int:
        with self._guard:
            return len(self._values)

    @contextmanager
    def singleflight(self, key: str) -> Iterator[None]:
        with self._guard:
            lock, users = self._locks.get(key, (Lock(), 0))
            self._locks[key] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                current_lock, current_users = self._locks[key]
                if current_users <= 1:
                    self._locks.pop(key, None)
                else:
                    self._locks[key] = (current_lock, current_users - 1)
