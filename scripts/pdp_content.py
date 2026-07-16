"""NX-168e-2 — conținut PDP derivat DETERMINIST din faptele deja la contract v3 (168e-1).

Pur (fără DB, fără random): din `attributes` (usage/key_benefit/key_ingredients/routine_step/
not_recommended_for/fragrance_free/spf) + `reviewSummary` produce secțiuni, listă de ingrediente,
badge-uri de trust și recenzii individuale plauzibile. Idempotent la seed (`external_id`/`slug`
stabile). NU inventează fapte — reformulează ce e deja în catalog.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

USAGE_RO = {
    "morning": "dimineața",
    "evening": "seara",
    "daily": "zilnic",
    "occasional": "ocazional",
}
# autori demo (rotați determinist după hash-ul slug-ului — fără random, ca seed-ul să fie stabil)
_AUTHORS = (
    "Ioana P.",
    "Andrei M.",
    "Maria D.",
    "Elena R.",
    "Cristina V.",
    "Alex T.",
    "Ana S.",
    "Bogdan L.",
)


def slugify(name: str) -> str:
    d = unicodedata.normalize("NFKD", (name or "").lower())
    ascii_ = "".join(c for c in d if not unicodedata.combining(c))
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_)).strip("-")


def _attrs(p: dict) -> dict:
    a = p.get("attributes")
    return a if isinstance(a, dict) else {}


def sections(p: dict) -> list[dict[str, str]]:
    """Secțiuni PDP: cum se folosește / beneficii / ingrediente-cheie / de reținut (avertizări)."""
    a = _attrs(p)
    out: list[dict[str, str]] = []
    times = (a.get("usage") or {}).get("time") or []
    if times:
        body = "Se folosește " + ", ".join(USAGE_RO.get(t, t) for t in times) + "."
        out.append({"kind": "usage", "title": "Cum se folosește", "body": body})
    kb = (a.get("key_benefit") or "").strip()
    if kb:
        body = kb if kb.endswith(".") else kb + "."
        bf = a.get("best_for")
        if bf:
            body += f" Recomandat pentru {bf}."
        out.append({"kind": "benefits", "title": "Beneficii", "body": body})
    ki = a.get("key_ingredients") or []
    if ki:
        out.append(
            {
                "kind": "ingredients",
                "title": "Ingrediente-cheie",
                "body": "Conține " + ", ".join(ki) + ".",
            }
        )
    warns = [
        x.get("value")
        for x in a.get("not_recommended_for") or []
        if isinstance(x, dict) and x.get("value")
    ]
    if warns:
        out.append(
            {
                "kind": "warnings",
                "title": "De reținut",
                "body": "Nepotrivit pentru: " + ", ".join(warns) + ".",
            }
        )
    return out


def ingredient_list(p: dict) -> list[str]:
    """Ingredientele-cheie (pt tabelul normalizat `ingredients` + `product_ingredients`)."""
    return list(_attrs(p).get("key_ingredients") or [])


def badges(p: dict) -> list[str]:
    """Badge-uri de trust DERIVATE din atribute reale (nu inventate)."""
    a = _attrs(p)
    out: list[str] = []
    if a.get("fragrance_free") is True:
        out.append("Fără parfum")
    if a.get("spf"):
        out.append(f"Cu SPF {a['spf']}")
    if float(p.get("rating") or 0) >= 4.7:
        out.append("Best-seller")
    return out


def reviews(p: dict) -> list[dict[str, Any]]:
    """Recenzii individuale plauzibile din `reviewSummary` (pros→pozitiv, con→mixt). Determinist:
    autor din hash-ul slug-ului, `external_id` stabil pt idempotență."""
    rs = p.get("reviewSummary") or {}
    pros = [s for s in (rs.get("topPros") or []) if s]
    cons = [s for s in (rs.get("topCons") or []) if s]
    rating = int(round(float(p.get("rating") or 4.5)))
    slug = p.get("slug", "")
    h = sum(ord(c) for c in slug)
    out: list[dict[str, Any]] = []
    for pro in pros[:2]:
        i = len(out)
        out.append(
            {
                "external_id": f"{slug}-r{i + 1}",
                "author": _AUTHORS[(h + i) % len(_AUTHORS)],
                "rating": min(5, rating + 1),
                "body": f"{pro[0].upper()}{pro[1:]}. Recomand!",
            }
        )
    if cons:
        i = len(out)
        out.append(
            {
                "external_id": f"{slug}-r{i + 1}",
                "author": _AUTHORS[(h + i) % len(_AUTHORS)],
                "rating": max(3, rating - 1),
                "body": f"În general mulțumită, dar {cons[0][0].lower()}{cons[0][1:]}.",
            }
        )
    return out
