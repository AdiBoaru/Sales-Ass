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

from src.catalog.render_text import display_name, name_keys, unique_prefixes
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


#: NX-382 faza 2c (recenzia): o trimitere SCRISĂ DE MODEL numește și medicul, ca fraza codului
#: („medicul sau farmacistul"). Amprenta singură ar fi primit «Farmacistul tău o să fie mulțumit».
_DOCTOR_STEMS = ("medic", "doctor", "orvos")
#: Câte cuvinte dinaintea farmacistului se caută o negație («nu e nevoie să mergi la farmacist»).
_NEGATION_WINDOW = 6


def is_model_referral(sentence: str, locale: str | None) -> bool:
    """O propoziție scrisă de model care TRIMITE la medic sau farmacist (NX-382 faza 2c): amprenta
    farmacistului, rădăcina medicului, și nicio negație a locale-i (`query_terms.
    negation_markers`) în cele `_NEGATION_WINDOW` cuvinte dinaintea farmacistului. PUR. Nu
    judecă un claim medical: acela îl scoate `has_medical_claim`, înainte."""
    from src.catalog.folding import fold_text  # noqa: PLC0415
    from src.catalog.query_terms import negation_markers  # noqa: PLC0415

    words = re.findall(r"\w+", fold_text(sentence or ""))
    at = next((i for i, w in enumerate(words) if any(f in w for f in _FINGERPRINTS)), None)
    if at is None or not any(w.startswith(_DOCTOR_STEMS) for w in words):
        return False
    window = set(words[max(0, at - _NEGATION_WINDOW) : at])
    return not (window & negation_markers(locale))


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
    """Aplică contractul pe reply-ul final. Chemat de runner (de două ori pe un tur cu ieșire
    timpurie: idempotent, cu UN singur eveniment).

    Face DOUĂ lucruri, în ordine:
      1. **scrub**: dacă un produs blocat a ajuns totuși în cardurile reply-ului (cale nouă care a
         uitat gate-ul), îl scoatem — clientul nu-l vede, chiar dacă un bug l-a adus până aici;
      2. **garanția**: prepend-ăm fraza de siguranță (recunoaștere + ce s-a omis + medic), o
         singură dată, localizată, în FIECARE câmp pe care îl randează un canal (`text`,
         `rich.intro`, `comparison.intro`). Fără ea, contractul ar depinde de ce a scris modelul.
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
    cmp = getattr(reply, "comparison", None)
    if _already_enforced(reply, rich, cmp, sentence):
        # A doua trecere a runnerului (ieșire timpurie): nimic de adăugat, nimic de raportat.
        return
    composed = getattr(ctx, "safety_referral_composed", None)
    if (
        composed
        # o excludere o spune MEREU codul (recenzia 2c): ce s-a lăsat deoparte nu se verifică
        and not getattr(decision, "blocked", None)
        and not _emptied(ctx, decision, reply)
        and _already_enforced(reply, rich, cmp, composed)
    ):
        # NX-382 faza 2c: trimiterea a scris-o compozitorul, iar poarta lui a verificat-o (după
        # scoaterea propozițiilor medicale). Ea e chiar în fiecare câmp randat, deci fraza codului
        # ar fi a doua. Un set golit de excludere rămâne al codului (NX-367): acolo proza modelului
        # vorbește despre un set golit de noi. Idempotent: a doua trecere nu mai emite.
        reported = any(
            e.type == "safety_sentence_enforced" for e in (getattr(ctx, "events", None) or [])
        )
        if not reported:
            ctx.emit(
                "safety_sentence_enforced",
                contexts=list(getattr(decision, "contexts", ()) or []),
                blocked=len(getattr(decision, "blocked", ()) or []),
                unavailable=False,
                outcome="composed",
            )
        return
    if _emptied(ctx, decision, reply):
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
        names = _card_names(reply, locale)
        reply.text = _with_sentence(reply.text, sentence, names)
        if rich is not None and hasattr(rich, "intro"):
            # Intro-ul rich e ce vede clientul sus pe canalele bogate; `text` e aplatizarea. Fraza
            # intră în AMBELE, o dată. NX-359: și când intro-ul e GOL (runda de proză sărită,
            # compunerea picată), altfel widgetul nu arăta trimiterea la medic deloc.
            rich.intro = _with_sentence(rich.intro, sentence, names)
            if getattr(rich, "education", None):
                rich.education = _without_referrals(rich.education, names) or None
        if cmp is not None and hasattr(cmp, "intro"):
            # Recenzia NX-367 (P0): pe web o comparație randează DOAR `comparison.intro` (plus
            # tabelul), nu `reply.text`, deci fraza nu ajungea la client.
            cmp.intro = _with_sentence(cmp.intro, sentence, names)
            if getattr(cmp, "subtitle", None):
                cmp.subtitle = _without_referrals(cmp.subtitle, names) or None
            if getattr(cmp, "closing", None):
                cmp.closing = [c for c in (_without_referrals(p, names) for p in cmp.closing) if c]
        outcome = "prepended"
    ctx.emit(
        "safety_sentence_enforced",
        contexts=list(getattr(decision, "contexts", ()) or []),
        blocked=len(getattr(decision, "blocked", ()) or []),
        unavailable=bool(getattr(decision, "unavailable", False)),
        outcome=outcome,
    )


def _emptied(ctx: Any, decision: Any, reply: Any) -> bool:
    """„Golit" = excluderea a lăsat setul turului GOL (nimic păstrat), nu e niciun card, nicio
    întrebare deschisă, iar turul n-a citit nimic în afara catalogului (o regulă a magazinului,
    o comandă): atunci proza modelului vorbește doar despre un set golit de noi."""
    retrieval = getattr(ctx, "retrieval", None)
    return (
        bool(getattr(decision, "blocked", None))
        and not getattr(decision, "kept", None)
        and not _has_cards(reply)
        and not getattr(reply, "pending_question", None)
        and not getattr(retrieval, "read_beyond_catalog", False)
    )


def _already_enforced(reply: Any, rich: Any, cmp: Any, sentence: str) -> bool:
    """Fraza NOASTRĂ e deja în fiecare câmp randat (a doua trecere a runnerului, retry)."""
    fields = [reply.text]
    if rich is not None and hasattr(rich, "intro"):
        fields.append(rich.intro)
    if cmp is not None and hasattr(cmp, "intro"):
        fields.append(cmp.intro)
    return all(f and sentence in f for f in fields)


#: Granița de propoziție. NU `worker.compose._sentences`: acolo orice punct după o cifră e
#: excepție (pentru „1. Curățare"), deci „…cu SPF 50. Întreabă și farmacistul." rămânea o singură
#: propoziție și se scotea cu tot cu recomandare. Aici se taie peste tot, iar un ordinal de listă
#: (cifra singură sau după „:", „,", „(") se lipește înapoi de ce urmează (`_sentences`).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_ORDINAL_TAIL = re.compile(r"(?:^|[:,(]\s*)\d{1,3}[.)]$")
#: Element de listă numerotată: nimic nu se scoate din el, altfel numerotarea rămâne cu o gaură.
_NUMBERED = re.compile(r"^\s*\d{1,3}[.)]\s")


def _sentences(line: str) -> list[str]:
    out: list[str] = []
    for part in _SENTENCE_SPLIT.split(line):
        if out and _ORDINAL_TAIL.search(out[-1]):
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


def _has_cards(reply: Any) -> bool:
    rich = getattr(reply, "rich", None)
    cmp = getattr(reply, "comparison", None)
    return (
        bool(getattr(reply, "products", None))
        or bool(getattr(rich, "items", None))
        or bool(getattr(cmp, "columns", None))
    )


def _card_names(reply: Any, locale: str | None) -> tuple[tuple[str, ...], ...]:
    """Cheile de cuvinte (NX-318) prin care o propoziție NUMEȘTE un card de pe ecran, ca să nu fie
    scoasă doar fiindcă pomenește și medicul.

    Per card, cel mai scurt prefix al numelui scurt care nu e cuvânt gol pe locale
    (`unique_prefixes` pe un singur nume): aici întrebarea e DACĂ propoziția numește un card, nu
    CARE, deci două carduri SOME BY MI nu fac „SOME BY MI e bun" anonim. Potrivirea e pe cuvinte
    întregi (`name_keys`), nu pe subșir: „ser" nu numește nimic în „Observ"."""
    products = getattr(reply, "products", None) or []
    names: list[str] = [str((p or {}).get("name") or "") for p in products]
    rich = getattr(reply, "rich", None)
    names += [str(getattr(it, "name", "") or "") for it in (getattr(rich, "items", None) or [])]
    cmp = getattr(reply, "comparison", None)
    names += [str(getattr(c, "name", "") or "") for c in (getattr(cmp, "columns", None) or [])]
    out: set[tuple[str, ...]] = set()
    for name in names:
        prefix = unique_prefixes({"_": display_name(name)}, locale=locale).get("_")
        if prefix:
            out.add(prefix)
    return tuple(sorted(out))


def _names_a_card(sentence: str, names: tuple[tuple[str, ...], ...]) -> bool:
    words = name_keys(sentence)
    return any(
        words[i : i + len(prefix)] == prefix
        for prefix in names
        for i in range(len(words) - len(prefix) + 1)
    )


def _without_referrals(text: str | None, keep_if_names: tuple[tuple[str, ...], ...] = ()) -> str:
    """Scoate propozițiile cu trimitere la medic scrise de model (contractul i le interzice: o
    scrie codul, o dată). Rămân: o propoziție care numește un produs de pe ecran (pierderea unei
    recomandări ar costa mai mult decât o trimitere în plus, declarat) și orice element de listă
    numerotată (o gaură în numerotare ar strica rutina)."""
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        if not already_has_sentence(line) or _NUMBERED.match(line):
            lines.append(line)
            continue
        kept = [
            sent
            for sent in _sentences(line)
            if not already_has_sentence(sent)
            or _NUMBERED.match(sent)
            or _names_a_card(sent, keep_if_names)
        ]
        lines.append(" ".join(kept).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _with_sentence(text: str | None, sentence: str, names: tuple[tuple[str, ...], ...] = ()) -> str:
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
