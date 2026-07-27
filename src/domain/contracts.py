"""NX-205 — Contractul TIPIZAT al adevărului despre produse (Facts / Evidence / Provenance /
DerivedSignals). Pur: fără DB, fără LLM, fără I/O.

**Extinde NX-168d, NU îl rescrie.** Vocabularul canonic (concerns, finish, coverage, texture,
routine_step) și shape-urile `claim_provenance` / `not_recommended_for` rămân ale contractului v3
(`db/seed/catalog_v3.schema.json`) — aici sunt doar TIPIZATE în Python, ca să existe un singur
validator pe care îl pot folosi și seed-ul, și auditul, și codul de runtime.

**Cele trei stări ale adevărului (D5) — separate STRUCTURAL, nu prin disciplină:**
  - *confirmat*  → `ProductFacts` + `ClaimProvenance` (are sursă verificabilă);
  - *derivat*    → `DerivedSignal` (are `rule_id`, deci se poate re-calcula/repara global);
  - *necunoscut* → **`None`**, distinct de „cunoscut și gol" (`()`).

**`None` ≠ `()` — invariantul necunoscutului (review #250).** Dacă absența unui fapt se
serializează ca listă goală, „nu știm dacă are parfum" devine „nu are parfum" la prima citire, iar
un UNKNOWN pierdut nu se mai recuperează. De aceea colecțiile sunt `... | None`, default `None`, iar
serializarea canonică (`to_artifact()`) OMITE necunoscutele în loc să le aplatizeze.

**Prețul/stocul NU intră în `ProductFacts`.** Sunt fapte LIVE (`LiveFacts`), citite din rând la
momentul răspunsului. Un preț copiat într-un artefact derivat e un preț care va fi greșit — iar
garanția „nu inventăm prețuri" nu are voie să depindă de prospețimea unui cache.

**Zero PII:** contractele descriu PRODUSE. `EvidenceChunk.text` e conținut de catalog și cere
`source` (fără sursă nu e „evidence", e afirmație).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from src.domain.normalize import normalize

# Versiunea contractului de artefact derivat (D3). Distinctă de `products.schema_version` (028):
# aceea versionează RÂNDUL de produs, asta versionează ARTEFACTUL derivat din el.
CONTRACT_SCHEMA_VERSION = 1

# String care nu poate fi „gol deghizat": strip ÎNAINTE de validare, deci `" "` e respins la fel ca
# `""` (review #250 — proveniența din whitespace trecea și de contract, și de auditul R8).
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Vocabulare ÎNCHISE, identice cu `db/seed/catalog_v3.schema.json` (sursa 168d). Testul
# `test_contract_vocabularies_match_json_schema` le ține sincronizate.
PROVENANCE_KINDS: frozenset[str] = frozenset({"ingredient", "badge", "certification"})
CONTRAINDICATION_LEVELS: frozenset[str] = frozenset({"hard", "soft"})

# Rolurile unui fragment de evidence (D9). `warning` e cel care face negativele citabile: o
# contraindicație nu se ascunde din document, se etichetează.
EVIDENCE_ROLES: frozenset[str] = frozenset(
    {"benefit", "usage", "warning", "ingredient", "faq", "review_summary", "policy"}
)

# Cheile de `attributes` pe care contractul le TIPIZEAZĂ.
KNOWN_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "concerns",
        "suitable_for",
        "not_recommended_for",
        "key_ingredients",
        "free_of",
        "claim_provenance",
        "finish",
        "coverage",
        "texture",
        "routine_step",
        "usage",
        "wear_time",
        "net_content",
        "best_for",
        "key_benefit",
        "differentiators",
        "hair_type",
        "skin_type",
        "fragrance_free",
        "spf",
        "gtin",
        "schema_version",
    }
)
# Chei de serviciu ale seed-ului/pipeline-ului: nu sunt fapte, dar nici „necunoscute" de raportat.
_INTERNAL_ATTRIBUTE_KEYS: frozenset[str] = frozenset({"_meta", "_seed_deal", "specs", "badges"})


class ContractError(ValueError):
    """Contract încălcat — fail-closed (artefactul NU se produce)."""


class ClaimProvenance(BaseModel):
    """Sursa unui claim verificabil (NX-168d, shape neschimbat). Acoperă FIECARE `key_ingredient`
    (`kind=ingredient`) și FIECARE `badge` (`kind=badge`). Contraindicațiile NU intră aici — au
    proveniență inline (vezi `NotRecommendedFor`).

    Toate câmpurile sunt `NonBlank`: o „sursă" din spații e o sursă inexistentă cu pași în plus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ingredient", "badge", "certification"]
    value: NonBlank
    source: NonBlank
    source_ref: NonBlank
    verified_at: NonBlank


