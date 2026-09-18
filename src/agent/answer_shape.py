"""NX-299 felia 2 — FORMA răspunsului e un contract cu condiții măsurabile, nu o speranță.

**Defectul.** Promptul cere deja forma iZi, cuvânt cu cuvânt: „În `intro` spune ce tipuri ai pus pe
masă și pentru ce e fiecare" (`prompt_builder._RICH_RULES`) și „SEGMENTARE (ca iZi) … câte o
recomandare CONDIȚIONATĂ per segment". Deci nu e o problemă de prompt. E o problemă de mecanism:
nimic nu verifică dacă slotul a ieșit, iar principiul 13 spune exact de ce nu ține — *vocea e cod,
nu speranță*. Aceeași frază se aplică formei.

Măsurat pe turul `42744330`, cele trei feluri în care slotul cerut nu ajunge la client:

1. **N-are ce descrie.** Pool-ul era raftul greșit, deci „ce tipuri ai pus pe masă" n-avea răspuns.
   Felia 1.
2. **E declarat opțional.** `education` are în prompt „REGULA DE AUR: pune-o DOAR dacă adaugă un
   criteriu NOU… mai bine gol". iZi consultă la FIECARE tur.
3. **Trece și cade la ieșire.** Modelul a scris încadrarea («Pentru coșuri, *cele mai potrivite*
   sunt produsele ZEROID…»), iar `scrub_intro` a aruncat PARAGRAFUL ÎNTREG pentru un superlativ.

**Ce face modulul.** Decide, din proprietăți MĂSURABILE ale setului servit, care sloturi sunt
obligatorii pe turul ăsta. Nimic altceva: e pur, nu scrie text, nu citește setări, nu atinge DB.
Textul de rezervă (`framing_text`) vine din `DomainPack`, ca la NX-296.

**De ce e dinamic și nu un șablon.** Niciuna dintre condiții nu numește o categorie, un vertical
sau o limbă: se citesc din setul servit și din axele pe care el variază. Pe o întrebare punctuală
(un singur produs, zero axe) `framing` și `closing` NU se cer deloc, fiindcă o încadrare peste un
singur card și o listă de criterii sub un răspuns de un rând arată ca un bot care nu te-a auzit.
E aceeași regulă ca `chip_moves.roles_for` (NX-296), extinsă de la chips la tot răspunsul.

**De ce lipsește „linia de tranziție".** Era în schița cardului („Iată câteva variante…") și a fost
scoasă deliberat: e singurul slot din listă care nu afirmă niciun fapt. Restul se derivă din date
(tipuri, axe, motive), el ar fi umplutură, iar umplutura e exact ce face un text să sune a AI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

#: Vocabular ÎNCHIS de sloturi. Un slot nedeclarat aici nu poate fi cerut și nu poate fi raportat
#: ca lipsă — registrul e poarta, ca la `ActionSpec` (NX-236) și `MOVE_ROLES` (NX-296). Contează
#: pentru observabilitate: eticheta unui event trebuie să vină dintr-o mulțime mărginită.
SLOT_FRAMING = "framing"
SLOT_FIT_LINE = "fit_line"
SLOT_CLOSING = "closing"

SLOTS: tuple[str, ...] = (SLOT_FRAMING, SLOT_FIT_LINE, SLOT_CLOSING)

#: Chips-urile NU sunt slot aici, deși erau în schița cardului. Au deja un proprietar care le
#: măsoară mai bine: `chip_moves` emite `kinds` ȘI `roles` chiar la producător, cu numărul de
#: candidați oferiți. Re-derivarea lor în runner ar cere semantică pe `Chip`, adică exact pe
#: contractul v1 pe care NX-236 îl descrie ca „o etichetă… și dispare". Un al doilea măsurător,
#: mai slab, peste unul bun.

#: Motivele, tot vocabular ÎNCHIS (aceeași regulă de cardinalitate ca la sloturi).
REASONS: tuple[str, ...] = (
    "multiple_types",
    "single_type",
    "no_items",
    "axes_present",
    "no_axes",
    "too_few_items",
    "has_items",
)

#: Sub două tipuri distincte nu există „ce am pus pe masă": e un singur fel de produs, iar o frază
#: care anunță o varietate inexistentă e mai rea decât tăcerea.
_MIN_TYPES_FOR_FRAMING = 2
#: Sub două carduri nu există alegere, deci nu există criterii de alegere.
_MIN_ITEMS_FOR_CLOSING = 2


@dataclass(frozen=True, slots=True)
class SlotVerdict:
    """Un slot, cu verdictul și motivul LUI. Motivul nu e decorativ: e ce transformă «răspunsul
    n-a avut încheiere» din reproș în diagnostic."""

    name: str
    required: bool
    reason: str


@dataclass(frozen=True, slots=True)
class AnswerShape:
    """Forma cerută turului ăsta. Imuabilă: se calculează o dată, din fapte, și nu se negociază."""

    verdicts: tuple[SlotVerdict, ...]

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(v.name for v in self.verdicts if v.required)

    def reason_for(self, slot: str) -> str | None:
        return next((v.reason for v in self.verdicts if v.name == slot), None)


def distinct_types(products: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Tipurile canonice DISTINCTE ale setului servit, în ordinea primei apariții.

    Tipul LIPSĂ nu formează o clasă, din același motiv ca la cota din `diversify_pool` (NX-298):
    un sfert din catalog n-are `product_type` derivat, iar tratate împreună ar apărea drept „încă
    un fel de produs" tocmai când nu știm ce sunt. Ordinea primei apariții, nu alfabetică: e
    ordinea în care clientul le vede pe ecran, deci și ordinea în care o frază de încadrare le
    poate numi fără să contrazică pagina.
    """
    seen: list[str] = []
    for p in products:
        attrs = p.get("attributes")
        raw = p.get("product_type")
        if not raw and isinstance(attrs, dict):
            raw = attrs.get("product_type")
        value = " ".join(str(raw).split()).lower() if raw else ""
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def shape_for(
    *,
    n_items: int,
    product_types: Sequence[str],
    n_axes: int,
) -> AnswerShape:
    """Ce sloturi CERE turul ăsta, din ce s-a servit efectiv.

    Condițiile se citesc din setul SERVIT, nu din obligațiile planului, și asta e deliberat: pe
    calea vie (v1) nu există `AnswerPlanV2`, deci o regulă scrisă pe obligații ar fi funcționat
    doar pe creierul unic, adică exact acolo unde nu se vede azi. Setul servit există pe AMBELE
    căi, iar D4 spune oricum că structura e sursa: dacă pe ecran sunt șase carduri din patru clase
    de produs, turul E o recomandare, indiferent cum l-ar eticheta cineva.
    """
    n_types = len(product_types)
    if n_items <= 0:
        framing = SlotVerdict(SLOT_FRAMING, False, "no_items")
        fit = SlotVerdict(SLOT_FIT_LINE, False, "no_items")
        closing = SlotVerdict(SLOT_CLOSING, False, "no_items")
    else:
        framing = SlotVerdict(
            SLOT_FRAMING,
            n_types >= _MIN_TYPES_FOR_FRAMING,
            "multiple_types" if n_types >= _MIN_TYPES_FOR_FRAMING else "single_type",
        )
        fit = SlotVerdict(SLOT_FIT_LINE, True, "has_items")
        if n_items < _MIN_ITEMS_FOR_CLOSING:
            closing = SlotVerdict(SLOT_CLOSING, False, "too_few_items")
        else:
            closing = SlotVerdict(
                SLOT_CLOSING, n_axes >= 1, "axes_present" if n_axes >= 1 else "no_axes"
            )
    return AnswerShape(verdicts=(framing, fit, closing))


