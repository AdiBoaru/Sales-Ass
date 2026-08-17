"""NX-246 (felia 2) — feedback: ce se poate minți într-un „👍" și de ce nu merge.

Testele urmăresc exact modelul de amenințare: un client care încearcă să voteze pentru turul
altcuiva, din altă sesiune, cu un rating pe care îl inventează el, sau pentru un prompt care nu
i-a fost niciodată oferit. Plus proprietățile de contabilitate: retry identic = același receipt
(fără revizie nouă), corecție = revizie nouă, plafon.

Fără Postgres: `submit_feedback` primește un provider fals. Comportamentul pe DB REALĂ (unique,
concurență, RLS) e în `test_web_feedback_db.py`.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.db.queries.feedback import FeedbackRow
from src.db.queries.web_turns import WebTurnRow
from src.observability import metrics
from src.web import action_service as svc
from src.web import feedback as fb
from src.web.action_crypto import parse_key_ring
from src.web.action_models import (
    FEEDBACK_KINDS,
    FEEDBACK_REASONS,
    KIND_REGISTRY,
    RATING_BY_KIND,
    SINK_FEEDBACK,
    SINK_TURN,
    ActionArgs,
    ActionPlan,
    TurnFacts,
    plan_actions,
    spec_for,
)
from src.web.turn_service import session_ref_hash

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
BIZ = "biz-1"
TOKEN = "public-token"
VISITOR = "visitor-1"
SECRET = "prompt-secret"
TURN_ID = "33333333-3333-4333-8333-333333333333"


def _ring(spec: str = "k1:") -> object:
    return parse_key_ring(f"k1:{base64.b64encode(bytes([7]) * 32).decode()}")


def _row(**over) -> WebTurnRow:
    base = dict(
        id=TURN_ID,
        business_id=BIZ,
        conversation_id="conv-1",
        contact_id="ct1",
        session_ref_hash=session_ref_hash(TOKEN, VISITOR),
        client_turn_id=str(uuid4()),
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="completed",
        attempt=1,
        lease_owner=None,
        lease_epoch=1,
        lease_expires_at=None,
        deadline_at=None,
        conversation_revision_at_accept=4,
        pipeline_version="web-chat.v1",
        response_json={"content": "ok", "products": [], "suggestions": []},
        safe_error_code=None,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )
    base.update(over)
    return WebTurnRow(**base)


def _source(plans=None, **over) -> WebTurnRow:
    """Un turn terminal care A EMIS planurile date (dovada de emitere e planul persistat)."""
    plans = plans if plans is not None else plan_actions({}, TurnFacts(feedback_prompt=True))
    view = svc.merge_actions_into_view({"content": "ok", "products": []}, tuple(plans))
    return _row(response_json=view, **over)


def _token(source: WebTurnRow, kind: str, ring, *, args: ActionArgs | None = None) -> str:
    """Tokenul REAL pe care l-ar emite serverul pentru planul dat."""
    issued = svc.issue_actions(
        source, (ActionPlan(kind, args or ActionArgs()),), ring=ring, ttl_s=3600
    )
    assert issued, f"{kind} nu a fost emis"
    return issued[0].token


class _Db:
    """Provider fals + starea tabelului `web_feedback`, ca un dict pe cheia de unicitate."""

    def __init__(self, source: WebTurnRow | None) -> None:
        self.source = source
        self.rows: dict[str, FeedbackRow] = {}
        self.fail_write = False

    def __call__(self, operation: str = "?"):
        db = self

        @asynccontextmanager
        async def _cm():
            yield db

        return _cm()


def _provider(monkeypatch, source: WebTurnRow | None) -> _Db:
    db = _Db(source)

    async def _get_turn_by_id(conn, business_id, turn_id):
        if source is None or business_id != source.business_id or turn_id != source.id:
            return None
        return source

    async def _upsert(conn, business_id, **kw):
        if db.fail_write:
            raise ConnectionError("storage jos")
        key = kw["feedback_prompt_id"]
        existing = db.rows.get(key)
        if existing is None:
            row = FeedbackRow(
                id=str(uuid4()),
                business_id=business_id,
                conversation_id=kw["conversation_id"],
                turn_id=kw["turn_id"],
                feedback_prompt_id=key,
                rating=kw["rating"],
                reason_code=kw["reason_code"],
                taxonomy_version=kw["taxonomy_version"],
                source="web_widget",
                schema_version=kw["schema_version"],
                release_sha=kw["release_sha"],
                release_track=kw["release_track"],
                pipeline_version=kw["pipeline_version"],
                last_action_id=kw["action_id"],
                revision=1,
                created_at=NOW,
                updated_at=NOW,
            )
            db.rows[key] = row
            return row
        # Oglinda `where`-ului din SQL: retry identic sau plafon ⇒ niciun rând întors.
        if existing.last_action_id == kw["action_id"]:
            return None
        if existing.revision >= kw["max_revisions"]:
            return None
        row = FeedbackRow(
            **{
                **existing.__dict__,
                "rating": kw["rating"],
                "reason_code": kw["reason_code"],
                "last_action_id": kw["action_id"],
                "revision": existing.revision + 1,
            }
        )
        db.rows[key] = row
        return row

    async def _get(conn, business_id, prompt_id):
        return db.rows.get(prompt_id)

    monkeypatch.setattr(fb, "get_turn_by_id", _get_turn_by_id)
    monkeypatch.setattr(fb, "upsert_feedback", _upsert)
    monkeypatch.setattr(fb, "get_feedback", _get)
    return db


async def _submit(db, token: str, *, visitor: str = VISITOR, business: str = BIZ, ring=None):
    return await fb.submit_feedback(
        db,
        token=token,
        business_id=business,
        channel_token=TOKEN,
        visitor_id=visitor,
        ring=ring or _ring(),
        prompt_secret=SECRET,
        locale="ro",
        release_sha="deadbeef",
        release_track="candidate",
        now=NOW,
    )


# ── Vocabular: ratingul e în KIND, deci în token ────────────────────────────────────────────


def test_ratingul_vine_din_kind_nu_din_client():
    """Nu există câmp `rating` nicăieri pe sârmă: browserul poate doar prezenta un token emis."""
    assert RATING_BY_KIND == {"feedback_up": "positive", "feedback_down": "negative"}
    assert set(RATING_BY_KIND) == set(FEEDBACK_KINDS)


def test_kindurile_de_feedback_au_sink_separat():
    """Structural, nu prin `if`: un token de feedback nu poate porni un tur conversațional."""
    for kind in FEEDBACK_KINDS:
        assert spec_for(kind).sink == SINK_FEEDBACK
    for kind, spec in KIND_REGISTRY.items():
        if kind not in FEEDBACK_KINDS:
            assert spec.sink == SINK_TURN


def test_reason_ul_e_vocabular_inchis():
    spec = spec_for("feedback_down")
    assert ActionArgs.parse({"reason": "not_relevant"}, spec).reason == "not_relevant"
    # Un motiv inventat e RESPINGERE, nu „other" tăcut: altfel taxonomia ar crește din date.
    assert ActionArgs.parse({"reason": "pentru ca da"}, spec) is None
    assert ActionArgs.parse({"reason": "not_relevant"}, spec_for("feedback_up")) is None


def test_promptul_se_planifica_doar_sub_flag():
    assert plan_actions({}, TurnFacts()) == ()
    kinds = [p.kind for p in plan_actions({}, TurnFacts(feedback_prompt=True))]
    assert kinds == ["feedback_up", "feedback_down"]


def test_actiunile_conversationale_au_prioritate_la_cap():
    """Feedbackul e ultimul: dacă un tur are prea multe acțiuni, el cedează, nu cardurile."""
    view = {"products": [{"product_id": f"p{i}"} for i in range(6)], "suggestions": []}
    plans = plan_actions(view, TurnFacts(feedback_prompt=True))
    assert len(plans) <= 16
    assert plans[0].kind == "request_details"


def test_prompt_id_e_determinist_si_legat_de_secret():
    """Derivat, nu random: un `GET` repetat trebuie să dea ACELAȘI prompt (altfel „un vot per
    prompt" ar deveni „un vot per reîncărcare de pagină")."""
    assert fb.prompt_id_for(TURN_ID, SECRET) == fb.prompt_id_for(TURN_ID, SECRET)
    assert fb.prompt_id_for(TURN_ID, SECRET) != fb.prompt_id_for(TURN_ID, "alt")
    assert TURN_ID not in fb.prompt_id_for(TURN_ID, SECRET)


# ── Drumul fericit + contabilitate ──────────────────────────────────────────────────────────


async def test_vot_pozitiv_scrie_si_intoarce_receipt(monkeypatch):
    source = _source()
    db = _provider(monkeypatch, source)
    out = await _submit(db, _token(source, "feedback_up", _ring()))
    assert isinstance(out, fb.FeedbackReceipt)
    assert out.rating == "positive" and out.revision == 1
    assert out.message == "Mă bucur că te-am ajutat."
    row = next(iter(db.rows.values()))
    assert row.turn_id == TURN_ID and row.release_track == "candidate"
    assert row.taxonomy_version == "feedback.v1"


async def test_retry_identic_intoarce_acelasi_receipt_fara_revizie(monkeypatch):
    """Failure matrix: „feedback retry identic ⇒ același receipt, un singur efect"."""
    source = _source()
    db = _provider(monkeypatch, source)
    token = _token(source, "feedback_up", _ring())
    first = await _submit(db, token)
    second = await _submit(db, token)
    assert first == second, "retry-ul a produs alt receipt"
    assert next(iter(db.rows.values())).revision == 1


