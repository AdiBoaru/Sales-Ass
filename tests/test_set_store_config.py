"""NX-99 — set_store_config (config jsonb store_base_url + checkout_url). Stub conn, ZERO DB."""

import importlib.util
import pathlib

# Scriptul stă în scripts/ (nu pachet importabil) — îl încărcăm prin spec.
_SPEC = importlib.util.spec_from_file_location(
    "set_store_config",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "set_store_config.py",
)
ssc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ssc)


class _FakeConn:
    def __init__(self, result="UPDATE 1"):
        self.result = result
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return self.result


def test_derive_with_explicit_checkout():
    assert ssc._derive("https://x.ro", "https://x.ro/cos") == ("https://x.ro", "https://x.ro/cos")


def test_derive_defaults_checkout_to_cart():
    assert ssc._derive("https://x.ro", None) == ("https://x.ro", "https://x.ro/cart")


def test_derive_normalizes_trailing_slash():
    # store cu `/` final → fără `//cart`; checkout cu `/` final → fără `/` final
    assert ssc._derive("https://x.ro/", None) == ("https://x.ro", "https://x.ro/cart")
    assert ssc._derive("https://x.ro/", "https://x.ro/cos/") == ("https://x.ro", "https://x.ro/cos")


async def test_set_store_config_calls_jsonb_set_with_both_keys():
    conn = _FakeConn("UPDATE 1")
    n = await ssc.set_store_config(conn, "biz-1", "https://x.ro", "https://x.ro/cart")
    assert n == 1
    sql, args = conn.calls[0]
    assert "store_base_url" in sql and "checkout_url" in sql and "jsonb_set" in sql
    assert args == ("biz-1", "https://x.ro", "https://x.ro/cart")  # scoped pe business_id ($1)


async def test_set_store_config_derives_checkout_when_absent():
    conn = _FakeConn("UPDATE 1")
    await ssc.set_store_config(conn, "biz-1", "https://x.ro")
    assert conn.calls[0][1] == ("biz-1", "https://x.ro", "https://x.ro/cart")


async def test_set_store_config_zero_rows_when_business_missing():
    conn = _FakeConn("UPDATE 0")
    n = await ssc.set_store_config(conn, "nope", "https://x.ro")
    assert n == 0  # business_id greșit → 0 rânduri (main raportează + exit non-zero)
