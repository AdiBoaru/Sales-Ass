"""NX-249 — evidence packetul: un artefact imutabil per etapă, agregat și sigur de publicat.

Un raport de canary are două feluri de a fi inutil. Poate fi prea sărac — „candidate arată bine" —
și atunci decizia se ia pe impresii. Sau poate fi prea bogat — cu transcripte, ID-uri de client și
conținut de holdout — și atunci nu poate fi arătat nimănui, deci tot nu se folosește. Pachetul de
aici e construit ca să fie amândouă lucrurile deodată: complet și publicabil.

## Ce intră

Fereastra exactă (UTC), policy-ul și revizia, ambele release SHA, amprentele artefactelor
upstream, alocarea vs traficul observat, cohorturile cu `n`, latențe și intervale, verdictele
porților, incidentele și riscurile deschise, plus verdictul automat.

## Ce NU intră — și de ce e verificat, nu promis

Niciun `business_id`, `conversation_id`, `turn_id`, `contact_id`. Niciun transcript, prompt,
token sau conținut de holdout. Tenantul apare ca BUCKET hash-uit (`tenant_bucket`, aceeași funcție
ca la metrici), fiindcă un raport de release ajunge în tichete, ecrane partajate și arhive de CI.
`tests/test_canary_report.py::test_pachetul_nu_contine_identificatori` scanează recursiv artefactul
și pică pe orice arată a UUID — nu ne bazăm pe disciplina celui care adaugă mâine un câmp.

## Determinism

`fingerprint` e SHA-256 peste forma canonică. `generated_at` NU intră în amprentă: două rulări pe
aceleași date trebuie să producă aceeași amprentă, altfel „a driftat raportul?" devine o întrebare
fără răspuns (aceeași disciplină ca manifestul canonic NX-247).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.release.gates import (
    CohortStats,
    GateReport,
    GateResult,
    gate_artifact_match,
    gate_completeness,
    gate_hard_stops,
    gate_non_inferiority,
    gate_stage_window,
    gate_upstream,
)
from src.release.models import (
    TRACK_CANDIDATE,
    TRACK_CHAMPION,
    TRACK_UNKNOWN,
    ReleasePolicy,
    stage_for,
)
from src.worker.admission import tenant_bucket

PACKET_SCHEMA_VERSION = "release-evidence-packet.v1"

#: Marja predeclarată pentru non-inferioritate, în puncte procentuale. Din card („marja maximă
#: predeclarată 5pp"). Constantă, nu parametru de CLI: un prag care se poate da din linia de
#: comandă e un prag care se va da DUPĂ ce s-au văzut cifrele.
NON_INFERIORITY_MARGIN_PP = 5.0

#: Sub atâtea ture într-un cohort nu se emite verdict statistic (aliniat cu `slo.MIN_SAMPLES`).
MIN_COHORT_SAMPLES = 30


def load_artifact(path: str | Path) -> dict[str, Any] | None:
    """Citește un artefact JSON produs de alt card. `None` = absent/ilizibil.

    Nu ridică: un artefact lipsă e o stare pe care raportul trebuie s-o EXPRIME (`UNKNOWN`), nu un
    traceback care oprește generarea raportului exact când ai mai multă nevoie de el.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def artifact_hash(path: str | Path) -> str:
    """SHA-256 peste bytes NORMALIZAȚI CRLF→LF — aceeași convenție ca manifestul NX-247.

    Fără normalizare, același artefact ar avea alt hash pe Windows și pe CI, iar poarta
    `artifact_match` ar raporta un mismatch care nu există (și, mai rău, ar fi „rezolvată" prin
    dezactivare).
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return "sha256:" + hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def aggregate_cohorts(facts: list[Any]) -> dict[str, CohortStats]:
    """Fapte de ledger → agregate per cohort. Pur; nu atinge DB și nu citește conținut.

    `facts` sunt `CohortTurnFact` (db/queries/release.py) sau orice obiect cu aceleași atribute —
    testele folosesc fixture-uri simple, ca agregarea să poată fi verificată fără Postgres.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for f in facts:
        b = buckets.setdefault(
            f.track,
            {
                "turns": 0,
                "terminal": 0,
                "completed": 0,
                "failed": 0,
                "renderable_terminal": 0,
                "first_attempt_ok": 0,
                "durations": [],
                "action_turns": 0,
                "action_failed": 0,
            },
        )
        b["turns"] += 1
        terminal = f.status in ("completed", "failed", "cancelled")
        if terminal:
            b["terminal"] += 1
            if f.renderable:
                b["renderable_terminal"] += 1
        if f.status == "completed":
            b["completed"] += 1
            if f.attempt <= 1:
                b["first_attempt_ok"] += 1
        if f.status == "failed":
            b["failed"] += 1
        if getattr(f, "has_action", False):
            b["action_turns"] += 1
            if f.status == "failed":
                b["action_failed"] += 1
        if f.completed_at is not None:
            ms = (f.completed_at - f.accepted_at).total_seconds() * 1000.0
            # Ceas sărit între accept și commit: sample-ul se ARUNCĂ (ca la NX-246), altfel un
            # p50 negativ ar face candidate să pară instantaneu.
            if ms >= 0:
                b["durations"].append(ms)
    return {
        track: CohortStats(
            track=track,
            turns=b["turns"],
            terminal=b["terminal"],
            completed=b["completed"],
            renderable_terminal=b["renderable_terminal"],
            failed=b["failed"],
            first_attempt_ok=b["first_attempt_ok"],
            durations_ms=tuple(b["durations"]),
            action_turns=b["action_turns"],
            action_failed=b["action_failed"],
        )
        for track, b in buckets.items()
    }


