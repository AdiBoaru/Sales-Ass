"""NX-173 — contractul de COMPUNERE: codul garantează fraza de siguranță, modelul scrie vânzarea.

Review Codex pe #229: `safety_note` ajungea în `llm_view`-ul PRIMULUI tool, dar al doilea apel
(rich) primea doar `notes=plan.commerce_note` → declinarea nu ajungea în reply-ul final, iar
golden-ul trecea cu un răspuns fără nicio trimitere la medic. Adică: contractul era o SPERANȚĂ.

Aici devine structural:
  - `SafetyPolicy` decide (`Decision`) → aici se randează, o singură dată, localizat (`messages`);
  - se aplică pe reply-ul FINAL, în runner, după orice stagiu → nicio cale nu-l poate ocoli
    (nici `try_pre_intents`, care iese înainte de agent, nici fallback-urile);
  - idempotent: dacă fraza e deja acolo (retry, re-render), nu se dublează.

Împărțirea rolurilor (contractul cerut): modelul compune DOAR recomandarea comercială + întrebarea
de continuare. Recunoașterea contextului, ce s-a omis și trimiterea la medic sunt ale codului.
"""

from __future__ import annotations

import re
from typing import Any

from src.safety import messages

# Amprenta frazei garantate → detectăm idempotent dacă trimiterea la medic e DEJA în text (a
# noastră, dintr-un retry, sau a modelului, care uneori o scrie singur).
#
# STEM, nu frază: RO flexionează („medicul sau farmacistul" vs „fără acordul medicului sau
# farmacistului"). Cu amprenta pe forma de nominativ, varianta la genitiv a modelului nu se
# potrivea → codul mai adăuga una și clientul primea avertismentul de DOUĂ ori (observat live).
# Contractul cere exact UNA — deci potrivim rădăcina, care prinde toate formele.
_FINGERPRINTS = ("farmacist", "pharmacist", "gyógyszerész", "gyogyszeresz")


# Eticheta internă a contextului pentru HINT-ul de model (RO, ca restul scaffoldingului de prompt).
# Separat de `messages._CONTEXT_LABELS`, care e copy de CLIENT localizat.
_CONTEXT_HINT_RO = {"pregnancy": "sarcină", "breastfeeding": "alăptare"}


def already_has_sentence(text: str | None) -> bool:
    t = (text or "").lower()
    return any(f in t for f in _FINGERPRINTS)


def model_hint(decision: Any) -> str:
    """Hint-ul MINIM dat modelului când un context de siguranță e activ: o linie, ca framing-ul
    lui comercial să fie coerent („în sarcină, aș merge pe ceva simplu…") în loc să pară că
    ignoră ce a spus clientul.

    NU e copy: fraza de siguranță (recunoaștere + medic) o scrie codul, o dată, în `enforce`.
    Modelul primește doar cât să aleagă bine — nu instrucțiuni de disclaimer, nu majuscule, nu
    jargon intern (review Codex: „EXCLUS determinist / REGULI DURE" era pseudo-copy trimis
    modelului și devenea ton de robot)."""
    if decision is None or not getattr(decision, "contexts", ()):
        return ""
    labels = [_CONTEXT_HINT_RO.get(c, c) for c in decision.contexts]
    hint = (
        f"(clientul a declarat: {', '.join(labels)}. Ține cont în alegere și în motivul fiecărui "
        "produs; nu scrie tu avertismentul de siguranță — e adăugat separat.)"
    )
    blocked = len(getattr(decision, "blocked", ()) or ())
    if blocked:
        # NX-367: fără faptul ăsta modelul vedea un set gol și conchidea „nu există în catalog"
        # (conversația c6, 13 retinoizi excluși). Excluderea e a noastră; modelul trebuie s-o știe.
        hint += (
            f" ({blocked} produse găsite au fost scoase pentru contextul declarat. Nu spune că nu "
            "există în catalog: excluderea o explică fraza de siguranță adăugată separat.)"
        )
    return hint


def safety_sentence_for(decision: Any, locale: str) -> str:
    """Fraza garantată pentru o `Decision`. Gol = nimic de spus (fără context activ)."""
    if decision is None or not getattr(decision, "must_refer", False):
        return ""
    if getattr(decision, "unavailable", False):
        return messages.unavailable_sentence(locale)
    return messages.safety_sentence(
        list(getattr(decision, "contexts", ()) or []),
        list(getattr(decision, "rule_ids", ()) or []),
        locale=locale,
        blocked=bool(getattr(decision, "blocked", None)),
    )


