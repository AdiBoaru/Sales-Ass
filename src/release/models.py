"""NX-249 — contractele de release: policy imutabil, asignare capturată, decizie umană.

Trei obiecte și un tabel, în ordinea în care circulă:

  1. `ReleasePolicy` — CE se promovează, CUI, cu ce dovezi. Validat integral la construcție:
     un policy imposibil nu există ca obiect, deci nu poate ajunge nici în DB, nici în routing.
  2. `Assignment` — rezultatul deciziei pentru O conversație: track + motiv + bucket.
  3. `CapturedExecution` — ce s-a scris pe rândul de ledger, la accept, înainte de orice claim.
     De aici încolo turul e legat: reclaim-ul citește, nu recalculează.
  4. `STAGES` — etapele de rollout cu minimele lor de timp ȘI eșantion. „Și" înseamnă ambele.

## De ce policy-ul e un obiect frozen cu amprentă, nu un dict de config

Un dict citit din DB și pasat mai departe permite exact patologia pe care cardul o interzice:
cineva schimbă un procent după ce a văzut cifrele. Aici policy-ul are o formă canonică (chei
sortate, separatori ficși) și o amprentă SHA-256 peste ea, iar amprenta intră în fiecare evidence
packet. Un policy editat produce altă amprentă, deci raportul lui nu se mai potrivește cu decizia
care l-a autorizat — mismatch, nu tăcere.

## Ce NU e în policy

Saltul de bucketing. Policy-ul poartă doar `stable_salt_id` (care salt, nu care valoare):
secretul stă în config, deci un policy exfiltrat din DB nu permite nimănui să prezică sau să-și
aleagă bucketul. La fel: niciun `business_id` nu vine din browser sau din output de model —
allowlistul e server-owned și trăiește AICI (P7).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Versiunea contractului. Un policy scris pe alt contract e respins, nu „interpretat generos".
POLICY_SCHEMA_VERSION = "release-policy.v1"

# ── Track-uri ───────────────────────────────────────────────────────────────────────────────
#: Vocabular ÎNCHIS, identic cu CHECK-ul din migrarea 044 și subset al `RELEASE_TRACKS` din
#: `src/observability/contract.py` (unde mai există `canary`/`unknown` ca etichete istorice).
#: Două valori, nu trei: „canary" e un MOD de rollout, nu un pipeline. Un turn a rulat ori
#: champion, ori candidate — a treia opțiune ar fi o cohortă pe care nimeni n-o poate reproduce.
TRACK_CHAMPION = "champion"
TRACK_CANDIDATE = "candidate"
TRACKS: frozenset[str] = frozenset({TRACK_CHAMPION, TRACK_CANDIDATE})

#: Ce se raportează pentru rândurile de dinainte de migrarea 044. NU e `champion`: lipsa datelor
#: nu se promovează tăcut la o valoare plauzibilă (NX-246, decizia 1).
TRACK_UNKNOWN = "unknown"

# ── Moduri ──────────────────────────────────────────────────────────────────────────────────
#: `observe`       — nimic livrat pe candidate; se măsoară champion. Etapa 0.
#: `internal`      — DOAR cohortul intern, 100%. Procentul e ignorat deliberat: „intern" e o
#:                   listă de tenanți, nu o proporție.
#: `canary`        — procentul se aplică pe conversațiile NOI din allowlist.
#: `force_control` — kill-switch: zero accepturi candidate noi. Turele deja acceptate se drenează.
#: `closed`        — v1 e închis public; candidate e default. Se intră doar prin etapa 7.
MODE_OBSERVE = "observe"
MODE_INTERNAL = "internal"
MODE_CANARY = "canary"
MODE_FORCE_CONTROL = "force_control"
MODE_CLOSED = "closed"
MODES: frozenset[str] = frozenset(
    {MODE_OBSERVE, MODE_INTERNAL, MODE_CANARY, MODE_FORCE_CONTROL, MODE_CLOSED}
)

#: Modurile în care candidate poate primi trafic. `force_control`/`observe` nu apar aici, deci
#: nicio ramură de cod nu trebuie să-și amintească să le excludă.
CANDIDATE_MODES: frozenset[str] = frozenset({MODE_INTERNAL, MODE_CANARY, MODE_CLOSED})

# ── Decizii de asignare ─────────────────────────────────────────────────────────────────────
DECISION_CONTROL = "control"
DECISION_CANDIDATE = "candidate"
#: A treia ieșire, cerută explicit de card: o conversație deja candidate, prinsă de un
#: `force_control` fără compatibilitate de rollback dovedită, NU se convertește la control (i-ar
#: schimba starea/referințele sub picioare) și nu se abandonează. Se drenează: turele active
#: termină, iar acceptul următor primește un error-view onest.
DECISION_DRAIN = "drain"

#: Motive FIXE (vocabular închis, sigure ca etichete de metrică — P12: fără UUID-uri).
REASON_STICKY = "sticky_epoch"  # conversația avea deja o asignare capturată
REASON_BUCKET_IN = "bucket_in_rollout"
REASON_BUCKET_OUT = "bucket_out_of_rollout"
REASON_INTERNAL = "internal_allowlist"
REASON_CLOSED = "v1_closed"
REASON_MODE_OBSERVE = "mode_observe"
REASON_FORCE_CONTROL = "force_control"
REASON_TENANT_NOT_ELIGIBLE = "tenant_not_eligible"
REASON_POLICY_MISSING = "policy_missing"
REASON_POLICY_INVALID = "policy_invalid"
REASON_POLICY_EXPIRED = "policy_expired"
REASON_STORE_UNAVAILABLE = "store_unavailable"
REASON_CONTROLLER_OFF = "controller_disabled"
REASON_OUTSIDE_ADMISSION = "outside_admission_window"
REASON_DRAIN_INCOMPATIBLE = "rollback_incompatible"

ASSIGNMENT_REASONS: frozenset[str] = frozenset(
    {
        REASON_STICKY,
        REASON_BUCKET_IN,
        REASON_BUCKET_OUT,
        REASON_INTERNAL,
        REASON_CLOSED,
        REASON_MODE_OBSERVE,
        REASON_FORCE_CONTROL,
        REASON_TENANT_NOT_ELIGIBLE,
        REASON_POLICY_MISSING,
        REASON_POLICY_INVALID,
        REASON_POLICY_EXPIRED,
        REASON_STORE_UNAVAILABLE,
        REASON_CONTROLLER_OFF,
        REASON_OUTSIDE_ADMISSION,
        REASON_DRAIN_INCOMPATIBLE,
    }
)


class PolicyError(ValueError):
    """Policy absent, malformat, expirat sau cu relații imposibile. Fail-closed peste tot."""


def _parse_utc(value: Any, field: str) -> datetime:
    """ISO-8601 → datetime aware UTC. Un timestamp naiv e REFUZAT, nu presupus UTC.

    Motivul e operațional: ferestrele de observare se compară între ele și cu ledgerul. Un naiv
    interpretat local ar decala o etapă cu ore, iar decalajul ar apărea ca „sample insuficient".
    """
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as e:
            raise PolicyError(f"{field}: timestamp ISO-8601 invalid ({value!r})") from e
    if dt.tzinfo is None:
        raise PolicyError(f"{field}: timestamp fără fus orar — se cere UTC explicit")
    return dt.astimezone(UTC)


class ReleasePolicy(BaseModel):
    """Policy-ul de release, imutabil și versionat.

    `frozen=True` + `extra="forbid"`: nu se mută nimic după validare și nu trece niciun câmp
    necunoscut. Un câmp scris greșit („precent") ar fi altfel acceptat tăcut și ar lăsa
    procentul la default — adică un rollout care nu face ce scrie în ticket.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default=POLICY_SCHEMA_VERSION)
    policy_id: str = Field(min_length=3, max_length=64)
    revision: int = Field(ge=0)
    environment: str = Field(min_length=2, max_length=32)
    created_at: str
    not_before: str
    expires_at: str
    #: Fereastra în care conversațiile NOI pot fi admise în epoch. Implicit = validitatea
    #: policy-ului. Există separat fiindcă „policy-ul e valid" și „mai admitem conversații noi"
    #: sunt două întrebări diferite: la drain vrei prima da, a doua nu.
    admission_from: str = ""
    admission_to: str = ""

    control_release_sha: str = Field(min_length=7, max_length=64)
    control_pipeline_version: str = Field(min_length=1, max_length=64)
    candidate_release_sha: str = Field(min_length=7, max_length=64)
    candidate_pipeline_version: str = Field(min_length=1, max_length=64)

    mode: str = Field(default=MODE_OBSERVE)
    #: Procentul de conversații NOI eligibile. Nu e „procent din ture": o conversație lungă
    #: produce multe ture, deci raportul publică și allocation, și observed traffic.
    percent: int = Field(default=0, ge=0, le=100)
    #: Etapa DECLARATĂ (0–7), validată contra tabelului `STAGES`.
    #:
    #: Nu se deduce din (mod, procent) fiindcă nu se POATE: etapa 2 („demo", tenant demo la 100%)
    #: și etapa 6 („default", toți eligibilii la 100%) au exact aceleași cifre și diferă prin
    #: allowlist. O deducere ar fi nimerit-o pe prima și ar fi cerut 24h/100 de ture acolo unde
    #: cardul cere 14 zile/2.000 — adică poarta ar fi fost mai slabă exact la ultima etapă.
    #: Declarată, etapa e și auditabilă: cine aprobă vede în policy ce etapă aprobă.
    stage: int = Field(default=0, ge=0, le=7)

    #: Allowlist SERVER-OWNED. Gol = niciun tenant eligibil (fail-closed, nu „toți").
    eligible_business_ids: tuple[str, ...] = ()
    #: Subsetul intern (etapa 1). Nu trebuie să fie inclus în `eligible_business_ids`: „intern"
    #: e o poartă proprie, ca să nu depindă de procent.
    internal_business_ids: tuple[str, ...] = ()

    #: CARE salt, nu care valoare. Rotirea saltului = `stable_salt_id` nou = epoch nou.
    stable_salt_id: str = Field(min_length=1, max_length=32)

    #: Amprentele dovezilor. Cerute în orice mod care livrează candidate (validat mai jos).
    quality_packet_hash: str = ""
    e2e_packet_hash: str = ""
    deploy_manifest_hash: str = ""
    slo_policy_version: str = ""
    quality_policy_version: str = ""

    #: A fost DOVEDIT că imaginea precedentă citește starea/acțiunile emise de candidate?
    #: Fals ⇒ o conversație candidate prinsă de `force_control` se DRENEAZĂ, nu se convertește.
    rollback_compatible: bool = False

    approved_by: str = ""
    approved_at: str = ""
    change_ticket: str = ""

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        if v not in MODES:
            raise ValueError(f"mod necunoscut: {v!r} (permise: {sorted(MODES)})")
        return v

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, v: str) -> str:
        if v != POLICY_SCHEMA_VERSION:
            raise ValueError(f"contract de policy necunoscut: {v!r}")
        return v

    @field_validator("eligible_business_ids", "internal_business_ids", mode="before")
    @classmethod
    def _as_tuple(cls, v: Any) -> tuple[str, ...]:
        if v is None:
            return ()
        if isinstance(v, str):
            raise ValueError("allowlistul e o listă de ID-uri, nu un string")
        return tuple(str(x) for x in v)

    @model_validator(mode="after")
    def _relations(self) -> ReleasePolicy:
        """Relațiile care nu se pot exprima câmp cu câmp. Fiecare are o consecință concretă."""
        created = _parse_utc(self.created_at, "created_at")
        not_before = _parse_utc(self.not_before, "not_before")
        expires = _parse_utc(self.expires_at, "expires_at")
        if not created <= not_before < expires:
            raise ValueError(
                "timestamps ne-monotone: se cere created_at <= not_before < expires_at"
            )
        if self.admission_from or self.admission_to:
            a_from = _parse_utc(self.admission_from or self.not_before, "admission_from")
            a_to = _parse_utc(self.admission_to or self.expires_at, "admission_to")
            if a_from >= a_to:
                raise ValueError("admission_from trebuie să fie strict înainte de admission_to")
            if a_from < not_before or a_to > expires:
                raise ValueError("fereastra de admisie trebuie să fie în validitatea policy-ului")
        # Două release-uri identice fac raportul o comparație a ceva cu el însuși — și, mai rău,
        # o fac să arate PASS. Un candidate care e champion nu e un canary, e o confuzie.
        if self.candidate_release_sha == self.control_release_sha:
            raise ValueError("candidate_release_sha și control_release_sha trebuie să difere")
        if self.mode in CANDIDATE_MODES:
            missing = [
                name
                for name, value in (
                    ("quality_packet_hash", self.quality_packet_hash),
                    ("e2e_packet_hash", self.e2e_packet_hash),
                    ("deploy_manifest_hash", self.deploy_manifest_hash),
                    ("slo_policy_version", self.slo_policy_version),
                    ("quality_policy_version", self.quality_policy_version),
                    ("approved_by", self.approved_by),
                    ("change_ticket", self.change_ticket),
                )
                if not str(value).strip()
            ]
            if missing:
                raise ValueError(
                    f"modul {self.mode!r} livrează candidate, deci cere dovezi: "
                    f"{', '.join(missing)} lipsesc"
                )
        if self.mode == MODE_CANARY and self.percent <= 0:
            # Un canary la 0% nu e un canary — e un observe care se prezintă greșit în rapoarte.
            raise ValueError("modul 'canary' cere percent > 0 (pentru 0% folosește 'observe')")
        stage = STAGES_BY_INDEX[self.stage]
        if self.mode == MODE_FORCE_CONTROL:
            # Kill-switchul NU e o etapă — e o întrerupere a uneia. `stage` rămâne cea din care s-a
            # oprit (`force_control_from` păstrează tot restul), fiindcă exact asta trebuie să știe
            # cine citește istoricul: nu „eram la etapa force_control", ci „am oprit etapa 4".
            return self
        if stage.mode != self.mode:
            raise ValueError(
                f"etapa {stage.label} e declarată cu modul {stage.mode!r}, "
                f"dar policy-ul are {self.mode!r}"
            )
        if stage.mode == MODE_CANARY and stage.percent != self.percent:
            # Progresia are pași ficși tocmai ca eșantionul minim să însemne ceva. Un 7% „care pare
            # sigur" ar rula sub pragurile etapei de 5%, dar cu alt trafic — necomparabil.
            raise ValueError(f"etapa {stage.label} cere percent={stage.percent}, nu {self.percent}")
        if self.mode == MODE_INTERNAL and not self.internal_business_ids:
            raise ValueError("modul 'internal' cere cel puțin un tenant în internal_business_ids")
        if self.mode == MODE_CANARY and not self.eligible_business_ids:
            raise ValueError("modul 'canary' cere allowlist de tenanți (gol = niciun tenant)")
        return self

    # ── Ferestre (pure, cu ceasul primit ca argument — niciodată `now()` intern) ────────────
    def is_valid_at(self, now: datetime) -> bool:
        return (
            _parse_utc(self.not_before, "not_before")
            <= now
            < _parse_utc(self.expires_at, "expires_at")
        )

    def admits_new_at(self, now: datetime) -> bool:
        """Se mai admit conversații NOI în epoch? Distinct de validitate (vezi `admission_*`)."""
        a_from = _parse_utc(self.admission_from or self.not_before, "admission_from")
        a_to = _parse_utc(self.admission_to or self.expires_at, "admission_to")
        return a_from <= now < a_to

    def is_eligible(self, business_id: str) -> bool:
        """Tenantul e în allowlist? `internal` e o poartă proprie, verificată în `assignment`."""
        return business_id in self.eligible_business_ids

    def is_internal(self, business_id: str) -> bool:
        return business_id in self.internal_business_ids

    # ── Formă canonică + amprentă ──────────────────────────────────────────────────────────
    def to_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["eligible_business_ids"] = sorted(self.eligible_business_ids)
        data["internal_business_ids"] = sorted(self.internal_business_ids)
        return data

    def canonical(self) -> str:
        """Chei sortate, separatori ficși: aceleași date → aceiași bytes pe orice mașină."""
        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @property
    def fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    @classmethod
    def from_payload(cls, payload: Any) -> ReleasePolicy:
        """Dict (din DB sau din fișier) → policy validat. Orice eșec e `PolicyError`."""
        if not isinstance(payload, dict):
            raise PolicyError("policy-ul nu e un obiect JSON")
        data = {k: v for k, v in payload.items() if k != "fingerprint"}
        try:
            return cls(**data)
        except PolicyError:
            raise
        except (TypeError, ValueError) as e:
            raise PolicyError(f"policy invalid: {e}") from e


