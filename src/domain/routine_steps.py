"""Pasul dintr-o rutină: cine e produsul în SECVENȚA pe care o cere clientul (NX-280).

De ce există fațeta
-------------------
Cererea de rutină e o clasă de sine stătătoare în pipeline: `brain_models` o detectează determinist,
`control_plane` o tratează ca BLOCANTĂ, iar `answer_plan` cere ≥2 produse. Pragul e însă pe
CARDINALITATE, nu pe secvență — deci două produse oarecare trec. Iar în aval nu există nicio poartă
care să prindă asta: validatorul (stagiul 8) și `grounding_guard` verifică ADEVĂRUL (prețul există?
produsul există?), nu POTRIVIREA. Un ruj cu preț real vândut ca pas dintr-o rutină pentru ten uscat
iese cu toate ștampilele puse. Măsurat pe catalogul SOLE: «rutina ten uscat» întorcea același ruj în
două nuanțe pe pozițiile 4 și 6.

Ce e în COD și ce e în PACHET (P9)
----------------------------------
Modulul ăsta știe ce e un PAS — o poziție într-o secvență, purtată de o familie, cu o ordine. Nu
știe, și nu are voie să știe, că „gel de curatare" e curățare: asta e o proprietate a verticalului.
La electrocasnice aceeași poziție e ocupată de pașii de instalare, iar un `if vertical == "beauty"`
aici ar fi exact greșeala pe care `product_type.py` a evitat-o folosind reguli gramaticale.

Deci harta `product_type → familie:pas`, ordinea pașilor și regulile de promovare vin din
`domain_pack.routine_steps`. Codul le VALIDEAZĂ și le aplică.

Cheia e COMPUSĂ, și asta nu e cosmetică
----------------------------------------
Valoarea canonică e `familie:pas` (`fata:curatare`), niciodată doar `curatare`. „sampon" și „gel de
curatare" sunt amândouă curățare, dar în rutine diferite; cu o singură scară liniară nimic n-ar
împiedica un șampon să ajungă în rutina de față — un eșec pe care validatorul l-ar lăsa să treacă,
identic cu cel pe care fațeta îl repară.

Promovarea pe atribut, și de ce e mărginită la familie
------------------------------------------------------
Nu există `product_type = "protectie solara"` în catalog: cele 83 de creme cu SPF sunt tipizate
`crema de fata`. Deci pasul de protecție vine dintr-un ATRIBUT (`spf`), nu din tip.

Regula evidentă — «`spf` prezent ⇒ pasul de protecție, indiferent de tip» — e GREȘITĂ, și a fost
măsurată ca atare pe catalogul SOLE înainte de a fi scrisă: ar muta 48 de produse din familia lor.
35 de bază de machiaj (cremă colorantă 21, fond de ten 9, cushion 5), 9 de corp (cremă de corp 7,
loțiune 2) și 4 balsamuri de buze. O loțiune de corp cu SPF nu e un pas din rutina feței, iar un
fond de ten cu SPF 30 rămâne bază de machiaj — nimeni nu-și bazează protecția solară pe fondul de
ten. Ar fi fost exact clasa de eroare pe care fațeta o repară: o categorie greșită, servită cu
încredere.

De aceea promovarea se aplică DUPĂ hartă (are nevoie de familia pe care harta o stabilește) și
DOAR în interiorul familiei declarate în regulă.

Ce NU face
----------
Nu ghicește. Un produs fără `product_type` nu primește pas, chiar dacă are `spf`: fără să știm ce e
produsul, familia ar fi o presupunere. Absența cheii înseamnă „nu știm", iar `load_vocabulary`
ignoră oricum valorile sub prag. Fațeta moștenește exact gaura lui `product_type` și nu adaugă una
nouă.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "EMPTY_ROUTINE_STEPS",
    "SEP",
    "Promotion",
    "RoutineSpec",
    "RoutineStepConfigError",
    "build_spec",
    "distinct_steps",
    "load_routine_steps",
    "resolve",
]

#: Separatorul dintre familie și pas în valoarea canonică. Nu e configurabil: apare în valorile
#: scrise în catalog, deci schimbarea lui ar invalida tăcut datele deja derivate.
SEP = ":"


class RoutineStepConfigError(ValueError):
    """Config de pași invalid — fail-closed (jobul refuză să scrie, nu inventează pași)."""


@dataclass(frozen=True)
class Promotion:
    """«Dacă produsul poartă atributul X și e deja în familia F, pasul lui devine S.»

    `within_family` e obligatoriu prin proiectare, nu prin convenție: o promovare nemărginită e
    exact bugul măsurat în docstringul modulului.

    `corroborate_name` cere ca NUMELE produsului să confirme atributul. Există fiindcă o promovare
    e la fel de bună ca atributul pe care se sprijină, iar `spf` s-a dovedit MĂSURABIL nesigur:
    derivarea lui citește numărul din fraze de SFAT, care trimit către alt produs — «Obligatoriu:
    foloseste crema cu SPF 50 in fiecare dimineata, deoarece retinolul poate sensibiliza pielea la
    soare» a pus `spf=50` pe un ser cu retinol. Opt produse pe catalogul SOLE, dintre care șapte
    în familia feței, deci promovate la pasul de protecție solară — iar un retinol prezentat ca
    protecție solară nu e o recomandare slabă, e sfat care contrazice propria fișă a produsului.

    Tokenii sunt vocabular de VERTICAL, deci stau în pachet, nu aici (P9). Gol → fără cerință de
    coroborare, adică promovare pe atribut simplu.
    """

    when_attribute: str
    within_family: str
    to_step: str
    corroborate_name: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutineSpec:
    """Harta validată, gata de aplicat. Imutabilă."""

    #: familie → pașii ei, ÎN ORDINE. Indexul din listă E ordinea; nu se declară separat, ca să nu
    #: existe două surse pentru același adevăr (principiul 3, aplicat la config).
    families: dict[str, tuple[str, ...]]
    #: product_type → valoare canonică `familie:pas`
    by_product_type: dict[str, str]
    promotions: tuple[Promotion, ...] = ()
    #: tipuri care NU sunt un pas — declarate EXPLICIT, ca să se distingă de cele uitate.
    not_a_step: frozenset[str] = field(default_factory=frozenset)
    #: tipuri pentru care TIPUL NU AJUNGE ca să determine pasul. Distinct de `not_a_step`, și
    #: distincția e tot `UNKNOWN ≠ MISMATCH`: un «set» nu e un pas (afirmație), o «lotiune de
    #: fata» e sigur un pas, doar nu știm care (ignoranță). Măsurat pe SOLE: cele 18 produse
    #: tipizate așa se împrăștie pe ȘASE categorii — demachiant, toner, hidratant, protecție
    #: solară, corp, all-in-one. Un pas majoritar le-ar da unei treimi pasul greșit, iar
    #: `not_a_step` ar afirma că nu sunt produse de rutină, ceea ce e fals.
    #: Amândouă întorc None din `resolve`; diferă în RAPORT, unde contează: o scăpare trebuie
    #: reparată, o ambiguitate declarată e o decizie luată.
    ambiguous: frozenset[str] = field(default_factory=frozenset)

    def canonical_values(self) -> tuple[str, ...]:
        """Toate valorile `familie:pas` posibile, în ordinea de parcurs. Astea intră ca `values`
        în declarația fațetei — deci vocabularul fațetei nu se întreține separat de hartă."""
        return tuple(f"{fam}{SEP}{step}" for fam, steps in self.families.items() for step in steps)

    def order_of(self, value: str) -> int | None:
        """Poziția pasului în familia lui (1-based), sau None dacă valoarea nu e cunoscută.
        Folosită la SORTAREA pașilor unei rutine — modelul primește produsele în ordinea reală."""
        fam, _, step = value.partition(SEP)
        steps = self.families.get(fam)
        if not steps or step not in steps:
            return None
        return steps.index(step) + 1

    def family_of(self, value: str) -> str | None:
        fam = value.partition(SEP)[0]
        return fam if fam in self.families else None


def build_spec(raw: Any) -> RoutineSpec:
    """Validează `domain_pack.routine_steps` → `RoutineSpec`. Fail-closed pe orice invaliditate.

    Validarea nu e paranoia de config: o hartă care numește un pas inexistent ar scrie în catalog o
    valoare pe care fațeta n-o declară, iar filtrul ar întoarce tăcut zero rânduri — adică exact
    forma de eșec pe care NX-280 o repară, mutată cu un nivel mai jos.
    """
    if not isinstance(raw, dict):
        raise RoutineStepConfigError(
            f"routine_steps trebuie să fie obiect, nu {type(raw).__name__}"
        )

    fam_raw = raw.get("families")
    if not isinstance(fam_raw, dict) or not fam_raw:
        raise RoutineStepConfigError("routine_steps.families lipsă sau gol")

    families: dict[str, tuple[str, ...]] = {}
    for fam, steps in fam_raw.items():
        if not isinstance(fam, str) or SEP in fam or not fam:
            raise RoutineStepConfigError(f"nume de familie invalid: {fam!r}")
        if not isinstance(steps, list) or not steps:
            raise RoutineStepConfigError(f"familia {fam!r} n-are pași")
        if not all(isinstance(s, str) and s and SEP not in s for s in steps):
            raise RoutineStepConfigError(f"pas invalid în familia {fam!r}: {steps!r}")
        if len(set(steps)) != len(steps):
            raise RoutineStepConfigError(f"pași duplicați în familia {fam!r}: {steps!r}")
        families[fam] = tuple(steps)

    known = {f"{fam}{SEP}{s}" for fam, steps in families.items() for s in steps}

    by_type_raw = raw.get("by_product_type")
    if not isinstance(by_type_raw, dict) or not by_type_raw:
        raise RoutineStepConfigError("routine_steps.by_product_type lipsă sau gol")
    by_product_type: dict[str, str] = {}
    for ptype, value in by_type_raw.items():
        if not isinstance(ptype, str) or not isinstance(value, str):
            raise RoutineStepConfigError(f"intrare invalidă în by_product_type: {ptype!r}")
        if value not in known:
            raise RoutineStepConfigError(
                f"by_product_type[{ptype!r}] = {value!r}, care nu e un pas declarat în families"
            )
        by_product_type[ptype] = value

    promotions: list[Promotion] = []
    for p in raw.get("promotions") or []:
        if not isinstance(p, dict):
            raise RoutineStepConfigError(f"promovare invalidă: {p!r}")
        attr, fam, step = p.get("when_attribute"), p.get("within_family"), p.get("to_step")
        if not all(isinstance(x, str) and x for x in (attr, fam, step)):
            raise RoutineStepConfigError(f"promovare incompletă: {p!r}")
        if fam not in families:
            raise RoutineStepConfigError(f"promovare către familia necunoscută {fam!r}")
        if step not in families[fam]:
            raise RoutineStepConfigError(f"promovare către pasul {step!r}, absent din {fam!r}")
        tokens = p.get("corroborate_name") or []
        if not isinstance(tokens, list) or not all(isinstance(t, str) and t for t in tokens):
            raise RoutineStepConfigError(f"corroborate_name invalid pt promovarea {p!r}")
        promotions.append(Promotion(attr, fam, step, tuple(t.lower() for t in tokens)))

    buckets: dict[str, frozenset[str]] = {}
    for name in ("not_a_step", "ambiguous"):
        items = raw.get(name) or []
        if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
            raise RoutineStepConfigError(f"routine_steps.{name} trebuie să fie listă de stringuri")
        if overlap := set(items) & by_product_type.keys():
            raise RoutineStepConfigError(
                f"tipuri și mapate, și declarate {name}: {sorted(overlap)}"
            )
        buckets[name] = frozenset(items)

    # Un tip nu poate fi simultan „nu e un pas" și „nu știm care pas" — sunt afirmații care se
    # contrazic, iar o contradicție în config ar face raportul să mintă indiferent de ramură.
    if both := buckets["not_a_step"] & buckets["ambiguous"]:
        raise RoutineStepConfigError(f"tipuri și not_a_step, și ambiguous: {sorted(both)}")

    return RoutineSpec(
        families=families,
        by_product_type=by_product_type,
        promotions=tuple(promotions),
        not_a_step=buckets["not_a_step"],
        ambiguous=buckets["ambiguous"],
    )


EMPTY_ROUTINE_STEPS = RoutineSpec(families={}, by_product_type={})


def load_routine_steps(raw: Any) -> RoutineSpec:
    """Varianta TOLERANTĂ, pentru încărcarea pachetului în producție.

    Asimetria față de `build_spec` e deliberată și e aceeași ca la `relation_kinds`: la CITIRE, o
    hartă stricată trebuie să coste o capabilitate (rutinele nu se mai pot compune), nu tot
    pachetul — un `raise` aici ar lăsa tenantul fără `concern_map` și fără fațete pentru o virgulă
    greșită în `routine_steps`. La SCRIERE (`scripts/derive_routine_step.py`) e exact invers:
    jobul refuză să scrie, fiindcă o hartă parțial validă ar pune în catalog pași pe care fațeta
    nu-i declară, iar filtrul ar întoarce tăcut zero rânduri.

    Config absent → spec gol, adică EXACT comportamentul de azi: niciun produs n-are pas.
    """
    if not raw:
        return EMPTY_ROUTINE_STEPS
    try:
        return build_spec(raw)
    except RoutineStepConfigError as e:
        log.warning("routine_steps: config respins, rutinele rămân indisponibile (%s)", e)
        return EMPTY_ROUTINE_STEPS


def _name_corroborates(name: str, tokens: tuple[str, ...]) -> bool:
    """Numele produsului conține vreunul dintre tokeni, la ÎNCEPUT de cuvânt?

    Granița e doar la început, deliberat: „SPF50+" trebuie să potrivească tokenul „spf", iar `\\b`
    la coadă n-ar potrivi (F și 5 sunt amândouă caractere de cuvânt). Aceeași greșeală a produs o
    măsurătoare falsă în timpul proiectării regulii — pinuită aici ca test.
    """
    low = (name or "").lower()
    return any(re.search(r"\b" + re.escape(t), low) for t in tokens)


def resolve(attributes: dict[str, Any] | None, spec: RoutineSpec, *, name: str = "") -> str | None:
    """`attributes` ale unui produs → valoarea canonică `familie:pas`, sau None dacă nu se știe.

    None e un răspuns, nu un eșec: înseamnă „produsul ăsta nu are pas cunoscut". Un produs fără
    `product_type` întoarce None chiar dacă are atributul unei promovări — familia ar fi o
    ghicitoare, iar pasul ar ajunge în catalog ca fapt.
    """
    attrs = attributes or {}
    ptype = attrs.get("product_type")
    if not isinstance(ptype, str) or not ptype:
        return None
    if ptype in spec.not_a_step or ptype in spec.ambiguous:
        return None

    base = spec.by_product_type.get(ptype)
    if base is None:
        return None

    # Promovarea vine DUPĂ hartă și e mărginită la familia ei — vezi docstringul modulului.
    family = base.partition(SEP)[0]
    for promo in spec.promotions:
        if promo.within_family != family:
            continue
        raw = attrs.get(promo.when_attribute)
        if raw is None or raw == "" or raw is False:
            continue
        if promo.corroborate_name and not _name_corroborates(name, promo.corroborate_name):
            continue  # atributul e prezent, dar produsul nu-l confirmă → rămâne pe pasul lui
        return f"{promo.within_family}{SEP}{promo.to_step}"
    return base


def distinct_steps(values: list[str] | tuple[str, ...], spec: RoutineSpec) -> dict[str, set[str]]:
    """familie → pașii DISTINCȚI prezenți printre valorile date.

    Asta e forma pe care o consumă poarta din `answer_plan`: o rutină cere ≥2 pași distincți din
    ACEEAȘI familie. Doi pași identici nu sunt o secvență (două creme nu sunt o rutină), iar două
    familii amestecate nu sunt una (un șampon și un ser de față nu sunt pași unul după altul).
    """
    out: dict[str, set[str]] = {}
    for v in values:
        if not isinstance(v, str) or SEP not in v:
            continue
        fam, _, step = v.partition(SEP)
        if fam in spec.families and step in spec.families[fam]:
            out.setdefault(fam, set()).add(step)
    return out
