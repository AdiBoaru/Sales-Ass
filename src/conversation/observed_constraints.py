"""NX-297 felia 3 — constrângerile turului, fără un model mic care le extrage.

## Problema

Stiva de constrângeri multi-tur (NX-133) se hrănea din sloturile triajului: nano re-extrăgea
„sub 100 lei" la FIECARE tur, iar `merge_constraints` le păstra peste rafinări. Scos nano, stiva
rămâne fără alimentare — și consecința nu e o eroare, e o uitare tăcută: clientul spune bugetul o
dată, iar la „mai arată-mi" botul revine cu produse peste el.

## Reparația: nu întrebăm un model ce a spus clientul, ne uităm ce a CĂUTAT agentul

Agentul cheamă `search_products(price_max=100, concerns=[...], brand=...)`. Argumentele ALEA sunt
constrângerile turului, deja rezolvate pe catalogul real de unealta care le-a primit. Nu mai e
nevoie de un al doilea model care să citească aceeași frază.

**Ce le face ale CLIENTULUI e codul, nu modelul.** Fiecare valoare trece prin `corroborated_by`
(NX-251): dacă „100" sau „ten gras" chiar apar în mesajul BRUT al turului, constrângerea e a
clientului și intră în stivă. Dacă nu apar, modelul a inferat-o — utilă pentru căutarea de ACUM,
dar nu are voie să devină lipicioasă peste ture. Asimetria e deliberată: pe v1 stiva nu are noțiune
de tărie, deci singurul mod onest de a exprima „soft" e să NU persiste.

Fără poarta asta, o constrângere halucinată la turul 3 ar filtra tăcut turele 4-9, iar clientul
n-ar avea cum să afle de ce nu mai vede nimic.

## Ce NU intră, și de ce

`category` nu intră în stivă, deși modelul o pasează. Categoria e singura cheie care declanșează
RESETUL stivei (`merge_constraints`), iar o categorie aleasă de model, necoroborată, ar putea
șterge constrângerile clientului. Resetul are deja o a doua sursă, nano-free: raftul citit din
arborele de catalog (`topic_switched`, NX-133). Nu-i dăm o a treia, mai slabă.

`sort_mode`, `in_stock_only`, `limit`, `product_name` nu sunt constrângeri ale clientului peste
ture: sunt decizii de execuție ale turului curent.
"""

from __future__ import annotations

from typing import Any

from src.conversation.needs import corroborated_by

#: Argument de tool → cheia din stiva v1. Traducerea e EXPLICITĂ: un `getattr` peste numele
#: argumentelor ar lega tăcut stiva de schema tool-ului, iar o redenumire acolo ar goli stiva fără
#: ca vreun test să pice.
_ARG_TO_SLOT: dict[str, str] = {
    "price_max": "budget_max",
    "brand": "brand",
}

#: Argumentele de tip listă, care se REUNESC în loc să se suprascrie.
_LIST_ARG = "concerns"

#: Cap pe `concerns`, ACELAȘI ca la `merge_constraints` (P4: bugetul stă în cod). Două plafoane
#: peste aceeași listă nu pot rămâne de acord decât dacă al doilea îl citează pe primul.
MAX_CONCERNS = 5


def from_search_args(
    calls: list[dict[str, Any]], message: str
) -> tuple[dict[str, Any], dict[str, int]]:
    """Argumentele cu care agentul a căutat → constrângerile de PERSISTAT + un contor de diagnostic.

    Pură, deterministă, agnostică de limbă. Ordinea apelurilor contează: pentru un scalar câștigă
    ULTIMA valoare coroborată (clientul poate corecta bugetul în același tur), iar `concerns` se
    reunesc în ordinea în care au fost cerute.

    Contorul are două chei, și diferența dintre ele e chiar decizia: `kept` = valori pe care
    clientul le-a ROSTIT, `inferred` = valori pe care modelul le-a compus. A doua nu e o eroare și
    nu se numără ca una — doar nu se persistă.
    """
    out: dict[str, Any] = {}
    concerns: list[str] = []
    seen: set[str] = set()
    stats = {"kept": 0, "inferred": 0}

    for args in calls:
        if not isinstance(args, dict):
            continue
        for arg, slot in _ARG_TO_SLOT.items():
            value = args.get(arg)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if corroborated_by(message, value):
                out[slot] = value
                stats["kept"] += 1
            else:
                stats["inferred"] += 1
        for item in args.get(_LIST_ARG) or ():
            if not isinstance(item, str) or not item.strip():
                continue
            if not corroborated_by(message, item):
                stats["inferred"] += 1
                continue
            key = item.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            concerns.append(item.strip())
            stats["kept"] += 1

    if concerns:
        out[_LIST_ARG] = concerns[:MAX_CONCERNS]
    return out, stats
