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


def framing_text(
    pack: object,
    locale: str,
    product_types: Sequence[str],
    *,
    min_types: int = _MIN_TYPES_FOR_FRAMING,
) -> str | None:
    """Încadrarea SERVERULUI: ce clase de produs sunt pe masă. `None` = nu se poate spune onest.

    Nu e un al doilea writer semantic și nu costă o rundă: nu afirmă nimic despre produse, doar
    NUMEȘTE tipurile pe care retrievalul chiar le-a servit. E același tipar ca la `render_move`
    (NX-296) — serverul are șablonul, modelul poate doar să scrie o variantă mai bună, iar poarta
    decide care pleacă.

    `min_types`: ca SLOT cerut, încadrarea are sens de la două clase în sus (peste una singură ar
    repeta ce se vede). Ca UNICĂ frază a răspunsului (NX-312: proza sărită, rich picat) pragul
    coboară la 1, fiindcă alternativa nu mai e „o frază mai bună", ci carduri fără niciun cuvânt.
    """
    types = [t for t in product_types if t]
    if len(types) < max(1, min_types):
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


# --- NX-315: forma după ce a CERUT clientul ------------------------------------------------------
#
# Sloturile de mai sus MĂSOARĂ. Directivele de mai jos CER: sunt liniile pe care serverul le pune în
# mesajul turului către compunerea rich, doar pe turele pe care setul servit le justifică. Nu intră
# în system (prefixul cache-uit rămâne byte-identic), iar fiecare e sub flagul feliei ei.
#
# Sunt scrise ÎN vocea pe care o cer (principiul 13), verificat la import: un exemplu cu liniuță
# într-un prompt îl învață pe model exact ce îi interzici.

_GUIDANCE_DIRECTIVE = (
    "Pe turul ăsta `education` NU e opțională. Scrie 1-2 fraze despre cum alege clientul între "
    "produsele de pe masă, pe axele {axes}: criteriile întâi, pe categorie, apoi, dacă ajută, ce "
    "produs se potrivește fiecărui segment, cu produsul lui din listă. Fără cifre."
)

_NARROWING_DIRECTIVE = (
    "Poți pune O singură întrebare clientului, despre {label}. Opțiunile din set sunt: {options}. "
    "Scrie-o în câmpul `question`, scurt, numind opțiunile, ca să poată răspunde dintr-un cuvânt. "
    "Nu pune nicio altă întrebare în `intro` sau în `education`. Dacă întrebarea n-ar schimba ce "
    "îi recomanzi, lasă `question` gol."
)

_HOWTO_DIRECTIVE = (
    "Turul ăsta cere instrucțiuni de folosire. Instrucțiunile magazinului pentru [{product_id}]: "
    "{instructions}\n"
    "`intro` = prima frază răspunde direct la cum se folosește. `education` = pașii din "
    "instrucțiunile de mai sus, în ordinea lor, apoi un singur avertisment dacă instrucțiunile au "
    "unul. Nu descrie din nou produsul, clientul tocmai l-a văzut. Nu adăuga pași care nu sunt în "
    "instrucțiuni și nu spune același lucru de două ori."
)

_HOWTO_MISSING_DIRECTIVE = (
    "Turul ăsta cere instrucțiuni de folosire, dar magazinul nu are instrucțiuni pentru produsul "
    "[{product_id}]. Spune asta pe scurt în `intro` și nu compune pași din ce știi tu."
)


def guidance_directive(axis_names: Sequence[str]) -> str | None:
    """Linia care face „cum alegi" obligatoriu. `None` fără axe: fără axă de decizie nu există
    criteriu onest de numit, iar un paragraf generic e exact umplutura pe care regula de aur din
    `_RICH_RULES` o interzice pe bună dreptate."""
    names = [n for n in axis_names if n]
    if not names:
        return None
    return _GUIDANCE_DIRECTIVE.format(axes=", ".join(names))


def narrowing_directive(label: str, phrases: Sequence[str]) -> str | None:
    """Oferta de întrebare. `None` sub două opțiuni: o întrebare cu un singur răspuns posibil nu
    îngustează nimic."""
    options = [p for p in phrases if p]
    if len(options) < 2 or not label:
        return None
    return _NARROWING_DIRECTIVE.format(label=label, options=", ".join(options))


