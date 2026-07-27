"""NX-205 — Contractul TIPIZAT al adevărului despre produse (Facts / Evidence / Provenance /
DerivedSignals). Pur: fără DB, fără LLM, fără I/O.

**Extinde NX-168d, NU îl rescrie.** Vocabularul canonic (concerns, finish, coverage, texture,
routine_step) și shape-urile `claim_provenance` / `not_recommended_for` rămân ale contractului v3
(`db/seed/catalog_v3.schema.json`) — aici sunt doar TIPIZATE în Python, ca să existe un singur
validator pe care îl pot folosi și seed-ul, și auditul, și codul de runtime. Delta cardului:

  1. `EvidenceChunk`  — fragmentul citabil, cu ROL explicit (D9: negativele intră aici ca
     `warning`).
  2. `DerivedSignal`  — semnal DERIVAT, separat fizic de faptul confirmat: `derived_from[]` +
     `rule_id` (regula e versionată → reparabilă global, nu produs cu produs).
  3. `CategoryRequirements` — câmpurile obligatorii PER CATEGORIE, din DomainPack (P9), nu din cod.
  4. `locale` + `schema_version` pe artefactele derivate (D3).

**Cele trei stări ale adevărului (D5) — nu se amestecă niciodată:**
  - *confirmat*  → `ProductFacts` + `ClaimProvenance` (are sursă verificabilă);
  - *derivat*    → `DerivedSignal` (are `rule_id`, deci se poate re-calcula/repara);
  - *necunoscut* → absența câmpului. NU se completează cu o presupunere.

**Zero PII:** contractele descriu PRODUSE. Niciun câmp nu ține text de utilizator, nume, telefon
sau id de canal — `EvidenceChunk.text` e conținut de catalog și cere `source` (fără sursă nu e
„evidence", e afirmație). Vezi `test_contracts.py::test_no_pii_shaped_fields`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Versiunea contractului de artefact derivat (D3). Distinctă de `products.schema_version` (028):
# aceea versionează RÂNDUL de produs, asta versionează ARTEFACTUL derivat din el.
CONTRACT_SCHEMA_VERSION = 1

# Vocabulare ÎNCHISE, identice cu `db/seed/catalog_v3.schema.json` (sursa 168d). Testul
# `test_contract_vocabularies_match_json_schema` le ține sincronizate — dacă schema se schimbă și
# aici nu, CI pică. Fără a treia copie „aproape la fel".
PROVENANCE_KINDS: frozenset[str] = frozenset({"ingredient", "badge", "certification"})
CONTRAINDICATION_LEVELS: frozenset[str] = frozenset({"hard", "soft"})

# Rolurile unui fragment de evidence (D9). `warning` e cel care face negativele citabile: o
# contraindicație nu se ascunde din document, se etichetează.
EVIDENCE_ROLES: frozenset[str] = frozenset(
    {"benefit", "usage", "warning", "ingredient", "faq", "review_summary", "policy"}
)


class ContractError(ValueError):
    """Contract încălcat — fail-closed (artefactul NU se produce)."""


class ClaimProvenance(BaseModel):
    """Sursa unui claim verificabil (NX-168d, shape neschimbat). Acoperă FIECARE `key_ingredient`
    (`kind=ingredient`) și FIECARE `badge` (`kind=badge`). Contraindicațiile NU intră aici — ele au
    proveniență inline (vezi `NotRecommendedFor`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ingredient", "badge", "certification"]
    value: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    verified_at: str = Field(min_length=1)


class NotRecommendedFor(BaseModel):
    """Contraindicație cu SEVERITATE (NX-168d + NX-173). `hard` verificat → excludere dură
    (NX-170/`reason_codes`); `soft` → penalizare + atenționare.

    `hard` cere proveniență INLINE (source + source_ref + verified_at): o excludere dură fără sursă
    e o afirmație medicală nesusținută, exact ce interzice P0-safety. `soft` cere `reason` — altfel
    penalizăm fără să putem spune de ce.

    `rule_id` / `reviewed_by` / `matched_on` sunt scrise de backfill-ul NX-173
    (`scripts/backfill_safety_flags.py`) și fac parte din contract: o intrare derivată dintr-o
    regulă
    trebuie să spună DIN CE regulă — altfel nu se poate repara global când se schimbă regula."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(min_length=1)
    level: Literal["hard", "soft"]
    reason: str | None = None
    source: str | None = None
    source_ref: str | None = None
    verified_at: str | None = None
    rule_id: str | None = None
    reviewed_by: str | None = None
    matched_on: str | None = None

    @model_validator(mode="after")
    def _severity_requires_backing(self) -> NotRecommendedFor:
        if self.level == "hard":
            missing = [
                f for f in ("source", "source_ref", "verified_at") if not (getattr(self, f) or "")
            ]
            if missing:
                raise ValueError(
                    f"contraindicație hard fără proveniență inline (lipsesc: {', '.join(missing)}) "
                    f"— o excludere dură fără sursă nu are voie să existe"
                )
        elif not (self.reason or ""):
            raise ValueError("contraindicație soft fără `reason` — penalizăm fără să putem explica")
        return self

    @property
    def is_enforceable(self) -> bool:
        """True dacă poate produce o EXCLUDERE dură (aceeași condiție ca `not_recommended_gate`)."""
        return self.level == "hard" and bool(self.source) and bool(self.verified_at)


class NetContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float = Field(gt=0)
    unit: Literal["ml", "l", "g", "kg", "buc"]


class _DerivedArtifact(BaseModel):
    """Bază pentru artefactele DERIVATE dintr-un produs. D3: fiecare poartă `business_id`, `locale`
    și `schema_version` — un artefact fără ele nu se poate nici izola pe tenant, nici migra."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    business_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    locale: str = Field(min_length=2, max_length=8)
    schema_version: int = Field(default=CONTRACT_SCHEMA_VERSION, ge=1)


class EvidenceChunk(_DerivedArtifact):
    """Fragment CITABIL dintr-un produs, cu rol explicit (D9).

    `source` e obligatoriu: un fragment fără sursă nu e evidence, e afirmație. Negativele
    (contraindicații, avertismente) INTRĂ aici cu `role="warning"` — nu se filtrează din document,
    ca să nu fie nevoie ca modelul să le „știe" din altă parte."""

    role: Literal["benefit", "usage", "warning", "ingredient", "faq", "review_summary", "policy"]
    text: str = Field(min_length=1)
    source: str = Field(min_length=1)

    @field_validator("text", "source")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("câmp gol (doar spații)")
        return v


class DerivedSignal(_DerivedArtifact):
    """Semnal DERIVAT, ținut separat de faptul confirmat (D5).

    `derived_from` = faptele care l-au produs (nevid: un semnal fără intrări nu e derivat, e
    inventat). `rule_id` = regula care l-a produs, versionată — când regula se dovedește greșită,
    se re-derivează TOATE semnalele ei, nu se repară produs cu produs. Precedentul de shape e
    `src/safety/contraindications.py` (`Block.rule_id`), reutilizat intenționat."""

    signal: str = Field(min_length=1)
    derived_from: tuple[str, ...] = Field(min_length=1)
    rule_id: str = Field(min_length=1)

    @field_validator("derived_from")
    @classmethod
    def _inputs_not_blank(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if any(not (x or "").strip() for x in v):
            raise ValueError("`derived_from` conține intrări goale")
        return v


class ProductFacts(_DerivedArtifact):
    """Faptele CANONICE ale unui produs, tipizate (proiecția `attributes` din contractul v3).

    Nu duplică vocabularul: valorile se validează contra DomainPack-ului prin
    `validate_vocabulary()`, nu contra unei liste din cod (P9). Aici se impun INVARIANTELE
    structurale, cele care nu depind de vertical:

      - un claim important fără sursă nu trece (`key_ingredients`/`badges` cer `claim_provenance`);
      - contradicțiile interne nu trec (aceeași valoare și recomandată, și contraindicată).
    """

    category_slug: str | None = None
    concerns: tuple[str, ...] = ()
    suitable_for: tuple[str, ...] = ()
    key_ingredients: tuple[str, ...] = ()
    free_of: tuple[str, ...] = ()
    badges: tuple[str, ...] = ()
    finish: str | None = None
    coverage: str | None = None
    texture: str | None = None
    routine_step: str | None = None
    net_content: NetContent | None = None
    not_recommended_for: tuple[NotRecommendedFor, ...] = ()
    claim_provenance: tuple[ClaimProvenance, ...] = ()

    def provenance_values(self, kind: str) -> frozenset[str]:
        """Valorile acoperite de proveniență COMPLETĂ pentru un `kind` (aceeași condiție ca
        `facet_coverage._claim_verified`: toate cele 5 câmpuri prezente — impus deja de model)."""
        return frozenset(p.value for p in self.claim_provenance if p.kind == kind)

    @model_validator(mode="after")
    def _claims_have_sources(self) -> ProductFacts:
        missing_ing = sorted(set(self.key_ingredients) - self.provenance_values("ingredient"))
        missing_badge = sorted(set(self.badges) - self.provenance_values("badge"))
        problems = []
        if missing_ing:
            problems.append(f"key_ingredients fără proveniență: {missing_ing}")
        if missing_badge:
            problems.append(f"badges fără proveniență: {missing_badge}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _no_internal_contradictions(self) -> ProductFacts:
        for problem in contradictions(self):
            raise ValueError(problem)
        return self


def contradictions(facts: ProductFacts) -> list[str]:
    """Contradicțiile INTERNE ale unui set de fapte (deterministe, fără vocabular de vertical).

    Nu sunt „stil", sunt fapte care se exclud reciproc: dacă produsul e recomandat pentru ceva ce
    e și contraindicat, una din cele două afirmații e falsă și nu putem ști care — deci artefactul
    nu are voie să existe (fail-closed), nu îl publicăm „cu un warning"."""
    out: list[str] = []
    contra = {n.value for n in facts.not_recommended_for}
    both_suitable = sorted(set(facts.suitable_for) & contra)
    if both_suitable:
        out.append(f"valoare și în `suitable_for`, și în `not_recommended_for`: {both_suitable}")
    hard_contra = {n.value for n in facts.not_recommended_for if n.level == "hard"}
    both_concern = sorted(set(facts.concerns) & hard_contra)
    if both_concern:
        out.append(f"concern tratat și contraindicat HARD simultan: {both_concern}")
    both_free = sorted(
        {_norm(x) for x in facts.free_of} & {_norm(x) for x in facts.key_ingredients}
    )
    if both_free:
        out.append(f"ingredient declarat și `free_of`, și `key_ingredient`: {both_free}")
    dupes = sorted({v for v in contra if [n.value for n in facts.not_recommended_for].count(v) > 1})
    if dupes:
        out.append(f"contraindicație duplicată (severități posibil divergente): {dupes}")
    return out


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def validate_vocabulary(facts: ProductFacts, allowed: Mapping[str, Iterable[str]]) -> list[str]:
    """Valorile de fapte contra vocabularului CONTROLAT al verticalului (DomainPack, P9).

    Separat de validarea structurală din model: vocabularul e config per-vertical, deci nu are ce
    căuta într-un model Pydantic din cod. Câmp fără vocabular declarat = nevalidat (nu respins):
    fail-open aici e corect: gate-ul de publicare (NX-206) e cel care decide, nu contractul.
    """
    problems: list[str] = []
    for field, values in (
        ("concerns", facts.concerns),
        ("suitable_for", facts.suitable_for),
        ("key_ingredients", facts.key_ingredients),
    ):
        vocab = {str(v) for v in (allowed.get(field) or ())}
        if not vocab:
            continue
        unknown = sorted({v for v in values if v not in vocab})
        if unknown:
            problems.append(f"{field}: valori în afara vocabularului {unknown}")
    for field in ("finish", "coverage", "texture", "routine_step"):
        value = getattr(facts, field)
        vocab = {str(v) for v in (allowed.get(field) or ())}
        if value is not None and vocab and value not in vocab:
            problems.append(f"{field}: valoare în afara vocabularului {value!r}")
    return problems


@dataclass(frozen=True, slots=True)
class CategoryRequirements:
    """Câmpurile OBLIGATORII per categorie — din DomainPack (P9), nu hardcodate în audit.

    Două niveluri, ca în contractul 168d: `by_slug` (categoria frunză exactă) și `by_root`
    (rădăcina arborelui, ex. `ingrijirea-tenului`).

    **Slug-ul BATE rădăcina (override), nu se cumulează** — semantica lui NX-168d (R10), păstrată
    intenționat: `mascara` cere `key_benefit`, dar NU `finish`-ul rădăcinii `machiaj`, pentru că
    produsele de ochi n-au finish de complexion (paletele au finishuri mixte). Un cumul ar fi
    inventat cerințe pe care contractul v3 le exclude explicit."""

    by_slug: Mapping[str, frozenset[str]]
    by_root: Mapping[str, frozenset[str]]

    def required_for(self, slug: str | None, root: str | None = None) -> frozenset[str]:
        """Obligatoriile efective: frunza dacă e declarată, altfel rădăcina (override, nu union)."""
        leaf = self.by_slug.get(slug or "")
        if leaf is not None:
            return frozenset(leaf)
        return frozenset(self.by_root.get(root or "", frozenset()))

    def missing(
        self, attributes: Mapping[str, Any], slug: str | None, root: str | None = None
    ) -> tuple[str, ...]:
        """Câmpurile obligatorii ABSENTE sau goale. Ordine sortată → mesaje deterministe."""
        required = self.required_for(slug, root)
        return tuple(sorted(k for k in required if attributes.get(k) in (None, "", [], {}, ())))


EMPTY_REQUIREMENTS = CategoryRequirements(by_slug={}, by_root={})


def build_category_requirements(raw: Any) -> CategoryRequirements:
    """Config → `CategoryRequirements`, **fail-closed per intrare** (tipar `build_facets`):
    o intrare invalidă e ignorată, nu dărâmă pack-ul și nu devine cerință pe jumătate. Config
    lipsă/gunoi → cerințe goale (comportamentul de azi pentru verticalele fără contract)."""
    if not isinstance(raw, dict):
        return EMPTY_REQUIREMENTS

    def _level(node: Any) -> dict[str, frozenset[str]]:
        out: dict[str, frozenset[str]] = {}
        if not isinstance(node, dict):
            return out
        for key, fields in node.items():
            if not isinstance(key, str) or not key.strip():
                continue
            if not isinstance(fields, list):
                continue
            clean = frozenset(f for f in fields if isinstance(f, str) and f.strip())
            if clean:
                out[key] = clean
        return out

    return CategoryRequirements(
        by_slug=_level(raw.get("by_slug")), by_root=_level(raw.get("by_root"))
    )