def missing_slots(shape: AnswerShape, filled: Mapping[str, bool]) -> tuple[str, ...]:
    """Sloturile CERUTE care n-au ieșit. Ordinea e cea din registru, deci stabilă între ture."""
    return tuple(s for s in SLOTS if s in shape.required and not filled.get(s, False))


# --- textul de rezervă -------------------------------------------------------------------------


def _template(pack: object, key: str, locale: str) -> str | None:
    """Șablonul în limba clientului, din pachet. Lipsă ⇒ None ⇒ slotul rămâne al modelului (P11).

    Fail-OPEN, spre deosebire de poarta per chip: un pachet fără șablon nu are voie să ȘTEARGĂ
    încadrarea scrisă de model, doar să nu poată oferi una de rezervă.
    """
    table = getattr(pack, "answer_shape_templates", None)
    per_key = table.get(key) if isinstance(table, dict) else None
    if not isinstance(per_key, dict):
        return None
    lang = (locale or "").strip().lower()
    value = per_key.get(lang) or per_key.get(lang.split("-")[0])
    return value if isinstance(value, str) and value.strip() else None


def _enumerate(values: Sequence[str], glue: str) -> str:
    """«a, b și c» — ultimul lipit cu legătura LOCALEI, restul cu virgulă.

    Legătura vine din pachet fiindcă e limbă, nu logică (P11): în engleză e „and", în maghiară
    „és", iar un „și" în cod ar fi exact constanta pe care D3 o interzice.
    """
    if len(values) <= 1:
        return values[0] if values else ""
    return f"{', '.join(values[:-1])}{glue}{values[-1]}"


def framing_text(pack: object, locale: str, product_types: Sequence[str]) -> str | None:
    """Încadrarea SERVERULUI: ce clase de produs sunt pe masă. `None` = nu se poate spune onest.

    Nu e un al doilea writer semantic și nu costă o rundă: nu afirmă nimic despre produse, doar
    NUMEȘTE tipurile pe care retrievalul chiar le-a servit. E același tipar ca la `render_move`
    (NX-296) — serverul are șablonul, modelul poate doar să scrie o variantă mai bună, iar poarta
    decide care pleacă.
    """
    types = [t for t in product_types if t]
    if len(types) < _MIN_TYPES_FOR_FRAMING:
        return None
    template = _template(pack, "framing", locale)
    glue = _template(pack, "list_glue", locale)
    if template is None or glue is None:
        return None
    try:
        return template.format(types=_enumerate(types, glue))
    except (KeyError, IndexError):
        # Șablon cu alt slot decât producem: eroare de configurare a tenantului, nu motiv de
        # crăpat turul. Încadrarea pur și simplu nu se oferă.
        return None


__all__ = [
    "REASONS",
    "SLOTS",
    "SLOT_CLOSING",
    "SLOT_FIT_LINE",
    "SLOT_FRAMING",
    "AnswerShape",
    "SlotVerdict",
    "distinct_types",
    "framing_text",
    "missing_slots",
    "shape_for",
]