def enforce(ctx: Any) -> None:
    """Aplică contractul pe reply-ul final. Chemat de runner, o singură dată pe tur.

    Face DOUĂ lucruri, în ordine:
      1. **scrub**: dacă un produs blocat a ajuns totuși în cardurile reply-ului (cale nouă care a
         uitat gate-ul), îl scoatem — clientul nu-l vede, chiar dacă un bug l-a adus până aici;
      2. **garanția**: prepend-ăm fraza de siguranță (recunoaștere + ce s-a omis + medic), o
         singură dată, localizată. Fără ea, contractul ar depinde de ce a scris modelul.
    """
    decision = getattr(ctx, "safety_decision", None)
    reply = getattr(ctx, "reply", None)
    if decision is None or reply is None or not getattr(decision, "must_refer", False):
        return
    # Reply-ul cu context de siguranță e RELATIV la acest client → nu intră în cache (un hit l-ar
    # servi altcuiva). NX-367: înainte de orice întoarcere timpurie, nu după ea: pe c6 răspunsul
    # cu „nu am găsit retinol" pleca `cacheable: true`.
    reply.cacheable = False
    _scrub_blocked_cards(ctx, decision)
    locale = getattr(ctx, "language", "ro")
    sentence = safety_sentence_for(decision, locale)
    if not sentence:
        return
    rich = getattr(reply, "rich", None)
    # „Golit" = excluderea a lăsat setul turului GOL (nimic păstrat) și nu e niciun card. Un răspuns
    # doar text care recomandă un produs păstrat nu e golit (`test_enforce_prepends_sentence...`).
    emptied = (
        bool(getattr(decision, "blocked", None))
        and not getattr(decision, "kept", None)
        and not _has_cards(reply)
        and not getattr(reply, "pending_question", None)
    )
    if emptied:
        # NX-367: excluderea a golit setul, deci proza modelului vorbește despre un set golit de
        # noi („nu am găsit seruri cu retinol în catalog"). Răspunsul e al codului.
        own = f"{sentence} {messages.alternatives_offer(locale)}"
        reply.text = own
        if rich is not None and hasattr(rich, "intro"):
            rich.intro = own
            if hasattr(rich, "education"):
                rich.education = None
        outcome = "replaced"
    else:
        names = _card_brands(reply)
        reply.text = _with_sentence(reply.text, sentence, names)
        if rich is not None and hasattr(rich, "intro"):
            # Intro-ul rich e ce vede clientul sus pe canalele bogate; `text` e aplatizarea. Fraza
            # intră în AMBELE, o dată. NX-359: și când intro-ul e GOL (runda de proză sărită,
            # compunerea picată), altfel widgetul nu arăta trimiterea la medic deloc.
            rich.intro = _with_sentence(rich.intro, sentence, names)
            if getattr(rich, "education", None):
                rich.education = _without_referrals(rich.education, names) or None
        outcome = "prepended"
    ctx.emit(
        "safety_sentence_enforced",
        contexts=list(getattr(decision, "contexts", ()) or []),
        blocked=len(getattr(decision, "blocked", ()) or []),
        unavailable=bool(getattr(decision, "unavailable", False)),
        outcome=outcome,
    )


#: Granița de propoziție, cu ordinalele de listă exceptate („1. Curățare"), ca `compose._sentences`.
_SENTENCE_SPLIT = re.compile(r"(?<![0-9].)(?<=[.!?])\s+")


def _has_cards(reply: Any) -> bool:
    rich = getattr(reply, "rich", None)
    return bool(getattr(reply, "products", None)) or bool(getattr(rich, "items", None))


def _card_brands(reply: Any) -> tuple[str, ...]:
    """Primul cuvânt din numele fiecărui card (marca, pe catalogul real), ca o propoziție care
    numește un produs să nu fie scoasă doar fiindcă pomenește și medicul."""
    out = []
    for p in getattr(reply, "products", None) or []:
        first = str(p.get("name") or "").split(" ", 1)[0].strip().lower()
        if len(first) >= 3:
            out.append(first)
    return tuple(out)


def _without_referrals(text: str | None, keep_if_names: tuple[str, ...] = ()) -> str:
    """Scoate propozițiile cu trimitere la medic scrise de model (contractul i le interzice: o
    scrie codul, o dată). O propoziție care numește și un produs de pe ecran rămâne: pierderea unei
    recomandări ar costa mai mult decât o trimitere în plus (declarat)."""
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        kept = []
        for sent in _SENTENCE_SPLIT.split(line):
            low = sent.lower()
            if already_has_sentence(sent) and not any(n in low for n in keep_if_names):
                continue
            kept.append(sent)
        lines.append(" ".join(kept).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _with_sentence(text: str | None, sentence: str, names: tuple[str, ...] = ()) -> str:
    """Textul cu fraza garantată în față, o singură dată. Idempotent pe fraza NOASTRĂ (retry,
    re-render); trimiterile modelului se scot, ca avertismentul să nu apară de două ori."""
    if text and sentence in text:
        return text
    rest = _without_referrals(text, names)
    return f"{sentence}\n\n{rest}".strip() if rest else sentence


def _scrub_blocked_cards(ctx: Any, decision: Any) -> None:
    """Ultima plasă: scoate din carduri/produse orice id blocat în acest tur."""
    bad = set(getattr(decision, "blocked_ids", ()) or ())
    if not bad:
        return
    reply = ctx.reply
    for attr in ("products",):
        items = getattr(reply, attr, None)
        if not items:
            continue
        kept = [p for p in items if str(p.get("product_id") or p.get("id") or "") not in bad]
        if len(kept) != len(items):
            ctx.emit("safety_card_scrubbed", removed=len(items) - len(kept))
            setattr(reply, attr, kept)
    rich = getattr(reply, "rich", None)
    if rich is not None and getattr(rich, "items", None):
        rich.items = [it for it in rich.items if str(getattr(it, "product_id", "")) not in bad]