class NotRecommendedFor(BaseModel):
    """Contraindicație cu SEVERITATE (NX-168d + NX-173). `hard` verificat → excludere dură
    (NX-170/`reason_codes`); `soft` → penalizare + atenționare.

    `hard` cere proveniență INLINE (source + source_ref + verified_at): o excludere dură fără sursă
    e o afirmație medicală nesusținută, exact ce interzice P0-safety. `soft` cere `reason`.

    `rule_id` / `reviewed_by` / `matched_on` sunt scrise de backfill-ul NX-173 și fac parte din
    contract: o intrare derivată dintr-o regulă trebuie să spună DIN CE regulă."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: NonBlank
    level: Literal["hard", "soft"]
    reason: NonBlank | None = None
    source: NonBlank | None = None
    source_ref: NonBlank | None = None
    verified_at: NonBlank | None = None
    rule_id: NonBlank | None = None
    reviewed_by: NonBlank | None = None
    matched_on: NonBlank | None = None

    @model_validator(mode="after")
    def _severity_requires_backing(self) -> NotRecommendedFor:
        if self.level == "hard":
            missing = [f for f in ("source", "source_ref", "verified_at") if not getattr(self, f)]
            if missing:
                raise ValueError(
                    f"contraindicație hard fără proveniență inline (lipsesc: {', '.join(missing)}) "
                    f"— o excludere dură fără sursă nu are voie să existe"
                )
        elif not self.reason:
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


class Usage(BaseModel):
    """Cum se folosește produsul. Schema v3 permite chei suplimentare aici
    (`additionalProperties: true`), deci modelul le IGNORĂ în loc să respingă — nucleul tipizat e
    cel de mai jos, restul rămâne în `attributes` brut."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    time: tuple[NonBlank, ...] | None = None
    steps: tuple[NonBlank, ...] | None = None
    frequency: NonBlank | None = None


class Variant(BaseModel):
    """Identitatea unei variante. **Fără preț/stoc** — acelea sunt `LiveFacts`: o variantă
    „ieftină" memorată într-un artefact devine minciună la prima schimbare de preț."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    label: NonBlank
    sku: NonBlank | None = None
    gtin: NonBlank | None = None
    net_content: NetContent | None = None


class LiveFacts(BaseModel):
    """Faptele care se citesc LA MOMENTUL răspunsului, niciodată dintr-un artefact derivat.

    Există ca tip ca să fie explicit unde NU au voie să ajungă: nu în `ProductFacts`, nu în
    `EvidenceChunk`, nu în embedding. Validatorul de răspuns (stagiul 8) compară prețul din reply
    cu ASTA, nu cu un snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    price: float | None = None
    sale_price: float | None = None
    availability: str | None = None
    stock_total: int | None = None


class _DerivedArtifact(BaseModel):
    """Bază pentru artefactele DERIVATE dintr-un produs. D3: fiecare poartă `business_id`, `locale`
    și `schema_version` — un artefact fără ele nu se poate nici izola pe tenant, nici migra."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    business_id: NonBlank
    product_id: NonBlank
    locale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=8)]
    schema_version: int = Field(default=CONTRACT_SCHEMA_VERSION, ge=1)


class EvidenceChunk(_DerivedArtifact):
    """Fragment CITABIL dintr-un produs, cu rol explicit (D9).

    `source` e obligatoriu: un fragment fără sursă nu e evidence, e afirmație. Negativele
    (contraindicații, avertismente) INTRĂ aici cu `role="warning"` — nu se filtrează din document,
    ca să nu fie nevoie ca modelul să le „știe" din altă parte."""

    role: Literal["benefit", "usage", "warning", "ingredient", "faq", "review_summary", "policy"]
    text: NonBlank
    source: NonBlank


