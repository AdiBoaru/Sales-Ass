"""NX-173 — copy de siguranță LOCALIZAT, pe CHEI (P11: limba e parte din cheie).

Policy-ul decide (`Decision`), aici se randează. Separarea contează din două motive:
  1. policy-ul nu poartă text (`*_ro` în reguli = cuplaj de limbă în stratul de decizie);
  2. fraza de siguranță e **garantată de cod**, o singură dată, revizuibilă — nu o speranță că
     modelul o scrie. Modelul compune DOAR partea comercială.

Ton (contractul de compunere): recunoaște contextul o dată → spune scurt ce s-a omis → declină
onest (nu putem confirma din catalog) → trimite la medic/farmacist. Fără jargon intern, fără
majuscule, fără repetare pe fiecare card.
"""

from __future__ import annotations

# Etichetele de context, per locale. Cheie = `context_id` din registru (NU text în registru).
_CONTEXT_LABELS: dict[str, dict[str, str]] = {
    "pregnancy": {"ro": "ești însărcinată", "en": "you're pregnant", "hu": "várandós vagy"},
    "breastfeeding": {"ro": "alăptezi", "en": "you're breastfeeding", "hu": "szoptatsz"},
}

# Ce s-a lăsat deoparte, per `rule_id` — formulare de client, nu `reason_ro` intern din registru.
_RULE_OMISSION: dict[str, dict[str, str]] = {
    "pregnancy-retinoids": {
        "ro": "opțiunile cu retinoizi",
        "en": "the options with retinoids",
        "hu": "a retinoidos termékeket",
    },
}

_FALLBACK_OMISSION = {
    "ro": "opțiunile nepotrivite",
    "en": "the unsuitable options",
    "hu": "a nem megfelelő termékeket",
}

# Declinarea + trimiterea la medic. Singura propoziție „medicală" pe care o scriem — și e o
# NE-afirmație: spunem explicit că NU putem confirma din datele catalogului.
_REFER: dict[str, str] = {
    "ro": (
        "Nu pot confirma doar din datele catalogului că un produs e potrivit în această situație, "
        "așa că verifică alegerea cu medicul sau farmacistul."
    ),
    "en": (
        "I can't confirm from catalogue data alone that a product is suitable in this situation, "
        "so please check your choice with your doctor or pharmacist."
    ),
    "hu": (
        "A katalógus adataiból nem tudom megerősíteni, hogy egy termék megfelelő-e ebben a "
        "helyzetben, ezért kérlek, egyeztesd az orvosoddal vagy a gyógyszerészeddel."
    ),
}


# Registru stricat + context activ → fail-closed. Onest despre ce se întâmplă (nu putem verifica
# ACUM), fără să sperie și fără să pretindem un motiv medical. P6: iese ceva, nu tăcere.
_UNAVAILABLE: dict[str, str] = {
    "ro": (
        "Țin cont de situația ta, dar acum nu pot verifica dacă produsele sunt potrivite, "
        "așa că prefer să nu-ți recomand nimic pe orbește. Te rog întreabă medicul sau "
        "farmacistul — sau revino puțin mai târziu."
    ),
    "en": (
        "I've noted your situation, but I can't verify right now whether the products are "
        "suitable, so I'd rather not recommend anything blindly. Please ask your doctor or "
        "pharmacist — or try again a bit later."
    ),
    "hu": (
        "Figyelembe veszem a helyzetedet, de most nem tudom ellenőrizni, hogy a termékek "
        "megfelelőek-e, ezért inkább nem ajánlok semmit vaktában. Kérdezd meg az orvosodat vagy "
        "a gyógyszerészedet — vagy nézz vissza kicsit később."
    ),
}


def _pick(table: dict[str, str], locale: str) -> str:
    return table.get(locale) or table.get("ro") or next(iter(table.values()), "")


def unavailable_sentence(locale: str) -> str:
    return _pick(_UNAVAILABLE, locale)


def context_label(context_id: str, locale: str) -> str:
    return _pick(_CONTEXT_LABELS.get(context_id, {}), locale) or context_id


def omission_label(rule_id: str, locale: str) -> str:
    return _pick(_RULE_OMISSION.get(rule_id, {}), locale) or _pick(_FALLBACK_OMISSION, locale)


def refer_sentence(locale: str) -> str:
    return _pick(_REFER, locale)


def safety_sentence(contexts: list[str], rule_ids: list[str], *, locale: str, blocked: bool) -> str:
    """Fraza de siguranță GARANTATĂ, o singură dată, localizată.

    `blocked` False (context declarat, dar nimic de exclus) → recunoaștem contextul + declinăm,
    fără să pretindem că am filtrat ceva (ar fi o minciună mică, dar tot minciună)."""
    labels = [context_label(c, locale) for c in contexts]
    if not labels:
        return ""
    who = labels[0] if len(labels) == 1 else " și ".join(labels[:2])
    refer = refer_sentence(locale)
    if not blocked:
        return f"{_ack(who, locale)} {refer}"
    omitted = [omission_label(r, locale) for r in rule_ids] or [_pick(_FALLBACK_OMISSION, locale)]
    what = omitted[0] if len(omitted) == 1 else ", ".join(dict.fromkeys(omitted))
    return f"{_ack_omitted(who, what, locale)} {refer}"


def _ack(who: str, locale: str) -> str:
    if locale == "en":
        return f"I've noted that {who}."
    if locale == "hu":
        return f"Figyelembe veszem, hogy {who}."
    return f"Țin cont că {who}."


def _ack_omitted(who: str, what: str, locale: str) -> str:
    if locale == "en":
        return f"I've noted that {who}, so I've left out {what}."
    if locale == "hu":
        return f"Figyelembe veszem, hogy {who}, ezért kihagytam {what}."
    return f"Țin cont că {who} și am lăsat deoparte {what}."
