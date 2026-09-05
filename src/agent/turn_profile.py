"""NX-275 felia 4 — PROFILE DE TUR: direcția de răspuns, declarată ca DATE, aleasă de COD.

Problema pe care o rezolvă. Creierul unic (NX-239) primește același system prompt pentru orice
fel de tur: o întrebare de preț, o recomandare, o comparație și un „adaugă în coș" citesc aceleași
instrucțiuni generale. Modelul trebuie deci să deducă singur ce fel de răspuns se cere, iar
deducția aia e gratuită doar în aparență: e exact locul unde un tur exact primește o recomandare
nesolicitată, sau o comparație iese fără axele pe care tool-ul le-a întors.

**Direcția o decide codul, o dată, înainte de orice apel.** Obligațiile sunt extrase determinist
din mesaj (`brain_models.extract_obligations`), din ele iese clasa de tur
(`turn_budget.turn_class_for`), iar profilul se alege PUR din cele două. Niciun model nu clasifică
nimic aici — altfel am plăti un apel ca să aflăm ce fel de apel să facem, adică exact cascada pe
care D1 o interzice.

**Un prefix, mai multe sufixe.** System-ul generat din DB, `_PLAN_V2_SYSTEM`, tool-urile și schema
rămân byte-identice pe toate direcțiile. Profilul adaugă un SUFIX scurt la finalul system-ului și,
cel mult, tool-uri ÎN PLUS. Trei prompturi separate ar însemna trei surse de adevăr care derivează
(motivul pentru care NX-239 le-a unificat) și trei prefixe de cache care se încălzesc separat.

**Un profil ADAUGĂ, niciodată nu scade.** Un „adaugă în coș" pe care regexul nu-l prinde trebuie să
aibă unealta la îndemână oricum. Tokenii tool-urilor nu sunt un motiv să tăiem: prefixul e
cache-uit (vezi felia 3), deci costă 0,1x, iar un tool absent costă un tur greșit.

**Clasa de tur NU se schimbă.** `TurnClass` rămâne cu patru valori fiindcă manifestul NX-241 e per
clasă (bugete de timp, tier de model). Profilul e ORTOGONAL: descrie forma răspunsului, nu bugetul.

Totul e PUR: zero I/O, zero ceas, zero stare. Registrul se validează la IMPORT, nu la primul tur.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.agent.voice import naturalize
from src.runtime.turn_budget import TurnClass

__all__ = ["PROFILES", "PROFILE_VERSION", "TurnProfile", "select"]

#: Versiunea registrului. Intră în `brain_versions` → atribute de trace, deci o schimbare de sufix
#: e vizibilă în telemetrie fără să ghicești ce prompt a rulat.
PROFILE_VERSION = "turn_profile.v1"


@dataclass(frozen=True, slots=True)
class TurnProfile:
    """O direcție de răspuns. `name` e low-cardinality (intră în evenimente ca etichetă)."""

    name: str
    extra_tools: tuple[str, ...]
    suffix: str
    speculative_retrieval: bool = False

    @property
    def version(self) -> str:
        return PROFILE_VERSION


# ── Sufixele ────────────────────────────────────────────────────────────────────────────────────
#
# Scrise ÎN vocea pe care o cer (principiul 13): fără liniuță de pauză, fără punct și virgulă. Un
# exemplu cu liniuță într-un prompt îl învață pe model exact ce îi interzici — așa a picat prima
# încercare de a impune regula doar prin memorie. Verificate la import prin `naturalize`.

_EXACT_SUFFIX = (
    "Turul ăsta cere un FAPT, nu o recomandare. Răspunde direct la ce s-a întrebat și oprește-te. "
    "Nu propune alte produse dacă nu ți s-a cerut. Dacă faptul cerut nu e în evidence, spune-l în "
    "`unknowns` și nu-l aproxima."
)

_RECOMMEND_SUFFIX = (
    "Turul ăsta cere o recomandare. Cel mult 6 produse, fiecare cu un motiv legat de ce a cerut "
    "clientul, nu generic. Pui o singură întrebare de clarificare, și doar dacă răspunsul ar "
    "schimba material ce recomanzi. Altfel recomanzi din ce ai."
)

_COMPARE_SUFFIX = (
    "Turul ăsta cere o comparație. Celulele din `comparison` vin DOAR din ce a întors "
    "`compare_products`. Dacă o axă lipsește pentru un produs, spune că lipsește, nu o completa. "
    "Recomanzi un câștigător doar dacă știi ce nevoie are clientul, altfel arăți diferențele."
)

_MUTATION_SUFFIX = (
    "Turul ăsta conține o acțiune. Confirmi DOAR acțiunile care apar în `successful_action_ids`. "
    "Dacă o acțiune n-a reușit, spui ce s-a întâmplat, nu presupui că a mers. După o adăugare în "
    "coș propui ce a întors unealta, nu ce crezi tu că s-ar potrivi."
)


# ── Registrul ───────────────────────────────────────────────────────────────────────────────────

_EXACT = TurnProfile(name="exact", extra_tools=(), suffix=_EXACT_SUFFIX)
_RECOMMEND = TurnProfile(
    name="recommend", extra_tools=(), suffix=_RECOMMEND_SUFFIX, speculative_retrieval=True
)
# `compare_products` e deja în toolsetul de bază azi, deci declararea lui aici e un no-op
# (adăugarea face dedupe pe nume). E declarat oricum fiindcă profilul spune de ce are NEVOIE, iar
# componența nucleului nu e treaba lui: dacă cineva subțiază `_SALES_TOOLS`, comparația rămâne
# întreagă.
_COMPARE = TurnProfile(name="compare", extra_tools=("compare_products",), suffix=_COMPARE_SUFFIX)
_MUTATION = TurnProfile(name="mutation", extra_tools=(), suffix=_MUTATION_SUFFIX)

#: Toate profilele, indexate pe nume. `routine` se adaugă în felia 5, împreună cu unealta de care
#: depinde: un profil care trimite modelul spre un tool inexistent e mai rău decât unul lipsă.
PROFILES: dict[str, TurnProfile] = {p.name: p for p in (_EXACT, _RECOMMEND, _COMPARE, _MUTATION)}


def select(turn_class: TurnClass, obligations: Iterable[object]) -> TurnProfile:
    """Profilul turului. PUR, determinist, fără model.

    Precedența e de la cel mai specific la cel mai general, iar necunoscutul URCĂ spre `recommend`
    (ca la `turn_class_for`): un tur pe care nu-l recunoaștem primește tratamentul bogat, nu pe cel
    îngust. Greșeala în direcția asta costă câțiva tokeni; greșeala inversă costă răspunsul — un
    tur de recomandare rulat cu sufixul `exact` ar refuza tocmai să recomande.

    Clasa de tur BATE obligațiile acolo unde ele se contrazic: `turn_class_for` a văzut deja
    întregul set (inclusiv regula „mesaj mixt ⇒ COMPLEX"), deci a re-deriva din obligații ar
    însemna două surse de adevăr pentru aceeași decizie.
    """
    kinds = {str(getattr(o, "kind", o) or "").strip().lower() for o in obligations}
    kinds.discard("")
    if turn_class is TurnClass.MUTATION or "action" in kinds:
        return _MUTATION
    if "compare" in kinds:
        return _COMPARE
    if turn_class is TurnClass.EXACT and kinds and kinds <= {"answer", "safety"}:
        # `EXACT` singur nu ajunge: clasa spune „ieftin", profilul spune „fapt". Un tur exact care
        # conține și o cerere de recomandare (`recommend`) n-are ce căuta pe sufixul care interzice
        # recomandările, oricât de mic ar fi bugetul lui.
        return _EXACT
    return _RECOMMEND


def _validate_registry() -> None:
    """Poartă de IMPORT: registrul stricat oprește procesul, nu primul tur.

    Trei lucruri, fiecare cu o consecință dacă lipsește: un sufix care încalcă vocea ar învăța
    modelul exact punctuația pe care i-o interzicem în altă parte (P13); un `extra_tools` cu un
    nume care nu există în registrul de unelte ar trimite modelul să cheme ceva inexistent, iar
    eroarea ar apărea ca „tool necunoscut" în mijlocul unui tur real; un nume de profil care nu e
    slug ar deschide cardinalitate în etichetele de telemetrie.
    """
    for name, profile in PROFILES.items():
        if name != profile.name or not name.isidentifier():
            raise ValueError(f"profil cu nume invalid: {name!r}")
        if not profile.suffix.strip():
            raise ValueError(f"profilul {name} n-are sufix")
        if naturalize(profile.suffix) != profile.suffix:
            raise ValueError(
                f"sufixul profilului {name} încalcă vocea (P13): conține liniuță de pauză sau "
                "punct și virgulă, adică exact ce interzicem modelului"
            )
        for tool in profile.extra_tools:
            if not tool.isidentifier():
                raise ValueError(f"{name}: nume de tool invalid: {tool!r}")


_validate_registry()