class DerivedSignal(_DerivedArtifact):
    """Semnal DERIVAT, ținut separat de faptul confirmat (D5).

    `derived_from` = faptele care l-au produs (nevid: un semnal fără intrări nu e derivat, e
    inventat). `rule_id` = regula care l-a produs, versionată — când regula se dovedește greșită,
    se re-derivează TOATE semnalele ei, nu se repară produs cu produs."""

    signal: NonBlank
    derived_from: tuple[NonBlank, ...] = Field(min_length=1)
    rule_id: NonBlank


class ProductFacts(_DerivedArtifact):
    """Faptele CANONICE ale unui produs (proiecția tipizată a `attributes` din contractul v3).

    **`None` = necunoscut, `()` = cunoscut și gol.** Constructorul `from_product()` respectă
    distincția: cheie lipsă → `None`; cheie prezentă cu listă goală → `()`.

    Nu duplică vocabularul: valorile se verifică contra DomainPack-ului prin `validate_vocabulary()`
    (P9). Aici se impun invariantele structurale, cele care nu depind de vertical:
      - un claim important fără sursă nu trece (`key_ingredients` cer `claim_provenance`);
      - contradicțiile interne nu trec — comparate NORMALIZAT (casing/diacritice/spații nu sunt
        portiță: „Alcohol " și „alcohol" sunt același ingredient).
    """

    category_slug: str | None = None
    concerns: tuple[str, ...] | None = None
    suitable_for: tuple[str, ...] | None = None
    key_ingredients: tuple[str, ...] | None = None
    free_of: tuple[str, ...] | None = None
    differentiators: tuple[str, ...] | None = None
    finish: str | None = None
    coverage: str | None = None
    texture: str | None = None
    routine_step: str | None = None
    skin_type: str | None = None
    hair_type: str | None = None
    best_for: str | None = None
    key_benefit: str | None = None
    wear_time: str | None = None
    spf: float | None = None
    fragrance_free: bool | None = None
    gtin: str | None = None
    usage: Usage | None = None
    net_content: NetContent | None = None
    variants: tuple[Variant, ...] | None = None
    not_recommended_for: tuple[NotRecommendedFor, ...] | None = None
    claim_provenance: tuple[ClaimProvenance, ...] | None = None

    # --- construcție din shape-ul BRUT (seed v3 sau rând de DB) ---------------

    @classmethod
    def from_product(
        cls,
        product: Mapping[str, Any],
        *,
        business_id: str,
        locale: str,
        product_id: str | None = None,
    ) -> ProductFacts:
        """Produs brut → fapte tipizate, FAIL-CLOSED. Ridică `ContractError` dacă intrarea e
        malformată și `ValidationError` dacă faptele încalcă contractul.

        Pentru inventarul unui catalog întreg (unde vrei toate golurile, nu prima excepție) există
        `parse_product()` — aceeași parsare, dar cu problemele întoarse ca listă."""
        values, problems = _collect_facts(
            product, business_id=business_id, locale=locale, product_id=product_id
        )
        if problems:
            raise ContractError("; ".join(problems))
        return cls(**values)

    def to_artifact(self) -> dict[str, Any]:
        """Serializare canonică: necunoscutele sunt OMISE, nu aplatizate la gol (review #250)."""
        return self.model_dump(exclude_none=True)

    @property
    def confirmed_badges(self) -> tuple[str, ...]:
        """Badge-urile CONFIRMATE — derivate din `claim_provenance`, nu dintr-un câmp propriu.

        D5 aplicat structural: un badge e un fapt doar dacă are sursă, deci existența lui E
        proveniența. Nu poate exista „badge fără sursă" ca stare a modelului. Badge-urile produse
        de reguli sunt `DerivedSignal` (vezi `derived_badge_candidates`)."""
        return tuple(
            sorted(
                {
                    p.value
                    for p in (self.claim_provenance or ())
                    if p.kind in ("badge", "certification")
                }
            )
        )

    def provenance_values(self, kind: str) -> frozenset[str]:
        """Valorile (NORMALIZATE) acoperite de proveniență completă pentru un `kind`."""
        return frozenset(
            normalize(p.value) for p in (self.claim_provenance or ()) if p.kind == kind
        )

    @model_validator(mode="after")
    def _claims_have_sources(self) -> ProductFacts:
        problems = claim_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _no_internal_contradictions(self) -> ProductFacts:
        for problem in contradictions(self):
            raise ValueError(problem)
        return self


