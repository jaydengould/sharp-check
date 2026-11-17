"""Signed read-only GET clients for Kalshi, Polymarket US and Novig. See docs/kalshi.md, docs/polymarket-us.md, docs/novig.md."""
import base64
import hashlib
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

KALSHI = "https://external-api.kalshi.com"
KALSHI_PREFIX = "/trade-api/v2"
PM_API = "https://api.polymarket.us"
PM_GATEWAY = "https://gateway.polymarket.us"
NOVIG = "https://api.novig.com"
TIMEOUT = 30

_kalshi_key = None
_pm_key = None
_novig_key = None


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


# ---- Novig (read-only management::read key; docs/novig.md) ----

def novig_key(text):
    """Private key text from .env: a PEM (literal \\n ok), base64 PKCS#8 DER, or a base64 raw 32-byte Ed25519 seed."""
    text = text.strip().strip('"').replace("\\n", "\n")
    if text.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(text.encode(), None)
    raw = base64.b64decode(text)
    if len(raw) == 32:
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw)
    return serialization.load_der_private_key(raw, None)


def novig_canon(ts, method, path, params, body=b""):
    """NOVIG-V3 string to sign. Params are ours (never pre-encoded), so quote + sort is the canonical query."""
    q = "&".join(sorted(f"{quote(str(k), safe='-._~')}={quote(str(v), safe='-._~')}" for k, v in (params or {}).items()))
    return "\n".join(["NOVIG-V3", ts, method, path, q, hashlib.sha256(body).hexdigest()])


def novig_sign(key, s):
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return base64.b64encode(key.sign(s.encode())).decode()
    return base64.b64encode(key.sign(s.encode(), ec.ECDSA(hashes.SHA256()))).decode()  # P-256, DER


def novig_get(path, params=None):
    """Signed Novig GET. Every call is location-checked: a 451 means a VPN or a stale phone geolocation."""
    global _novig_key
    if _novig_key is None:
        _novig_key = novig_key(os.environ["NOVIG_READ_PRIVATE_KEY"])
    ts = str(int(time.time() * 1000))
    r = requests.get(NOVIG + path, params=params, timeout=TIMEOUT, headers={
        "Novig-Key-Id": os.environ["NOVIG_READ_KEY_ID"],
        "Novig-Timestamp": ts,
        "Novig-Signature": novig_sign(_novig_key, novig_canon(ts, "GET", path, params)),
    })
    if r.status_code == 451:
        raise requests.HTTPError("Novig location check failed (open the Novig app; no VPN)")
    return r


def novig_pages(path, params=None, limit=500, get=None):
    """Yield (params, page) for every page; page = {"items": [...], ...}. A management-family key's personal reads
    come keyed by wallet key ID: unwrapped here, and paging past page 1 then needs keyId with after. More than one
    wallet (a subaccount) isn't supported: it would mix wallets the cash ledger and balance don't cover."""
    get = get or novig_get
    p = {**(params or {}), "limit": limit}
    while True:
        r = get(path, p)
        r.raise_for_status()
        body = r.json()
        if "items" not in body:
            if len(body) != 1:
                raise ValueError("Novig: more than one wallet")
            (wallet, body), = body.items()
            p = {**p, "keyId": wallet}
        yield p, body
        if not body.get("next"):
            return
        p = {**p, "after": body["next"]}
