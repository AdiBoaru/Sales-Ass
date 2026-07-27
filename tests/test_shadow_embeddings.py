from contextlib import asynccontextmanager

from src.jobs.embed_products import _content_hash, embed_shadow_pending


class _Conn:
    def __init__(self, existing=None):
        self.existing = existing
        self.executed = []
        self.fetched = None

    async def fetch(self, query, *args):
        self.fetched = (query, args)
        return [
            {
                "id": "product-1",
                "business_id": "business-1",
                "text": "Document pozitiv pentru cautare.",
                "existing": self.existing,
            }
        ]

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _LLM:
    model_embed = "embed-model"

    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        self.calls.append(texts)
        return [[0.1, 0.2] for _ in texts]


async def test_shadow_embeddings_use_versioned_doc_type_and_tenant_scope():
    conn = _Conn()
    llm = _LLM()

    done = await embed_shadow_pending(conn, llm, "business-1")

    assert done == 1
    assert llm.calls == [["Document pozitiv pentru cautare."]]
    fetch_query, fetch_args = conn.fetched
    assert "d.business_id = $2" in fetch_query
    assert fetch_args == ("embed-model", "business-1", 1)
    query, args = conn.executed[0]
    assert "'search_document_v1'" in query
    assert "business_id" in query
    assert args[0:3] == ("product-1", "business-1", "embed-model")


async def test_shadow_embeddings_skip_document_with_matching_content_hash():
    text = "Document pozitiv pentru cautare."
    conn = _Conn(existing=_content_hash(text, "embed-model"))
    llm = _LLM()

    done = await embed_shadow_pending(conn, llm, "business-1")

    assert done == 0
    assert llm.calls == []
    assert conn.executed == []
