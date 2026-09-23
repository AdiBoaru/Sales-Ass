"""NX-316 felia 1 — serverul RECUNOAȘTE apăsarea unui chip, în loc s-o ghicească din text.

## Problema

NX-296 a făcut din chip o MUTARE (`kind` + sloturi + dovadă), dar pe contractul v1 apăsarea
retrimite doar TEXTUL, ca mesaj nou al clientului. Serverul primea deci „Trimite-mi linkul la
IUNIK Beta Glucan" fără să știe că e mutarea `link:<id>` pe care tocmai o oferise, iar
handlerele deterministe o reinterpretau cu regexurile lor:

- `_handle_link_intent` ignora numele și servea linkurile TUTUROR produselor afișate;
- `_handle_compare_intent` compara primele două afișate, nu cele două numite.

Pe traficul real (`scripts/nx316_chip_press_probe.py`, `sole-ro`, 30 de zile) **24,6% din turele
de după un răspuns cu chips sunt apăsări** (14 din 57). Perechea comparată a fost corectă de fiecare
dată, dar din noroc: mutarea `compare` se construiește chiar din primele două carduri.

## Mecanismul

Recunoașterea NU citește textul ca să-l înțeleagă. Refolosește EXACT lanțul care a produs
chips-urile (`chip_moves.from_cards` → `renderable` → `render_move`), pe aceleași carduri (setul
afișat din stare), aceeași limbă și același pachet, deci textul re-randat e identic prin
construcție. O mutare e recunoscută doar dacă:

1. `move_id`-ul ei a fost OFERIT (`state.offered_chips`, P8: id-uri, nu texte);
2. textul ei re-randat e egal, cuvânt cu cuvânt, cu mesajul (`words_of`: fără diacritice și fără
   punctuație, deci „Compară" tastat „Compara" trece);
3. felul ei are un handler determinist (`PRESSABLE`).

Orice altceva ⇒ `None` ⇒ turul merge exact ca azi. Nu există „aproape egal": un chip reformulat de
client e un mesaj nou, iar regexurile de azi rămân plasa lui.

**De ce doar mutările din carduri.** Cele din meniu (`refine_facet`, `pivot_shelf`) sunt fraze de
CĂUTARE, iar apăsarea lor merge deja unde trebuie: textul se rezolvă prin aceeași `resolve_any`.
Re-randarea lor ar cere meniul turului trecut (o interogare), pentru o mutare care n-are handler
propriu. `price_band` e o constrângere de preț, pe care extracția o prinde deja din text.

**Felia 2 — mutările pe fațete.** `choose_within` și `fit_question` nu se nasc din nume, ci din
fațetele setului afișat, pe care starea nu le ține (P8: doar `{id, nume, preț}`). Tot ce trebuie
pentru re-randare e însă în `move_id` (fațetă + cheie, plus produsul la `fit_question`), iar fraza
valorii o aduce apelantul din vocabular (`value_phrases`, aceeași funcție ca la emitere). Deci
recunoașterea rămâne re-randare, nu interpretare.

Modulul e PUR: fără DB, fără ceas, fără model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.catalog.clarify_menu import words_of
from src.conversation import chip_moves

__all__ = ["PRESSABLE", "facet_of", "product_ids", "recognize"]

#: Felurile de mutare care au un handler determinist. Un fel în afara listei nu se recunoaște,
#: deci apăsarea lui rămâne a regexurilor și a modelului, ca înainte.
PRESSABLE: frozenset[str] = frozenset(
    {
        "detail",
        "reviews",
        "link",
        "compare",
        "choose_within",
        "fit_question",
        "routine_next",
        "similar_to",
    }
)


def recognize(
    message: str,
    cards: Sequence[Mapping[str, Any]],
    offered: Iterable[str],
    pack: object,
    locale: str,
    *,
    unique_anchor: bool = False,
    phrases: Mapping[tuple[str, str], str] | None = None,
) -> chip_moves.ChipMove | None:
    """Mutarea oferită pe care mesajul o reproduce EXACT, sau `None`.

    `cards` = setul afișat, în ordinea de pe ecran (`state.displayed_products`), cu `product_id`,
    `name`, `price`. `unique_anchor` trebuie să fie ACELAȘI flag cu care s-au construit chips-urile
    (`UNIQUE_NAME_PREFIX_ENABLED`): el decide pragul de scurtare al numelor, deci textul.
    `phrases` = `{(fațetă, cheie): frază}` pentru mutările pe fațete oferite (`wanted_phrases`);
    lipsă ⇒ ele nu se recunosc, iar turul merge ca înainte.
    """
    said = words_of(message or "")
    offered_ids = {str(m) for m in offered if m}
    if not said or not offered_ids or not cards:
        return None
    built = chip_moves.from_cards(cards, unique_anchor=unique_anchor, locale=locale)
    # Mutările pe fațete și din graf se reconstruiesc din `move_id`; `similar_to` nu cere frază,
    # deci se construiește și fără `phrases`.
    built += chip_moves.facet_moves_for(
        offered_ids, cards, phrases or {}, unique_anchor=unique_anchor, locale=locale
    )
    moves = chip_moves.renderable(built, pack, locale)
    hits = []
    for move in moves:
        if move.kind not in PRESSABLE or move.move_id not in offered_ids:
            continue
        text = chip_moves.render_move(move, pack, locale)
        if text is not None and words_of(text) == said:
            hits.append(move)
    # Două mutări cu ACELAȘI text ar însemna o apăsare ambiguă. NX-318 (prefixe unice) o face
    # imposibilă pe setul afișat, dar dacă apare, nu alegem noi în locul clientului.
    return hits[0] if len(hits) == 1 else None


def product_ids(move: chip_moves.ChipMove) -> tuple[str, ...]:
    """Produsele pe care le numește mutarea, din `move_id` (`kind:id` sau `compare:a:b`).

    Mutările pe fațete au în `move_id` și fațeta + cheia, care NU sunt produse: `choose_within`
    nu numește niciunul (lucrează pe tot setul afișat), `fit_question` numește unul."""
    parts = chip_moves.facet_move_parts(move.move_id)
    if parts is not None:
        return (parts[1],) if parts[1] else ()
    _, _, rest = move.move_id.partition(":")
    return tuple(p for p in rest.split(":") if p)


def facet_of(move: chip_moves.ChipMove) -> tuple[str, str] | None:
    """`(fațetă, cheie)` a unei mutări pe fațete, altfel `None`."""
    parts = chip_moves.facet_move_parts(move.move_id)
    return (parts[2], parts[3]) if parts is not None else None
