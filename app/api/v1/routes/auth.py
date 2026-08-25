"""Authentication routes.

Field naming follows the frontend contract (`full_name`, not `name`) and the session
response is `{"user": {...}}` rather than a bare user, because a wrapper leaves room
to add session metadata later without breaking every client.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import AuthServiceDep, CurrentUser
from app.core.errors import AppError, ErrorCode
from app.core.sessions import REFRESH_COOKIE, clear_session_cookies, set_session_cookies
from app.modules.auth.google import get_identity_provider

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    """Note the absence of `roles` and `status`. They are not accepted, so a crafted
    body cannot self-assign SUPER_ADMIN — the classic mass-assignment escalation."""

    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=12, max_length=256)


class RegisterResponse(BaseModel):
    user_id: str
    verification_required: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    otp: str | None = Field(default=None, min_length=6, max_length=6)


class PublicUser(BaseModel):
    id: str
    full_name: str
    email: EmailStr
    roles: list[str]
    email_verified: bool
    mfa_enabled: bool


class SessionResponse(BaseModel):
    user: PublicUser


class TokenResponse(BaseModel):
    """Tokens are returned for API and mobile clients. Browsers ignore this body and
    use the httpOnly cookies set alongside it."""

    access_token: str
    refresh_token: str
    expires_in: int
    csrf_token: str
    user: PublicUser | None = None


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _public_user(payload: dict) -> PublicUser:
    return PublicUser(
        id=payload["id"],
        full_name=payload.get("full_name") or payload.get("name") or "",
        email=payload["email"],
        roles=payload["roles"],
        email_verified=payload.get("email_verified", False),
        mfa_enabled=payload.get("mfa_enabled", False),
    )


def _issue(response: Response, result: dict) -> TokenResponse:
    csrf = set_session_cookies(
        response, access=result["access_token"], refresh=result["refresh_token"]
    )
    return TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        expires_in=result["expires_in"],
        csrf_token=csrf,
        user=_public_user(result["user"]) if result.get("user") else None,
    )


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, service: AuthServiceDep, request: Request
) -> RegisterResponse:
    result = await service.register(
        name=body.full_name,
        email=str(body.email),
        phone=body.phone,
        password=body.password,
        ip=client_ip(request),
    )
    return RegisterResponse(**result)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, service: AuthServiceDep, request: Request, response: Response
) -> TokenResponse:
    result = await service.login(
        email=str(body.email), password=body.password, otp=body.otp, ip=client_ip(request)
    )
    return _issue(response, result)


class RefreshRequest(BaseModel):
    """Optional. Browsers send nothing and the refresh cookie is used instead."""

    refresh_token: str | None = None


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, service: AuthServiceDep, request: Request, response: Response
) -> TokenResponse:
    token = body.refresh_token or request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AppError(ErrorCode.AUTHENTICATION_REQUIRED, detail="no refresh token or cookie")
    result = await service.refresh(refresh_token=token, ip=client_ip(request))
    return _issue(response, result)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(principal: CurrentUser, service: AuthServiceDep, response: Response) -> None:
    await service.logout(session_id=principal.session_id, actor_id=principal.user_id)
    # Revoke server-side first, then clear the cookies. Clearing alone would leave a
    # usable session for anyone holding a copy of the token.
    clear_session_cookies(response)


@router.get("/session", response_model=SessionResponse)
async def session(principal: CurrentUser) -> SessionResponse:
    return SessionResponse(
        user=PublicUser(
            id=principal.user_id,
            full_name=principal.full_name,
            email=principal.email,
            roles=list(principal.roles),
            email_verified=principal.email_verified,
            mfa_enabled=principal.mfa_enabled,
        )
    )


# -- Google sign-in ---------------------------------------------------------


class GoogleStartResponse(BaseModel):
    authorization_url: str


class GoogleCallbackRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=256)


@router.get("/google/authorize", response_model=GoogleStartResponse)
async def google_authorize(service: AuthServiceDep) -> GoogleStartResponse:
    """Returns the URL to send the browser to. State and nonce are stored
    server-side and expire in ten minutes."""
    return GoogleStartResponse(**await service.begin_google(get_identity_provider()))


@router.post("/google/callback", response_model=TokenResponse)
async def google_callback(
    body: GoogleCallbackRequest,
    service: AuthServiceDep,
    request: Request,
    response: Response,
) -> TokenResponse:
    """Exchanges the code server-side, verifies the ID token against Google's JWKS,
    then links to an existing account by provider subject or verified email — one
    account per person, never a duplicate."""
    result = await service.complete_google(
        get_identity_provider(), code=body.code, state=body.state, ip=client_ip(request)
    )
    return _issue(response, result)
