import pytest

from scripts import embed_search_documents


@pytest.mark.asyncio
async def test_shadow_embedding_runner_requires_explicit_llm_credentials(monkeypatch, capsys):
    monkeypatch.setattr(embed_search_documents, "get_llm", lambda: None)

    assert await embed_search_documents.run("business-1", force=False, limit=0) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
