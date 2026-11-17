"""Novig signing and paging. No network."""
import base64

import pytest
from cryptography.hazmat.primitives import serialization

from sharp_check import clients

TEST_PEM = ("-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIJvNzamVA44euwrCHa4ZdHbACbiOx+HIYd07z4WUnIL3\n"
            "-----END PRIVATE KEY-----\n")  # Novig's published test key (docs signing-vectors.json), never registered


def test_novig_canon_and_sign_vectors():
    key = clients.novig_key(TEST_PEM)
    assert clients.novig_sign(key, clients.novig_canon("1755000000000", "GET", "/api/orders/all", None)) == \
        "Joy/3dQBha3GmPeSqlXLvx871R/vRcm6eEJsRja82vzdEyfXulwC15+T+fDwVzvRHnwHJRurOatFQfVpw2M2DA=="
    assert clients.novig_sign(key, clients.novig_canon("1755000000000", "GET", "/api/orders",
                                                       {"status": "open", "limit": "50"})) == \
        "D9/FzREoDCsoQwdcwUB2TnINgSoE+axZdcP8f/UbpCFd++Rdn4vmUhRtEKXHxKIT3YiFsvfZoMLBv/RN04yGCA=="


def test_novig_key_formats():
    raw = clients.novig_key(TEST_PEM).private_bytes_raw()
    der = clients.novig_key(TEST_PEM).private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption())
    for text in (TEST_PEM.replace("\n", "\\n"), f'"{TEST_PEM}"', base64.b64encode(der).decode(),
                 base64.b64encode(raw).decode()):
        assert clients.novig_key(text).private_bytes_raw() == raw


class Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


def test_novig_pages_unwraps_and_follows():
    calls = []
    pages = [{"K": {"items": [1], "next": "c1"}}, {"K": {"items": [2]}}]

    def get(path, params):
        calls.append(params)
        return Resp(pages[len(calls) - 1])

    out = list(clients.novig_pages("/v3/portfolio/fills", {"status": "X"}, limit=2, get=get))
    assert [p["items"] for _, p in out] == [[1], [2]]
    assert calls == [{"status": "X", "limit": 2}, {"status": "X", "limit": 2, "keyId": "K", "after": "c1"}]
    assert out[0][0]["keyId"] == "K"


def test_novig_pages_rejects_two_wallets():
    get = lambda path, params: Resp({"A": {"items": []}, "B": {"items": []}})
    with pytest.raises(ValueError, match="more than one wallet"):
        list(clients.novig_pages("/v3/portfolio/fills", get=get))


def test_novig_pages_bare():
    pages = [{"items": [1], "next": "c"}, {"items": [2]}]
    calls = []

    def get(path, params):
        calls.append(params)
        return Resp(pages[len(calls) - 1])

    assert [p["items"] for _, p in clients.novig_pages("/v3/account/transactions", get=get)] == [[1], [2]]
    assert calls[1] == {"limit": 500, "after": "c"}