@dataclass(frozen=True, slots=True)
class Assignment:
    """Decizia pentru O conversație, la UN moment. Pură: aceleași intrări → același rezultat.

    `decision` e ce se întâmplă (`control`/`candidate`/`drain`); `track` e ce pipeline rulează
    (None la drain — nu rulează niciunul). Sunt separate fiindcă „drenat" nu e un pipeline, iar
    a-l codifica drept `champion` ar face un turn refuzat să apară în cohortul champion.
    """

    decision: str
    reason: str
    track: str | None = None
    bucket: int | None = None
    policy_id: str = ""
    policy_revision: int | None = None

    def __post_init__(self) -> None:
        if self.reason not in ASSIGNMENT_REASONS:
            raise ValueError(f"motiv de asignare necunoscut: {self.reason!r}")
        if self.track is not None and self.track not in TRACKS:
            raise ValueError(f"track necunoscut: {self.track!r}")

    @property
    def is_candidate(self) -> bool:
        return self.decision == DECISION_CANDIDATE

    @property
    def is_drain(self) -> bool:
        return self.decision == DECISION_DRAIN

    def as_props(self) -> dict[str, Any]:
        """Proprietăți SAFE pentru evenimente/metrici: vocabular închis, zero identificatori."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "track": self.track or TRACK_UNKNOWN,
            "policy_revision": self.policy_revision,
        }


@dataclass(frozen=True, slots=True)
class CapturedExecution:
    """Ce s-a scris pe rândul de ledger la accept. Citit de executor, NICIODATĂ recalculat.

    Există ca tip separat de `Assignment` fiindcă răspunde la altă întrebare: `Assignment` e
    „ce am decis acum", `CapturedExecution` e „ce s-a decis atunci". Un reclaim după deploy are
    voie să vadă doar a doua.
    """

    track: str
    policy_id: str
    policy_revision: int | None
    pipeline_version: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> CapturedExecution | None:
        """Rând de ledger → captura lui. `None` = rând de dinainte de migrarea 044.

        `None` NU se completează cu champion: raportul trebuie să vadă `unknown` și să-l numere
        în completeness, nu să primească o cohortă inventată.
        """
        track = getattr(row, "release_track", None)
        if not track:
            return None
        return cls(
            track=str(track),
            policy_id=str(getattr(row, "release_policy_id", "") or ""),
            policy_revision=getattr(row, "release_policy_revision", None),
            pipeline_version=getattr(row, "pipeline_version", None),
        )


# ── Etapele de rollout ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RolloutStage:
    """O etapă: procentul ei, minimul de timp ȘI minimul de eșantion.

    „Și" e literal: `min_hours` și `min_candidate_turns` se verifică amândouă. La trafic mic
    verdictul e `INSUFFICIENT` și etapa se prelungește — nu se promovează pe timp scurs.
    """

    index: int
    name: str
    mode: str
    percent: int
    min_hours: float
    min_candidate_turns: int
    extra_gate: str

    @property
    def label(self) -> str:
        return f"{self.index}-{self.name}"


#: Tabelul din card, ca DATE. E aici, nu în docs, fiindcă `gates.py` îl impune — o etapă
#: documentată dar neimpusă e o etapă care se sare sub presiune.
STAGES: tuple[RolloutStage, ...] = (
    RolloutStage(0, "offline", MODE_OBSERVE, 0, 0.0, 0, "NX-246/NX-247 PASS"),
    RolloutStage(1, "internal", MODE_INTERNAL, 100, 0.0, 100, "review uman transcript safe"),
    RolloutStage(2, "demo", MODE_CANARY, 100, 24.0, 100, "zero hard stop, spot-check manual"),
    RolloutStage(3, "pilot", MODE_CANARY, 5, 48.0, 200, "SLO/burn/feedback non-inferior"),
    RolloutStage(4, "expand", MODE_CANARY, 20, 72.0, 500, "cohort/turn-class gates"),
    RolloutStage(5, "majority", MODE_CANARY, 50, 168.0, 1000, "rollback drill recent"),
    RolloutStage(6, "default", MODE_CANARY, 100, 336.0, 2000, "v1 drain + on-call sign-off"),
    RolloutStage(7, "close-v1", MODE_CLOSED, 100, 336.0, 2000, "aprobarea explicită a userului"),
)

STAGES_BY_INDEX: dict[int, RolloutStage] = {s.index: s for s in STAGES}


def stage_for(policy: ReleasePolicy) -> RolloutStage:
    """Etapa policy-ului. Totală (nu `None`): validarea garantează că indexul e din tabel.

    Nu deduce nimic — citește ce a declarat cine a aprobat. Coerența cu modul și procentul e deja
    impusă la construcție, deci aici nu mai există „aproape etapa X".
    """
    return STAGES_BY_INDEX[policy.stage]


# ── Decizia umană ───────────────────────────────────────────────────────────────────────────
DECISION_PROMOTE = "PROMOTE"
DECISION_HOLD = "HOLD"
DECISION_ROLLBACK = "ROLLBACK"
HUMAN_DECISIONS: frozenset[str] = frozenset({DECISION_PROMOTE, DECISION_HOLD, DECISION_ROLLBACK})


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Decizia umană peste un evidence packet. Verdictul automat NU o poate înlocui.

    Cardul e explicit: „`PASS` nu execută singur promotionul". De aceea decizia e un obiect
    separat, cu actor și motiv, iar `scripts/release_control.py apply` cere ambele — un packet
    verde fără `DecisionRecord` nu mută nimic.
    """

    decision: str
    actor: str
    reason: str
    decided_at: str
    packet_fingerprint: str
    expected_revision: int

    def __post_init__(self) -> None:
        if self.decision not in HUMAN_DECISIONS:
            raise ValueError(f"decizie umană necunoscută: {self.decision!r}")
        if not self.actor.strip() or not self.reason.strip():
            raise ValueError("decizia cere actor și motiv (un release fără om nu e o decizie)")

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "actor": self.actor,
            "reason": self.reason,
            "decided_at": self.decided_at,
            "packet_fingerprint": self.packet_fingerprint,
            "expected_revision": self.expected_revision,
        }
