"""Prospețimea faptelor comerciale e a TENANTULUI, nu a mediului.

Testele apără trei propoziții, în ordinea în care contează:

1. o declarație lipsă sau stricată cade pe politica CONSERVATOARE (se judecă, deci se poate omite);
2. un catalog declarat static nu produce fapte `stale` doar fiindcă a trecut timpul;
3. un catalog declarat static **nu** devine prin asta un catalog fără adevăr: un produs epuizat
   rămâne epuizat, e etichetat ca atare și nu primește CTA de coș.

A treia e cea care ține totul în frâu. Prima variantă a reparației (a șterge `synced_at`) ar fi
trecut primele două teste și l-ar fi picat pe al treilea în producție, nu în suită.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agent.evidence_bundle import EvidenceBundle, build_evidence_bundle, build_product_evidence
from src.catalog.freshness import MODE_STATIC, MODE_SYNCED, SETTINGS_KEY, facts_sla_s, is_static
from tests.nx240_helpers import BUSINESS_ID, NOW, row

DEFAULT_SLA = 86_400
#: Vechimea reală a catalogului SOLE la momentul descoperirii: citit pe 27.08, judecat pe 31.08.
OLD = NOW - timedelta(hours=67)


def _static() -> dict:
    return {SETTINGS_KEY: {"mode": MODE_STATIC}}


# ── 1. Rezolvarea declarației ───────────────────────────────────────────────────────────────
def test_missing_declaration_falls_back_to_the_environment_default():
    assert facts_sla_s(None, default=DEFAULT_SLA) == DEFAULT_SLA
    assert facts_sla_s({}, default=DEFAULT_SLA) == DEFAULT_SLA


def test_static_snapshot_means_facts_are_not_judged_in_time():
    assert facts_sla_s(_static(), default=DEFAULT_SLA) is None
    assert is_static(_static()) is True


def test_synced_declaration_carries_its_own_threshold():
    settings = {SETTINGS_KEY: {"mode": MODE_SYNCED, "sla_s": 3600}}
    assert facts_sla_s(settings, default=DEFAULT_SLA) == 3600
    assert is_static(settings) is False


@pytest.mark.parametrize(
    "declared",
    [
        {"mode": "typo_snapshot"},  # mod necunoscut
        {"mode": MODE_SYNCED},  # `synced` fără prag
        {"mode": MODE_SYNCED, "sla_s": 0},  # prag zero = totul expirat
        {"mode": MODE_SYNCED, "sla_s": -5},
        {"mode": MODE_SYNCED, "sla_s": "86400"},  # text, nu întreg
        {"mode": MODE_SYNCED, "sla_s": True},  # `bool` e subclasă de `int` → ar trece ca 1s
        "static_snapshot",  # declarație care nu e obiect
        [],
    ],
)
def test_a_malformed_declaration_never_relaxes_the_policy(declared):
    """Necunoscutul duce la politica conservatoare. O greșeală de tastare într-un jsonb nu are
    voie să scutească tăcut un catalog viu de verificare — ar fi exact eșecul pe care declarația
    a fost introdusă să-l facă VIZIBIL."""
    assert facts_sla_s({SETTINGS_KEY: declared}, default=DEFAULT_SLA) == DEFAULT_SLA
    assert is_static({SETTINGS_KEY: declared}) is False


# ── 2. Efectul pe fapte ─────────────────────────────────────────────────────────────────────
def test_old_facts_go_stale_under_the_default_threshold():
    """Punctul de plecare: exact ce se întâmpla pe SOLE, măsurat."""
    product = build_product_evidence(
        row(synced_at=OLD), business_id=BUSINESS_ID, now=NOW, sla_s=DEFAULT_SLA
    )
    assert product.fact("price").status == "stale"
    assert product.fact("availability").status == "stale"
    assert product.sellable == "availability_stale"


def test_a_static_catalog_keeps_old_facts_usable():
    product = build_product_evidence(
        row(synced_at=OLD),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=facts_sla_s(_static(), default=DEFAULT_SLA),
    )
    assert product.fact("price").status == "known"
    assert product.fact("price").usable is True
    assert product.fact("availability").status == "known"
    assert product.sellable is None  # poate purta CTA de coș


def test_static_does_not_erase_the_age_it_only_stops_judging_it():
    """`verified_at` rămâne pe fapt: vârsta se RAPORTEAZĂ în continuare. Reparația prin
    `update products set synced_at = null` ar fi aruncat exact informația asta."""
    product = build_product_evidence(
        row(synced_at=OLD),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=facts_sla_s(_static(), default=DEFAULT_SLA),
    )
    price = product.fact("price")
    assert price.verified_at == OLD
    assert price.verified is True
    assert price.age_s is not None and price.age_s > DEFAULT_SLA


# ── 3. Ce NU relaxează declarația ───────────────────────────────────────────────────────────
def test_a_sold_out_product_stays_sold_out_on_a_static_catalog():
    """Cele 391 de produse epuizate din SOLE. Sub prag expirat, disponibilitatea era `stale`, deci
    cardul NU purta eticheta „indisponibil" (politica interzice s-o inventeze) — un produs de
    nevândut arăta ca oricare altul. Declarat static, faptul redevine `known` și spune adevărul."""
    product = build_product_evidence(
        row(synced_at=OLD, availability="out_of_stock", stock=0),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=facts_sla_s(_static(), default=DEFAULT_SLA),
    )
    assert product.fact("availability").value == "out_of_stock"
    assert product.fact("availability").usable is True  # se poate AFIȘA ca indisponibil
    assert product.sellable == "out_of_stock"  # dar NU poate purta CTA de cumpărare


def test_a_missing_value_is_still_unknown_on_a_static_catalog():
    """Static înseamnă „nu judec vechimea", nu „presupun ce lipsește"."""
    product = build_product_evidence(
        row(synced_at=OLD, price=None, availability=None),
        business_id=BUSINESS_ID,
        now=NOW,
        sla_s=facts_sla_s(_static(), default=DEFAULT_SLA),
    )
    assert product.fact("price").status == "unknown"
    assert product.fact("availability").status == "unknown"
    assert product.sellable == "availability_unknown"


def test_an_unverified_row_is_unaffected_by_the_declaration():
    """Un rând fără `synced_at` nu putea deveni stale nici înainte — declarația nu-l schimbă."""
    for sla in (DEFAULT_SLA, None):
        product = build_product_evidence(
            row(synced_at=None), business_id=BUSINESS_ID, now=NOW, sla_s=sla
        )
        assert product.fact("price").status == "known"
        assert product.fact("price").verified is False  # afișabil, dar nu „verificat"


# ── 4. Persistarea verdictului ──────────────────────────────────────────────────────────────
def test_the_bundle_round_trips_a_static_declaration_without_becoming_a_zero_threshold():
    """`None` citit înapoi ca `0` ar însemna prag zero, adică verdictul OPUS: totul expirat."""
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(synced_at=OLD)],
        now=NOW,
        sla_s=None,
    )
    assert bundle.sla_s is None
    restored = EvidenceBundle.from_jsonb(bundle.to_jsonb())
    assert restored is not None
    assert restored.sla_s is None
    assert restored.products[0].fact("price").status == "known"


def test_a_bundle_written_before_the_declaration_still_reads_back():
    """Compatibilitate: bundle-urile persistate cu `sla_s: 0` rămân citibile ca atare."""
    bundle = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row()],
        now=NOW,
        sla_s=DEFAULT_SLA,
    )
    payload = bundle.to_jsonb()
    payload["sla_s"] = 0
    restored = EvidenceBundle.from_jsonb(payload)
    assert restored is not None and restored.sla_s == 0


def test_the_declaration_is_read_from_the_tenant_not_from_the_environment():
    """Două tenant-uri, același rând, aceeași clipă, verdicte diferite. Asta e tot rostul."""
    static_bundle = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(synced_at=OLD)],
        now=NOW,
        sla_s=facts_sla_s(_static(), default=DEFAULT_SLA),
    )
    synced_bundle = build_evidence_bundle(
        business_id=BUSINESS_ID,
        locale="ro",
        rows=[row(synced_at=OLD)],
        now=NOW,
        sla_s=facts_sla_s({}, default=DEFAULT_SLA),
    )
    assert static_bundle.products[0].fact("price").status == "known"
    assert synced_bundle.products[0].fact("price").status == "stale"


def test_now_is_not_read_from_the_wall_clock():
    """Determinismul rămâne: `now` se pasează, deci testele de mai sus nu depind de ziua rulării."""
    later = NOW + timedelta(days=365)
    product = build_product_evidence(
        row(synced_at=OLD),
        business_id=BUSINESS_ID,
        now=later,
        sla_s=facts_sla_s(_static(), default=DEFAULT_SLA),
    )
    assert product.fact("price").status == "known"


def test_utc_datetimes_stay_comparable():
    """Plasă ieftină: `OLD` e tz-aware, ca diferența de vârstă să nu crape pe naive vs aware."""
    assert OLD.tzinfo is UTC or OLD.utcoffset() is not None
    assert datetime.now(UTC) > OLD
