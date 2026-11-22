"""Kraken Spot REST request signing (docs: exchange/guides/rest/authentication).

API-Sign = base64( HMAC-SHA512( key=base64decode(secret), msg=uri_path + SHA256(nonce + post_data) ) )
where post_data is exactly the url-encoded body that is sent.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

__all__ = ["decode_secret", "sign"]


def decode_secret(secret_b64: str) -> bytes:
    try:
        key = base64.b64decode(secret_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("KRAKEN_API_SECRET is not valid base64") from exc
    if len(key) < 32:
        raise ValueError("KRAKEN_API_SECRET decodes to fewer than 32 bytes")
    return key


def sign(uri_path: str, nonce: int, post_data: str, secret_key: bytes) -> str:
    sha = hashlib.sha256((str(nonce) + post_data).encode()).digest()
    mac = hmac.new(secret_key, uri_path.encode() + sha, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()
