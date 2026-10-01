"""NX-366 felia A — ce iese din model și cu ce pornește turul se înregistrează, complet și sigur."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
from openai.types import CreateEmbeddingResponse, ModerationCreateResponse
from openai.types.chat import ChatCompletion

from src.agent import model_io
from src.agent.llm import LLMClient
from src.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
LLM_SRC = ROOT / "src" / "agent" / "llm.py"


# --- poarta mecanică: niciun apel la furnizor fără captură ---------------------------------------


def _endpoint(node: ast.AST) -> str | None:
    """`self._client.a.b.create` → „a.b”; altfel None."""
    if not (isinstance(node, ast.Attribute) and node.attr == "create"):
        return None
    parts: list[str] = []
    cur: ast.AST = node.value
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not (isinstance(cur, ast.Name) and cur.id == "self" and parts and parts[-1] == "_client"):
        return None
    return ".".join(reversed(parts[:-1]))


def uncaptured_provider_calls(source: str) -> list[str]:
    """Fiecare `self._client.<endpoint>.create` trebuie să stea într-o metodă care îl trece prin
    `_guarded` sau `_recorded`, pe un endpoint din vocabularul închis `model_io.ENDPOINTS`."""
    bad: list[str] = []
    tree = ast.parse(source)
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.unparse(fn)
            for node in ast.walk(fn):
                ep = _endpoint(node)
                if ep is None:
                    continue
                if ep not in model_io.ENDPOINTS:
                    bad.append(f"{fn.name}: endpoint necunoscut {ep}")
                elif "self._guarded(" not in body and "_recorded(" not in body:
                    bad.append(f"{fn.name}: {ep} fără captură")
    return bad


def test_every_provider_call_in_llm_is_captured():
    assert uncaptured_provider_calls(LLM_SRC.read_text(encoding="utf-8")) == []


def test_gate_fails_on_an_uncaptured_call():
    """Exemplul care TREBUIE să pice: o metodă nouă care cheamă furnizorul direct."""
    src = (
        "class LLMClient:\n"
        "    async def speak(self, text):\n"
        "        return await self._client.audio.speech.create(input=text)\n"
        "    async def moderate2(self, text):\n"
        "        return await self._client.moderations.create(input=text)\n"
    )
    bad = uncaptured_provider_calls(src)
    assert any("audio.speech" in b for b in bad)
    assert any("moderate2" in b and "fără captură" in b for b in bad)


# --- acumulatorul ---------------------------------------------------------------------------------


def _completion(content: str = "salut", tool_args: dict | None = None) -> ChatCompletion:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_args is not None:
        msg["content"] = None
        msg["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_products", "arguments": json.dumps(tool_args)},
            }
        ]
    return ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-6-luna",
            "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def test_no_accumulator_means_no_capture():
    assert model_io.current() is None
    model_io.record_ok("chat.completions", {}, _completion())  # nu ridică, nu înregistrează


def test_request_key_names_shape_purpose_schema_and_tools_not_prompt():
    kwargs = {
        "model": "m",
        "messages": [{"role": "user", "content": "PROMPT SECRET"}],
        "tools": [{"type": "function", "function": {"name": "faq_lookup"}}],
    }
    key = model_io.request_key("chat.completions", kwargs)
    assert key == {"shape": "tools", "purpose": None, "schema": None, "tools": ["faq_lookup"]}
    schema_kw = {
        "model": "m",
        "messages": [],
        "response_format": {"type": "json_schema", "json_schema": {"name": "turn_interpretation"}},
    }
    key = model_io.request_key("chat.completions", schema_kw)
    assert key["shape"] == "schema" and key["schema"] == "turn_interpretation"
    assert key["purpose"] == "interpret"
    assert "PROMPT SECRET" not in json.dumps(key)


def test_ok_and_error_rows_in_call_order():
    acc, tok = model_io.push()
    try:
        model_io.record_ok("chat.completions", {"model": "m", "messages": []}, _completion())
        model_io.record_error("moderations", None, TimeoutError("detalii cu text de client"))
    finally:
        model_io.pop(tok)
    doc = acc.as_trace()
    assert [c["i"] for c in doc["calls"]] == [0, 1]
    assert doc["calls"][0]["ok"] is True
    assert doc["calls"][0]["response"]["choices"][0]["message"]["content"] == "salut"
    assert doc["calls"][1] == {
        "endpoint": "moderations",
        "shape": None,
        "purpose": None,
        "schema": None,
        "tools": [],
        "input_sha": None,
        "ok": False,
        "error": "TimeoutError",
        "i": 1,
    }
    assert doc["replayable"] is True and doc["unreplayable"] == []


def test_cancellation_is_recorded_as_such():
    """Recenzia: interpretarea dark tăiată de `wait_for` nu lăsa niciun rând, deci replay-ul cerea
    un apel care „nu există" și fiecare astfel de tur ieșea divergent."""
    acc, tok = model_io.push()
    try:
        model_io.record_error("chat.completions", {}, asyncio.CancelledError())
    finally:
        model_io.pop(tok)
    (row,) = acc.calls
    assert row["error"] == "CancelledError" and row["cancelled"] is True


