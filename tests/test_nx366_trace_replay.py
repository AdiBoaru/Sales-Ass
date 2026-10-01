"""NX-366 felia B — un tur capturat se rejoacă pe cod, fără model și fără scrieri.

Testele rulează turul de DOUĂ ori prin aceleași funcții ca producția (`prepare_turn_context` +
`run_pipeline`): o dată cu un transport fals care joacă furnizorul, cu captura aprinsă, și o dată
prin `replay_turn` pe traceul rezultat. Stagiul de test cheamă modelul prin `LLMClient`-ul REAL,
deci captura și reconstrucția obiectelor de răspuns sunt cele de producție.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from typing import Any

import pytest
from openai.types.chat import ChatCompletion

from src.agent.llm import LLMClient
from src.config import get_settings
from src.evals import trace_replay
from src.models import BusinessConfig, Contact
from src.worker import turn_capture
from src.worker.processor import prepare_turn_context
from src.worker.runner import PipelineDeps, run_pipeline
from src.worker.turn_uow import TurnLoadSnapshot

BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"


def _completion(content: str) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-6-luna",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )


class _Provider:
    """Furnizorul fals: întoarce pe rând răspunsurile date (sau ridică o eroare dată)."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _client(provider: _Provider) -> Any:
    return NS(chat=NS(completions=provider), responses=None, moderations=None, embeddings=None)


class _Conn:
    """Conexiune falsă cu tranzacții; `fail` = instrucțiunea de sondă pică (tranzacție anulată)."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.rollbacks = 0

    def transaction(self, readonly: bool = False):
        assert readonly is True
        conn = self

        class _Tx:
            async def start(self):
                return None

            async def rollback(self):
                conn.rollbacks += 1

        return _Tx()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if self.fail:
            raise RuntimeError("current transaction is aborted")
        return 1


def _db(conn: _Conn):
    @asynccontextmanager
    async def _cm(operation: str = "x"):
        yield conn

    return _cm


async def _schema_stage(ctx, deps) -> None:
    """Agentul în miniatură: UN apel de schemă prin clientul real; o eroare de furnizor cade pe
    un text de rezervă (ca fallback-ul real), restul devine răspunsul turului."""
    try:
        reply = await deps.llm.complete_schema_raw(
            "sys", ctx.message.body, {"name": "rich_reply", "schema": {"type": "object"}}
        )
        text = json.loads(reply.content)["text"]
    except Exception:  # noqa: BLE001
        text = "fallback"
    ctx.set_reply(f"{text} | {ctx.state.displayed_products[0].name}")


@pytest.fixture
def capture_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "conversation_trace_enabled", True)
    monkeypatch.setattr(s, "trace_model_io_enabled", True)
    monkeypatch.setattr(s, "llm_retry_max", 0)
    monkeypatch.setattr(s, "web_context_enabled", False)
    return s


def _business() -> BusinessConfig:
    return BusinessConfig(id=BIZ, slug="sole-ro", name="SOLE", settings={"domain_pack": {"k": 1}})


def _snap() -> TurnLoadSnapshot:
    return TurnLoadSnapshot(
        deduped=False,
        contact=Contact(id="c1", business_id=BIZ),
        conversation_id="conv1",
        state={"displayed_products": [{"product_id": "p1", "name": "Crema", "price": 90.0}]},
        state_version=2,
        locale="ro",
    )


async def _produce(provider: _Provider, stages) -> dict[str, Any]:
    """Turul „de producție": aceleași funcții ca `_run_turn`, cu captura aprinsă."""
    business, snap = _business(), _snap()
    event = {"content_type": "text", "channel_kind": "webchat"}
    db = _db(_Conn())
    llm = LLMClient(_client(provider), model_agent="gpt-6-luna")
    ctx = await prepare_turn_context(
        db,
        business,
        event,
        snap,
        turn_id="t1",
        raw_body="vreau o crema",
        safe_body="vreau o crema",
        channel_id="ch1",
        channel_kind="webchat",
        verified_customer_ref=None,
    )
    pending = turn_capture.begin_turn_input(snap, event, business, channel_id="ch1", verified=False)
    await run_pipeline(ctx, PipelineDeps(db=db, redis=None, llm=llm), stages)
    turn_capture.finish_turn_input(ctx, pending)
    return {
        "turn_id": "t1",
        "business_id": BIZ,
        "client_text": "vreau o crema",
        "reply": json.loads(json.dumps(ctx.reply.__dict__, default=str)),
        "diagnostics": json.loads(json.dumps(ctx.trace, default=str)),
    }


