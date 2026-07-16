"""Teste unit pentru jobul de embeddings — helpers + adaptor (mock, fără apeluri reale)."""

from src.agent.llm import LLMClient
from src.jobs.embed_products import _content_hash, _embed_text


def test_embed_text_composition():
    # NX-170: doc determinist din faptele canonice (attributes), nu concerns top-level.
    row = {
        "name": "Crema X",
        "brand": "BrandY",
        "category": "Creme hidratante",
        "ai_summary": "hidratare profundă",
        "attributes": {
            "concerns": ["dry"],
            "finish": "matte",
            "key_ingredients": ["acid hialuronic"],
        },
    }
    t = _embed_text(row)
    assert "Crema X" in t and "BrandY" in t and "Creme hidratante" in t
    assert "hidratare profundă" in t
    assert "Potrivit pentru: dry" in t  # concerns din attributes
    assert "Finish: matte" in t and "acid hialuronic" in t


def test_embed_text_handles_missing_fields():
    row = {"name": "Doar nume", "brand": None, "ai_summary": None, "concerns": None}
    assert _embed_text(row) == "Doar nume"


def test_content_hash_deterministic_and_model_sensitive():
    h1 = _content_hash("acelasi text", "model-a")
    h2 = _content_hash("acelasi text", "model-a")
    h3 = _content_hash("acelasi text", "model-b")
    assert h1 == h2  # determinist
    assert h1 != h3  # se schimbă cu modelul (re-embed la schimbare de model)


class _FakeEmbeddings:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.last_call: tuple | None = None

    async def create(self, *, model, input):
        self.last_call = (model, list(input))
        data = [type("D", (), {"embedding": [0.1] * self.dim})() for _ in input]
        return type("Resp", (), {"data": data})()


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddings()


async def test_adapter_embed_returns_vectors_and_uses_embed_model():
    client = _FakeClient()
    llm = LLMClient(client, model_triage="n", model_agent="m", model_embed="emb-model")
    vecs = await llm.embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert len(vecs[0]) == 4
    assert client.embeddings.last_call[0] == "emb-model"  # folosește model_embed
    assert client.embeddings.last_call[1] == ["a", "b", "c"]  # batch
