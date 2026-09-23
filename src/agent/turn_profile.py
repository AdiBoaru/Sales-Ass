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

__all__ = [
    "PER_PROFILE_FLAGS",
    "PROFILES",
    "PROFILE_VERSION",
    "TurnProfile",
    "enabled_names",
    "gate",
    "name_for_turn",
    "select",
]

#: Versiunea registrului. Intră în `brain_versions` → atribute de trace, deci o schimbare de sufix
#: e vizibilă în telemetrie fără să ghicești ce prompt a rulat.
PROFILE_VERSION = "turn_profile.v2"  # NX-315: sufixul `howto` capătă forma „răspunsul întâi"


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

_ROUTINE_SUFFIX = (
    "Turul ăsta cere o SECVENȚĂ de pași, nu o listă de produse. Pașii vin din `routine_plan`, în "
    "ordinea și cu numerotarea întoarse de unealtă, un produs per pas. Scrie fiecare pas pe rândul "
    "lui: ce face, de ce produsul ăla, și cum se leagă de ce a cerut clientul. Un pas marcat LIPSĂ "
    "se spune ca atare și nu se renumerotează restul. Nu inventa pași și nu muta produse dintr-un "
    "pas în altul."
)

# NX-315: a doua jumătate e FORMA. Pe turul real «cum se foloseste prima», răspunsul revindea
# produsul într-un paragraf, îngropa instrucțiunile la mijloc și repeta „dimineața și seara".
# Clientul tocmai a văzut cardul, deci prima frază e răspunsul la „cum", nu descrierea.
_HOWTO_SUFFIX = (
    "Turul ăsta cere instrucțiuni de folosire pentru un produs anume. Cheamă "
    "`get_product_details` și scrie pașii din ce întoarce el, fiindcă instrucțiunile sunt ale "
    "magazinului, nu ale tale. Dacă nu e clar despre care produs vorbește clientul, întreabă care "
    "dintre cele afișate. Dacă unealta nu întoarce instrucțiuni, spune că nu le ai și nu le "
    "compune din ce știi tu. Prima frază răspunde direct la cum se folosește. Apoi pașii, în "
    "ordinea din fișă, apoi un singur avertisment dacă fișa are unul. Nu descrie din nou produsul, "
    "clientul tocmai l-a văzut, și nu spune același lucru de două ori."
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
_ROUTINE = TurnProfile(name="routine", extra_tools=("routine_plan",), suffix=_ROUTINE_SUFFIX)
# `get_product_details` e deja în toolsetul de bază, deci declararea e un no-op la compunere (se
# face dedupe pe nume). E declarată oricum, din același motiv ca la `compare`: profilul spune de ce
# are NEVOIE, iar componența nucleului nu e treaba lui.
_HOWTO = TurnProfile(name="howto", extra_tools=("get_product_details",), suffix=_HOWTO_SUFFIX)
_MUTATION = TurnProfile(name="mutation", extra_tools=(), suffix=_MUTATION_SUFFIX)

#: Toate profilele, indexate pe nume.
PROFILES: dict[str, TurnProfile] = {
    p.name: p for p in (_EXACT, _RECOMMEND, _COMPARE, _ROUTINE, _HOWTO, _MUTATION)
}

#: NX-307 — profile care pot fi aprinse INDIVIDUAL, fără să schimbe sufixul pentru tot traficul:
#: nume de profil → atributul de settings care îl aprinde.
#:
#: Există ca DATE, într-un singur loc, fiindcă poarta se aplică în DOUĂ căi (`brain.py` și
#: `stages/agent.py`). Până acum fiecare purta scris în cod `candidate.name == "routine"`, iar al
#: doilea profil cu flag propriu ar fi însemnat două liste care diverg. Un profil nou cu flag
#: propriu = o intrare aici, și ambele căi îl văd.
PER_PROFILE_FLAGS: dict[str, str] = {
    "routine": "routine_enabled",
    "howto": "howto_from_catalog_enabled",
}


def gate(
    candidate: TurnProfile, *, all_on: bool, enabled_names: frozenset[str]
) -> TurnProfile | None:
    """Profilul ales trece de flag-uri? PURĂ.

    `all_on` (`TURN_PROFILES_ENABLED`) aprinde tot, deci schimbă sufixul pentru TOT traficul și se
    decide pe golden (D15). Altfel trec doar profilele aprinse individual. Nimic aprins ⇒ `None`,
    adică drumul de azi, byte-identic.
    """
    if all_on:
        return candidate
    return candidate if candidate.name in enabled_names else None


def enabled_names(settings: object) -> frozenset[str]:
    """Numele profilelor aprinse individual, citite din settings prin `PER_PROFILE_FLAGS`.

    Singurul loc din modul care atinge configul. Restul rămâne pur, ca în docstring-ul de sus.
    """
    return frozenset(
        name for name, flag in PER_PROFILE_FLAGS.items() if bool(getattr(settings, flag, False))
    )


def select(
    turn_class: TurnClass,
    obligations: Iterable[object],
    *,
    has_routine: bool = False,
) -> TurnProfile:
    """Profilul turului. PUR, determinist, fără model.

    `has_routine` = tenantul a DECLARAT familii de pași (`DomainPack.routine_steps.families`).
    Fără ele, profilul `routine` nu se poate selecta, oricât de clar ar cere clientul o rutină:
    sufixul lui trimite modelul la `routine_plan`, iar unealta n-ar avea ce pași să umple. Un profil
    care promite o secvență inexistentă e mai rău decât unul absent, fiindcă modelul ar umple golul
    singur. Se cade pe `recommend`, care răspunde onest cu produse.

    Condiția e pe FAMILII, nu pe muchii ordonate (cum era înainte de NX-292): compunerea se face din
    fațeta `routine_step`, iar graful intră doar ca ancoră opțională. Un tenant cu pași declarați și
    zero muchii poate compune o rutină perfect validă — cerând muchii, i-am fi refuzat-o.

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
    if "routine" in kinds:
        return _ROUTINE if has_routine else _RECOMMEND
    if "explain" in kinds and "recommend" not in kinds:
        # NX-307: „cum se folosește" cere instrucțiunile MAGAZINULUI, nu o recomandare. Condiția pe
        # `recommend` urmează exact precedentul de mai jos: un tur care cere și o recomandare n-are
        # ce căuta pe un sufix care îl trimite să răspundă dintr-o singură fișă de produs.
        return _HOWTO
    if turn_class is TurnClass.EXACT and kinds and kinds <= {"answer", "safety"}:
        # `EXACT` singur nu ajunge: clasa spune „ieftin", profilul spune „fapt". Un tur exact care
        # conține și o cerere de recomandare (`recommend`) n-are ce căuta pe sufixul care interzice
        # recomandările, oricât de mic ar fi bugetul lui.
        return _EXACT
    return _RECOMMEND


def name_for_turn(ctx: object) -> str:
    """NX-315: numele profilului turului, FĂRĂ poarta de flaguri. Întrebarea e „ce fel de tur e",
    nu „ce sufix primește bucla", și o pun trei locuri (compunerea rich, raportul de formă, bucla
    v1). Un singur calcul, ca să nu poată numi același tur în două feluri.

    Singura funcție din modul care citește un `TurnContext`; importurile sunt leneșe fiindcă
    `brain_models` și `tool_definitions` trag după ele jumătate din agent."""
    from src.agent.brain_models import extract_obligations  # noqa: PLC0415
    from src.agent.tool_definitions import tenant_enum_values  # noqa: PLC0415
    from src.runtime.turn_budget import turn_class_for  # noqa: PLC0415

    message = getattr(ctx, "message", None)
    obligations = extract_obligations(str(getattr(message, "body", "") or ""))
    pack = getattr(getattr(ctx, "business", None), "domain_pack", None)
    families = tenant_enum_values(pack)["families"]
    return select(turn_class_for(obligations), obligations, has_routine=bool(families)).name


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
    # NX-307: un flag per-profil care numește un profil inexistent n-ar aprinde nimic și n-ar da
    # nicio eroare — exact felul de flag „legal și inert" pe care NX-304 l-a găsit costisitor.
    unknown = set(PER_PROFILE_FLAGS) - set(PROFILES)
    if unknown:
        raise ValueError(f"PER_PROFILE_FLAGS numește profile inexistente: {sorted(unknown)}")


_validate_registry()