async def test_corectia_incrementeaza_revizia_fara_rand_nou(monkeypatch):
    source = _source()
    db = _provider(monkeypatch, source)
    up = await _submit(db, _token(source, "feedback_up", _ring()))
    down = await _submit(db, _token(source, "feedback_down", _ring()))
    assert up.revision == 1 and down.revision == 2
    assert down.rating == "negative"
    assert len(db.rows) == 1, "o corecție a creat un al doilea rând (agregatele ar dubla)"


async def test_flip_flop_se_opreste_la_plafon(monkeypatch):
    source = _source()
    db = _provider(monkeypatch, source)
    tokens = [_token(source, k, _ring()) for k in ("feedback_up", "feedback_down")]
    outcomes = [await _submit(db, tokens[i % 2]) for i in range(8)]
    assert isinstance(outcomes[-1], fb.FeedbackRejected)
    assert outcomes[-1].code == "feedback_locked"
    assert next(iter(db.rows.values())).revision == fb.MAX_FEEDBACK_REVISIONS


# ── Modelul de amenințare ───────────────────────────────────────────────────────────────────


async def test_tokenul_altei_sesiuni_e_refuzat(monkeypatch):
    source = _source()
    db = _provider(monkeypatch, source)
    out = await _submit(db, _token(source, "feedback_up", _ring()), visitor="alt-vizitator")
    assert isinstance(out, fb.FeedbackRejected)
    assert out.code == "feedback_not_found"
    assert db.rows == {}, "un token străin a scris un rând"