def test_concurrent_calls_make_the_turn_unreplayable():
    """Înregistrarea urmează ordinea în care s-au TERMINAT apelurile, replay-ul pe cea în care s-au
    CERUT: două apeluri deodată s-ar putea inversa. Azi nu există pe drumul turului; garda spune."""

    async def two_at_once():
        async def one():
            with model_io.in_flight():
                await asyncio.sleep(0)

        await asyncio.gather(one(), one())

    acc, tok = model_io.push()
    try:
        asyncio.run(two_at_once())
    finally:
        model_io.pop(tok)
    assert acc.as_trace()["unreplayable"] == ["concurrent"]


def test_sequential_calls_stay_replayable():
    async def in_turn():
        for _ in range(3):
            with model_io.in_flight():
                await asyncio.sleep(0)

    acc, tok = model_io.push()
    try:
        asyncio.run(in_turn())
    finally:
        model_io.pop(tok)
    assert acc.as_trace()["replayable"] is True


def test_oversized_response_is_marked_not_cut():
    acc, tok = model_io.push()
    try:
        model_io.record_ok("chat.completions", {"model": "m"}, _completion("x" * 70_000))
    finally:
        model_io.pop(tok)
    doc = acc.as_trace()
    assert doc["calls"][0]["response"]["truncated"] is True
    assert doc["replayable"] is False and doc["unreplayable"] == ["truncated"]


def test_turn_cap_marks_turn_not_replayable():
    acc, tok = model_io.push()
    try:
        for _ in range(6):
            model_io.record_ok("chat.completions", {"model": "m"}, _completion("y" * 50_000))
    finally:
        model_io.pop(tok)
    assert acc.bytes <= model_io.MAX_TURN_BYTES + 1024
    assert acc.as_trace()["replayable"] is False


def test_phone_in_tool_args_is_redacted_product_ids_are_not():
    acc, tok = model_io.push()
    pid = "fc07644e-9851-4672-8ed1-dc31bf29128e"
    try:
        model_io.record_ok(
            "chat.completions",
            {"model": "m"},
            _completion(tool_args={"query": "suna-ma la 0722123456", "product_id": pid}),
        )
    finally:
        model_io.pop(tok)
    doc = acc.as_trace()
    raw = json.dumps(doc, ensure_ascii=False)
    assert "0722123456" not in raw
    assert pid in raw
    # Recenzia: redactarea a schimbat ce a scris modelul, deci replay-ul ar juca alt argument decât
    # cel pe care l-a citit codul. Turul se declară nerejucabil, nu se rejoacă pe un text fals.
    assert doc["unreplayable"] == ["redacted"] and doc["calls"][0]["redacted"] is True


