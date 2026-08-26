"""AES-GCM helpers for encrypted telemetry ingress."""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def _key() -> bytes:
    raw = os.getenv("ENCRYPTION_KEY", "")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise RuntimeError("ENCRYPTION_KEY must be base64 encoded") from exc
    if len(key) != 32:
        raise RuntimeError("ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def decrypt_payload(nonce: str, ciphertext: str) -> str:
    try:
        return AESGCM(_key()).decrypt(base64.b64decode(nonce), base64.b64decode(ciphertext), None).decode("utf-8")
    except Exception as exc:
        raise ValueError("Encrypted telemetry failed integrity verification") from exc
