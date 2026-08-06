"""/auth/* routes: register, login, refresh, logout, me."""

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth_service import (
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
from ..config import get_settings
from ..database import get_db
from ..models import User
from ..rate_limit_service import (
    enforce_login_success_rate_limit,
    enforce_registration_rate_limit,
)
from ..schemas import (
    LoginRequest,
    RegisterRequest,
    RegistrationPolicyRead,
    TokenResponse,
    UserRead,
)
from ..security import (
    DUMMY_PASSWORD_HASH,
    decode_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from ._shared import enforce_registration_policy, token_response

router = APIRouter(prefix="/api/v1")


@router.get("/auth/registration-policy", response_model=RegistrationPolicyRead)
def registration_policy() -> RegistrationPolicyRead:
    """当前注册模式，供注册页如实渲染表单。

    此前表单固定写"试用邀请码（开放环境可留空）"，而生产默认是邀请制——
    用户照着提示留空，收到的却是一句英文拒绝。只回模式，不回邀请码本身。
    """
    mode = get_settings().registration_mode
    return RegistrationPolicyRead(mode=mode, invite_required=mode == "invite", open=mode == "open")


@router.post("/auth/register", response_model=TokenResponse, status_code=201)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    email = normalize_email(str(payload.email))
    enforce_registration_rate_limit(db, request, email)
    enforce_registration_policy(payload, get_settings())
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(email=email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.flush()
        issued = issue_authentication(db, user)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered") from None
    db.refresh(user)
    set_refresh_cookie(response, issued.refresh_token)
    return token_response(user, issued.access_token)


@router.post("/auth/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    email = normalize_email(str(payload.email))
    acquire_login_attempt_lock(db, email, request)
    enforce_login_rate_limit(db, email, request)
    user = db.scalar(select(User).where(User.email == email))
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_password(payload.password, password_hash)
    if user is None or not password_valid:
        record_login_failure(db, email, request)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="Account disabled")
    clear_login_failures(db, email, request)
    # 密码对了不等于可以无限刷：每次成功登录都要签发 token 并写 refresh_tokens，
    # 成功路径此前完全没有速率约束（体检 D5）。放在清除失败计数之后，
    # 保证合法用户不会因为限流而永远清不掉自己的失败记录。
    enforce_login_success_rate_limit(db, email, request)
    issued = issue_authentication(db, user)
    db.commit()
    set_refresh_cookie(response, issued.refresh_token)
    return token_response(user, issued.access_token)


@router.post("/auth/refresh", response_model=TokenResponse)
def refresh_authentication(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh_token is None:
        raise HTTPException(status_code=401, detail="Refresh token required")
    user, issued = rotate_refresh_token(db, raw_refresh_token)
    set_refresh_cookie(response, issued.refresh_token)
    return token_response(user, issued.access_token)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    revoked_session_id = logout_refresh_token(db, request.cookies.get(REFRESH_COOKIE_NAME))
    if revoked_session_id is None:
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            try:
                _user_id, access_session_id = decode_access_token(token)
            except (ValueError, TypeError, jwt.PyJWTError):
                pass
            else:
                revoke_session(db, access_session_id, reason="logout")
                db.commit()
    response = Response(status_code=204)
    delete_refresh_cookie(response)
    return response


@router.get("/auth/me", response_model=UserRead)
def get_me(user: User = Depends(get_current_user)) -> User:
    return user