def test_provider_ids_are_never_redacted():
    """`chatcmpl-…` are cifre pe care detectorul de telefon le-ar citi ca număr; un id tehnic nu e
    vocea nimănui, iar a-l modifica ar marca nerejucabile ture perfect bune."""
    resp = _completion("salut").model_copy(update={"id": "chatcmpl-0722123456789"})
    acc, tok = model_io.push()
    try:
        model_io.record_ok("chat.completions", {"model": "m"}, resp)
    finally:
        model_io.pop(tok)
    doc = acc.as_trace()
    assert doc["calls"][0]["response"]["id"] == "chatcmpl-0722123456789"
    assert doc["replayable"] is True


def test_input_fingerprint_follows_what_the_model_reads():
    base = {"model": "m", "messages": [{"role": "user", "content": "a"}]}
    assert model_io.input_sha(base) == model_io.input_sha({**base, "model": "alt", "timeout": 3})
    other = {**base, "messages": [{"role": "user", "content": "b"}]}
    assert model_io.input_sha(base) != model_io.input_sha(other)


# --- prin clientul real ------------------------------------------------------------------------


class _Create:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _client(**endpoints: Any) -> Any:
    chat = NS(completions=endpoints.get("chat", _Create(_completion())))
    return NS(
        chat=chat,
        responses=endpoints.get("responses", _Create(None)),
        moderations=endpoints.get("moderations", _Create(None)),
        embeddings=endpoints.get("embeddings", _Create(None)),
    )


@pytest.fixture
def no_retry(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_retry_max", 0)


def test_schema_call_through_guarded_is_captured(no_retry):
    llm = LLMClient(_client(chat=_Create(_completion('{"ok": true}'))), model_agent="gpt-6-luna")
    acc, tok = model_io.push()
    try:
        schema = {"name": "turn_interpretation", "schema": {"type": "object"}}
        asyncio.run(llm.complete_schema_raw("sys", "user", schema))
    finally:
        model_io.pop(tok)
    (row,) = acc.calls
    assert row["endpoint"] == "chat.completions" and row["schema"] == "turn_interpretation"
    assert row["response"]["choices"][0]["message"]["content"] == '{"ok": true}'


def _moderation(*, flagged: bool, category: str) -> ModerationCreateResponse:
    """Răspuns de moderare complet (SDK-ul cere toate categoriile), o categorie aprinsă."""
    from openai.types.moderation import Categories  # noqa: PLC0415

    names = [f.alias or n for n, f in Categories.model_fields.items()]
    return ModerationCreateResponse.model_validate(
        {
            "id": "m",
            "model": "omni-moderation-latest",
            "results": [
                {
                    "flagged": flagged,
                    "categories": {n: n == category for n in names},
                    "category_scores": {n: 0.6 if n == category else 0.0 for n in names},
                    "category_applied_input_types": {n: ["text"] for n in names},
                }
            ],
        }
    )


def test_moderation_and_embedding_are_captured(no_retry):
    mod = _moderation(flagged=True, category="violence")
    emb = CreateEmbeddingResponse.model_validate(
        {
            "object": "list",
            "model": "text-embedding-3-small",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
    )
    llm = LLMClient(_client(moderations=_Create(mod), embeddings=_Create(emb)), model_agent="m")
    acc, tok = model_io.push()
    try:
        res = asyncio.run(llm.moderate("text"))
        asyncio.run(llm.embed(["a"]))
    finally:
        model_io.pop(tok)
    assert res.flagged is True
    assert [c["endpoint"] for c in acc.calls] == ["moderations", "embeddings"]
    assert acc.calls[0]["response"]["results"][0]["flagged"] is True


def test_failed_call_is_captured_and_still_raised(no_retry):
    llm = LLMClient(_client(moderations=_Create(RuntimeError("down"))), model_agent="m")
    acc, tok = model_io.push()
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(llm.moderate("text"))
    finally:
        model_io.pop(tok)
    assert acc.calls[0]["ok"] is False and acc.calls[0]["error"] == "RuntimeError"