async def test_tokenul_altui_tenant_e_refuzat(monkeypatch):
    source = _source()
    db = _provider(monkeypatch, source)
    out = await _submit(db, _token(source, "feedback_up", _ring()), business="alt-biz")
    assert isinstance(out, fb.FeedbackRejected)
    assert out.code == "feedback_not_found"
    assert db.rows == {}


async def test_token_neemis_e_refuzat_desi_sigiliul_e_valid(monkeypatch):
    """Dovada de emitere (NX-236): sigiliu perfect, dar planul persistat nu conține acțiunea."""
    emitent = _source()  # a emis feedback
    fara_prompt = _source(plans=())  # ACELAȘI turn, dar fără plan de feedback persistat
    db = _provider(monkeypatch, fara_prompt)
    out = await _submit(db, _token(emitent, "feedback_up", _ring()))
    assert isinstance(out, fb.FeedbackRejected)
    assert out.code == "feedback_not_found" and out.reason == "not_emitted"
    assert db.rows == {}


async def test_tokenul_de_tur_nu_poate_vota(monkeypatch):
    """Poarta de `sink`, în direcția cealaltă: o acțiune conversațională nu scrie feedback."""
    source = _source(plans=(ActionPlan("request_details", ActionArgs(product_ref="p1")),))
    db = _provider(monkeypatch, source)
    out = await _submit(
        db, _token(source, "request_details", _ring(), args=ActionArgs(product_ref="p1"))
    )
    assert isinstance(out, fb.FeedbackRejected)
    assert out.code == "feedback_not_found" and out.reason == "wrong_sink"
    assert db.rows == {}


