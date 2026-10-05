from fastapi import Request, HTTPException, status
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from app.config import settings

import hmac
import hashlib

serializer = URLSafeTimedSerializer(settings.secret_key)


def create_session_token() -> str:
    """Generate a signed session token."""
    return serializer.dumps({"authenticated": True})


def verify_session_token(token: str) -> bool:
    """Verify the signed session token against secret and max age."""
    if not token:
        return False
    try:
        data = serializer.loads(token, max_age=settings.session_max_age_seconds)
        return bool(data.get("authenticated"))
    except (BadSignature, SignatureExpired):
        return False


def create_csrf_token(session_token: str) -> str:
    """Generate an HMAC-SHA256 CSRF token bound to the signed session token."""
    if not session_token:
        return ""
    key = settings.secret_key.encode("utf-8")
    return hmac.new(key, session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf_token(session_token: str, csrf_token: str) -> bool:
    """Verify that the provided CSRF token matches the session token."""
    if not session_token or not csrf_token:
        return False
    expected = create_csrf_token(session_token)
    return hmac.compare_digest(expected, csrf_token)


def verify_pin(input_pin: str) -> bool:
    """Compare entered PIN with configured dashboard PIN."""
    if not input_pin or not settings.dashboard_pin:
        return False
    return input_pin.strip() == settings.dashboard_pin.strip()


def is_authenticated(request: Request) -> bool:
    """Check if the current request has a valid authentication session cookie."""
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return False
    return verify_session_token(token)


async def require_auth(request: Request):
    """FastAPI dependency for endpoints requiring valid authentication."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Valid PIN session required",
        )


async def require_csrf(request: Request):
    """FastAPI dependency for mutative operational endpoints requiring CSRF validation."""
    token = request.cookies.get(settings.session_cookie_name)
    if not token or not verify_session_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Valid PIN session required",
        )
    csrf_token = request.headers.get("X-CSRF-Token", "")
    if not csrf_token or not verify_csrf_token(token, csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Token CSRF tidak valid atau tidak disertakan.",
        )
