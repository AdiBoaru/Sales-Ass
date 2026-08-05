"""NX-217 felia 3 — „Actions This Week": faptele de cerere devin rânduri de ACȚIUNE.

Funcție PURĂ (zero DB, zero LLM): primește faptele agregate + starea curentă a catalogului și
produce lista de acțiuni. Nu se stochează niciodată — o acțiune materializată ar continua să
ceară „adaugă brandul X" și după ce comerciantul l-a adăugat.

**Ordinea regulilor nu e cosmetică**: `restock` se evaluează ÎNAINTEA lui `add_to_catalog`.
„Adaugă în catalog un brand pe care îl AI (dar e epuizat)" e exact eroarea care distruge
încrederea în raport, iar încrederea e singurul lucru care îl face pe comerciant să-l deschidă
a doua oară.

**Onestitate (D.4)**: fapte numărate, nu estimări. Fără `estimated_value`, fără `confidence`.
Semnalele de forță diferită se afișează SEPARAT, niciodată însumate („41 cereri + 19 abonări",
niciodată 60). Backend-ul nu compune text de UI: întoarce dimensiuni + numere + dovadă, iar
fraza o scrie frontend-ul.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Praguri implicite. Trăiesc în config (`demand_action_min_requests`), nu în cod — un raport
# care nu poate fi calibrat per client ajunge ignorat pe tenanții mici.
DEFAULT_MIN_REQUESTS = 3

# Semnalele de cerere neîmplinită care cer produse noi în catalog (spre deosebire de cele care
# cer stoc sau variante pe produse existente).
_CATALOG_GAP_SIGNALS = ("unmet_no_result", "unmet_named_not_found")


@dataclass(frozen=True)
class Action:
    """Un rând de acțiune. `kind` + dimensiunea spun CE, `count` spune CÂT DE TARE, `evidence`
    dovedește. `supporting_counts` ține semnalele secundare, separat (nu se însumează cu `count`).
    `prev_count` = aceeași măsură pe fereastra precedentă de aceeași lungime → trend."""

    kind: str
    dimension_kind: str
    dimension_key: str
    count: int
    prev_count: int = 0
    supporting_counts: dict[str, int] = field(default_factory=dict)
    evidence_conversation_ids: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "dimension_kind": self.dimension_kind,
            "dimension_key": self.dimension_key,
            "count": self.count,
            "prev_count": self.prev_count,
            "supporting_counts": dict(self.supporting_counts),
            "evidence_conversation_ids": list(self.evidence_conversation_ids),
            "context": dict(self.context),
        }


def _by_signal(facts: list[dict[str, Any]], *signals: str) -> list[dict[str, Any]]:
    return [f for f in facts if f["signal_kind"] in signals]


def _prev_lookup(prev_facts: list[dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    return {
        (f["signal_kind"], f["dimension_kind"], f["dimension_key"]): f["request_count"]
        for f in prev_facts
    }


def build_actions(
    facts: list[dict[str, Any]],
    *,
    prev_facts: list[dict[str, Any]] | None = None,
    brand_presence: dict[str, dict[str, int]] | None = None,
    product_state: dict[str, dict[str, Any]] | None = None,
    min_requests: int = DEFAULT_MIN_REQUESTS,
) -> list[dict[str, Any]]:
    """Faptele + starea catalogului → acțiuni, ordonate după cât de tare e semnalul.

    `facts` = ieșirea lui `window_facts`; `brand_presence` / `product_state` = starea de ACUM.
    O dimensiune produce cel mult O acțiune: regulile sunt evaluate în ordine și prima potrivire
    o consumă (`claimed`), ca să nu apară același brand și la „adaugă" și la „reaprovizionează".
    """
    brand_presence = brand_presence or {}
    product_state = product_state or {}
    prev = _prev_lookup(prev_facts or [])
    out: list[Action] = []
    claimed: set[tuple[str, str]] = set()  # (dimension_kind, dimension_key) deja acoperite

    def _emit(kind: str, fact: dict[str, Any], **kw: Any) -> None:
        """Prima potrivire câștigă: o dimensiune deja acoperită nu mai produce o a doua acțiune.
        Garda stă AICI, nu în fiecare regulă — două semnale diferite pe același produs (epuizat
        ȘI fără rezultat) altfel ar genera două rânduri pentru aceeași problemă."""
        key = (fact["dimension_kind"], fact["dimension_key"])
        if key in claimed:
            return
        claimed.add(key)
        out.append(
            Action(
                kind=kind,
                dimension_kind=fact["dimension_kind"],
                dimension_key=fact["dimension_key"],
                count=fact["request_count"],
                prev_count=prev.get(
                    (fact["signal_kind"], fact["dimension_kind"], fact["dimension_key"]), 0
                ),
                evidence_conversation_ids=list(fact.get("evidence_conversation_ids") or []),
                **kw,
            )
        )

    # --- 1. RESTOCK — produs cerut care EXISTĂ dar nu e cumpărabil ------------------------
    # Prima regulă din motiv de corectitudine, nu de importanță: consumă dimensiunile pe care
    # regula 2 le-ar eticheta greșit ca „lipsă din catalog".
    for f in _by_signal(facts, "unmet_out_of_stock", *_CATALOG_GAP_SIGNALS):
        if f["request_count"] < min_requests:
            continue
        if f["dimension_kind"] == "product":
            st = product_state.get(f["dimension_key"])
            if st and st["availability"] not in ("in_stock", "low_stock"):
                _emit(
                    "restock",
                    f,
                    supporting_counts={"back_in_stock_subscribers": st.get("subscribers", 0)},
                    context={"product_name": st.get("name"), "availability": st["availability"]},
                )
        elif f["dimension_kind"] == "brand":
            pres = brand_presence.get(f["dimension_key"].lower())
            if pres and pres["products"] > 0 and pres["buyable"] == 0:
                _emit("restock", f, context={"products_in_catalog": pres["products"]})

    # --- 2. ADD TO CATALOG — cerut și ABSENT complet din catalog ---------------------------
    for f in _by_signal(facts, *_CATALOG_GAP_SIGNALS):
        if f["request_count"] < min_requests:
            continue
        if f["dimension_kind"] == "brand" and not brand_presence.get(f["dimension_key"].lower()):
            _emit("add_to_catalog", f)

    # --- 3. ADD VARIANT — produsul există, varianta cerută nu ------------------------------
    for f in _by_signal(facts, "unmet_missing_variant"):
        if f["request_count"] < min_requests:
            continue
        if f["dimension_kind"] == "variant_attr":
            _emit("add_variant", f)

    # --- 4. PRICE GAP — s-a cerut ceva mai ieftin și n-a existat ---------------------------
    for f in _by_signal(facts, "unmet_price_gap"):
        if f["request_count"] < min_requests:
            continue
        if f["dimension_kind"] == "product":
            st = product_state.get(f["dimension_key"])
            _emit("price_gap", f, context={"product_name": st.get("name") if st else None})

    # --- 5. ADD FAQ CONTENT — informația care lipsește ca să se poată vinde ----------------
    # `clarify_asked` are dimensiune reală (câmpul cerut) → acțiune concretă. `faq_miss` nu are
    # încă una (nu există topic la momentul lookup-ului) → rămâne un indicator de sănătate,
    # raportat ca atare, nu prezentat ca acțiune pe un subiect pe care nu-l cunoaștem.
    for f in _by_signal(facts, "clarify_asked"):
        if f["request_count"] >= min_requests and f["dimension_kind"] == "clarify_field":
            _emit("add_faq_content", f)

    out.sort(key=lambda a: (-a.count, a.kind, a.dimension_key))
    return [a.as_dict() for a in out]


def health_indicators(facts: list[dict[str, Any]]) -> dict[str, int]:
    """Indicatori care NU sunt acțiuni, dar spun ceva despre sănătatea sistemului.

    `faq_miss` intră aici, nu în acțiuni: știm CÂTE întrebări n-au primit răspuns din FAQ, dar nu
    știm DESPRE CE erau (gruparea semantică e Faza 4). „Botul ratează N întrebări" e onest;
    „scrie un FAQ despre X" ar fi inventat."""
    return {
        "faq_misses": sum(f["request_count"] for f in facts if f["signal_kind"] == "faq_miss"),
        "clarifications": sum(
            f["request_count"] for f in facts if f["signal_kind"] == "clarify_asked"
        ),
    }
