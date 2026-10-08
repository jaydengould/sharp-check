"""Signed read-only GET clients for Kalshi and Polymarket US. See docs/kalshi.md and docs/polymarket-us.md."""
import base64
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

KALSHI = "https://external-api.kalshi.com"
KALSHI_PREFIX = "/trade-api/v2"
PM_API = "https://api.polymarket.us"
PM_GATEWAY = "https://gateway.polymarket.us"
TIMEOUT = 30

_kalshi_key = None
_pm_key = None


def _kalshi_sign(msg: bytes) -> str:
    global _kalshi_key
    if _kalshi_key is None:
        _kalshi_key = serialization.load_der_private_key(base64.b64decode(os.environ["KALSHI_PRIVATE_KEY"]), None)
    if isinstance(_kalshi_key, rsa.RSAPrivateKey):
        sig = _kalshi_key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        sig = _kalshi_key.sign(msg)
    return base64.b64encode(sig).decode()


def _pm_sign(msg: bytes) -> str:
    global _pm_key
    if _pm_key is None:
        _pm_key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ["POLYMARKET_US_SECRET_KEY"])[:32])
    return base64.b64encode(_pm_key.sign(msg)).decode()


def kalshi_get(path, params=None):
    """path is relative to /trade-api/v2. The query string is not signed."""
    full = KALSHI_PREFIX + path
    ts = str(int(time.time() * 1000))
    return requests.get(KALSHI + full, params=params, timeout=TIMEOUT, headers={
        "KALSHI-ACCESS-KEY": os.environ["KALSHI_API_KEY_ID"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": _kalshi_sign((ts + "GET" + full).encode()),
    })


def pm_get(path, params=None):
    """Authenticated Polymarket US GET. Sign the bare path; signing the query string returns 401."""
    ts = str(int(time.time() * 1000))
    return requests.get(PM_API + path, params=params, timeout=TIMEOUT, headers={
        "X-PM-Access-Key": os.environ["POLYMARKET_US_KEY_ID"],
        "X-PM-Timestamp": ts,
        "X-PM-Signature": _pm_sign((ts + "GET" + path).encode()),
    })


def gateway_get(path, params=None):
    """Public Polymarket US market data. No auth."""
    return requests.get(PM_GATEWAY + path, params=params, timeout=TIMEOUT)


def kalshi_pages(path, params=None, limit=100):
    """Yield (params, body) for every page. Raises on HTTP errors."""
    cursor = None
    while True:
        p = {**(params or {}), "limit": limit, **({"cursor": cursor} if cursor else {})}
        r = kalshi_get(path, p)
        r.raise_for_status()
        body = r.json()
        yield p, body
        cursor = body.get("cursor")
        if not cursor:
            return


def pm_pages(path, params=None, limit=100):
    """Yield (params, body) for every page. Raises on HTTP errors."""
    cursor = None
    while True:
        p = {**(params or {}), "limit": limit, **({"cursor": cursor} if cursor else {})}
        r = pm_get(path, p)
        r.raise_for_status()
        body = r.json()
        yield p, body
        cursor = body.get("nextCursor")
        if body.get("eof") or not cursor:
            return
