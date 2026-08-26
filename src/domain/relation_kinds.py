"""Registrul de TIPURI DE RELAȚIE — ce înseamnă o muchie din `product_relations` și cât adânc
are voie cineva s-o urmeze.

Motivul pentru care există modulul: `product_relations` (migrarea 027) e un graf dirijat etichetat,
dar semantica muchiilor era coaptă în cod. Prioritatea de traversare trăia într-un `case kind when
'routine_next' then 0 ...` din SQL, iar `routine_next` e un cuvânt de cosmetică. Un tenant de
electrocasnice are `requires` / `compatible_with` / `consumable_for`, iar unul auto `fits_model`.
Cu semantica în cod, fiecare vertical nou ar fi cerut o migrare și o editare de query.

Linia de separare e aceeași ca la `src/domain/facets.py`, și e singura care ține pe termen lung:

    **Verticalul își NUMEȘTE muchiile. Codul definește ce COMPORTAMENTE există.**

Deci `TraversalMode` și `RelationPurpose` sunt vocabular ÎNCHIS aici, iar `kind` e complet liber și
vine din catalogul tenantului. Nicio listă de tipuri de muchie nu apare în acest fișier.

**De ce contează, concret.** Nu toate relațiile sunt tranzitive, iar diferența nu e teoretică:

  • o secvență (`routine_next` la beauty, „pașii de instalare" la electrocasnice) SE înlănțuie:
    A→B→C→D e un drum cu sens;
  • o relație SIMETRICĂ („merge bine cu", `complement`) NU se înlănțuie: A merge bine cu B, B merge
    bine cu C, dar A și C n-au nicio treabă. Măsurat pe catalogul demo, **toate** ancorele de
    `complement` sunt ciclice, fiindcă simetria ESTE definiția relației, nu un defect de seed;
  • o relație de COMPATIBILITATE (`compatible_with`) e capcana propriu-zisă: filtrul X se
    potrivește cu aspiratorul Y, Y se potrivește cu accesoriul Z, dar X nu se potrivește cu Z.

Un traversal scris „ca la rutine" și aplicat pe compatibilitate produce recomandări care trec și
validatorul (stagiul 8), și grounding guardul (NX-240): produsele EXISTĂ, prețurile sunt REALE.
Doar relația e inventată. Nicio poartă de adevăr existentă nu prinde asta, fiindcă nicio poartă nu
verifică relații. De aceea adâncimea e o proprietate DECLARATĂ a tipului, nu o presupunere.

**Fail-closed, cu direcția verificată.** Un tip care lipsește din config, sau al cărui config e
invalid, primește `NEIGHBORS` / adâncime 1: se comportă exact ca astăzi. Direcția contează. La un
FILTRU, a arunca o intrare invalidă LĂRGEȘTE tăcut rezultatul (mai puține constrângeri = mai multe
produse), deci acolo tăcerea e periculoasă. Aici, a arunca o intrare invalidă doar PIERDE o
capabilitate: nu se poate ajunge la un lanț pe care nimeni nu l-a declarat. Tăcerea nu acordă
traversare.

Totul e PUR: zero I/O, zero ceas, zero SQL. Sursa e `DomainPack` (defaults JSON + override
per-tenant, P9); plafonul dur de adâncime rămâne în cod, fiindcă un config care cere adâncime 50 ar
cheltui bugetul de tur (NX-241), iar un plafon negociabil nu e plafon.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "EMPTY_RELATION_KINDS",
    "MAX_TRAVERSAL_DEPTH",
    "RelationKindConfigError",
    "RelationKindRegistry",
    "RelationKindSpec",
    "RelationPurpose",
    "TraversalMode",
    "load_relation_kinds",
]


class TraversalMode(str, Enum):
    """CÂT de adânc are voie cineva să urmeze muchia.

    Vocabular ÎNCHIS: descrie comportament de sistem, nu un vertical."""

    NEIGHBORS = "neighbors"  # doar vecinii direcți; relația nu se înlănțuie
    BOUNDED = "bounded"  # tranzitivă, dar slab: plafon mic + gardă de ciclu obligatorie
    CHAIN = "chain"  # tranzitivă și ordonată: drumul are sens ca secvență


class RelationPurpose(str, Enum):
    """DE CE există muchia — decide dacă rezultatul poate fi suprimat.

    `UPSELL` e o ocazie: dacă nu încape în răspuns, nu s-a pierdut nimic adevărat.
    `REQUIREMENT` e o condiție de funcționare (kitul de încastrare, adaptorul, consumabilul fără
    care aparatul nu pornește). Lipsa lui nu e o vânzare ratată, e un colet returnat, deci trebuie
    afișat chiar și când scade conversia. Distincția e de SISTEM, nu de vertical: și beauty poate
    avea cerințe (un developer fără care vopseaua nu funcționează).
    """

    UPSELL = "upsell"
    REQUIREMENT = "requirement"


# Plafonul DUR de adâncime, în cod. Config-ul poate cere mai puțin, niciodată mai mult: un tur are
# UN buget monoton (NX-241), iar o traversare adâncă îl cheltuie fără ca modelul să poată cere mai
# mult. Valoarea e peste ce a măsurat proba (`scripts/relations_graph_probe.py`: adâncime reală 4),
# ca un catalog mai bogat să nu fie tăiat de plafon, dar suficient de mică cât să rămână mărginită.
MAX_TRAVERSAL_DEPTH = 6

# Plafon de cardinalitate a registrului. Un pack cu sute de tipuri de muchie nu e config, e un
# catalog prost normalizat, iar promptul și traversarea l-ar plăti amândouă.
MAX_RELATION_KINDS = 32

# `kind` e identificator, nu text liber: ajunge în `where kind = $n` (parametrizat, deci fără risc
# de injecție) dar și în chei de config, etichete și metrici. Un vocabular de forme previzibile ține
# cardinalitatea mărginită și face driftul de config vizibil.
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# Cheile de etichetă sunt locale (`ro`, `en`, `hu`, `ro-RO`). Pilotul e `ro-RO`, dar nucleul rămâne
# locale-aware (D3): eticheta unui tip de muchie e text către client, deci nu poate fi o constantă.
_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


class RelationKindConfigError(ValueError):
    """Config de tip de relație invalid. Ridicat la CONSTRUCȚIE, nu la folosire."""


@dataclass(frozen=True)
class RelationKindSpec:
    """Ce suportă un tip de muchie. Imutabil, validat în `__post_init__`.

    Validarea stă în constructor deliberat, pe modelul lui `vocabulary.Resolution`: garanția „un
    `NEIGHBORS` nu poate avea adâncime 3" nu e un câmp opțional pe care cineva uită să-l verifice,
    e condiția ca obiectul să existe.
    """

    kind: str
    mode: TraversalMode = TraversalMode.NEIGHBORS
    max_depth: int = 1
    ordered: bool = False  # rezultatul e o SECVENȚĂ (pași), nu un set
    purpose: RelationPurpose = RelationPurpose.UPSELL
    labels: Mapping[str, str] = field(default_factory=dict)  # locale → titlu afișabil

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _KIND_RE.match(self.kind):
            raise RelationKindConfigError(f"`kind` invalid: {self.kind!r}")
        if not isinstance(self.max_depth, int) or isinstance(self.max_depth, bool):
            raise RelationKindConfigError(f"{self.kind}: `max_depth` trebuie să fie int")
        if self.mode is TraversalMode.NEIGHBORS:
            if self.max_depth != 1:
                raise RelationKindConfigError(
                    f"{self.kind}: `neighbors` are adâncime 1 prin definiție, nu {self.max_depth}"
                )
            if self.ordered:
                raise RelationKindConfigError(
                    f"{self.kind}: un set de vecini nu poate fi `ordered` — ordinea presupune drum"
                )
        elif not (2 <= self.max_depth <= MAX_TRAVERSAL_DEPTH):
            raise RelationKindConfigError(
                f"{self.kind}: `max_depth` {self.max_depth} în afara [2, {MAX_TRAVERSAL_DEPTH}] "
                f"pentru modul `{self.mode.value}`"
            )
        for locale, text in (self.labels or {}).items():
            if not _LOCALE_RE.match(str(locale)):
                raise RelationKindConfigError(
                    f"{self.kind}: locale invalid în `labels`: {locale!r}"
                )
            if not str(text).strip():
                raise RelationKindConfigError(f"{self.kind}: etichetă goală pentru `{locale}`")

    @property
    def traversable(self) -> bool:
        """Se poate urma dincolo de vecinii direcți?"""
        return self.mode is not TraversalMode.NEIGHBORS

    def label(self, locale: str, default: str | None = None) -> str | None:
        """Eticheta pentru `locale`, cu cădere pe limba de bază (`ro-RO` → `ro`). Fără potrivire →
        `default`. NU inventăm un titlu: un bloc fără titlu e onest, unul cu titlu greșit nu e."""
        if not self.labels:
            return default
        if locale in self.labels:
            return self.labels[locale]
        base = str(locale).split("-")[0]
        return self.labels.get(base, default)


@dataclass(frozen=True)
class RelationKindRegistry:
    """Registrul tenantului. `get` întoarce MEREU un spec, ca apelantul să n-aibă ramură de `None`
    în care să greșească direcția de fail."""

    specs: Mapping[str, RelationKindSpec] = field(default_factory=dict)

    def get(self, kind: str) -> RelationKindSpec:
        """Specul pentru `kind`, sau unul `NEIGHBORS`/1 pentru un tip nedeclarat.

        Default-ul e chiar garanția de siguranță: cineva poate insera rânduri cu un `kind` nou în
        `product_relations` (seed, sync, import) fără ca asta să creeze tăcut lanțuri. Traversarea
        se acordă prin DECLARAȚIE, nu prin apariția unor date."""
        spec = self.specs.get(kind)
        if spec is not None:
            return spec
        return RelationKindSpec(kind=kind) if _KIND_RE.match(kind or "") else _UNSAFE_FALLBACK

    def traversable(self) -> tuple[RelationKindSpec, ...]:
        """Tipurile care se pot urma dincolo de vecinii direcți, ordonate stabil (nume)."""
        return tuple(
            sorted((s for s in self.specs.values() if s.traversable), key=lambda s: s.kind)
        )

    def sequences(self) -> tuple[RelationKindSpec, ...]:
        """Tipurile care produc o SECVENȚĂ de pași (`ordered`), ordonate stabil.

        Astea sunt tipurile pe care o cale DETERMINISTĂ le poate propune ca „pașii următori": la
        beauty rutina, la electrocasnice ce mai trebuie la instalare. Ordonarea pe nume nu e
        cosmetică — dacă un tenant declară două secvențe, alegerea trebuie să fie aceeași la fiecare
        tur, altfel același client primește alt răspuns la aceeași întrebare."""
        return tuple(s for s in self.traversable() if s.ordered)


# Ultima plasă: un `kind` care nu trece nici măcar de `_KIND_RE` (venit dintr-o coloană stricată sau
# dintr-un import prost) nu poate produce un `RelationKindSpec` valid, dar apelantul tot are nevoie
# de un răspuns. Îi dăm unul inert, cu un nume rezervat care nu se poate potrivi cu nicio muchie.
_UNSAFE_FALLBACK = RelationKindSpec(kind="unknown_kind")


def _coerce_enum(raw: Any, enum_cls: type[Enum], kind: str, field_name: str) -> Any:
    try:
        return enum_cls(str(raw))
    except ValueError as exc:
        allowed = ", ".join(sorted(e.value for e in enum_cls))
        raise RelationKindConfigError(
            f"{kind}: `{field_name}` = {raw!r} necunoscut (permise: {allowed})"
        ) from exc


def load_relation_kinds(raw: Any) -> RelationKindRegistry:
    """Construiește registrul din config-ul brut al DomainPack-ului.

    Formă acceptată: `{kind: {mode, max_depth, ordered, purpose, labels}}`. O intrare invalidă e
    RESPINSĂ individual (logată), nu ridică — vezi antetul modulului: aici a arunca o intrare doar
    pierde o capabilitate, nu lărgește tăcut nimic. Config absent sau de tip greșit → registru gol,
    adică exact comportamentul de azi (vecini direcți peste tot).
    """
    if not isinstance(raw, Mapping):
        if raw:
            log.warning("relation_kinds: config ignorat, așteptat obiect, primit %s", type(raw))
        return RelationKindRegistry()

    specs: dict[str, RelationKindSpec] = {}
    for kind, cfg in raw.items():
        if len(specs) >= MAX_RELATION_KINDS:
            log.warning(
                "relation_kinds: peste %d tipuri, restul ignorate (începând cu %r)",
                MAX_RELATION_KINDS,
                kind,
            )
            break
        if not isinstance(cfg, Mapping):
            log.warning("relation_kinds: `%s` ignorat, așteptat obiect, primit %s", kind, type(cfg))
            continue
        try:
            key = str(kind).strip().lower()
            mode = _coerce_enum(cfg.get("mode", "neighbors"), TraversalMode, key, "mode")
            purpose = _coerce_enum(cfg.get("purpose", "upsell"), RelationPurpose, key, "purpose")
            depth_raw = cfg.get("max_depth", 1 if mode is TraversalMode.NEIGHBORS else 2)
            labels = cfg.get("labels") or {}
            if not isinstance(labels, Mapping):
                raise RelationKindConfigError(f"{key}: `labels` trebuie să fie obiect locale→text")
            specs[key] = RelationKindSpec(
                kind=key,
                mode=mode,
                max_depth=int(depth_raw) if not isinstance(depth_raw, bool) else -1,
                ordered=bool(cfg.get("ordered", False)),
                purpose=purpose,
                labels={str(k): str(v) for k, v in labels.items()},
            )
        except (RelationKindConfigError, TypeError, ValueError) as exc:
            log.warning("relation_kinds: `%s` respins (rămâne vecini-direcți): %s", kind, exc)
    return RelationKindRegistry(specs=specs)


# Default-ul unui `DomainPack` fără config de relații. Partajabil fiindcă e imutabil, și e chiar
# starea de azi: fiecare tip e vecini-direcți, nimic nu se înlănțuie.
EMPTY_RELATION_KINDS = RelationKindRegistry()
