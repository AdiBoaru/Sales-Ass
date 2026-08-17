"""NX-246 (felia 3) — corpusul de journey-uri web: schemă versionată, familii, acoperire, sigiliu.

Un golden test verifică un RĂSPUNS. Un journey verifică o CONVERSAȚIE — și diferența e chiar
lucrul pe care produsul îl vinde: „și ceva mai ieftin?" nu are sens fără turul dinainte, „nu, fără
parfum" trebuie să ȘTEARGĂ un criteriu vechi, iar „compară-le pe primele două" se referă la o listă
pe care userul o vede acum. Harnessul NX-210 (`nx210_blind`, `nx210_h3`) rămâne sursa pentru
grounding, hard constraints și pairwise-ul orb; aici se adaugă exact ce el nu are: mai multe ture,
context de pagină/coș, corecții, referințe ordinale și criterii de succes per tur.

**Familiile sunt un vocabular ÎNCHIS**, nu etichete libere. Motivul e acoperirea: un gate care
raportează „92% pass" fără să spună pe ce familii a măsurat ascunde exact cazul pe care nu l-a
testat. Cu familii închise, `coverage()` poate afirma „no-results n-a fost testat niciodată" —
ceea ce e o informație, nu o absență.

**Holdoutul nu intră în repo.** În repo intră doar `holdout_manifest.json`: `suite_id`, numărul de
journey-uri, distribuția pe familii și SHA-256 al conținutului. Runner-ul verifică hashul ÎNAINTE
de execuție și refuză să pornească dacă nu se potrivește — altfel „holdout sigilat" e o promisiune,
nu o proprietate. Conținutul stă într-un store restricționat (vezi `docs/WEB-QUALITY-EVAL.md`).

Modulul e PUR: fără DB, fără LLM, fără rețea, fără ceas.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.safety.external_data import contains_pii

#: Versiunea SCHEMEI de corpus. Un journey scris sub altă versiune nu se amestecă în aceeași suită:
#: dacă înțelesul unui câmp se schimbă, două rulări n-ar mai fi comparabile — exact la momentul în
#: care cineva compară champion cu candidate.
JOURNEY_SCHEMA_VERSION = "web-journey.v1"

#: Cele 10 familii cerute de card. ÎNCHISE: `coverage()` poate spune „familia X lipsește" doar
#: dacă mulțimea e cunoscută dinainte.
Family = Literal[
    "typo_diacritics",  # 1. typo/diacritice și formulări colocviale
    "elliptical_followup",  # 2. „și ceva mai ieftin?"
    "correction",  # 3. corecții care ÎNLOCUIESC criterii vechi
    "useful_clarification",  # 4. clarificare când chiar lipsește informația
    "ordinal_reference",  # 5. „primul", „acesta", „compară-le"
    "page_context",  # 6. context de pagină + schimbare de pagină între ture
    "hard_constraint",  # 7. constrângeri dure, UNKNOWN ≠ MISMATCH, safety
    "no_results",  # 8. no-results cu relaxare onestă, fără produse inventate
    "cart_mutation",  # 9. coș: snapshot, mutație cu receipt, stale/conflict
    "mixed_conversation",  # 10. greeting → recomandare → comparație → acțiune
]
FAMILIES: frozenset[str] = frozenset(Family.__args__)  # type: ignore[attr-defined]

# ── Minimele de acoperire (cardul, literal) ─────────────────────────────────────────────────
MIN_DEV_JOURNEYS = 60
MIN_HOLDOUT_JOURNEYS = 40
#: Fiecare familie de minimum 4 ori ÎN HOLDOUT — altfel „acoperă toate familiile" înseamnă „are
#: câte un exemplu", iar un exemplu nu distinge o regresie de o coincidență.
MIN_FAMILY_IN_HOLDOUT = 4
#: Minimum 30% din holdout adversarial. Un set format doar din cazuri prietenoase măsoară cât de
#: bine merge produsul când nimeni nu-l încearcă.
MIN_ADVERSARIAL_RATIO = 0.30

MIN_TURNS = 2
MAX_TURNS = 6


class JourneyTurn(BaseModel):
    """Un tur: ce spune userul, ce vede, și ce trebuie să fie adevărat în răspuns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_input: str = Field(min_length=1, max_length=500)
    #: Suprafața + ID-uri opace (NX-234). Zero fapte comerciale — aceeași regulă ca la runtime.
    page_context: dict[str, Any] = Field(default_factory=dict)
    #: Ce TREBUIE să conțină răspunsul, ca fapte verificabile determinist (product refs, câmpuri).
    must_ground: tuple[str, ...] = ()
    #: Constrângeri dure care nu au voie să fie încălcate (buget, categorie, safety).
    hard_constraints: tuple[str, ...] = ()
    #: Acțiunile pe care turul are voie să le emită. O acțiune în afara listei e eșec de scope.
    allowed_actions: tuple[str, ...] = ()
    #: Criteriul de succes, în cuvintele evaluatorului (nu se execută; ancorează rubrica umană).
    success_criteria: str = Field(default="", max_length=400)
    #: Ce NU are voie să apară (produse inventate, promisiuni de livrare, superlative fără sursă).
    forbidden: tuple[str, ...] = ()

    @field_validator("user_input", "success_criteria")
    @classmethod
    def _no_pii(cls, value: str) -> str:
        # Corpusul e citit de oameni și ajunge în artefacte; un telefon real strecurat într-un
        # „journey realist" ar deveni PII versionat în git, adică imposibil de retras.
        if contains_pii(value):
            raise ValueError("journey text contains PII")
        return value

    @field_validator("page_context")
    @classmethod
    def _context_is_id_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Aceeași regulă ca `web.context.reject_commercial_fields`: contextul poartă ID-uri, nu
        # fapte. Un `price` în corpus ar însemna că testăm cu date pe care serverul nu le acceptă.
        commercial = {"price", "sale_price", "stock", "availability", "rating", "currency"}
        leaked = commercial & set(value)
        if leaked:
            raise ValueError(f"page_context poartă fapte comerciale: {sorted(leaked)}")
        return value