def _collect_facts(
    product: Mapping[str, Any], *, business_id: str, locale: str, product_id: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Produs brut → (valori pentru `ProductFacts`, probleme de FORMĂ).

    Review 2 #250: parserul confunda „malformat" cu „necunoscut" (un `concerns: "oily"` devenea
    tăcut `None`) și arunca `ValidationError` din sub-modele chiar și în regimul de raport — adică
    exact acolo unde promisiunea era că nu aruncă.

    Regula: **absent → `None`, fără problemă; malformat → `None` + problemă RAPORTATĂ.** O intrare
    invalidă dintr-o listă e dropată individual și raportată, ca un singur rând stricat să nu
    ascundă restul faptelor bune."""
    raw_attrs = product.get("attributes")
    problems: list[str] = []
    if raw_attrs is not None and not isinstance(raw_attrs, dict):
        problems.append(
            f"attributes: malformat (așteptat obiect, primit {type(raw_attrs).__name__})"
        )
    attrs: Mapping[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
    pid = product_id or str(product.get("id") or product.get("slug") or "")
    if not pid:
        # Un artefact fără identitate de produs nu poate fi nici scris, nici corelat înapoi la
        # sursă. În regimul strict pica oricum (la validare), dar în raport trecea tăcut.
        problems.append("product_id: absent (nici `id`, nici `slug` pe produs)")

    def _strs(key: str, source: Mapping[str, Any] = attrs) -> tuple[str, ...] | None:
        if key not in source or source.get(key) is None:
            return None  # absent = necunoscut, fără problemă
        raw = source[key]
        if not isinstance(raw, list):
            problems.append(f"{key}: malformat (așteptat listă, primit {type(raw).__name__})")
            return None
        out, bad = [], 0
        for v in raw:
            if isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool)):
                out.append(str(v).strip())
            else:
                bad += 1
        if bad:
            problems.append(f"{key}: {bad} element(e) de tip nepermis, ignorate")
        return tuple(out)

    def _str(key: str, source: Mapping[str, Any] = attrs) -> str | None:
        if key not in source or source.get(key) is None:
            return None
        raw = source[key]
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            problems.append(f"{key}: malformat (așteptat text, primit {type(raw).__name__})")
            return None
        return str(raw).strip() or None

    def _num(key: str) -> float | None:
        if key not in attrs or attrs.get(key) is None:
            return None
        raw = attrs[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            problems.append(f"{key}: malformat (așteptat număr, primit {type(raw).__name__})")
            return None
        return float(raw)

    def _flag(key: str) -> bool | None:
        if key not in attrs or attrs.get(key) is None:
            return None
        raw = attrs[key]
        if not isinstance(raw, bool):
            problems.append(f"{key}: malformat (așteptat boolean, primit {type(raw).__name__})")
            return None
        return raw

    def _sub(key: str, model: type[BaseModel]) -> Any:
        if key not in attrs or attrs.get(key) is None:
            return None
        raw = attrs[key]
        if not isinstance(raw, dict):
            problems.append(f"{key}: malformat (așteptat obiect, primit {type(raw).__name__})")
            return None
        try:
            return model.model_validate(raw)
        except ValidationError as e:
            problems.append(f"{key}: invalid ({e.error_count()} eroare/erori de contract)")
            return None

    def _items(key: str, model: type[BaseModel], source: Mapping[str, Any] = attrs) -> Any:
        if key not in source or source.get(key) is None:
            return None
        raw = source[key]
        if not isinstance(raw, list):
            problems.append(f"{key}: malformat (așteptat listă, primit {type(raw).__name__})")
            return None
        out, bad = [], []
        for idx, entry in enumerate(raw):
            if not isinstance(entry, dict):
                bad.append(f"#{idx} nu e obiect")
                continue
            try:
                out.append(model.model_validate(entry))
            except ValidationError as e:
                bad.append(f"#{idx} {e.errors()[0].get('msg', 'invalid')}")
        if bad:
            problems.append(f"{key}: intrări invalide, dropate — {'; '.join(bad)}")
        return tuple(out)

    values = {
        "business_id": business_id,
        "product_id": pid,
        "locale": locale,
        "category_slug": _str("primaryCategorySlug", product) or _str("category_slug", product),
        "concerns": _strs("concerns"),
        "suitable_for": _strs("suitable_for"),
        "key_ingredients": _strs("key_ingredients"),
        "free_of": _strs("free_of"),
        "differentiators": _strs("differentiators"),
        "finish": _str("finish"),
        "coverage": _str("coverage"),
        "texture": _str("texture"),
        "routine_step": _str("routine_step"),
        "skin_type": _str("skin_type"),
        "hair_type": _str("hair_type"),
        "best_for": _str("best_for"),
        "key_benefit": _str("key_benefit"),
        "wear_time": _str("wear_time"),
        "spf": _num("spf"),
        "fragrance_free": _flag("fragrance_free"),
        "gtin": _str("gtin"),
        "usage": _sub("usage", Usage),
        "net_content": _sub("net_content", NetContent),
        "variants": _items("variants", Variant, product),
        "not_recommended_for": _items("not_recommended_for", NotRecommendedFor),
        "claim_provenance": _items("claim_provenance", ClaimProvenance),
    }
    return values, problems


def derived_badge_candidates(product: Mapping[str, Any]) -> tuple[str, ...]:
    """Badge-urile brute ale unui produs — **candidați de `DerivedSignal`, NU fapte**.

    Review 2 #250 (D5): badge-urile din catalogul demo sunt produse de reguli (`badge_rules` din
    DomainPack) din alte atribute („Fără parfum" ← `fragrance_free`). A le pune în `ProductFacts` ar
    fi amestecat DERIVATUL cu CONFIRMATUL — exact separarea pe care o apără cardul. Un badge devine:
      - `DerivedSignal{signal, derived_from[], rule_id}` dacă vine dintr-o regulă, sau
      - un fapt confirmat DOAR prin `claim_provenance` (vezi `ProductFacts.confirmed_badges`),
        pentru badge-urile care nu se pot deriva (certificări externe).
    Funcția expune materia primă; clasificarea o face NX-206 (are `badge_rules` și datele)."""
    raw = product.get("badges")
    if not isinstance(raw, list):
        attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
        raw = attrs.get("badges")
    if not isinstance(raw, list):
        return ()
    return tuple(str(b).strip() for b in raw if isinstance(b, str) and b.strip())


def claim_problems(facts: ProductFacts) -> list[str]:
    """Claims care nu-și au sursa (regula R8 din 168d, exprimată pe fapte tipizate).

    Extras din validator ca funcție de sine stătătoare pentru că e nevoie de el în DOUĂ regimuri:
    strict (producerea artefactului — fail-closed) și de RAPORT (`parse_product`, unde vrem să
    NUMĂRĂM golurile catalogului, nu să ne oprim la primul)."""
    problems: list[str] = []
    values = facts.key_ingredients
    if values:
        covered = facts.provenance_values("ingredient")
        missing = sorted({v for v in values if normalize(v) not in covered})
        if missing:
            problems.append(f"key_ingredients fără proveniență: {missing}")
    # `badges` NU se verifică aici: nu mai e un câmp de fapte (D5). Un badge e ori DERIVAT
    # (`DerivedSignal{rule_id}`), ori confirmat prin proveniență — și atunci există prin
    # construcție (`confirmed_badges`), deci nu poate fi „fără sursă".
    return problems


def parse_product(
    product: Mapping[str, Any], *, business_id: str, locale: str, product_id: str | None = None
) -> tuple[ProductFacts, tuple[str, ...]]:
    """Produs brut → (fapte, probleme) — regim de RAPORT, care NU aruncă pe date incomplete.

    `from_product()` e fail-closed: refuză să producă artefactul unui produs cu intrare malformată
    sau claims nesusținute. Corect pentru runtime, inutil pentru inventarul unui catalog întreg,
    unde vrei să vezi TOATE golurile deodată. Aici faptele se construiesc fără validare, iar
    problemele — de FORMĂ (malformat) și de CONTRACT (claims, contradicții) — se întorc ca listă.
    Nu aruncă niciodată pe date: dacă aruncă, e un bug al parserului (review 2 #250).

    Cine consumă asta NU are voie să publice un produs cu `problems` nevide — raportul e pentru
    gate-ul de completitudine (NX-206), nu o portiță de ocolire a contractului."""
    values, problems = _collect_facts(
        product, business_id=business_id, locale=locale, product_id=product_id
    )
    facts = ProductFacts.model_construct(**values)
    return facts, tuple(problems + claim_problems(facts) + contradictions(facts))


def unknown_attribute_keys(product: Mapping[str, Any]) -> tuple[str, ...]:
    """Cheile de `attributes` pe care contractul NU le tipizează (fără cele de serviciu).

    Nu e o eroare — e vizibilitate: un vertical poate avea atribute proprii, dar un „ingredient
    folosit din greșeală drept cheie" trebuie să se vadă, nu să se piardă tăcut în JSON."""
    attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
    return tuple(
        sorted(
            k for k in attrs if k not in KNOWN_ATTRIBUTE_KEYS and k not in _INTERNAL_ATTRIBUTE_KEYS
        )
    )


def contradictions(facts: ProductFacts) -> list[str]:
    """Contradicțiile INTERNE ale unui set de fapte (deterministe, fără vocabular de vertical).

    Comparațiile sunt NORMALIZATE (lower + fără diacritice + trim): altfel „Alcohol " și „alcohol"
    ar fi valori diferite, iar contradicția s-ar ocoli cu o majusculă (review #250).

    Nu sunt „stil", sunt fapte care se exclud reciproc: dacă produsul e recomandat pentru ceva ce e
    și contraindicat, una din afirmații e falsă și nu putem ști care — deci artefactul nu are voie
    să existe (fail-closed), nu îl publicăm „cu un warning"."""
    out: list[str] = []
    nrf = facts.not_recommended_for or ()
    contra = {normalize(n.value) for n in nrf}
    hard_contra = {normalize(n.value) for n in nrf if n.level == "hard"}

    both_suitable = sorted({v for v in (facts.suitable_for or ()) if normalize(v) in contra})
    if both_suitable:
        out.append(f"valoare și în `suitable_for`, și în `not_recommended_for`: {both_suitable}")
    both_concern = sorted({v for v in (facts.concerns or ()) if normalize(v) in hard_contra})
    if both_concern:
        out.append(f"concern tratat și contraindicat HARD simultan: {both_concern}")
    free_of = {normalize(v) for v in (facts.free_of or ())}
    both_free = sorted({v for v in (facts.key_ingredients or ()) if normalize(v) in free_of})
    if both_free:
        out.append(f"ingredient declarat și `free_of`, și `key_ingredient`: {both_free}")
    seen: set[str] = set()
    dupes: set[str] = set()
    for n in nrf:
        key = normalize(n.value)
        if key in seen:
            dupes.add(n.value)
        seen.add(key)
    if dupes:
        out.append(f"contraindicație duplicată (severități posibil divergente): {sorted(dupes)}")
    return out


@dataclass(frozen=True, slots=True)
class VocabularyReport:
    """Rezultatul verificării de vocabular — cu `unchecked` EXPLICIT.

    Review #250: a întoarce „fără probleme" pentru un câmp care n-a fost verificat înseamnă a
    confunda „nevalidat" cu „valid". Cine consumă raportul trebuie să vadă ce NU s-a verificat, ca
    să poată decide (gate-ul NX-206 blochează, telemetria doar numără)."""

    problems: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Curat = zero probleme ȘI zero câmpuri neverificate. Un câmp fără vocabular declarat NU e
        o promisiune de corectitudine."""
        return not self.problems and not self.unchecked


_VOCAB_LIST_FIELDS = ("concerns", "suitable_for", "key_ingredients")
_VOCAB_SCALAR_FIELDS = ("finish", "coverage", "texture", "routine_step")


def validate_vocabulary(
    facts: ProductFacts, allowed: Mapping[str, Iterable[str]]
) -> VocabularyReport:
    """Valorile de fapte contra vocabularului CONTROLAT al verticalului (DomainPack, P9).

    Separat de validarea structurală din model: vocabularul e config per-vertical, deci nu are ce
    căuta într-un model Pydantic din cod. Câmpurile PREZENTE fără vocabular declarat se întorc în
    `unchecked` — nu ca „ok"."""
    problems: list[str] = []
    unchecked: list[str] = []
    for field in _VOCAB_LIST_FIELDS + _VOCAB_SCALAR_FIELDS:
        value = getattr(facts, field)
        if value is None:
            continue  # necunoscut: nu e nici valid, nici invalid — nu se verifică
        vocab = {normalize(str(v)) for v in (allowed.get(field) or ())}
        if not vocab:
            unchecked.append(field)
            continue
        values = value if isinstance(value, tuple) else (value,)
        unknown = sorted({v for v in values if normalize(v) not in vocab})
        if unknown:
            problems.append(f"{field}: valori în afara vocabularului {unknown}")
    return VocabularyReport(problems=tuple(problems), unchecked=tuple(unchecked))


@dataclass(frozen=True, slots=True)
class CategoryRequirements:
    """Câmpurile OBLIGATORII per categorie — din DomainPack (P9), nu hardcodate în audit.

    Două niveluri, ca în contractul 168d: `by_slug` (categoria frunză exactă) și `by_root`
    (rădăcina arborelui, ex. `ingrijirea-tenului`).

    **Slug-ul BATE rădăcina (override), nu se cumulează** — semantica lui NX-168d (R10), păstrată
    intenționat: `mascara` cere `key_benefit`, dar NU `finish`-ul rădăcinii `machiaj`, pentru că
    produsele de ochi n-au finish de complexion."""

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
        """Câmpurile obligatorii ABSENTE sau GOALE — ambele încalcă cerința (un `coverage: ""` nu
        spune mai mult decât lipsa lui). Distincția o dă `missing_detail()`."""
        return tuple(k for k, _ in self.missing_detail(attributes, slug, root))

    def missing_detail(
        self, attributes: Mapping[str, Any], slug: str | None, root: str | None = None
    ) -> tuple[tuple[str, str], ...]:
        """Ca `missing()`, dar cu motivul: `absent` (cheia nu există) vs `empty` (există, e goală).
        Ordine sortată → mesaje deterministe."""
        out: list[tuple[str, str]] = []
        for key in sorted(self.required_for(slug, root)):
            if key not in attributes:
                out.append((key, "absent"))
                continue
            value = attributes[key]
            if value is None:
                out.append((key, "empty"))
            elif isinstance(value, str) and not value.strip():
                out.append((key, "empty"))
            elif not isinstance(value, str) and hasattr(value, "__len__") and len(value) == 0:
                out.append((key, "empty"))
        return tuple(out)


EMPTY_REQUIREMENTS = CategoryRequirements(by_slug={}, by_root={})


def build_category_requirements(raw: Any) -> CategoryRequirements:
    """Config → `CategoryRequirements`, **fail-closed per intrare** (tipar `build_facets`): o
    intrare invalidă e ignorată, nu dărâmă pack-ul și nu devine cerință pe jumătate. Config
    lipsă/gunoi → cerințe goale (comportamentul de azi pentru verticalele fără contract)."""
    if not isinstance(raw, dict):
        return EMPTY_REQUIREMENTS

    def _level(node: Any) -> dict[str, frozenset[str]]:
        out: dict[str, frozenset[str]] = {}
        if not isinstance(node, dict):
            return out
        for key, fields in node.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(fields, list):
                continue
            clean = frozenset(f.strip() for f in fields if isinstance(f, str) and f.strip())
            if clean:
                out[key] = clean
        return out

    return CategoryRequirements(
        by_slug=_level(raw.get("by_slug")), by_root=_level(raw.get("by_root"))
    )
