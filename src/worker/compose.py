"""Compoziția deterministă a recomandării bogate (model iZi) — NX-richreply.

Pur: fără I/O, fără LLM, fără DB. Input = JSON-ul structurat al agentului (intro +
referințe `product_id` + pro_index + fit_clause + pick + education + chip_intents) și
produsele retrievate în acest tur. Output = un `RichReply` neutru de canal, în care
TOATE faptele (preț, rating, link, badge) vin din retrieval, iar singurul text al
modelului (`fit_clause`, `intro`, `education`, `justification`) e trecut printr-un
scrub dur. Garanția anti-halucinație:

  1. set-membership: un `product_id` care nu e în retrieval → DROP tăcut (imbatabil).
  2. prețuri/rating/linkuri NU vin din model → nimic de inventat pe ele.
  3. proza liberă (fit/intro/education/justificare) → scrub: cifre / procente /
     claim-uri (stele/recenzii/livrare/reducere) / superlative neverificabile → DROP
     câmpul la None (cardul rămâne real). Ancora factuală (`top_pro`) e DATĂ, nu scrub.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from src.models import Chip, RichItem, RichReply

if TYPE_CHECKING:
    from src.models import TurnContext

# Disclaimer per-locale (ține în sync cu greeting._WELCOME[..]['disclaimer'], art. 50 AI Act).
_DISCLAIMER: dict[str, str] = {
    "ro": "Funcționez cu inteligență artificială, așa că pot greși uneori.",
    "en": "I run on artificial intelligence, so I can be wrong sometimes.",
    "hu": "Mesterséges intelligenciával működöm, ezért néha tévedhetek.",
}

# --- scrub proză LLM (validatorul de proză, extins per audit) ----------------
_DIGIT = re.compile(r"\d")
_PCT = re.compile(r"%|\bla sută\b", re.IGNORECASE)
_CLAIMY = re.compile(
    r"\b(stele|stea|recenzii|review|rating|zile|ore|livrare|reducere|garan)\w*", re.IGNORECASE
)
_SUPER = re.compile(
    r"\b(cel mai|cea mai|cei mai|cele mai|nr\.?\s*1|#\s*1|best\s*seller"
    r"|recomandat de specialiști)\b",
    re.IGNORECASE,
)


def scrub_prose(s: str | None) -> str | None:
    """Proza LLM poate referi NEVOIA clientului, nu fapte cuantificate. Strecoară cifre /
    procente / claim-uri / superlative neverificabile → DROP (None). Faptele reale vin
    din card, randate de cod. Drop, nu retry (degradare grațioasă)."""
    if not s:
        return None
    t = " ".join(s.split())
    if not t:
        return None
    if _DIGIT.search(t) or _PCT.search(t) or _CLAIMY.search(t) or _SUPER.search(t):
        return None
    return t


def _safe_badge(label: str | None) -> str | None:
    """Acceptă DOAR badge-uri de curare (ex. „Top Favorite"). Tag-urile de discount
    („-50%", „reducere") sunt respinse: ar afirma un markdown pe care validatorul nu-l
    poate verifica față de prețul original (și prețul afișat e deja min(variant))."""
    if not label:
        return None
    t = label.strip()
    if not t or "%" in t or "-" in t or any(c.isdigit() for c in t):
        return None
    if re.search(r"\b(reducere|discount|off|sale|promo)\b", t, re.IGNORECASE):
        return None
    return t


def _pros(p: dict[str, Any]) -> list[str]:
    """Avantajele reale ale produsului (din recenzii, D3): preferă lista `top_pros`,
    fallback pe `review_pro` (un singur pro din search). Doar string-uri ne-goale."""
    raw = p.get("top_pros") or ([p["review_pro"]] if p.get("review_pro") else [])
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()]


def _join_reason(fit: str | None, anchor: str | None) -> str | None:
    """Motivul cardului = clauza de potrivire (LLM, scrubuită) — avantaj real (dată)."""
    if fit and anchor:
        return f"{fit} — {anchor}"
    return fit or anchor


def disclaimer(language: str | None) -> str:
    return _DISCLAIMER.get(language or "ro") or _DISCLAIMER["ro"]


def _suggestion_chips(suggestions: list[str]) -> list[Chip]:
    """Chips = mesaje de follow-up DIN PARTEA CLIENTULUI, generate de model pe contextul lui
    (NU hardcodate). Apăsarea trimite `label` ca mesaj NOU → reintră în pipeline ca tur nou
    → e voce de client, nu afirmație a botului, deci FĂRĂ scrub. Doar normalizare: trim,
    dedupe, scurtează (limita butonului), cap 4."""
    out: list[Chip] = []
    seen: set[str] = set()
    for s in suggestions:
        if not isinstance(s, str):
            continue
        label = " ".join(s.split()).strip(" .")
        if len(label) > 48:
            label = label[:47].rstrip() + "…"
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        out.append(Chip(label=label, payload=label))
        if len(out) >= 4:
            break
    return out


def assemble(ctx: TurnContext, j: dict[str, Any], retrieved: list[dict[str, Any]]) -> RichReply:
    """Asamblează `RichReply` din JSON-ul modelului + produsele retrievate. Hidratează
    fiecare card din `facts` (preț/rating/link/badge), motivul = fit scrubuit + pro real;
    id necunoscut → drop tăcut; cap 6, dedupe."""
    facts = {p["id"]: p for p in retrieved if p.get("id")}
    items: list[RichItem] = []
    seen: set[str] = set()
    for it in j.get("items") or []:
        pid = it.get("product_id")
        p = facts.get(pid)
        if p is None or pid in seen:
            continue
        seen.add(pid)
        pros = _pros(p)
        idx = it.get("pro_index")
        in_range = isinstance(idx, int) and 0 <= idx < len(pros)
        anchor = pros[idx] if in_range else (pros[0] if pros else None)
        rc = p.get("review_count")
        items.append(
            RichItem(
                product_id=pid,
                name=p["name"],
                price=float(p["price"]),
                reason=_join_reason(scrub_prose(it.get("fit_clause")), anchor),
                url=p.get("url"),
                image=p.get("image"),
                rating=float(p["rating"]) if p.get("rating") is not None else None,
                review_count=int(rc) if rc else None,
                badge=_safe_badge(p.get("badge")),
            )
        )
        if len(items) >= 6:
            break

    pick: tuple[str, str] | None = None
    pj = j.get("pick")
    if isinstance(pj, dict) and pj.get("product_id") in facts:
        anchor = (_pros(facts[pj["product_id"]]) or [None])[0]
        reason = _join_reason(scrub_prose(pj.get("justification")), anchor)
        if reason:
            pick = (pj["product_id"], reason)

    return RichReply(
        intro=scrub_prose(j.get("intro")),
        items=items,
        pick=pick,
        education=scrub_prose(j.get("education")),
        chips=_suggestion_chips(j.get("suggestions") or []),
        disclaimer=disclaimer(ctx.language),
    )


def card_products(items: list[RichItem]) -> list[dict[str, Any]]:
    """Carduri compacte (pt cache signature + state refs): product_id + price obligatorii."""
    return [
        {
            "product_id": it.product_id,
            "name": it.name,
            "price": it.price,
            "url": it.url,
            "image": it.image,
        }
        for it in items
    ]


def flatten(rich: RichReply) -> str:
    """Aplatizare deterministă în text — floor-ul pentru canale fără rich (WhatsApp),
    messages.body, log și cache. Toate cifrele vin din card (cod), nu din proză."""
    lines: list[str] = []
    if rich.intro:
        lines += [rich.intro, ""]
    for i, it in enumerate(rich.items, 1):
        head = f"{i}. {it.name} — {it.price:.2f} lei"
        if it.rating:
            head += f"  ⭐{it.rating:.1f}"
        if it.badge:
            head += f"  • {it.badge}"
        lines.append(head)
        if it.reason:
            lines.append(f"   {it.reason}")
    if rich.pick:
        name = next((it.name for it in rich.items if it.product_id == rich.pick[0]), None)
        head = f"👉 Recomandarea mea: {name} — " if name else "👉 "
        lines += ["", head + rich.pick[1]]
    if rich.education:
        lines += ["", rich.education]
    if rich.chips:
        lines += ["", "Poți cere și: " + " · ".join(c.label for c in rich.chips)]
    if rich.disclaimer:
        lines += ["", rich.disclaimer]
    return "\n".join(lines).strip()
