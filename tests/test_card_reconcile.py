"""NX-324 — cardul eliminat ia cu el textul care îl numește; produsul se alege prin handle.

Turul real `0d8a3541` (conversația `bc7a356e`): intro-ul spunea „HARUHARU oferă hidratare
lejeră, REAL BARRIER…, iar MIZON…", dar cardul HARUHARU căzuse la apartenență, fiindcă modelul îi
copiase greșit UUID-ul (`…-8062-dc2bb…` → `…-806c-d2bb…`). Zero DB, zero model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.agent import finalize
from src.worker import compose
from tests.test_compose import _ctx, _settings

HARUHARU = "3a775014-4e07-46bf-8062-dc2bb4db5549"
REAL_BARRIER = "4003fd28-24f0-446b-9a84-74a680a07374"
MIZON = "2b21880b-e9c9-438c-8574-5ff6d7d9767b"
SOME_BY_MI = "80404833-ec16-4eff-ae09-8875237bbe48"

#: Retrievalul real al turului (numele scurte), fără epuizatul și produsul pentru ten gras.
RETRIEVED = [
    {"id": HARUHARU, "name": "HARUHARU WONDER Black Rice 10 Hyaluronic Cream crema de fata"},
    {"id": REAL_BARRIER, "name": "REAL BARRIER Aqua Soothing, 50 ml - Crema de fata"},
    {"id": MIZON, "name": "MIZON Collagen Power crema pentru fata"},
    {"id": SOME_BY_MI, "name": "SOME BY MI Beta Panthenol Repair Cream"},
]
for p in RETRIEVED:
    p.update(price=100.0, rating=4.9, availability="in_stock")

#: `rich_raw` real al turului.
REAL_J = {
    "intro": (
        "Dacă te referi la ten, după duș aș prioritiza hidratarea și confortul barierei cutanate. "
        "HARUHARU oferă hidratare lejeră, REAL BARRIER pune accent pe pielea sensibilă și uscată, "
        "iar MIZON este destinat tenului uscat."
    ),
    "items": [
        {"product_id": "3a775014-4e07-46bf-806c-d2bb4db5549", "pro_index": 1, "fit_clause": "ok"},
        {"product_id": REAL_BARRIER, "pro_index": 2, "fit_clause": "confort"},
        {"product_id": MIZON, "pro_index": 1, "fit_clause": "ten uscat"},
        {"product_id": SOME_BY_MI, "pro_index": 2, "fit_clause": "bariera"},
    ],
    "pick": None,
    "education": "",
    "suggestions": ["Compară HARUHARU cu REAL BARRIER", "Arată-mi creme pentru ten uscat"],
}


def _run(monkeypatch, j, *, reconcile=True):
    monkeypatch.setattr(
        compose, "get_settings", lambda: _settings(rich_card_reconcile_enabled=reconcile)
    )
    ctx = _ctx()
    events: list = []
    ctx.emit = lambda type_, **props: events.append((type_, props))
    rich = compose.assemble(ctx, j, [dict(p) for p in RETRIEVED])
    return rich, events


# ── Reconcilierea ─────────────────────────────────────────────────────────────────────────────


def test_turul_real_textul_nu_mai_numeste_produsul_fara_card(monkeypatch):
    rich, events = _run(monkeypatch, REAL_J)

    assert HARUHARU not in [it.product_id for it in rich.items]
    assert "HARUHARU" not in (rich.intro or "")
    # Prima propoziție nu numește niciun produs, deci rămâne: un text onest mai scurt.
    assert rich.intro.startswith("Dacă te referi la ten")
    assert all("HARUHARU" not in c.label for c in (rich.chips or []))
    fields = {props["field"] for kind, props in events if kind == "rich_text_reconciled"}
    assert fields == {"intro", "chips"}


def test_flag_stins_textul_pleaca_neatins(monkeypatch):
    rich, events = _run(monkeypatch, REAL_J, reconcile=False)
    assert "HARUHARU oferă" in rich.intro
    assert not [e for e in events if e[0] == "rich_text_reconciled"]


def test_toate_numite_si_afisate_nimic_nu_se_taie(monkeypatch):
    j = {
        **REAL_J,
        "items": [
            {**it, "product_id": HARUHARU} if i == 0 else it for i, it in enumerate(REAL_J["items"])
        ],
    }
    rich, events = _run(monkeypatch, j)
    assert "HARUHARU oferă" in rich.intro
    assert not [e for e in events if e[0] == "rich_text_reconciled"]


def test_intro_doar_despre_produse_fara_card_devine_gol(monkeypatch):
    j = {**REAL_J, "intro": "HARUHARU oferă hidratare lejeră."}
    rich, _ = _run(monkeypatch, j)
    assert rich.intro is None  # apoi rezerva de încadrare, dacă e aprinsă (NX-299/NX-325)


def test_prefix_neunic_nu_taie_nimic():
    """Două produse cu același brand: «SOME BY MI» nu mai e prefix unic, deci nu declanșează."""
    prefixes = compose.unique_prefixes(
        {"a": "SOME BY MI Beta Panthenol", "b": "SOME BY MI Yuja Niacin"}, locale="ro"
    )
    text, n = compose.drop_sentences_naming("SOME BY MI e bun. Altceva.", {"b"}, prefixes)
    assert n == 0 and text == "SOME BY MI e bun. Altceva."


# ── Handle-urile ──────────────────────────────────────────────────────────────────────────────


def test_handle_urile_urmeaza_ordinea_listei():
    assert finalize.item_handles(RETRIEVED) == {
        "P1": HARUHARU,
        "P2": REAL_BARRIER,
        "P3": MIZON,
        "P4": SOME_BY_MI,
    }


def test_schema_cu_handle_uri():
    schema = finalize._rich_schema(frozenset(), n_items=4)
    props = schema["schema"]["properties"]
    assert props["items"]["items"]["properties"]["product_id"]["enum"] == ["P1", "P2", "P3", "P4"]
    assert props["pick"]["properties"]["product_id"]["enum"] == ["P1", "P2", "P3", "P4"]
    # Cache-uibilă: același număr de produse ⇒ același obiect, oricare ar fi id-urile.
    assert finalize._rich_schema(frozenset(), n_items=4) is schema
    # Constanta de modul nu e mutată.
    assert finalize._RICH_SCHEMA["schema"]["properties"]["items"]["items"]["properties"][
        "product_id"
    ] == {"type": "string"}


def test_fara_handle_uri_schema_de_azi():
    assert finalize._rich_schema(frozenset()) is finalize._RICH_SCHEMA
    assert finalize._rich_schema(frozenset(), n_items=None) is finalize._RICH_SCHEMA


def test_lista_trimisa_modelului_poarta_handle_urile():
    handles = finalize.item_handles(RETRIEVED)
    bundle = finalize._rich_bundle(RETRIEVED, (), "ro", handles)
    assert bundle.splitlines()[0].startswith("[P1] HARUHARU")
    assert HARUHARU not in bundle


def test_raspunsul_se_traduce_inapoi_si_nimic_nu_cade(monkeypatch):
    handles = finalize.item_handles(RETRIEVED)
    j = {
        **REAL_J,
        "items": [{**it, "product_id": f"P{i}"} for i, it in enumerate(REAL_J["items"], start=1)],
        "pick": {"product_id": "P2", "justification": "x"},
    }
    resolved = finalize._resolve_handles(j, handles)
    assert [it["product_id"] for it in resolved["items"]] == [
        HARUHARU,
        REAL_BARRIER,
        MIZON,
        SOME_BY_MI,
    ]
    assert resolved["pick"]["product_id"] == REAL_BARRIER
    assert j["items"][0]["product_id"] == "P1"  # copie, nu mutație (diagnoza brută rămâne)
    rich, events = _run(monkeypatch, resolved)
    assert len(rich.items) == 4
    assert not [e for e in events if e[0] == "rich_membership_dropped"]


async def test_finalize_rich_trimite_handle_uri_si_pastreaza_maparea(monkeypatch):
    class _LLM:
        def __init__(self):
            self.calls = []

        async def complete_schema(self, system, user, schema):
            self.calls.append((system, user, schema))
            return {**REAL_J, "items": [{"product_id": "P2", "pro_index": 0, "fit_clause": "x"}]}

    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "rich_item_handles_enabled", True)
    monkeypatch.setattr(
        compose, "get_settings", lambda: _settings(rich_card_reconcile_enabled=True)
    )
    llm = _LLM()
    ctx = _ctx()
    ctx.emit = lambda *a, **k: None
    ctx.business = SimpleNamespace(vertical="beauty", domain_pack=None)
    out = await finalize._finalize_rich(llm, "sys", "crema", [dict(p) for p in RETRIEVED], ctx, "")

    _, user, schema = llm.calls[0]
    assert "[P2] REAL BARRIER" in user
    assert schema["schema"]["properties"]["items"]["items"]["properties"]["product_id"]["enum"]
    assert ctx.trace["rich_handles"]["P2"] == REAL_BARRIER
    assert json.dumps(ctx.trace["rich_raw"]).count("P2") >= 1
    assert [it.product_id for it in out.reply.items] == [REAL_BARRIER]