class Journey(BaseModel):
    """Un journey complet: 2-6 ture, familie, snapshot de catalog, locale, stare inițială."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    journey_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    schema_version: Literal["web-journey.v1"] = JOURNEY_SCHEMA_VERSION
    family: Family
    locale: str = Field(default="ro", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    #: Snapshotul de catalog pe care s-a scris journey-ul. FIX: un corpus care rulează pe catalogul
    #: „de acum" măsoară catalogul, nu modelul — iar ieri și azi ar da verdicte diferite.
    catalog_snapshot: str = Field(min_length=1, max_length=64)
    #: Adversarial = scris ca să spargă ceva (typo agresiv, contradicție, constrângere imposibilă).
    adversarial: bool = False
    initial_state: dict[str, Any] = Field(default_factory=dict)
    cart_refs: tuple[str, ...] = ()
    turns: tuple[JourneyTurn, ...]

    @field_validator("turns")
    @classmethod
    def _turn_count(cls, value: tuple[JourneyTurn, ...]) -> tuple[JourneyTurn, ...]:
        if not MIN_TURNS <= len(value) <= MAX_TURNS:
            raise ValueError(f"un journey are {MIN_TURNS}-{MAX_TURNS} ture (are {len(value)})")
        return value

    @model_validator(mode="after")
    def _family_shape(self) -> Journey:
        """Coerența dintre familie și conținut: o etichetă care nu descrie journey-ul face
        acoperirea să mintă (raportezi 4 cazuri de `no_results` care de fapt sunt altceva)."""
        if self.family == "page_context" and not any(t.page_context for t in self.turns):
            raise ValueError("familia page_context cere cel puțin un tur cu page_context")
        if self.family == "cart_mutation" and not self.cart_refs:
            raise ValueError("familia cart_mutation cere cart_refs")
        if self.family == "hard_constraint" and not any(t.hard_constraints for t in self.turns):
            raise ValueError("familia hard_constraint cere constrângeri dure explicite")
        return self

    @property
    def fingerprint(self) -> str:
        """Amprenta pentru SIGILIU — include `journey_id`.

        Include id-ul deliberat: dacă cineva redenumește un journey din holdout, holdoutul S-A
        schimbat (raportarea per journey nu mai e comparabilă), deci hashul trebuie să difere.
        """
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def content_fingerprint(self) -> str:
        """Amprenta pentru DUPLICATE — EXCLUDE `journey_id`.

        Scop diferit, deci amprentă diferită: copiat-lipit cu alt id e același caz de test, iar
        el umflă numărul fără să adauge semnal — exact ce ar face cineva grăbit să atingă 60 de
        journey-uri. Cu id-ul inclus, verificarea n-ar fi putut prinde niciodată asta.
        """
        payload = self.model_dump(mode="json")
        payload.pop("journey_id", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Coverage:
    """Ce acoperă o suită, față de minimele cardului. `gaps` e lista de motive, nu un bool."""

    total: int
    by_family: dict[str, int]
    adversarial: int
    missing_families: tuple[str, ...]
    thin_families: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    @property
    def adversarial_ratio(self) -> float:
        return self.adversarial / self.total if self.total else 0.0

    @property
    def complete(self) -> bool:
        return not self.gaps

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_family": dict(sorted(self.by_family.items())),
            "missing_families": list(self.missing_families),
            "thin_families": list(self.thin_families),
            "adversarial": self.adversarial,
            "adversarial_ratio": round(self.adversarial_ratio, 4),
            "complete": self.complete,
            "gaps": list(self.gaps),
        }


def coverage(journeys: list[Journey], *, holdout: bool) -> Coverage:
    """Acoperirea unei suite. `holdout=True` aplică ȘI pragurile per familie + adversarial.

    Suita de development are un singur prag (numărul total): rolul ei e să prindă regresii în timp
    ce dezvolți, deci contează să existe și să crească. Holdoutul e cel pe care se ia DECIZIA, deci
    el trebuie să fie echilibrat — altfel „candidate a câștigat" poate însemna „a câștigat pe cele
    trei familii care se întâmplă să fie suprareprezentate".
    """
    by_family: dict[str, int] = {}
    adversarial = 0
    for j in journeys:
        by_family[j.family] = by_family.get(j.family, 0) + 1
        adversarial += 1 if j.adversarial else 0
    missing = tuple(sorted(FAMILIES - set(by_family)))
    gaps: list[str] = []
    thin: tuple[str, ...] = ()

    minimum = MIN_HOLDOUT_JOURNEYS if holdout else MIN_DEV_JOURNEYS
    if len(journeys) < minimum:
        gaps.append(f"journeys {len(journeys)}/{minimum}")
    if missing:
        gaps.append(f"familii absente: {', '.join(missing)}")
    if holdout:
        thin = tuple(sorted(f for f in FAMILIES if by_family.get(f, 0) < MIN_FAMILY_IN_HOLDOUT))
        if thin:
            gaps.append(f"familii sub {MIN_FAMILY_IN_HOLDOUT} cazuri: {', '.join(thin)}")
        ratio = adversarial / len(journeys) if journeys else 0.0
        if ratio < MIN_ADVERSARIAL_RATIO:
            gaps.append(f"adversarial {ratio:.0%} < {MIN_ADVERSARIAL_RATIO:.0%}")
    return Coverage(
        total=len(journeys),
        by_family=by_family,
        adversarial=adversarial,
        missing_families=missing,
        thin_families=thin,
        gaps=tuple(gaps),
    )


# ── Încărcare + sigiliu ─────────────────────────────────────────────────────────────────────


class DuplicateJourney(ValueError):
    """Două journey-uri cu același id sau același conținut. Ambele strică acoperirea: primul face
    raportarea ambiguă, al doilea umflă un număr fără să adauge semnal."""


def load_journeys(path: Path) -> list[Journey]:
    """Încarcă un director de journey-uri (`*.json`), validate STRICT. Ordonate după id.

    Ordinea e stabilă deliberat: rularea trebuie să fie reproductibilă, iar `os.listdir` nu e.
    """
    journeys: list[Journey] = []
    seen_ids: set[str] = set()
    seen_prints: dict[str, str] = {}
    for file in sorted(path.glob("*.json")):
        raw = json.loads(file.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            journey = Journey.model_validate(item)
            if journey.journey_id in seen_ids:
                raise DuplicateJourney(f"journey_id duplicat: {journey.journey_id}")
            fp = journey.content_fingerprint
            if fp in seen_prints:
                raise DuplicateJourney(
                    f"{journey.journey_id} e identic cu {seen_prints[fp]} (conținut duplicat)"
                )
            seen_ids.add(journey.journey_id)
            seen_prints[fp] = journey.journey_id
            journeys.append(journey)
    return sorted(journeys, key=lambda j: j.journey_id)


class HoldoutManifest(BaseModel):
    """Ce intră în repo despre holdout: numere și un hash. NICIODATĂ conținut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str = Field(min_length=3, max_length=64)
    schema_version: Literal["web-journey.v1"] = JOURNEY_SCHEMA_VERSION
    journey_count: int = Field(ge=0)
    adversarial_count: int = Field(ge=0)
    by_family: dict[str, int] = Field(default_factory=dict)
    catalog_snapshot: str = Field(default="", max_length=64)
    #: SHA-256 al conținutului sigilat (vezi `seal_holdout`). Gol = holdout INEXISTENT, nu „orice".
    content_sha256: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")
    #: Unde stă conținutul (descriere, nu credențiale). Text liber SCURT, fără URL cu token.
    location: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _counts_agree(self) -> HoldoutManifest:
        total = sum(self.by_family.values())
        if self.by_family and total != self.journey_count:
            raise ValueError(f"by_family însumează {total}, journey_count e {self.journey_count}")
        if self.adversarial_count > self.journey_count:
            raise ValueError("adversarial_count peste journey_count")
        unknown = sorted(set(self.by_family) - FAMILIES)
        if unknown:
            raise ValueError(f"familii necunoscute în manifest: {unknown}")
        return self

    @property
    def sealed(self) -> bool:
        """Un manifest fără hash NU e sigilat. Distincția contează: `journey_count: 40` fără
        `content_sha256` e o afirmație pe care nimeni nu o poate verifica."""
        return bool(self.content_sha256) and self.journey_count > 0