@dataclass
class EvidencePacket:
    """Pachetul complet. Serializat întreg: cine îl citește poate reface fiecare verdict."""

    policy_id: str
    policy_revision: int
    policy_fingerprint: str
    environment: str
    stage: str
    window_from: str
    window_to: str
    tenant_bucket: str
    control_release_sha: str
    candidate_release_sha: str
    control_pipeline_version: str
    candidate_pipeline_version: str
    gates: GateReport = field(default_factory=lambda: GateReport(stage="unknown"))
    cohorts: dict[str, CohortStats] = field(default_factory=dict)
    allocation: dict[str, Any] = field(default_factory=dict)
    upstream: dict[str, Any] = field(default_factory=dict)
    completeness: dict[str, Any] = field(default_factory=dict)
    incidents: list[str] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    open_risks: list[str] = field(default_factory=list)
    generated_at: str = ""

    @property
    def verdict(self) -> str:
        return self.gates.verdict

    def body(self) -> dict[str, Any]:
        """Conținutul care INTRĂ în amprentă (fără `generated_at`, fără amprenta însăși)."""
        return {
            "schema_version": PACKET_SCHEMA_VERSION,
            "policy": {
                "policy_id": self.policy_id,
                "revision": self.policy_revision,
                "fingerprint": self.policy_fingerprint,
                "environment": self.environment,
            },
            "stage": self.stage,
            "window": {"from": self.window_from, "to": self.window_to, "utc": True},
            "tenant_bucket": self.tenant_bucket,
            "releases": {
                "control": {
                    "release_sha": self.control_release_sha,
                    "pipeline_version": self.control_pipeline_version,
                },
                "candidate": {
                    "release_sha": self.candidate_release_sha,
                    "pipeline_version": self.candidate_pipeline_version,
                },
            },
            "verdict": self.verdict,
            "gates": self.gates.as_dict(),
            "cohorts": {t: c.as_dict() for t, c in sorted(self.cohorts.items())},
            "allocation": self.allocation,
            "upstream": self.upstream,
            "completeness": self.completeness,
            "incidents": sorted(self.incidents),
            "overrides": self.overrides,
            "open_risks": sorted(self.open_risks),
            "human_decision": {
                "decision": None,
                "actor": None,
                "reason": None,
                "note": "PASS nu promovează singur — decizia se înregistrează la `apply`.",
            },
        }

    def canonical(self) -> str:
        return json.dumps(self.body(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def fingerprint(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["fingerprint"] = self.fingerprint
        # În AFARA amprentei, deliberat: două rulări pe aceleași date trebuie să dea aceeași
        # amprentă, altfel driftul nu se poate distinge de trecerea timpului.
        payload["generated_at"] = self.generated_at
        return payload


def build_packet(
    policy: ReleasePolicy,
    *,
    business_id: str,
    window_from: datetime,
    window_to: datetime,
    facts: list[Any],
    truncated: bool = False,
    hard_stops: list[str] | None = None,
    slo_artifact: dict[str, Any] | None = None,
    quality_artifact: dict[str, Any] | None = None,
    e2e_artifact: dict[str, Any] | None = None,
    deploy_evidence: dict[str, Any] | None = None,
    feedback_artifact: dict[str, Any] | None = None,
    artifact_hashes: dict[str, str] | None = None,
    incidents: list[str] | None = None,
    overrides: list[dict[str, Any]] | None = None,
    generated_at: str = "",
) -> EvidencePacket:
    """Fapte + artefacte upstream → pachet cu verdict. PUR: fără DB, fără rețea, fără ceas.

    Ordinea porților e ordinea autorității, ca peste tot în card: hard stops ÎNAINTEA oricărui
    scor, apoi artefactele (ce comparăm), apoi completitudinea (avem cu ce), apoi etapa (destul
    timp și eșantion), abia la urmă statisticile.
    """
    cohorts = aggregate_cohorts(facts)
    candidate = cohorts.get(TRACK_CANDIDATE, CohortStats(TRACK_CANDIDATE))
    control = cohorts.get(TRACK_CHAMPION, CohortStats(TRACK_CHAMPION))
    unknown = cohorts.get(TRACK_UNKNOWN, CohortStats(TRACK_UNKNOWN))
    stage = stage_for(policy)
    hashes = artifact_hashes or {}

    elapsed_hours = max(0.0, (window_to - window_from).total_seconds() / 3600.0)

    gates = GateReport(stage=stage.label)
    gates.gates.append(gate_hard_stops(hard_stops or []))
    gates.gates.append(
        gate_artifact_match(
            policy,
            quality_packet_hash=hashes.get("quality_packet_hash", ""),
            e2e_packet_hash=hashes.get("e2e_packet_hash", ""),
            deploy_manifest_hash=hashes.get("deploy_manifest_hash", ""),
        )
    )
    gates.gates.append(
        gate_upstream(
            "e2e_stage1",
            (e2e_artifact or {}).get("verdict"),
            source="NX-247",
        )
    )
    gates.gates.append(
        gate_upstream(
            "quality_holdout",
            (quality_artifact or {}).get("verdict"),
            source="NX-246 felia 3",
        )
    )
    gates.gates.append(
        gate_upstream("slo", (slo_artifact or {}).get("verdict"), source="NX-246 slo_policy.v1")
    )
    gates.gates.append(
        gate_upstream("deploy_evidence", (deploy_evidence or {}).get("verdict"), source="NX-248")
    )
    gates.gates.append(
        gate_completeness(
            cohort_stats={t: c for t, c in cohorts.items() if t != TRACK_UNKNOWN},
            truncated=truncated,
            unknown_turns=unknown.turns,
        )
    )
    gates.gates.append(
        gate_stage_window(stage, elapsed_hours=elapsed_hours, candidate_turns=candidate.turns)
    )

    # Eșecuri terminale: candidate nu are voie să eșueze mai des decât control cu >5pp.
    gates.gates.append(
        gate_non_inferiority(
            name="terminal_failure_rate",
            candidate_bad=candidate.failed,
            candidate_n=candidate.terminal,
            control_bad=control.failed,
            control_n=control.terminal,
            margin_pp=NON_INFERIORITY_MARGIN_PP,
            min_n=MIN_COHORT_SAMPLES,
        )
    )
    # P6 pe cohorturi: un terminal fără conținut randabil e hard stop, nu o rată. Aici verificăm
    # doar dacă s-a întâmplat — pragul e zero, ca la `slo._non_empty`.
    empty_candidate = candidate.terminal - candidate.renderable_terminal
    empty_control = control.terminal - control.renderable_terminal
    if empty_candidate or empty_control:
        gates.gates.append(
            GateResult(
                "non_empty_terminal",
                "FAIL",
                f"terminale goale: candidate={empty_candidate}, control={empty_control}",
                {"candidate": empty_candidate, "control": empty_control},
            )
        )
    else:
        gates.gates.append(
            GateResult("non_empty_terminal", "PASS", "toate terminalele au conținut randabil")
        )
    # Cohortul de ACȚIUNI, separat: cardul cere explicit ca „candidate mai lent/mai fragil doar pe
    # cart/retry" să nu se piardă într-o medie globală.
    gates.gates.append(
        gate_non_inferiority(
            name="action_cohort_failure_rate",
            candidate_bad=candidate.action_failed,
            candidate_n=candidate.action_turns,
            control_bad=control.action_failed,
            control_n=control.action_turns,
            margin_pp=NON_INFERIORITY_MARGIN_PP,
            min_n=MIN_COHORT_SAMPLES,
        )
    )
    # Feedback negativ: același test de non-inferioritate, pe artefactul NX-246 felia 2.
    fb = _feedback_counts(feedback_artifact)
    gates.gates.append(
        gate_non_inferiority(
            name="negative_feedback_rate",
            candidate_bad=fb["candidate"]["negative"],
            candidate_n=fb["candidate"]["n"],
            control_bad=fb["champion"]["negative"],
            control_n=fb["champion"]["n"],
            margin_pp=NON_INFERIORITY_MARGIN_PP,
            min_n=MIN_COHORT_SAMPLES,
        )
    )

    total_turns = sum(c.turns for c in cohorts.values())
    allocation = {
        "policy_percent": policy.percent,
        "policy_mode": policy.mode,
        # Alocarea CERUTĂ vs traficul OBSERVAT. Cardul insistă pe distincție: procentul e despre
        # conversații noi, iar o conversație lungă produce multe ture — deci 5% conversații poate
        # însemna 12% ture, iar asta nu e un bug.
        "observed_turn_share": (
            None if total_turns == 0 else round(candidate.turns / total_turns, 4)
        ),
        "candidate_turns": candidate.turns,
        "control_turns": control.turns,
        "uncaptured_turns": unknown.turns,
        "total_turns": total_turns,
    }

    return EvidencePacket(
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        policy_fingerprint=policy.fingerprint,
        environment=policy.environment,
        stage=stage.label,
        window_from=window_from.isoformat(),
        window_to=window_to.isoformat(),
        tenant_bucket=tenant_bucket(business_id),
        control_release_sha=policy.control_release_sha,
        candidate_release_sha=policy.candidate_release_sha,
        control_pipeline_version=policy.control_pipeline_version,
        candidate_pipeline_version=policy.candidate_pipeline_version,
        gates=gates,
        cohorts=cohorts,
        allocation=allocation,
        upstream={
            "slo": _upstream_summary(slo_artifact),
            "quality": _upstream_summary(quality_artifact),
            "e2e": _upstream_summary(e2e_artifact),
            "deploy": _upstream_summary(deploy_evidence),
            "feedback": fb,
            "hashes": dict(sorted(hashes.items())),
        },
        completeness={
            "truncated": truncated,
            "elapsed_hours": round(elapsed_hours, 2),
            "cohorts_present": sorted(t for t in cohorts if t != TRACK_UNKNOWN),
            "missing": _missing(cohorts, slo_artifact, quality_artifact, e2e_artifact),
        },
        incidents=list(incidents or []),
        overrides=list(overrides or []),
        open_risks=_open_risks(gates),
        generated_at=generated_at,
    )


def _feedback_counts(artifact: dict[str, Any] | None) -> dict[str, Any]:
    """Voturile pe cohort, din artefactul NX-246 felia 2. Absent ⇒ zerouri, nu zero-uri optimiste.

    Un artefact lipsă produce `n=0`, ceea ce face poarta `INSUFFICIENT` — corect: nu știm. Nu se
    completează cu „presupunem la fel ca control", fiindcă exact acolo s-ar ascunde regresia.
    """
    out = {
        "champion": {"n": 0, "negative": 0},
        "candidate": {"n": 0, "negative": 0},
        "source": "absent" if not artifact else "nx246_feedback_report",
    }
    by_track = (artifact or {}).get("by_release_track")
    if not isinstance(by_track, dict):
        return out
    for track in ("champion", "candidate"):
        bucket = by_track.get(track)
        if not isinstance(bucket, dict):
            continue
        n = int(bucket.get("n") or 0)
        positive = int(bucket.get("positive") or 0)
        out[track] = {"n": n, "negative": max(0, n - positive)}
    return out


def _upstream_summary(artifact: dict[str, Any] | None) -> dict[str, Any]:
    """Doar verdictul + versiunea de policy: artefactele upstream se citează, nu se copiază.

    Copiate întregi, ar aduce în pachet exact ce n-are voie să conțină (id-uri, motive libere,
    eșantioane) și l-ar face nepublicabil.
    """
    if not artifact:
        return {"present": False, "verdict": None}
    return {
        "present": True,
        "verdict": artifact.get("verdict"),
        "policy_version": artifact.get("policy_version") or artifact.get("schema_version"),
    }


def _missing(
    cohorts: dict[str, CohortStats],
    slo_artifact: dict[str, Any] | None,
    quality_artifact: dict[str, Any] | None,
    e2e_artifact: dict[str, Any] | None,
) -> list[str]:
    missing: list[str] = []
    if TRACK_CANDIDATE not in cohorts:
        missing.append("candidate_cohort")
    if TRACK_CHAMPION not in cohorts:
        missing.append("control_cohort")
    if slo_artifact is None:
        missing.append("slo_artifact")
    if quality_artifact is None:
        missing.append("quality_artifact")
    if e2e_artifact is None:
        missing.append("e2e_artifact")
    # Cost/turn nu se poate calcula din ledger: `web_turns` nu are coloană de cost (el trăiește pe
    # `messages`/`usage_daily`). Se declară LIPSĂ, nu se aproximează — un buget verificat pe o
    # aproximare e un buget neverificat.
    missing.append("cost_per_turn_by_track")
    return missing


def _open_risks(gates: GateReport) -> list[str]:
    """Riscurile deschise se DERIVĂ din porțile care n-au trecut — nu se scriu de mână.

    Altfel lista ar rămâne în urma raportului exact în cazurile în care contează, iar un pachet cu
    „open_risks: []" peste trei porți roșii e mai periculos decât unul fără secțiunea asta.
    """
    return [f"{g.name}: {g.note}" for g in gates.gates if g.verdict != "PASS" and g.note]