async def test_replay_reproduces_the_recorded_reply(capture_on):
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])
    assert row["diagnostics"]["model_io"]["replayable"] is True
    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[_schema_stage]
    )
    assert res.status == "replayed", res
    assert res.reply["text"] == row["reply"]["text"] == "Uite | Crema"
    assert trace_replay.reply_fingerprint(res.reply) == trace_replay.reply_fingerprint(row["reply"])
    assert res.drift == {"pack": False}


async def test_recorded_provider_error_replays_the_same_fallback(capture_on):
    row = await _produce(_Provider(TimeoutError("lent")), [_schema_stage])
    assert row["reply"]["text"] == "fallback | Crema"
    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[_schema_stage]
    )
    assert res.status == "replayed" and res.reply["text"] == "fallback | Crema"


async def test_an_extra_model_call_is_divergence_not_an_invented_answer(capture_on):
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])

    async def twice(ctx, deps):
        await _schema_stage(ctx, deps)
        ctx.reply = None
        await _schema_stage(ctx, deps)  # un apel pe care înregistrarea nu-l are

    res = await trace_replay.replay_turn(row, db=_db(_Conn()), business=_business(), stages=[twice])
    assert res.status == "diverged"
    assert res.divergence["index"] == 1 and res.divergence["recorded"] is None
    assert res.reply["text"] == "fallback | Crema"  # codul a înghițit excepția, statusul nu


async def test_a_different_request_kind_is_divergence(capture_on):
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])

    async def other_schema(ctx, deps):
        try:
            await deps.llm.complete_schema_raw("s", "u", {"name": "turn_interpretation"})
        except Exception:  # noqa: BLE001
            pass
        ctx.set_reply("x")

    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[other_schema]
    )
    assert res.status == "diverged"
    assert res.divergence["wanted"]["schema"] == "turn_interpretation"
    assert res.divergence["recorded"]["schema"] == "rich_reply"


async def test_fewer_calls_than_recorded_is_shortened_not_identical(capture_on):
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])

    async def no_model(ctx, deps):
        ctx.set_reply("fără model")

    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[no_model]
    )
    assert res.status == "shortened" and res.unused_calls == 1


async def test_a_failed_statement_in_a_checkout_is_reported(capture_on):
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])

    async def touches_db(ctx, deps):
        async with deps.db("write_attempt"):
            pass
        await _schema_stage(ctx, deps)

    conn = _Conn(fail=True)
    res = await trace_replay.replay_turn(
        row, db=_db(conn), business=_business(), stages=[touches_db]
    )
    assert res.status == "db_aborted"
    assert conn.rollbacks >= 1  # fiecare checkout se anulează, inclusiv cel eșuat


async def test_rows_without_capture_are_not_replayable():
    v_in, v_io = turn_capture.FORMAT_VERSION, trace_replay.model_io.FORMAT_VERSION
    for diag, reason in [
        ({}, "no_turn_input"),
        ({"turn_input": {"v": v_in, "error": "oversize"}}, "turn_input_unusable"),
        ({"turn_input": {"v": v_in - 1}}, "turn_input_unusable"),
        ({"turn_input": {"v": v_in}}, "no_model_io"),
        ({"turn_input": {"v": v_in}, "model_io": {"v": v_io - 1}}, "model_io_unknown_version"),
        (
            {
                "turn_input": {"v": v_in},
                "model_io": {"v": v_io, "replayable": False, "unreplayable": ["concurrent"]},
            },
            "model_io_concurrent",
        ),
    ]:
        res = await trace_replay.replay_turn(
            {"turn_id": "t", "business_id": BIZ, "diagnostics": diag}, db=_db(_Conn())
        )
        assert (res.status, res.reason) == ("not_replayable", reason)


async def test_business_comes_from_the_row_not_from_the_recording(capture_on):
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])
    other = BusinessConfig(id="00000000-0000-0000-0000-000000000001", slug="x", name="x")
    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=other, stages=[_schema_stage]
    )
    assert (res.status, res.reason) == ("not_replayable", "business_not_found")


