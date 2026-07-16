"""NX-170 — reason_codes + gate `not_recommended_for` (DETERMINIST, fără LLM).

`reason_codes` = DE CE se potrivește produsul cu CEREREA (concern/budget/ingredient match) — semnal
intern consumat de proiecție (NX-169) și de golden (NX-172). `not_recommended_for` = excludere DURĂ
(`level='hard'` + verificat) SAU penalizare + atenționare grounded (`soft`). Pur, testabil pe dict.
NICIODATĂ nu transformăm o inferență în excludere dură (doar hard+source+verified_at exclude).
"""

from __future__ import annotations

import unicodedata
from typing import Any


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _attrs(product: dict) -> dict:
    a = product.get("attributes")
    return a if isinstance(a, dict) else {}


def reason_codes(
    product: dict,
    *,
    concerns: list[str] | None = None,
    price_max: float | None = None,
    features: list[str] | None = None,
) -> list[str]:
    """Codurile de potrivire cu cererea (ordine stabilă): concern_match, budget_match,
    ingredient_match. Bază pt motivul CONTEXTUAL compus la proiecție (NX-169)."""
    a = _attrs(product)
    codes: list[str] = []
    prod_for = {_norm(x) for x in (a.get("concerns") or []) + (a.get("suitable_for") or [])}
    if concerns and prod_for & {_norm(c) for c in concerns}:
        codes.append("concern_match")
    if price_max is not None:
        pr = product.get("price")
        if pr is not None and float(pr) <= float(price_max):
            codes.append("budget_match")
    if features:
        ki = {_norm(x) for x in (a.get("key_ingredients") or [])}
        want = {_norm(f) for f in features}
        if any(any(w in k or k in w for k in ki) for w in want):
            codes.append("ingredient_match")
    return codes


def not_recommended_gate(
    product: dict, *, concerns: list[str] | None = None
) -> tuple[bool, str | None]:
    """`(exclus_hard, atenționare_soft)` pt cererea curentă. `hard` + `source` + `verified_at` pe un
    concern CERUT → excludere dură. `soft` (ori hard neverificat) → atenționare grounded, NU exclus.
    Fără concern cerut → nu se aplică (nu excludem preventiv)."""
    a = _attrs(product)
    want = {_norm(c) for c in (concerns or [])}
    if not want:
        return False, None
    soft: str | None = None
    for nrf in a.get("not_recommended_for") or []:
        if not isinstance(nrf, dict):
            continue
        val = _norm(str(nrf.get("value") or ""))
        if not val or val not in want:
            continue
        verified = nrf.get("source") and nrf.get("verified_at")
        if nrf.get("level") == "hard" and verified:
            return True, None  # excludere DURĂ (verificată)
        soft = f"nu e ideal pentru {nrf.get('value')}"  # soft ori hard-neverificat → atenționare
    return False, soft


def annotate(
    products: list[dict[str, Any]],
    *,
    concerns: list[str] | None = None,
    price_max: float | None = None,
    features: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filtrează EXCLUDERILE dure + atașează `reason_codes` și `warning` (soft) pe fiecare produs
    rămas. Ordinea (ranking) rămâne — soft doar penalizează + coboară la coadă (stabil)."""
    out: list[dict[str, Any]] = []
    for p in products:
        excluded, warn = not_recommended_gate(p, concerns=concerns)
        if excluded:
            continue
        p = dict(p)
        p["reason_codes"] = reason_codes(
            p, concerns=concerns, price_max=price_max, features=features
        )
        if warn:
            p["warning"] = warn
        out.append(p)
    # soft-penalizare: produsele cu atenționare coboară SUB cele fără (stabil, nu re-rank global)
    out.sort(key=lambda x: 1 if x.get("warning") else 0)
    return out
