"""Browser notifications (Web Push), with nothing but the ``cryptography`` package.

A browser that turns on notifications hands Kalends a *subscription*: an address at its
push service (Apple, Google, Mozilla...) and two keys. Kalends encrypts each message for
that browser alone (RFC 8291, "aes128gcm"), so the push service only passes it on, and
signs the request with its own key (VAPID, RFC 8292) so the service knows who sent it.

Kalends' signing key is made the first time it's needed and kept in ``.env``.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECORD_SIZE = 4096
MAX_PAYLOAD = 3000  # well under what every push service accepts


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_private_key() -> str:
    """A new VAPID signing key, as the base64url of its 32-byte private number."""
    key = ec.generate_private_key(ec.SECP256R1())
    return b64e(key.private_numbers().private_value.to_bytes(32, "big"))


def _private(key_b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64d(key_b64), "big"), ec.SECP256R1())


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key(key_b64: str) -> str:
    """The key browsers subscribe with (``applicationServerKey``)."""
    return b64e(_raw_public(_private(key_b64).public_key()))


def vapid_header(endpoint: str, key_b64: str, contact: str, ttl_seconds: int = 12 * 3600) -> str:
    """The ``Authorization`` header for a push to ``endpoint``."""
    parts = urlsplit(endpoint)
    audience = f"{parts.scheme}://{parts.netloc}"
    claims = {"aud": audience, "exp": int(time.time()) + ttl_seconds, "sub": contact}
    head = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64e(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    der = _private(key_b64).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = b64e(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={public_key(key_b64)}"


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """The message body for one browser (RFC 8291 + RFC 8188, a single record)."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("notification too long")
    ua_public = b64d(p256dh)
    auth_secret = b64d(auth)
    if len(ua_public) != 65 or len(auth_secret) < 16:
        raise ValueError("that browser's keys don't look right")
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    sender = ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(sender.public_key())
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=b"WebPush: info\x00" + ua_public + as_public,
    ).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    sealed = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02: the last (only) record
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + sealed


@dataclass
class PushResult:
    ok: bool
    gone: bool  # the browser unsubscribed or the address expired: forget it
    detail: str


def send(
    client: httpx.Client,
    sub: dict,
    message: dict,
    key_b64: str,
    contact: str,
    ttl: int = 24 * 3600,
    urgency: str = "normal",
) -> PushResult:
    """Push ``message`` (title, body, url, tag) to one browser subscription."""
    endpoint = str(sub.get("endpoint") or "")
    if not endpoint.startswith("https://"):
        return PushResult(False, True, "not a push address")
    body = encrypt(json.dumps(message, ensure_ascii=False).encode("utf-8"), sub["p256dh"], sub["auth"])
    headers = {
        "Authorization": vapid_header(endpoint, key_b64, contact),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
    }
    try:
        r = client.post(endpoint, content=body, headers=headers, timeout=15)
    except httpx.HTTPError as exc:
        return PushResult(False, False, f"Couldn't reach the browser's push service ({type(exc).__name__}).")
    if r.status_code in (200, 201, 202):
        return PushResult(True, False, "Sent")
    if r.status_code in (404, 410):
        return PushResult(False, True, "That browser turned notifications off.")
    return PushResult(False, False, f"The push service answered HTTP {r.status_code}.")