def test_recorded_settings_apply_and_restore(capture_on):
    s = get_settings()
    before = s.card_slots
    with trace_replay.settings_profile({"card_slots": before + 1, "gone_flag": True}):
        assert s.card_slots == before + 1 and s.llm_retry_max == 0
    assert s.card_slots == before


def test_a_setting_missing_from_the_recording_takes_the_code_default_not_the_local_env(
    capture_on, monkeypatch
):
    """Recenzia: un `.env` local (creierul unic aprins) nu are voie să schimbe calea rejucată."""
    s = get_settings()
    default = type(s).model_fields["single_brain_enabled"].default
    monkeypatch.setattr(s, "single_brain_enabled", not default)  # ce ar pune `.env`-ul local
    with trace_replay.settings_profile({}):
        assert s.single_brain_enabled is default
    assert s.single_brain_enabled is (not default)
    with trace_replay.settings_profile(None):  # `--flags current` = profilul local
        assert s.single_brain_enabled is (not default)


def _moderation(flagged: bool):
    from openai.types import ModerationCreateResponse  # noqa: PLC0415
    from openai.types.moderation import Categories  # noqa: PLC0415

    names = [f.alias or n for n, f in Categories.model_fields.items()]
    return ModerationCreateResponse.model_validate(
        {
            "id": "m",
            "model": "omni-moderation-latest",
            "results": [
                {
                    "flagged": flagged,
                    "categories": {n: flagged and n == "violence" for n in names},
                    "category_scores": {n: 0.0 for n in names},
                    "category_applied_input_types": {n: ["text"] for n in names},
                }
            ],
        }
    )


async def _moderation_stage(ctx, deps) -> None:
    """Ca poarta reală: moderarea e fail-open, deci o reconstrucție picată ar trece neobservată."""
    try:
        flagged = (await deps.llm.moderate(ctx.message.body)).flagged
    except Exception:  # noqa: BLE001
        flagged = False
    ctx.set_reply("neutru" if flagged else "normal")


async def _produce_moderated(flagged: bool) -> dict[str, Any]:
    business, snap = _business(), _snap()
    db = _db(_Conn())
    client = NS(chat=None, responses=None, moderations=_Provider(_moderation(flagged)))
    llm = LLMClient(client, model_agent="gpt-6-luna")
    ctx = await prepare_turn_context(
        db,
        business,
        {"content_type": "text"},
        snap,
        turn_id="t2",
        raw_body="ma mananca",
        safe_body="ma mananca",
        channel_id="ch1",
        channel_kind="webchat",
        verified_customer_ref=None,
    )
    pending = turn_capture.begin_turn_input(snap, {}, business, channel_id="", verified=False)
    await run_pipeline(ctx, PipelineDeps(db=db, redis=None, llm=llm), [_moderation_stage])
    turn_capture.finish_turn_input(ctx, pending)
    return {
        "turn_id": "t2",
        "business_id": BIZ,
        "client_text": "ma mananca",
        "reply": {"text": ctx.reply.text},
        "diagnostics": json.loads(json.dumps(ctx.trace, default=str)),
    }


async def test_flagged_moderation_replays_as_flagged(capture_on):
    """Câmpurile cu alias ale moderării („sexual/minors") trebuie să supraviețuiască înregistrării:
    prima versiune le pierdea, iar replay-ul cădea pe fail-open (prins pe pipeline-ul real)."""
    row = await _produce_moderated(flagged=True)
    assert row["reply"]["text"] == "neutru"
    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[_moderation_stage]
    )
    assert (res.status, res.reply["text"]) == ("replayed", "neutru")


async def test_unreconstructable_recording_is_divergence_not_fallback(capture_on):
    row = await _produce_moderated(flagged=True)
    rec = row["diagnostics"]["model_io"]["calls"][0]
    rec["response"]["results"][0].pop("categories")  # înregistrare stricată
    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[_moderation_stage]
    )
    assert res.status == "diverged"
    assert res.divergence["wanted"]["reconstruct"] == "failed"
    assert res.reply["text"] == "normal"  # fallback-ul a rulat; statusul îl spune


