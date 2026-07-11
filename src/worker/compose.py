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

from src.agent.fallbacks import _card_variants
from src.config import get_settings
from src.models import (
    Chip,
    Comparison,
    ComparisonColumn,
    ComparisonRow,
    Direction,
    Relevance,
    RichItem,
    RichReply,
)
from src.worker.badges import BADGE_TONE, badge_label, derive_badge_kind
from src.worker.text_scrub import (
    has_marketing_claim,
    has_medical_claim,
    has_stock_claim,
    has_unverifiable_claim,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.domain.pack import FacetSpec
    from src.models import TurnContext

# Disclaimer per-locale (ține în sync cu greeting._WELCOME[..]['disclaimer'], art. 50 AI Act).
_DISCLAIMER: dict[str, str] = {
    "ro": "Funcționez cu inteligență artificială, așa că pot greși uneori.",
    "en": "I run on artificial intelligence, so I can be wrong sometimes.",
    "hu": "Mesterséges intelligenciával működöm, ezért néha tévedhetek.",
}

# Eticheta pick-ului („Recomandarea mea") per-locale — text de UI pus de COD; numele + motivul vin
# din date / proza LLM scrubuită. Fallback pe 'ro'. (IZI-parity G1: pick-ul e vizibil pe web acum,
# deci eticheta trebuie să urmeze limba clientului — un bot HU/EN nu mai scoate „Recomandarea mea".)
_PICK_LABEL: dict[str, str] = {
    "ro": "👉 Recomandarea mea: ",
    "en": "👉 My pick: ",
    "hu": "👉 Az ajánlatom: ",
}


def _pick_label(language: str | None) -> str:
    return _PICK_LABEL.get(language or "ro") or _PICK_LABEL["ro"]


# izi-parity hardening — mesajul ONEST de redirect când retrievalul e OFF-CATEGORY (categoria
# greșită): întâi „nu am exact ce cauți", apoi „dar astea sunt cele mai apropiate + spune-mi".
# Determinist (proprietar UNIC al onestității → nu se contrazice cu proza modelului), per-locale.
# Cardurile rămân (alternative); pick-ul e suprimat (nu recomandăm ferm un produs din altă gamă).
_OFF_CATEGORY_INTRO: dict[str, str] = {
    "ro": (
        "Ca să fiu sincer, nu am exact ce cauți în gama de acum. Ți-am pus totuși mai jos cele "
        "mai apropiate opțiuni — s-ar putea să ți se potrivească. Spune-mi dacă vreuna merge în "
        "direcția bună sau caut cu totul altceva."
    ),
    "en": (
        "To be honest, I don't have exactly what you're looking for in the current range. Still, "
        "here are the closest options I could find — they might suit you. Tell me if any of these "
        "is heading in the right direction, or I'll look for something else entirely."
    ),
    "hu": (
        "Őszintén szólva, pontosan azt, amit keresel, most nincs a kínálatban. De összeszedtem a "
        "legközelebbi lehetőségeket — lehet, hogy megfelelnek. Mondd meg, ha valamelyik jó irányba "
        "mutat, vagy keresek valami egészen mást."
    ),
}


def _off_category_intro(language: str | None) -> str:
    return _OFF_CATEGORY_INTRO.get(language or "ro") or _OFF_CATEGORY_INTRO["ro"]


def _off_category(relevance: Relevance | None) -> bool:
    """izi-parity hardening: rezultatul e OFF-CATEGORY (categoria greșită)? True ⇒ `assemble`
    suprimă „👉 Recomandarea mea" + pune mesajul onest de redirect. Două semnale independente:
    `category_dropped` (filtrul de categorie cerut a fost renunțat — categorie inexistentă) SAU
    `top_cosine` peste prag (cel mai apropiat vector e semantic departe — prinde free-text fără
    filtru de categorie). Gated de kill-switch; `None`/OFF ⇒ False (fail-open). Jumătatea cosine
    e dezactivată dacă pragul e None sau distanța lipsește (calea lexical-only)."""
    if relevance is None:  # verificat ÎNAINTE de get_settings (fail-open fără a atinge configul)
        return False
    s = get_settings()
    if not s.rich_pick_relevance_gate_enabled:
        return False
    if relevance.category_dropped:
        return True
    floor = s.rich_pick_relevance_cosine_max
    return floor is not None and relevance.top_cosine is not None and relevance.top_cosine > floor


# IZI-parity (feedback Adi 2026-06-30): câte produse / chips afișăm în recomandarea bogată.
# Constante de PRODUS (decizii de UX, nu ops-tuning) — pick-ul pe web e separat un kill-switch în
# config (`rich_pick_web_enabled`). `_MAX_RICH_ITEMS` = câte carduri (modelul curează, codul taie la
# cap); `_MAX_CHIPS` = câte sugestii de follow-up (ca iZi: ~5-6, nu 3).
_MAX_RICH_ITEMS = 4
_MAX_CHIPS = 6

# --- scrub proză LLM (validatorul de proză) ----------------------------------
# NX-117: pattern-urile trăiesc în `text_scrub` (loc canonic partajat cu calea de proză).


def _unsafe_medical(t: str) -> bool:
    """P0-safety (CONV-COMMERCE): claim MEDICAL în proza modelului (tratează afecțiuni / sigur în
    sarcină / fără alergeni / recomandat de medic) → câmpul se DROP-uiește (cardul rămâne real,
    fără claim periculos). Gated de kill-switch (default ON). Răspundere juridică (beauty)."""
    return get_settings().safety_medical_guardrail_enabled and has_medical_claim(t)


def scrub_prose(s: str | None) -> str | None:
    """Proza LLM poate referi NEVOIA clientului, nu fapte cuantificate. Strecoară cifre /
    procente / claim-uri / superlative neverificabile → DROP (None). Faptele reale vin
    din card, randate de cod. Drop, nu retry (P0-safety: claim medical → DROP)."""
    if not s:
        return None
    t = " ".join(s.split())
    if not t:
        return None
    if has_unverifiable_claim(t):  # NX-117: digit + pct + claim + super (semantică neschimbată)
        return None
    if _unsafe_medical(t):  # P0-safety: sfat medical/terapeutic → DROP câmpul
        return None
    return t


def _allowed_client_numbers(ctx: TurnContext) -> set[str]:
    """Cifrele scrise de CLIENT (mesaj curent + mesajele LUI din istoric + constrângeri știute).
    Permise în intro (ex. bugetul „sub 80 lei"). NU includem replicile botului (ar reintroduce
    prețuri de produs scrubuite). R4."""
    out: set[str] = set(re.findall(r"\d+", ctx.message.body or ""))
    for m in ctx.history:
        if m.direction == Direction.INBOUND:
            out |= set(re.findall(r"\d+", m.body or ""))
    for v in (ctx.state.constraints or {}).values():
        out |= set(re.findall(r"\d+", str(v)))
    return out


def scrub_intro(s: str | None, allowed_numbers: set[str]) -> str | None:
    """Ca `scrub_prose`, dar PERMITE cifrele pe care CLIENTUL le-a scris (bugetul lui) — intro-ul
    reia nevoia lui în cuvintele lui, deci un buget pe care el l-a dat NU e halucinație (repară
    „Ai ceva sub lei", R4). Cifre NEcunoscute (inventate), procente, claim-uri, superlative → DROP
    (ca scrub_prose). Așa intro-ul nu mai iese trunchiat, dar nici nu strecoară un preț inventat."""
    if not s:
        return None
    t = " ".join(s.split())
    if not t:
        return None
    unknown = [n for n in re.findall(r"\d+", t) if n not in allowed_numbers]
    if unknown or has_marketing_claim(t) or _unsafe_medical(t):
        # NX-117: pct + claim + super (cifrele clientului permise). P0-safety: claim medical → DROP.
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
    """Motivul cardului = clauza de potrivire (LLM, scrubuită) — avantaj real (dată). Dedup:
    dacă clauza modelului și avantajul real sunt cvasi-identice (unul îl conține pe altul, după
    lower+collapse), NU le lipi „X — X" (bug live: „…confortabilă și calmă — …confortabilă și
    calmă") — păstrează clauza modelului (mai contextuală)."""
    if fit and anchor:
        nf = " ".join(fit.lower().split())
        na = " ".join(anchor.lower().split())
        if na in nf or nf in na:  # cvasi-duplicat → o singură dată
            return fit
        return f"{fit} — {anchor}"
    return fit or anchor


def disclaimer(language: str | None) -> str:
    return _DISCLAIMER.get(language or "ro") or _DISCLAIMER["ro"]


def ensure_disclaimer(text: str | None, language: str | None) -> str:
    """Garantează că `text` se termină cu disclaimer-ul AI (art. 50 AI Act), o singură dată.

    Idempotent: dacă disclaimer-ul (pe ORICE locale cunoscut) e deja prezent, nu-l re-adaugă —
    welcome (greeting) și rich (`flatten`) îl pun deja, iar un text scris în `ro` rămâne acoperit
    chiar dacă `ctx.language` a derivat altceva între timp (set-membership pe toate locale-urile).
    Pur, fără I/O. Aplicat DOAR la Sender (P5) → acoperă toate rutele, fără a atinge stagiile."""
    body = (text or "").rstrip()
    if not get_settings().ai_disclaimer_enabled:
        return body  # #2: disclaimer OFF (default) → text neatins (gate unic pt toate canalele)
    d = disclaimer(language)
    if any(known in body for known in _DISCLAIMER.values()):
        return body
    return f"{body}\n\n{d}" if body else d


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
        if len(out) >= _MAX_CHIPS:
            break
    return out


def _stock_available(retrieved: list[dict[str, Any]]) -> bool:
    """Vreun produs retrievat e efectiv pe stoc (in_stock/low_stock)? (NX-118)"""
    return any((p.get("availability") or "") in ("in_stock", "low_stock") for p in retrieved)


def _drop_unfounded_stock(text: str | None, stock_present: bool) -> str | None:
    """NX-118: pe calea bogată, dacă framing-ul afirmă disponibilitate dar niciun produs nu e pe
    stoc → drop (None). Gated FAIL-OPEN de `validator_stock_claims_enabled`."""
    if not text or stock_present or not get_settings().validator_stock_claims_enabled:
        return text
    return None if has_stock_claim(text) else text


def scrub_education(
    s: str | None, stock_present: bool, allowed_numbers: set[str] | frozenset[str] = frozenset()
) -> str | None:
    """Coaching de final (`education`), scrubuit la nivel de PROPOZIȚIE (IZI-parity, G4). Vechiul
    comportament arunca TOT paragraful la o singură propoziție „murdară" (un „SPF 30" ucidea și
    sfatul util de lângă) → coaching-ul dispărea des, iar răspunsul părea mai subțire decât iZi
    (care consultă la fiecare tur). Granular = păstrăm propozițiile SIGURE, aruncăm doar pe cele cu
    cifre NEpermise/procente/claim/superlativ/medical sau cu o afirmație de stoc nefondată
    (`_drop_unfounded_stock`). NX-139: `allowed_numbers` (cifrele clientului + cifrele de
    SPECIFICAȚIE din produsele afișate, `spec_numbers`) supraviețuiesc — „SPF 30" grounded nu mai
    ucide sfatul (exact ce face iZi expert). Gol → semantica veche, byte-identic. Agnostic de
    vertical; toate pică → None. O singură propoziție = identic cu vechiul comportament."""
    if not s:
        return None
    t = " ".join(s.split())
    if not t:
        return None
    kept: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", t):
        safe = _drop_unfounded_stock(scrub_intro(sent, allowed_numbers), stock_present)
        if safe:
            kept.append(safe)
    return " ".join(kept) or None


def _select_pick(
    j: dict[str, Any],
    facts: dict[str, dict[str, Any]],
    items: list[RichItem],
    stock_present: bool,
    deterministic: bool,
) -> tuple[str, str] | None:
    """„Recomandarea mea" (pick). ARCH-2026 P0 (determinist): produsul cel mai bine CLASAT afișat
    (`items[0]`, deja în ordinea de ranking) — NU alegerea liberă a modelului (popularity/position
    bias: ar pune cel mai ieftin/primul în prompt). Reia justificarea modelului DOAR dacă a ales
    același produs; altfel ancora reală (top_pro din recenzii). Kill-switch OFF → pick-ul liber al
    modelului (byte-identic). Motiv gol după scrub/stoc → fără pick (degradare, nu pick fals)."""
    pj = j.get("pick")
    if deterministic:
        if not items:
            return None
        top = items[0]
        just = (
            scrub_prose(pj.get("justification"))
            if isinstance(pj, dict) and pj.get("product_id") == top.product_id
            else None
        )
        anchor = (_pros(facts[top.product_id]) or [None])[0]
        reason = _drop_unfounded_stock(_join_reason(just, anchor), stock_present)
        return (top.product_id, reason) if reason else None
    # Legacy (kill-switch OFF): pick-ul liber al modelului, ancorat pe un pro real.
    if isinstance(pj, dict) and pj.get("product_id") in facts:
        anchor = (_pros(facts[pj["product_id"]]) or [None])[0]
        reason = _drop_unfounded_stock(
            _join_reason(scrub_prose(pj.get("justification")), anchor), stock_present
        )
        if reason:
            return (pj["product_id"], reason)
    return None


def assemble(ctx: TurnContext, j: dict[str, Any], retrieved: list[dict[str, Any]]) -> RichReply:
    """Asamblează `RichReply` din JSON-ul modelului + produsele retrievate. Hidratează
    fiecare card din `facts` (preț/rating/link/badge), motivul = fit scrubuit + pro real;
    id necunoscut → drop tăcut; cap la `_MAX_RICH_ITEMS`, dedupe."""
    facts = {p["id"]: p for p in retrieved if p.get("id")}
    # NX-118: stoc availability-aware — orice „în stoc" din proza modelului (reason/pick/intro/
    # education) cade dacă NICIUN produs retrievat nu e pe stoc (gated fail-open de kill-switch).
    stock_present = _stock_available(retrieved)
    # IZI: badge DERIVAT (Top Favorit / Super Preț) din semnale reale, prin pragurile DomainPack.
    # Badge-ul pre-seedat curat (rar) are prioritate; gated de kill-switch (OFF → vechi).
    badges_on = get_settings().card_badges_enabled
    pack = getattr(ctx.business, "domain_pack", None)
    badge_rules = pack.badge_rules if pack else None
    currency = getattr(pack, "currency", None)  # Full-eMAG: moneda pe card (din DomainPack)
    # Modelul NAREAZĂ per-produs (fit_clause/pro_index), keyed pe product_id; CODUL decide ORDINEA.
    # Membership (P1): id care nu e în retrieval → ignorat tăcut. Dedupe pe prima apariție.
    llm_items: dict[str, dict[str, Any]] = {}
    llm_order: list[str] = []
    for it in j.get("items") or []:
        pid = it.get("product_id")
        if pid in facts and pid not in llm_items:
            llm_items[pid] = it
            llm_order.append(pid)

    # ARCH-2026 P0: ordinea cardurilor = RANKINGUL de retrieval (determinist), nu ordinea liberă a
    # modelului (position bias). Modelul CURATEAZĂ ce produse intră (setul lui de items), rankingul
    # decide ORDINEA. `retrieved` vine deja rankat (fuziune blended P0). Kill-switch OFF → ordinea
    # modelului (byte-identic).
    deterministic = get_settings().rich_pick_deterministic_enabled
    ordered_ids = (
        [str(p["id"]) for p in retrieved if p.get("id") in llm_items]
        if deterministic
        else llm_order
    )

    def _build(pid: str) -> RichItem:
        p = facts[pid]
        it = llm_items[pid]
        pros = _pros(p)
        idx = it.get("pro_index")
        in_range = isinstance(idx, int) and 0 <= idx < len(pros)
        anchor = pros[idx] if in_range else (pros[0] if pros else None)
        rc = p.get("review_count")
        eff = float(p["price"])
        lp = p.get("list_price")  # preț de listă DOAR la reducere reală (SQL: case when on_sale)
        # Full-eMAG: badge cu TON semantic (deal→danger, top→info) + `details` extins din ai_summary
        # (catalog, medical-guarded, cap 400). `seeded` (badge pre-curat) n-are kind → fără ton.
        seeded = _safe_badge(p.get("badge"))
        kind = derive_badge_kind(p, badge_rules) if (badges_on and not seeded) else None
        ai = " ".join((p.get("ai_summary") or "").split())[:400]
        return RichItem(
            product_id=pid,
            name=p["name"],
            price=eff,
            reason=_drop_unfounded_stock(
                _join_reason(scrub_prose(it.get("fit_clause")), anchor), stock_present
            ),
            url=p.get("url"),
            image=p.get("image"),
            rating=float(p["rating"]) if p.get("rating") is not None else None,
            review_count=int(rc) if rc else None,
            badge=seeded or badge_label(kind, ctx.language),
            badge_tone=BADGE_TONE.get(kind) if kind else None,
            list_price=float(lp) if lp is not None and float(lp) > eff else None,
            currency=currency,
            details=ai if (ai and not _unsafe_medical(ai)) else None,
            variants=_card_variants(p),
        )

    items: list[RichItem] = [_build(pid) for pid in ordered_ids[:_MAX_RICH_ITEMS]]

    # izi-parity hardening: retrieval OFF-CATEGORY (produse din categoria greșită — ex. „fond de
    # ten" pe catalog skincare) → NU pretinde o recomandare. Suprimă pick-ul ȘI înlocuiește intro-ul
    # cu mesajul ONEST de redirect (un SINGUR proprietar al onestității → dispare contradicția
    # „nu am fond de ten" din proza modelului vs „👉 Recomandarea mea: <alt produs>" din cod).
    # Cardurile rămân ca ALTERNATIVE apropiate. Fail-open: fără semnal / gate OFF ⇒ vechi.
    retrieval = getattr(ctx, "retrieval", None)
    relevance = getattr(retrieval, "relevance", None) if retrieval is not None else None
    # NX-139: cifrele permise în proză = ale CLIENTULUI (R4) + cifrele de SPECIFICAȚIE din
    # produsele AFIȘATE (nume/fațete, `spec_numbers` — gated). Prețurile nu intră (nu-s în nume).
    allowed_numbers = _allowed_client_numbers(ctx)
    if get_settings().spec_digits_grounded_enabled:
        pack = getattr(ctx.business, "domain_pack", None)
        shown = [facts[it.product_id] for it in items if it.product_id in facts]
        allowed_numbers |= spec_numbers(shown, pack.comparison_facets if pack else (), ctx.language)

    if _off_category(relevance):
        ctx.emit(
            "pick_suppressed",
            reason="off_category",
            category_dropped=bool(relevance and relevance.category_dropped),
            top_cosine=(relevance.top_cosine if relevance else None),
        )
        pick = None
        intro = _off_category_intro(ctx.language)
    else:
        pick = _select_pick(j, facts, items, stock_present, deterministic)
        intro = _drop_unfounded_stock(scrub_intro(j.get("intro"), allowed_numbers), stock_present)

    return RichReply(
        intro=intro,
        items=items,
        pick=pick,
        education=scrub_education(j.get("education"), stock_present, allowed_numbers),
        chips=_suggestion_chips(j.get("suggestions") or []),
        disclaimer=disclaimer(ctx.language) if get_settings().ai_disclaimer_enabled else None,
    )


def card_products(items: list[RichItem]) -> list[dict[str, Any]]:
    """Carduri compacte (pt cache signature + state refs): product_id + price obligatorii."""
    cards: list[dict[str, Any]] = []
    for it in items:
        card = {
            "product_id": it.product_id,
            "name": it.name,
            "price": it.price,
            "url": it.url,
            "image": it.image,
        }
        if it.variants:
            card["variants"] = it.variants
        cards.append(card)
    return cards


def comparison_cards(comparison: Comparison) -> list[dict[str, Any]]:
    """Carduri compacte ale produselor comparate (→ `displayed_products`, ca un follow-up «adaugă
    prima» să le regăsească; + cache signature). `price` = prețul afișat al coloanei."""
    return [
        {
            "product_id": c.product_id,
            "name": c.name,
            "price": c.price,
            "url": c.url,
            "image": c.image,
        }
        for c in comparison.columns
    ]


# Intro-uri pt linia de chips pe floor-ul TEXT (WhatsApp/cache) — 2-3 variante naturale per limbă,
# alese DETERMINIST din conținut (același reply → același text: cache-safe), ca linia să nu sune
# „template" la fiecare recomandare. Web omite linia (chips = butoane); Telegram are calea lui.
_CHIP_LEADS: dict[str, tuple[str, ...]] = {
    "ro": ("Dacă vrei, putem continua cu:", "Îți mai pot arăta:", "Sau, dacă preferi:"),
    "en": ("If you like, we can continue with:", "I can also show you:", "Or, if you prefer:"),
    "hu": ("Ha szeretnéd, folytathatjuk:", "Ezt is meg tudom mutatni:", "Vagy, ha inkább:"),
}


def _chip_lead(language: str | None, seed: str) -> str:
    """Un intro natural pt linia de chips, ales DETERMINIST din `seed` (conținutul chips-urilor) →
    același reply produce mereu același text (cache-safe), dar recomandări diferite variază."""
    variants = _CHIP_LEADS.get((language or "ro").lower()) or _CHIP_LEADS["ro"]
    return variants[sum(ord(c) for c in seed) % len(variants)]


def flatten(rich: RichReply, language: str | None = None) -> str:
    """Aplatizare deterministă în text — floor-ul pentru canale fără rich (WhatsApp),
    messages.body, log și cache. Toate cifrele vin din card (cod), nu din proză. `language` →
    eticheta pick-ului în limba clientului (fallback 'ro' = byte-identic cu vechiul comport.)."""
    lines: list[str] = []
    if rich.intro:
        lines += [rich.intro, ""]
    for i, it in enumerate(rich.items, 1):
        head = f"{i}. {it.name} — {it.price:.2f} lei"
        if it.list_price and it.list_price > it.price:  # IZI-anchor: preț redus în floor
            head += f" (de la {it.list_price:.2f})"
        if it.rating:
            head += f"  ⭐{it.rating:.1f}"
        if it.badge:
            head += f"  • {it.badge}"
        lines.append(head)
        if it.reason:
            lines.append(f"   {it.reason}")
    # Linia „👉 Recomandarea mea" e OFF pe TOATE canalele (preferința fermă a userului) — gate ȘI pe
    # floor (WhatsApp/Telegram/cache), nu doar pe web (`flatten_framing`). Reactivabil din env.
    if rich.pick and get_settings().rich_pick_web_enabled:
        name = next((it.name for it in rich.items if it.product_id == rich.pick[0]), None)
        head = f"{_pick_label(language)}{name} — " if name else "👉 "
        lines += ["", head + rich.pick[1]]
    if rich.education:
        lines += ["", rich.education]
    if rich.chips:
        labels = [c.label for c in rich.chips]
        lines += ["", f"{_chip_lead(language, ''.join(labels))} " + " · ".join(labels)]
    if rich.disclaimer:
        lines += ["", rich.disclaimer]
    return "\n".join(lines).strip()


def flatten_framing(rich: RichReply, language: str | None = None) -> str:
    """Aplatizare pentru canalele care randează produsele ca CARDURI (widget web, /web/chat):
    framing conversațional UȘOR și VARIABIL ca structură (#4 — evită tiparul identic la fiecare
    mesaj): intro + (opțional) recomandarea („pick") + coaching de final.

    OMITE: enumerarea numerotată (o fac cardurile) și linia „Poți cere și:" (o fac chips-urile).

    IZI-parity (Tier 1, G1): pe WEB pick-ul („Recomandarea mea") e VIZIBIL by default — ca iZi care
    se angajează la o recomandare clară (răspunsul fără el părea mai „subțire"). Gated de
    `rich_pick_web_enabled` (ON; OFF → ascuns, varianta din 2026-06-30). Eticheta urmează
    `language`. La un SINGUR produs nu se pune oricum (cardul ESTE recomandarea).

    IZI-coaching: `education` (paragraful „cum alegi" + cross-sell) revine ca PARAGRAF DE FINAL pe
    widget (gap-ul iZi — botul listează, nu consultă). E scrub-uit (fără cifre/claim-uri), deci
    sigur. `flatten()` rămâne floor-ul COMPLET pt canalele fără carduri (WhatsApp/cache)."""
    blocks: list[str] = []
    if rich.intro:
        blocks.append(rich.intro)
    # „pick" doar dacă e PORNIT pe web (default OFF) ȘI departajează ≥2 produse (la unul singur
    # cardul vorbește de la sine). Pe WhatsApp `flatten` îl pune oricum — vezi docstring.
    if get_settings().rich_pick_web_enabled and rich.pick and len(rich.items) > 1:
        name = next((it.name for it in rich.items if it.product_id == rich.pick[0]), None)
        head = f"{_pick_label(language)}{name} — " if name else "👉 "
        blocks.append(head + rich.pick[1])
    if rich.education:  # coaching de final (model iZi) — randat și pe widget acum
        blocks.append(rich.education)
    if rich.disclaimer:
        blocks.append(rich.disclaimer)
    return "\n\n".join(blocks).strip()


# --- comparație structurată (model iZi) --------------------------------------
# Etichetele de RÂND + disponibilitate sunt per-locale (text de UI, NU vocabular de rutare).
# Faptele din celule (preț/rating/avantaje) vin DOAR din retrieval → zero halucinație.
_COMPARE_LABELS: dict[str, dict[str, str]] = {
    "ro": {
        "title": "Comparație",
        "price": "Preț",
        "rating": "Rating",
        "avail": "Disponibilitate",
        "pros": "Avantaje",
        "cons": "De luat în calcul",
        "brand": "Brand",
        "lead": "Iată diferențele principale — alege în funcție de ce contează pentru tine.",
        "cheapest": "Cea mai accesibilă: {name}.",
        "top_rated": "Cea mai bine cotată: {name}.",
    },
    "en": {
        "title": "Comparison",
        "price": "Price",
        "rating": "Rating",
        "avail": "Availability",
        "pros": "Pros",
        "cons": "To consider",
        "brand": "Brand",
        "lead": "Here are the main differences — pick based on what matters to you.",
        "cheapest": "Most affordable: {name}.",
        "top_rated": "Top rated: {name}.",
    },
    "hu": {
        "title": "Összehasonlítás",
        "price": "Ár",
        "rating": "Értékelés",
        "avail": "Elérhetőség",
        "pros": "Előnyök",
        "cons": "Megfontolandó",
        "brand": "Márka",
        "lead": "Íme a fő különbségek — válassz aszerint, ami neked fontos.",
        "cheapest": "A legkedvezőbb: {name}.",
        "top_rated": "A legjobbra értékelt: {name}.",
    },
}
_AVAIL_LABELS: dict[str, dict[str, str]] = {
    "ro": {"in_stock": "În stoc", "low_stock": "Stoc limitat", "out_of_stock": "Indisponibil"},
    "en": {"in_stock": "In stock", "low_stock": "Low stock", "out_of_stock": "Out of stock"},
    "hu": {"in_stock": "Raktáron", "low_stock": "Kevés", "out_of_stock": "Elfogyott"},
}


def _labels(language: str | None) -> dict[str, str]:
    return _COMPARE_LABELS.get(language or "ro") or _COMPARE_LABELS["ro"]


def _join_list(raw: Any, n: int) -> str | None:
    """Primele `n` elemente ne-goale ca text (avantaje/minusuri din recenzii). Gol → None („—")."""
    if not isinstance(raw, (list, tuple)):
        return None
    items = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    return "; ".join(items[:n]) or None


def _comparison_lead(chosen: list[dict[str, Any]], language: str | None) -> str:
    """Lead determinist: framing scurt + verdict DERIVAT din date (cel mai ieftin / cel mai bine
    cotat, doar când departajează). Zero proză LLM → sigur prin construcție."""
    L = _labels(language)
    parts = [L["lead"]]
    priced = [p for p in chosen if p.get("price") is not None]
    if len(priced) > 1:
        prices = [float(p["price"]) for p in priced]
        if len(set(prices)) > 1:
            cheapest = min(priced, key=lambda p: float(p["price"]))
            parts.append(L["cheapest"].format(name=cheapest["name"]))
    rated = [p for p in chosen if p.get("rating") is not None]
    if len(rated) > 1:
        ratings = [float(p["rating"]) for p in rated]
        if len(set(ratings)) > 1:
            top = max(rated, key=lambda p: float(p["rating"]))
            # nu repeta dacă „cel mai ieftin" == „cel mai bine cotat" (un singur câștigător clar)
            parts.append(L["top_rated"].format(name=top["name"]))
    return " ".join(parts)


def _facet_value_label(facet: FacetSpec, code: Any, language: str | None) -> str | None:
    """O valoare de fațetă → text afișabil (Tier 2). Cod canonic cu traducere în `value_labels` →
    eticheta per-locale (fallback 'ro' → cod). Fără traducere (atribut display-ready) → valoarea ca
    atare. Gol/None → None."""
    if code is None:
        return None
    s = str(code).strip()
    if not s:
        return None
    trans = facet.value_labels.get(s)
    if trans:
        return trans.get(language or "ro") or trans.get("ro") or s
    return s


def _facet_cell(facet: FacetSpec, attributes: Any, language: str | None) -> str | None:
    """Celula de fațetă pentru un produs: citește `attributes[facet.key]` (listă → etichete unite,
    scalar → o etichetă). Lipsă/gol → None (randat „—"; rândul TOT-gol e sărit). Fapt din date, zero
    LLM → zero halucinație (aceeași garanție ca restul tabelului)."""
    if not isinstance(attributes, dict):
        return None
    raw = attributes.get(facet.key)
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        labels = [_facet_value_label(facet, v, language) for v in raw]
        labels = [lbl for lbl in labels if lbl]
        return ", ".join(labels[:4]) or None
    return _facet_value_label(facet, raw, language)


def _facet_label(facet: FacetSpec, language: str | None) -> str:
    return facet.labels.get(language or "ro") or facet.labels.get("ro") or facet.key


def facet_summary(
    product: dict[str, Any], facets: Sequence[FacetSpec], language: str | None
) -> str:
    """Tier 2b: rezumat compact al fațetelor unui produs pentru BUNDLE-ul rich (input pentru model):
    „Label: val; Label: val". Fapt din `attributes` (`_facet_cell`) → grounded, nu inventat.
    Fațetele fără valoare sunt sărite; niciuna → "" (degradare lină pe date sărace)."""
    attrs = product.get("attributes")
    parts = [
        f"{_facet_label(f, language)}: {cell}"
        for f in facets
        if (cell := _facet_cell(f, attrs, language))
    ]
    return "; ".join(parts)


def decision_axes(
    products: list[dict[str, Any]],
    facets: Sequence[FacetSpec] = (),
    language: str | None = None,
    *,
    max_axes: int = 3,
) -> list[str]:
    """NX-139: axele pe care VARIAZĂ efectiv setul afișat — input grounded pentru intro/segmentare
    (model iZi). 100% GENERIC pe vertical: fațetele vin din DomainPack (beauty=tip de ten/SPF,
    auto=fitment/material, bijuterii=material/carate) — zero cuvinte de vertical în cod.

    O fațetă e AXĂ DE DECIZIE când setul are ≥2 valori DISTINCTE acoperite de ≥2 produse (un
    atribut identic pe tot setul nu ajută alegerea). Prețul e axă când spread-ul e ≥1.5× (cifrele
    sunt OK aici — e INPUT pentru model, care oricum vede prețurile în bundle; regulile rich decid
    ce cifre pot ieși în proză). Fațete goale / set <2 → [] (degradare lină, ca azi)."""
    if len(products) < 2:
        return []
    axes: list[str] = []
    for facet in facets:
        if len(axes) >= max_axes - 1:  # păstrăm un slot pentru axa de preț
            break
        vals: list[str] = []
        seen: set[str] = set()
        covered = 0
        for p in products:
            cell = _facet_cell(facet, p.get("attributes"), language)
            if not cell:
                continue
            covered += 1
            for v in cell.split(", "):
                key = v.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    vals.append(v.strip())
        if len(vals) >= 2 and covered >= 2:
            axes.append(f"{_facet_label(facet, language)}: " + " / ".join(vals[:4]))
    prices = [float(p["price"]) for p in products if p.get("price") is not None]
    if prices and min(prices) > 0 and max(prices) / min(prices) >= 1.5:
        # RO e ok: linia e input de model (nu iese spre client); modelul răspunde în limba lui.
        axes.append(f"Preț: de la {min(prices):.0f} la {max(prices):.0f} lei")
    return axes[:max_axes]


def spec_numbers(
    products: list[dict[str, Any]], facets: Sequence[FacetSpec] = (), language: str | None = None
) -> set[str]:
    """NX-139: cifrele de SPECIFICAȚIE din datele produselor AFIȘATE — numele („Crema SPF 30,
    50 ml") + valorile de fațete din `attributes`. Grounded prin construcție → permise în
    intro/education (`scrub_intro`/`scrub_education`). Prețurile NU trec pe aici: ele stau în
    câmpuri dedicate (price/list_price), hidratate de cod pe card, niciodată în proză."""
    out: set[str] = set()
    for p in products:
        out |= set(re.findall(r"\d+", p.get("name") or ""))
        attrs = p.get("attributes")
        for facet in facets:
            cell = _facet_cell(facet, attrs, language)
            if cell:
                out |= set(re.findall(r"\d+", cell))
    return out


def build_comparison(
    products: list[dict[str, Any]],
    language: str | None,
    facets: Sequence[FacetSpec] = (),
) -> Comparison | None:
    """Tabel comparativ STRUCTURAT din 2-3 produse retrievate (get_products_by_ids). Determinist:
    fiecare celulă e un fapt real (preț/rating/disponibilitate/avantaje-minusuri din recenzii/brand)
    — NICIUN text LLM → zero halucinație prin construcție. `None` dacă < 2 produse valide.

    Tier 2 (IZI-parity): `facets` = fațete de DOMENIU din DomainPack (finish/acoperire/potrivit-
    pentru/material/...), randate ca rânduri din `products.attributes`. Generic (nimic hardcodat de
    vertical); un rând TOT-gol e sărit (date sărace → tabel ca azi). Gol → comportament neschimbat.

    Anchor preț redus (PR2): dacă produsul are `list_price` > prețul efectiv, coloana ține prețul
    de listă + `sale_price` = efectivul (frontendul taie listul). Forward-compatible: fără
    `list_price` → fără anchor (prețul afișat e cel efectiv, ca azi)."""
    chosen = [
        p
        for p in products[:3]
        if p.get("id") and p.get("name") is not None and p.get("price") is not None
    ]
    if len(chosen) < 2:
        return None
    L = _labels(language)
    AV = _AVAIL_LABELS.get(language or "ro") or _AVAIL_LABELS["ro"]

    columns: list[ComparisonColumn] = []
    for p in chosen:
        eff = float(p["price"])
        lp = p.get("list_price")  # preț de listă DOAR la reducere reală (SQL: case when on_sale)
        on_sale = lp is not None and float(lp) > eff
        columns.append(
            ComparisonColumn(
                product_id=str(p["id"]),
                name=p["name"],
                price=eff,  # CURENT (efectiv)
                list_price=float(lp) if on_sale else None,  # ORIGINAL tăiat (anchor)
                image=p.get("image"),
                url=p.get("url"),
                rating=float(p["rating"]) if p.get("rating") is not None else None,
            )
        )

    def _row(label: str, values: list[str | None]) -> ComparisonRow | None:
        # Rând sărit dacă TOATE celulele sunt goale (ex. niciun produs n-are minusuri).
        return ComparisonRow(label=label, values=values) if any(v for v in values) else None

    def _price_cell(p: dict[str, Any]) -> str:
        eff = float(p["price"])
        lp = p.get("list_price")
        if lp is not None and float(lp) > eff:  # IZI-anchor: preț redus (de la X)
            return f"{eff:.2f} lei (de la {float(lp):.2f})"
        return f"{eff:.2f} lei"

    # Tier 2: rânduri de DOMENIU (finish/acoperire/potrivit-pentru/..., din `attributes`), între
    # Rating și Disponibilitate. Generice (din DomainPack), deterministe; un rând TOT-gol e sărit.
    facet_rows = [
        _row(
            _facet_label(f, language),
            [_facet_cell(f, p.get("attributes"), language) for p in chosen],
        )
        for f in facets
    ]
    candidate_rows = [
        ComparisonRow(label=L["price"], values=[_price_cell(p) for p in chosen]),
        _row(
            L["rating"],
            [f"{float(p['rating']):.1f}★" if p.get("rating") is not None else None for p in chosen],
        ),
        *facet_rows,
        _row(L["avail"], [AV.get(p.get("availability") or "") or None for p in chosen]),
        _row(L["pros"], [_join_list(p.get("top_pros"), 3) for p in chosen]),
        _row(L["cons"], [_join_list(p.get("top_cons"), 2) for p in chosen]),
        _row(L["brand"], [p.get("brand") for p in chosen]),
    ]
    rows = [r for r in candidate_rows if r is not None]
    return Comparison(columns=columns, rows=rows, intro=_comparison_lead(chosen, language))


def flatten_comparison(comparison: Comparison, language: str | None) -> str:
    """Floor aplatizat al tabelului (WhatsApp/cache/messages.body + canale fără randare de tabel):
    lead + antet cu numele produselor + un rând per dimensiune, celulele separate cu „ · "."""
    title = _labels(language)["title"]
    head = f"{title}: " + " · ".join(c.name for c in comparison.columns)
    lines = [comparison.intro, "", head] if comparison.intro else [head]
    for row in comparison.rows:
        lines.append(f"{row.label}: " + " · ".join(v or "—" for v in row.values))
    return "\n".join(line for line in lines if line is not None).strip()