def seal_holdout(journeys: list[Journey]) -> str:
    """SHA-256 peste amprentele ORDONATE ale journey-urilor.

    Peste amprente, nu peste fișiere: un holdout re-serializat cu altă indentare rămâne același
    holdout, dar unul căruia i s-a schimbat un `must_ground` NU — și exact asta trebuie să prindă
    verificarea.
    """
    digest = hashlib.sha256()
    for fp in sorted(j.fingerprint for j in journeys):
        digest.update(fp.encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class SealCheck:
    """Verdictul verificării de sigiliu. `ok=False` ⇒ runner-ul iese NON-ZERO înainte de eval."""

    ok: bool
    reason: str = ""
    expected: str = ""
    actual: str = ""


def verify_holdout(manifest: HoldoutManifest, journeys: list[Journey] | None) -> SealCheck:
    """Conținutul de holdout se potrivește cu manifestul? Fail-closed pe TOATE ramurile.

    Failure matrix: „holdout hash diferit/lipsește ⇒ runner exit non-zero ÎNAINTE de eval". Un
    holdout care nu se verifică nu e „un holdout mai slab", e absența unui holdout — și a rula
    peste el ar produce un verdict care arată la fel ca unul valid.
    """
    if not manifest.sealed:
        return SealCheck(False, "manifest nesigilat (fără content_sha256 sau count=0)")
    if journeys is None:
        return SealCheck(False, "conținutul de holdout nu e disponibil", manifest.content_sha256)
    actual = seal_holdout(journeys)
    if actual != manifest.content_sha256:
        return SealCheck(False, "hash de holdout diferit", manifest.content_sha256, actual)
    if len(journeys) != manifest.journey_count:
        return SealCheck(
            False,
            f"numărul de journey-uri diferă ({len(journeys)} vs {manifest.journey_count})",
        )
    return SealCheck(True)


__all__ = [
    "FAMILIES",
    "JOURNEY_SCHEMA_VERSION",
    "MIN_ADVERSARIAL_RATIO",
    "MIN_DEV_JOURNEYS",
    "MIN_FAMILY_IN_HOLDOUT",
    "MIN_HOLDOUT_JOURNEYS",
    "Coverage",
    "DuplicateJourney",
    "HoldoutManifest",
    "Journey",
    "JourneyTurn",
    "SealCheck",
    "coverage",
    "load_journeys",
    "seal_holdout",
    "verify_holdout",
]