def howto_directive(product_id: str, instructions: str | None) -> str:
    """Instrucțiunile MAGAZINULUI în compunere, sau spusa onestă că nu există (regula NX-307)."""
    if instructions:
        return _HOWTO_DIRECTIVE.format(product_id=product_id, instructions=instructions)
    return _HOWTO_MISSING_DIRECTIVE.format(product_id=product_id)


def axis_names(axes: Sequence[str]) -> tuple[str, ...]:
    """„Tip de ten: uscat / gras" → „Tip de ten". Axele vin din `compose.decision_axes`, deci
    numele sunt etichetele fațetelor din pachet, nu cuvinte alese aici."""
    return tuple(a.split(":", 1)[0].strip() for a in axes if a and a.split(":", 1)[0].strip())


#: Motivele pentru care întrebarea scrisă de model NU pleacă. Vocabular ÎNCHIS (event).
QUESTION_REJECTIONS: tuple[str, ...] = ("no_question", "not_a_question", "off_target", "unsafe")

#: Câte dintre opțiunile oferite trebuie să NUMEASCĂ întrebarea ca să fie a fațetei oferite.
#: Două, nu una: „ai tenul uscat?" e o întrebare de confirmare, nu una care desparte setul.
_MIN_OPTIONS_NAMED = 2


def distinguishing_parts(phrases: Sequence[str]) -> tuple[str, ...]:
    """Partea din fiecare frază care o DEOSEBEȘTE de celelalte.

    „ten uscat", „ten gras", „ten sensibil" → „uscat", „gras", „sensibil". Un om întreabă „ai tenul
    uscat, gras sau sensibil?", nu repetă subiectul la fiecare opțiune, deci o poartă care ar cere
    fraza întreagă ar respinge exact întrebarea firească. Cuvintele comune TUTUROR frazelor sunt
    subiectul, nu opțiunea, iar ele se scot. Pur, fără listă de cuvinte: ce e comun se calculează.
    """
    tokenized = [[w for w in p.lower().split() if w] for p in phrases]
    if len(tokenized) < 2:
        return tuple(" ".join(t) for t in tokenized)
    common = set(tokenized[0]).intersection(*tokenized[1:])
    out: list[str] = []
    for words in tokenized:
        rest = [w for w in words if w not in common]
        out.append(" ".join(rest or words))
    return tuple(out)


def judge_question(question: object, phrases: Sequence[str]) -> tuple[str | None, str | None]:
    """`(întrebarea curățată, None)` dacă pleacă, `(None, motiv)` altfel. PURĂ.

    Poarta e „întrebarea e pe fațeta OFERITĂ", măsurată prin câte opțiuni numește. Potrivirea e
    `corroborated_by` (NX-251): pe prefix, deci „uscată" numește „uscat" fără nicio regulă de
    flexiune scrisă pentru română. Siguranța textului (cifre, claim-uri, medical) e a apelantului,
    care are scrub-ul compunerii.
    """
    from src.conversation.needs import corroborated_by  # noqa: PLC0415 — modul altfel fără deps

    if not isinstance(question, str) or not question.strip():
        return None, "no_question"
    text = " ".join(question.split())
    if text.count("?") != 1 or not text.endswith("?"):
        return None, "not_a_question"
    named = sum(1 for part in distinguishing_parts(phrases) if corroborated_by(text, part))
    if named < _MIN_OPTIONS_NAMED:
        return None, "off_target"
    return text, None


#: Motivele pentru `howto_instructions`, vocabular ÎNCHIS (event `howto_instructions`).
HOWTO_REASONS: tuple[str, ...] = ("instructions", "no_instructions", "no_sheet", "not_declared")


