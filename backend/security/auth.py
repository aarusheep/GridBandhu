"""JWT authentication for the GridBandhu API."""

from datetime import datetime, timedelta, timezone
import hmac
import os
from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


ALGORITHM = "HS256"
TOKEN_TTL_MINUTES = int(os.getenv("JWT_TTL_MINUTES", "60"))
JWT_SECRET = os.getenv("JWT_SECRET", "")
bearer = HTTPBearer(auto_error=False)


def _secret() -> str:
    if len(JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be set to at least 32 characters in backend/.env")
    return JWT_SECRET


def create_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": username, "role": role, "iat": now, "exp": now + timedelta(minutes=TOKEN_TTL_MINUTES)}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except (JWTError, RuntimeError):
        return {}


def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    payload = verify_token(credentials.credentials)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return payload


def authenticate_websocket(token: str | None) -> dict:
    payload = verify_token(token or "")
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired WebSocket token")
    return payload


def credentials_match(username: str, password: str) -> bool:
    expected_user = os.getenv("GRIDBANDHU_OPERATOR_USERNAME", "operator")
    expected_password = os.getenv("GRIDBANDHU_OPERATOR_PASSWORD", "gridbandhu")
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_password)