async def test_sursa_disparuta_e_refuzata(monkeypatch):
    source = _source()
    token = _token(source, "feedback_up", _ring())
    db = _provider(monkeypatch, None)  # retenție / GDPR erase
    out = await _submit(db, token)
    assert isinstance(out, fb.FeedbackRejected)
    assert out.code == "feedback_not_found"


async def test_token_stricat_e_refuzat_generic(monkeypatch):
    db = _provider(monkeypatch, _source())
    out = await _submit(db, "nu-e-un-token")
    assert isinstance(out, fb.FeedbackRejected)
    assert out.code == "feedback_invalid"
    assert db.rows == {}


async def test_storage_jos_nu_strica_conversatia(monkeypatch):
    """Failure matrix: „feedback storage unavailable ⇒ răspunsul conversațional rămâne intact"."""
    source = _source()
    db = _provider(monkeypatch, source)
    db.fail_write = True
    out = await _submit(db, _token(source, "feedback_up", _ring()))
    assert isinstance(out, fb.FeedbackRejected)
    assert out.code == "feedback_unavailable"


# ── Poarta de prompt ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,asteptat",
    [("completed", True), ("failed", False), ("cancelled", False), ("running", False)],
)
def test_promptul_se_cere_doar_pe_ture_completate(status, asteptat):
    """A cere părerea despre un `failed` ar strânge voturi despre eroarea scrisă de NOI."""
    assert fb.should_prompt(enabled=True, row=_row(status=status)) is asteptat


def test_promptul_e_stins_cu_flagul_stins():
    assert fb.should_prompt(enabled=False, row=_row()) is False
    assert fb.should_prompt(enabled=True, row=None) is False


# ── Metrici ─────────────────────────────────────────────────────────────────────────────────


async def test_corectia_e_numarata_separat_de_votul_nou(monkeypatch):
    """Altfel o răzgândire ar arăta în agregate ca încă un vot."""
    from types import SimpleNamespace

    from src.observability import bootstrap
    from src.observability import config as obs_config

    bootstrap.setup(
        SimpleNamespace(
            observability_enabled=True,
            observability_exporter="none",
            observability_sample_ratio=0.0,
            env="test",
            release_sha="x",
            release_track="candidate",
        )
    )
    metrics.reset()
    try:
        source = _source()
        db = _provider(monkeypatch, source)
        await _submit(db, _token(source, "feedback_up", _ring()))
        await _submit(db, _token(source, "feedback_down", _ring()))
        counters = metrics.snapshot()["counters"]
        assert any("outcome=recorded" in k and "rating=positive" in k for k in counters)
        assert any("outcome=updated" in k and "rating=negative" in k for k in counters)
    finally:
        obs_config.configure(None)
        metrics.reset()


# ── Fără PII, prin construcție ──────────────────────────────────────────────────────────────


def test_randul_de_feedback_nu_are_camp_de_text():
    """Cardul: „fără body, comment, IP, token sau identity raw" — verificat pe TIP."""
    campuri = set(FeedbackRow.__dataclass_fields__)
    interzise = {"comment", "body", "text", "ip", "user_agent", "token", "visitor_id", "contact_id"}
    assert not (campuri & interzise), f"câmp interzis pe rândul de feedback: {campuri & interzise}"


def test_taxonomia_e_inchisa_si_versionata():
    assert "other" in FEEDBACK_REASONS
    assert all(r.islower() and " " not in r for r in FEEDBACK_REASONS)
