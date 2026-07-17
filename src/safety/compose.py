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
    return (
        f"(clientul a declarat: {', '.join(labels)}. Ține cont în alegere și în motivul fiecărui "
        "produs; nu scrie tu avertismentul de siguranță — e adăugat separat.)"
    )


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
    _scrub_blocked_cards(ctx, decision)
    sentence = safety_sentence_for(decision, getattr(ctx, "language", "ro"))
    if not sentence or already_has_sentence(reply.text):
        return
    reply.text = f"{sentence}\n\n{reply.text}".strip() if reply.text else sentence
    # Reply-ul cu frază de siguranță e RELATIV la contextul acestui client → nu intră în cache
    # (un hit l-ar servi altcuiva, ori i-ar servi lui un răspuns fără frază — clasa de
    # cache-poisoning deja știută pe „mai ieftin"/fallback).
    reply.cacheable = False
    rich = getattr(reply, "rich", None)
    if rich is not None and getattr(rich, "intro", None) is not None:
        # Intro-ul rich e ce vede clientul sus pe canalele bogate; `text` e aplatizarea. Punem
        # fraza în AMBELE, o dată, ca să nu apară doar pe canalul sărac.
        if not already_has_sentence(rich.intro):
            rich.intro = f"{sentence}\n\n{rich.intro}".strip() if rich.intro else sentence
    ctx.emit(
        "safety_sentence_enforced",
        contexts=list(getattr(decision, "contexts", ()) or []),
        blocked=len(getattr(decision, "blocked", ()) or []),
        unavailable=bool(getattr(decision, "unavailable", False)),
    )


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
