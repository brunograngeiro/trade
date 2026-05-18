"""Kalshi v2 request signing (RSA-PSS-SHA256)."""

from __future__ import annotations

import base64
import time
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


@lru_cache(maxsize=4)
def _load_private_key(path: str):
    pem = Path(path).read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def sign_request(private_key_path: str, method: str, signed_path: str) -> tuple[str, str]:
    """Return (timestamp_ms, base64_signature) for a Kalshi v2 request.

    signed_path must start with /trade-api/v2/...
    """
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{signed_path}".encode("utf-8")
    key = _load_private_key(private_key_path)
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return timestamp, base64.b64encode(signature).decode("utf-8")