def howto_instructions(product: Mapping[str, Any], pack: object) -> tuple[str | None, str]:
    """`(textul instrucțiunilor MAGAZINULUI, motiv)` pentru un produs. PURĂ.

    Motivul contează mai mult decât pare: `no_instructions` (fișa e încărcată și n-are secțiunea)
    îi dă turului dreptul să spună „nu am instrucțiuni", pe când `no_sheet` (produsul a venit
    fără fișă, de exemplu dintr-o căutare) înseamnă doar că NU ȘTIM, iar a afirma lipsa ar fi o
    minciună. `not_declared`: pachetul nu spune care secțiuni sunt instrucțiuni.

    Plafonul fiecărei secțiuni e cel din `detail_sections`, deci compunerea vede exact cât a
    văzut modelul la detaliu, iar măsurătoarea compară cu exact ce i s-a arătat."""
    from src.catalog.render_text import cut_at_sentence  # noqa: PLC0415

    kinds = tuple(getattr(pack, "howto_sections", ()) or ())
    if not kinds:
        return None, "not_declared"
    if "sections" not in product:
        return None, "no_sheet"
    caps = {s.kind: s.max_chars for s in (getattr(pack, "detail_sections", ()) or ())}
    by_kind: dict[str, str] = {}
    for sec in product.get("sections") or []:
        if isinstance(sec, dict) and sec.get("kind") in kinds and sec.get("body"):
            by_kind.setdefault(str(sec["kind"]), str(sec["body"]))
    parts = [
        cut_at_sentence(" ".join(by_kind[k].split()), caps.get(k, 400))
        for k in kinds
        if k in by_kind
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None, "no_instructions"
    return " ".join(parts), "instructions"


#: Sub proporția asta de cuvinte de conținut din instrucțiuni regăsite în răspuns, pașii au venit
#: din memoria modelului, nu din fișă. PRIMĂ calibrare, declarată ca atare: nu există încă o
#: distribuție măsurată pe trafic (flagul e OFF), iar pragul se mută pe date, nu pe păreri.
EXPLAIN_GROUNDED_MIN = 0.25
#: „Prima frază revinde produsul": peste jumătate din cuvintele ei vin din descriere și sub o
#: cincime din instrucțiuni.
_RESOLD_FROM_DESCRIPTION = 0.5
_RESOLD_MAX_FROM_USAGE = 0.2


def explain_measure(
    answer: str, first_sentence: str, instructions: str, description: str, locale: str
) -> dict[str, Any]:
    """NX-315 felia 3 — cât din răspunsul la „cum se folosește" vine din fișa MAGAZINULUI. PURĂ.

    Măsurătoare, nu poartă. O poartă pe proză ar putea tăia un răspuns bun care parafrazează
    instrucțiunile, iar asta e exact decizia pe care o lăsăm datelor (D15). Cuvintele de conținut
    vin din `query_terms.content_terms`, cu cuvintele goale ale LOCALEI (P11), deci nicio listă
    scrisă aici. Doar numere și booleeni la ieșire (P12)."""
    from src.catalog.query_terms import content_terms  # noqa: PLC0415

    usage = set(content_terms(instructions, locale))
    said = set(content_terms(answer, locale))
    grounded = len(usage & said) / len(usage) if usage else 0.0
    first = set(content_terms(first_sentence, locale)) if first_sentence.strip() else set()
    desc = set(content_terms(description, locale)) if description.strip() else set()
    resold = bool(
        first
        and len(first & desc) / len(first) > _RESOLD_FROM_DESCRIPTION
        and len(first & usage) / len(first) < _RESOLD_MAX_FROM_USAGE
    )
    return {
        "grounded": round(grounded, 2),
        "grounded_below": grounded < EXPLAIN_GROUNDED_MIN,
        "resold": resold,
    }


def _validate_directives() -> None:
    """Poartă de IMPORT, ca la `turn_profile`: un text de prompt care încalcă vocea oprește
    procesul, nu primul tur."""
    from src.agent.voice import naturalize  # noqa: PLC0415

    for name, text in (
        ("guidance", _GUIDANCE_DIRECTIVE),
        ("narrowing", _NARROWING_DIRECTIVE),
        ("howto", _HOWTO_DIRECTIVE),
        ("howto_missing", _HOWTO_MISSING_DIRECTIVE),
    ):
        if naturalize(text) != text:
            raise ValueError(f"directiva {name} încalcă vocea (P13)")


_validate_directives()


__all__ = [
    "EXPLAIN_GROUNDED_MIN",
    "HOWTO_REASONS",
    "QUESTION_REJECTIONS",
    "REASONS",
    "SLOTS",
    "SLOT_CLOSING",
    "SLOT_FIT_LINE",
    "SLOT_FRAMING",
    "AnswerShape",
    "SlotVerdict",
    "axis_names",
    "distinct_types",
    "distinguishing_parts",
    "explain_measure",
    "framing_text",
    "guidance_directive",
    "howto_directive",
    "howto_instructions",
    "judge_question",
    "missing_slots",
    "narrowing_directive",
    "shape_for",
]
