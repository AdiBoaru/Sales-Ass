"""NX-183 — ResponseEnvelope V2-light: `lead` liber + motive compuse DETERMINIST din evidence OPACE.

Decizia de produs (închisă): modelul scrie `lead` (conversațional, scrub ca `intro`) + selectează,
per produs, `evidence_ids` DINTR-UN MENIU OPAC generat de cod (e1, e2… mapate la fapte reale) +
`reason_style` (enum). CODUL compune microcopy-ul factual din evidence — modelul NU poate inventa
dovezi (nu există căi semantice ghicibile). Prețurile/numele/linkurile rămân injectate de cod.

Pur: zero I/O. `_finalize_v2` (finalize.py) cheamă modelul + adaptează la `RichReply` (carduri) sau
text-only. Gated de `response_envelope_v2_effective` (global AND businesses.settings). OFF = rich.
"""

from __future__ import annotations

from typing import Any

from src.config import get_settings
from src.worker.text_scrub import has_medical_claim

# Stiluri de motiv (enum închis) → conector per-locale. Microcopy compact, nu propoziție rigidă.
_STYLE_LEAD: dict[str, dict[str, str]] = {
    "best_if": {"ro": "Bun dacă vrei", "en": "Best if you want", "hu": "Jó, ha"},
    "good_for": {"ro": "Potrivit pentru", "en": "Good for", "hu": "Alkalmas"},
    "note": {"ro": "De reținut", "en": "Note", "hu": "Megjegyzés"},
}


def response_envelope_v2_effective(business: Any) -> bool:
    """NX-183: flag EFECTIV = global master AND opt-in per business; fail-closed pe
    lipsă/false/invalid (strict `is True`). Single source — finalize nu citește global-ul."""
    if not getattr(get_settings(), "response_envelope_v2_enabled", False):
        return False
    settings = getattr(business, "settings", None)
    return isinstance(settings, dict) and settings.get("response_envelope_v2_enabled") is True


V2_SCHEMA: dict[str, Any] = {
    "name": "response_envelope_v2",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["lead", "products", "answer", "follow_up"],
        "properties": {
            "lead": {"type": "string"},
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["product_id", "evidence_ids", "reason_style"],
                    "properties": {
                        "product_id": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "reason_style": {
                            "type": "string",
                            "enum": ["best_if", "good_for", "note"],
                        },
                    },
                },
            },
            # răspuns FACTUAL text-only (ex. „care e mai lejeră?") — codul compune afirmația din
            # evidence; `presentation: inline` = fără card. `product_id` gol/null → fără answer.
            "answer": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["product_id", "evidence_ids", "presentation"],
                "properties": {
                    "product_id": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "presentation": {"type": "string", "enum": ["inline", "card"]},
                },
            },
            "follow_up": {"type": ["string", "null"]},
        },
    },
}


def _evidence_facts(product: dict[str, Any]) -> list[str]:
    """Faptele REALE ale unui produs, ordine stabilă (avantaje din recenzii + reason_codes deja
    materializate ca `best_for`/`reasons` dacă există). Doar ce e verificabil în date."""
    facts: list[str] = []
    for key in ("best_for", "reasons"):
        v = product.get(key)
        if isinstance(v, list):
            facts += [str(x).strip() for x in v if isinstance(x, str) and x.strip()]
    raw = product.get("top_pros") or ([product["review_pro"]] if product.get("review_pro") else [])
    facts += [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    # P0-safety (Codex): faptele intră DIRECT în microcopy-ul servit (fit_clause pe card SAU text-
    # only via set_reply), care NU trece prin `validate_prose`. Scrub la SURSĂ: un fapt cu claim
    # medical/terapeutic („tratează acneea în 7 zile", dintr-o recenzie) e ELIMINAT din meniu →
    # modelul nu-l poate selecta, codul nu-l poate compune. Gated de kill-switch (ca restul
    # guardrail-ului medical). Dedup păstrând ordinea.
    med_guard = get_settings().safety_medical_guardrail_enabled
    seen: list[str] = []
    for f in facts:
        if f in seen:
            continue
        if med_guard and has_medical_claim(f):
            continue
        seen.append(f)
    return seen[:4]


def evidence_menu(products: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Meniu OPAC de evidence per produs: `{product_id: {eid: fapt}}` cu eid = `e{i}_{j}` (generat
    de cod). Modelul referă DOAR eid-uri → nu poate inventa dovezi. Fapte = `_evidence_facts`."""
    menu: dict[str, dict[str, str]] = {}
    for i, p in enumerate(products):
        pid = str(p.get("id") or p.get("product_id") or "")
        if not pid:
            continue
        menu[pid] = {f"e{i}_{j}": fact for j, fact in enumerate(_evidence_facts(p))}
    return menu


def render_evidence_menu(menu: dict[str, dict[str, str]]) -> str:
    """Textul meniului de evidence pentru mesajul USER (id opac → fapt), grupat pe produs."""
    lines: list[str] = []
    for pid, items in menu.items():
        for eid, fact in items.items():
            lines.append(f"[{eid}] {fact}  (produs {pid})")
    return "\n".join(lines) or "(fără dovezi)"


def compose_reason(
    product_id: str,
    evidence_ids: list[str],
    reason_style: str,
    menu: dict[str, dict[str, str]],
    language: str | None,
) -> str:
    """Microcopy DETERMINIST din evidence VALIDATE (id-uri care aparțin produsului în meniu). Un id
    invalid e ignorat. Gol dacă nu rămâne nicio dovadă. Conector din `reason_style` per-locale."""
    valid = menu.get(product_id, {})
    facts = [valid[e] for e in evidence_ids if e in valid]
    if not facts:
        return ""
    lang = (language or "ro").lower()
    lead = _STYLE_LEAD.get(reason_style, _STYLE_LEAD["good_for"])
    conn = lead.get(lang) or lead.get("ro") or ""
    body = " · ".join(facts)
    return f"{conn} {body}".strip() if conn else body