async def test_capture_off_leaves_the_turn_untouched(monkeypatch):
    """Captura stinsă (implicitul de cod): nici `model_io`, nici `turn_input`, nici evenimentul."""
    s = get_settings()
    monkeypatch.setattr(s, "conversation_trace_enabled", False)
    monkeypatch.setattr(s, "llm_retry_max", 0)
    monkeypatch.setattr(s, "web_context_enabled", False)
    business, snap = _business(), _snap()
    db = _db(_Conn())
    llm = LLMClient(_client(_Provider(_completion('{"text": "Uite"}'))), model_agent="m")
    ctx = await prepare_turn_context(
        db,
        business,
        {},
        snap,
        turn_id="t3",
        raw_body="x",
        safe_body="x",
        channel_id="",
        channel_kind="webchat",
        verified_customer_ref=None,
    )
    pending = turn_capture.begin_turn_input(snap, {}, business, channel_id="", verified=False)
    await run_pipeline(ctx, PipelineDeps(db=db, redis=None, llm=llm), [_schema_stage])
    turn_capture.finish_turn_input(ctx, pending)
    assert "model_io" not in ctx.trace and "turn_input" not in ctx.trace
    assert not [e for e in ctx.events if e.type == "model_io_captured"]


async def test_a_changed_model_input_is_reported_not_hidden(capture_on):
    """Recenzia: același fel de cerere peste altă intrare (alt prompt, alt set de produse) juca
    răspunsul înregistrat peste altă întrebare, fără să spună."""
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])

    async def other_prompt(ctx, deps):
        ctx.message.body = "altă întrebare"
        await _schema_stage(ctx, deps)

    res = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[other_prompt]
    )
    assert res.status == "replayed" and res.inputs_changed == [0]
    same = await trace_replay.replay_turn(
        row, db=_db(_Conn()), business=_business(), stages=[_schema_stage]
    )
    assert same.inputs_changed == []


async def test_recorded_error_comes_back_with_the_same_class(capture_on):
    """Recenzia: o clasă generică schimba `failure_cause` și evenimentele turului."""
    seen: list[str] = []

    async def remember(ctx, deps):
        try:
            await deps.llm.complete_schema_raw("s", ctx.message.body, {"name": "rich_reply"})
        except Exception as exc:  # noqa: BLE001
            seen.append(type(exc).__name__)
        ctx.set_reply("x")

    row = await _produce(_Provider(TimeoutError("lent")), [remember])
    await trace_replay.replay_turn(row, db=_db(_Conn()), business=_business(), stages=[remember])
    assert seen == ["TimeoutError", "TimeoutError"]


def test_status_errors_and_cancellations_are_rebuilt():
    import openai  # noqa: PLC0415

    err = trace_replay.rebuild_error({"error": "RateLimitError", "status": 429})
    assert isinstance(err, openai.RateLimitError) and err.status_code == 429
    assert isinstance(
        trace_replay.rebuild_error({"error": "APITimeoutError"}), openai.APITimeoutError
    )
    assert isinstance(
        trace_replay.rebuild_error({"error": "CancelledError", "cancelled": True}), TimeoutError
    )
    assert isinstance(
        trace_replay.rebuild_error({"error": "Weird"}), trace_replay.ReplayedProviderError
    )


class _ReadOnlyError(Exception):
    sqlstate = "25006"


class _WritingConn(_Conn):
    async def execute(self, sql: str, *args: Any) -> Any:
        raise _ReadOnlyError("cannot execute INSERT in a read-only transaction")


async def test_a_swallowed_write_is_reported_even_inside_a_savepoint(capture_on):
    """Recenzia: printr-un `db_tx` scrierea rulează într-un SAVEPOINT; rollback-ul lui lasă
    tranzacția exterioară sănătoasă, deci sonda de final nu vedea nimic."""
    row = await _produce(_Provider(_completion('{"text": "Uite"}')), [_schema_stage])

    async def writes(ctx, deps):
        async with deps.db("cart_add") as conn:
            try:
                await conn.execute("insert into conversation_carts default values")
            except Exception:  # noqa: BLE001 — apelantul înghite, ca `CartService`
                pass
        await _schema_stage(ctx, deps)

    res = await trace_replay.replay_turn(
        row, db=_db(_WritingConn()), business=_business(), stages=[writes]
    )
    assert res.status == "wrote"
