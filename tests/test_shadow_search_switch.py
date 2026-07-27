from types import SimpleNamespace

import pytest

from src.db.queries import catalog


class _Conn:
    def __init__(self):
        self.call = None

    async def fetchrow(self, query, *args):
        self.call = (query, args)
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(False, "product"), (True, "search_document_v1")],
)
async def test_shadow_switch_selects_one_versioned_doc_type(monkeypatch, enabled, expected):
    monkeypatch.setattr(
        catalog,
        "get_settings",
        lambda: SimpleNamespace(model_embed="embed-model", search_shadow_enabled=enabled),
    )
    conn = _Conn()

    assert catalog.semantic_embedding_doc_type() == expected
    assert await catalog.has_embeddings(conn, "business-1") is False
    query, args = conn.call
    assert "business_id = $1 and doc_type = $2 and model = $3" in query
    assert args == ("business-1", expected, "embed-model")
