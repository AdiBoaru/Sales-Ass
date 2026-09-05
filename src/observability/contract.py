"""NX-246 — VOCABULARUL de observabilitate: ce nume de span, ce atribute, ce metrici, ce etichete.

Fișierul ăsta se scrie ÎNAINTEA oricărui SDK, deliberat (pasul 1 din card). Motivul e că un
sistem de telemetrie nu moare din lipsă de date, ci din exces: o etichetă construită dintr-o
valoare de client (`business_id`, `turn_id`, un query, un URL) înmulțește seriile temporale până
când backendul devine mai scump decât produsul pe care îl măsoară — și, în drum, scrie pe disc
exact datele pe care NX-230 le-a scos din pipeline. De aceea aici nu există „adaugă ce vrei":

  • numele de span sunt un set ÎNCHIS (`SPAN_NAMES`) — un nume construit dinamic e refuzat;
  • atributele de span sunt un set ÎNCHIS (`SPAN_ATTRIBUTES`) — restul se aruncă, numărat;
  • metricile sunt DECLARATE (`METRICS`): tip, unitate, etichete, bucket-uri. Ce nu e declarat
    nu se poate emite; ce e declarat nu poate primi o etichetă nedeclarată.

Pentru valorile de etichetă avem două regimuri, fiindcă realitatea are două cazuri:

  • **set închis** (`values`) — `outcome`, `status`: le știm pe toate, orice altceva e bug;
  • **buget de valori distincte** (`max_distinct`) — `model_id`, `tool_name`: mulțimea e mărginită
    de configurație/registru, dar nu e o constantă de cod. Le lăsăm să treacă până la un plafon,
    după care devin `other` și se numără în `web_observability_dropped_total`. Un registru care
    crește sănătos rămâne vizibil; unul otrăvit de date de client se izolează singur.

Ordinea de precedență e mereu aceeași: **allowlist, nu denylist**. O listă de interziceri e o
promisiune că ne-am gândit la tot ce poate veni; o listă de permisiuni e o garanție că nu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ── Spans (taxonomia din card, set ÎNCHIS) ──────────────────────────────────────────────────
#: Un nume de span e o etichetă în backend-ul de traces: dinamic = cardinalitate infinită.
SPAN_NAMES: frozenset[str] = frozenset(
    {
        "web.turn.accept",
        "web.turn.queue_wait",
        "web.turn.execute",
        "web.context.load",
        "web.fast_path",
        "web.agent.call",
        "web.tool.call",
        "web.retrieval.lexical",
        "web.retrieval.embedding",
        "web.retrieval.fusion",
        "web.evidence.hydrate",
        "web.answer_plan",
        "web.validate",
        "web.view_project",
        "web.result.commit",
        "web.aftercare.schedule",
    }
)

#: Atributele permise pe un span. `turn_id` e AICI și NU printre etichetele de metrică: un trace
#: e per-request (drill-down autorizat), o metrică e agregat (cardinalitate). Distincția e chiar
#: cerința cardului — „`turn_id` ca atribut de trace, nu ca label de metrică".
SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "service",
        "env",
        "release_sha",
        "release_track",
        "pipeline_version",
        "schema_major",
        "stage",
        "outcome",
        "safe_error_code",
        "attempt_bucket",
        "model_role",
        "model_id",
        # NX-275 felia 1: tokenii de prompt serviți din cache pe APELUL ăsta. Numeric, deci fără
        # risc de cardinalitate pe metrici (e atribut de span, nu etichetă) și fără conținut de
        # client. Separă apelul care SCRIE cache-ul de cel care îl citește.
        "tokens_cached",
        "tool_name",
        "cache_result",
        "turn_id",
        "conversation_ref",  # hash de corelare, NU `conversation_id` brut (vezi sanitize.py)
        "exception_type",
        "exception_chain",
        "degraded",
    }
)

#: Status-urile OTel pe care le folosim (`unset` = fără verdict explicit).
STATUS_UNSET = "unset"
STATUS_OK = "ok"
STATUS_ERROR = "error"
SPAN_STATUSES: frozenset[str] = frozenset({STATUS_UNSET, STATUS_OK, STATUS_ERROR})


# ── Vocabulare de valori (închise) ──────────────────────────────────────────────────────────
OUTCOMES: frozenset[str] = frozenset(
    {
        "accepted",
        "replayed",
        "conflict",
        "rejected",
        "completed",
        "failed",
        "cancelled",
        "fenced",
        "deferred",
        "timeout",
        "error",
        "ok",
        "degraded",
        "skipped",
    }
)

#: Statusurile terminale/durabile ale ledgerului (NX-232) — reproduse ca ETICHETĂ, nu importate:
#: un import ar lega vocabularul de metrici de o schimbare de ledger, iar o metrică redenumită în
#: tăcere e mai rea decât una lipsă (dashboardul rămâne verde pe o serie care nu mai există).
LEDGER_STATUSES: frozenset[str] = frozenset(
    {"accepted", "running", "completed", "failed", "cancelled"}
)

ATTEMPT_BUCKETS: frozenset[str] = frozenset({"1", "2", "3+"})
RELEASE_TRACKS: frozenset[str] = frozenset({"champion", "candidate", "canary", "unknown"})
CACHE_RESULTS: frozenset[str] = frozenset({"hit", "miss", "bypass", "skip"})
#: NX-246 felia 2 — ce s-a întâmplat cu un vot. `updated` (corecție) e SEPARAT de `recorded`
#: fiindcă altfel o răzgândire ar arăta în agregate ca încă un vot, iar `positive_feedback_rate`
#: ar număra același om de două ori.
FEEDBACK_OUTCOMES: frozenset[str] = frozenset(
    {"recorded", "updated", "unchanged", "rejected", "error"}
)
MODEL_ROLES: frozenset[str] = frozenset(
    {"triage", "agent", "brain", "vision", "moderation", "embed"}
)
SIGNALS: frozenset[str] = frozenset({"span", "metric", "label", "trace_context", "attribute"})
DROP_REASONS: frozenset[str] = frozenset(
    {
        "queue_full",
        "export_timeout",
        "export_error",
        "not_allowlisted",
        "cardinality_budget",
        "untrusted_inbound",
        "invalid_value",
        "sanitizer_error",
        "disabled",
    }
)

#: NX-248 — vocabularul de HEALTH. Spre deosebire de `LEDGER_STATUSES` (reprodus deliberat),
#: astea se IMPORTĂ de `src/ops/health.py`, nu se copiază. Motivul e diferit fiindcă și riscul e
#: diferit: la ledger, o redenumire tăcută ar strica dashboardul; aici, un cod de motiv adăugat în
#: `health.py` și uitat aici ar fi normalizat tăcut la `other`, adică exact motivul pentru care
#: te uiți la metrică ar deveni invizibil. O singură definiție face abaterea imposibilă.
#: NX-249 — vocabularul controllerului de release. REPRODUS aici, ca `LEDGER_STATUSES`, ca să nu
#: legăm registrul de metrici de un import de business (contract.py n-are nicio dependență din
#: `src/`, și asta e o proprietate, nu un accident). Drift-ul nu e lăsat pe seama atenției:
#: `tests/test_release_gates.py::test_vocabularul_de_release_e_sincron_cu_contractul` compară
#: mulțimile de aici cu cele din `src/release/models.py` și pică dacă diverg.
RELEASE_DECISIONS: frozenset[str] = frozenset({"control", "candidate", "drain"})
RELEASE_MODES: frozenset[str] = frozenset(
    {"observe", "internal", "canary", "force_control", "closed"}
)
RELEASE_ASSIGNMENT_REASONS: frozenset[str] = frozenset(
    {
        "sticky_epoch",
        "bucket_in_rollout",
        "bucket_out_of_rollout",
        "internal_allowlist",
        "v1_closed",
        "mode_observe",
        "force_control",
        "tenant_not_eligible",
        "policy_missing",
        "policy_invalid",
        "policy_expired",
        "store_unavailable",
        "controller_disabled",
        "outside_admission_window",
        "rollback_incompatible",
    }
)
RELEASE_POLICY_OUTCOMES: frozenset[str] = frozenset(
    {"ok", "absent", "store_down", "invalid", "env_mismatch"}
)
RELEASE_POLICY_AGE_BUCKETS: frozenset[str] = frozenset({"fresh", "recent", "stale", "very_stale"})
#: Verdictele porților de release. `INSUFFICIENT` și `UNKNOWN` sunt DISTINCTE de `FAIL`
#: (NX-238/246): „n-am măsurat" și „am măsurat și a picat" cer acțiuni diferite.
RELEASE_GATE_VERDICTS: frozenset[str] = frozenset({"PASS", "FAIL", "INSUFFICIENT", "UNKNOWN"})

HEALTH_ROLES: frozenset[str] = frozenset({"api", "worker", "scheduler"})
HEALTH_STATES: frozenset[str] = frozenset({"ok", "degraded", "failed", "skipped"})
HEALTH_COMPONENTS: frozenset[str] = frozenset(
    {
        "postgres_control",
        "postgres_tenant",
        "schema",
        "ledger",
        "redis",
        "executor",
        "safety_registry",
        "action_keys",
    }
)
HEALTH_REASONS: frozenset[str] = frozenset(
    {
        "ok",
        "timeout",
        "unreachable",
        "auth_failed",
        "schema_too_old",
        "schema_too_new",
        "schema_unknown",
        "privilege_missing",
        "ledger_missing",
        "pool_exhausted",
        "stale_heartbeat",
        "not_configured",
        "disabled",
        "probe_error",
        "keys_missing",
        "registry_invalid",
    }
)

#: Fazele de deadline (NX-241) reproduse ca valori de etichetă — `web_turn_deadline_total{stage}`.
DEADLINE_STAGES: frozenset[str] = frozenset(
    {
        "queue",
        "load",
        "gates",
        "retrieval",
        "model",
        "tools",
        "validation",
        "projection",
        "commit",
        "aftercare",
    }
)

#: Valoarea de înlocuire când o etichetă iese din allowlist sau din bugetul de cardinalitate.
OTHER = "other"
#: Valoarea folosită când un fapt lipsește. `unknown` NU e `0` și nu e `other` (NX-234, D8).
UNKNOWN = "unknown"

#: Forma acceptată pentru valorile „deschise" (model_id, tool_name): fără spații, fără text liber.
_OPEN_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

#: Atributele a căror valoare o construim NOI dintr-un vocabular închis, deci sunt sigure prin
#: construcție, dar nu au forma unui identificator. `exception_chain` e
#: `tip<tip<tip` — fiecare verigă trecută deja prin `sanitize.safe_error_code`.
SELF_GENERATED_ATTRIBUTES: frozenset[str] = frozenset({"exception_chain"})
_CHAIN_VALUE = re.compile(r"^[a-z0-9_]+(<[a-z0-9_]+)*$")

#: Identificatori STRUCTURALI generați de server (uuid4, sha256 trunchiat). Se validează pe formă,
#: dar NU se trec prin detectorul de PII — și ăsta e un caz în care fals-pozitivul e paguba.
#:
#: Concret: `11111111-2222-3333-4444-555555555555` conține un run de 16 cifre care începe cu `4`
#: și trece Luhn, deci `privacy.detectors` îl raportează, corect pentru un text de client, drept
#: CARD. Aplicat pe un `turn_id` asta ar șterge ALEATORIU (la ~o parte din UUID-uri) exact cheia
#: de corelare pentru care există traceul. Valorile astea nu vin niciodată din input de client —
#: le generăm noi — deci scanarea lor nu poate găsi PII, doar coincidențe.
STRUCTURAL_ATTRIBUTES: frozenset[str] = frozenset({"turn_id", "conversation_ref"})
_STRUCTURAL_VALUE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")


@dataclass(frozen=True, slots=True)
class LabelSpec:
    """Contractul UNEI etichete de metrică.

    `values` = set închis (orice altceva devine `other`). `max_distinct` = buget de valori
    distincte pentru mulțimile mărginite-dar-necunoscute-la-compilare (model_id din settings,
    tool_name din registru). Cele două se exclud: dacă ai lista, nu ai nevoie de buget.
    """

    key: str
    values: frozenset[str] | None = None
    max_distinct: int = 0

    def __post_init__(self) -> None:
        if self.values is None and self.max_distinct <= 0:
            raise ValueError(f"eticheta {self.key!r} n-are nici set închis, nici buget de valori")


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Contractul UNEI metrici: nume, tip, unitate, etichete, bucket-uri.

    `unit` e parte din contract fiindcă o histogramă fără unitate e o capcană: cineva va compara
    milisecunde cu secunde într-un panel și va trage concluzii despre p90 dintr-un factor de 1000.
    """

    name: str
    kind: str  # "counter" | "histogram"
    unit: str  # "1" | "s" | "By" | "USD"
    labels: tuple[LabelSpec, ...] = ()
    buckets: tuple[float, ...] = ()
    description: str = ""

    @property
    def label_keys(self) -> frozenset[str]:
        return frozenset(spec.key for spec in self.labels)


# ── Bucket-uri (secunde) — documentate o dată, folosite peste tot ──────────────────────────
#: Latențe de tur: dens sub 3s (unde trăiește experiența), rar peste 10s (unde trăiește incidentul).
LATENCY_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.3, 0.8, 1.5, 3.0, 6.0, 10.0, 15.0, 30.0)
#: Accept-ul e o operație de margine: dacă ajunge la 1s, e deja un incident.
ACCEPT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
#: Așteptarea în coadă: separată de execuție, altfel un backlog arată ca un model lent.
QUEUE_BUCKETS: tuple[float, ...] = (0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
#: Costul unui tur în dolari — cozile lungi contează (un tur de $0.10 e o anomalie de investigat).
COST_BUCKETS: tuple[float, ...] = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1)
#: Tokeni: puteri de 2 aproximative, ca să vedem saltul de contexte fără 50 de bucket-uri.
TOKEN_BUCKETS: tuple[float, ...] = (256, 512, 1024, 2048, 4096, 8192, 16384, 32768)


_OUTCOME = LabelSpec("outcome", OUTCOMES)
_RELEASE_TRACK = LabelSpec("release_track", RELEASE_TRACKS)
_ATTEMPT = LabelSpec("attempt_bucket", ATTEMPT_BUCKETS)
_SAFE_ERROR = LabelSpec("safe_error_code", max_distinct=32)
_MODE = LabelSpec("mode", max_distinct=16)
_STATUS = LabelSpec("status", LEDGER_STATUSES)


#: REGISTRUL. O metrică absentă de aici nu se poate emite (`record_*` ridică în teste, numără în
#: producție). Contractul din card, unu la unu, plus sănătatea exporterului.
METRICS: dict[str, MetricSpec] = {
    m.name: m
    for m in (
        MetricSpec(
            "web_turn_requests_total",
            "counter",
            "1",
            (_OUTCOME, _RELEASE_TRACK),
            description="Requesturi v2 la margine, după verdictul de accept.",
        ),
        MetricSpec(
            "web_turn_accept_duration_seconds",
            "histogram",
            "s",
            (_OUTCOME,),
            ACCEPT_BUCKETS,
            "Durata acceptului durabil (margine), fără execuție.",
        ),
        MetricSpec(
            "web_turn_queue_wait_seconds",
            "histogram",
            "s",
            (_ATTEMPT,),
            QUEUE_BUCKETS,
            "Accept → claim. Separată de execuție: un backlog nu e un model lent.",
        ),
        MetricSpec(
            "web_turn_execution_seconds",
            "histogram",
            "s",
            (LabelSpec("route_mode", max_distinct=16), _OUTCOME),
            LATENCY_BUCKETS,
            "Claim → terminal, doar munca.",
        ),
        MetricSpec(
            "web_turn_end_to_end_seconds",
            "histogram",
            "s",
            (LabelSpec("turn_class", max_distinct=8), _OUTCOME, _RELEASE_TRACK),
            LATENCY_BUCKETS,
            "Accept → terminal durabil. Ăsta e timpul pe care îl simte clientul.",
        ),
        MetricSpec(
            "web_turn_terminal_total",
            "counter",
            "1",
            (_STATUS, _SAFE_ERROR, _RELEASE_TRACK),
            description="Terminale durabile, pe status și cod de eroare stabil.",
        ),
        MetricSpec(
            "web_turn_replay_total",
            "counter",
            "1",
            (LabelSpec("result_status", LEDGER_STATUSES),),
            description="Replay exact pe cheia de idempotency (zero execuție nouă).",
        ),
        MetricSpec(
            "web_turn_idempotency_conflict_total",
            "counter",
            "1",
            description="Același client_turn_id cu alt fingerprint (409).",
        ),
        MetricSpec(
            "web_turn_reclaim_total",
            "counter",
            "1",
            (_ATTEMPT,),
            description="Lease expirat preluat de alt owner (epoch+1).",
        ),
        MetricSpec(
            "web_turn_fenced_completion_total",
            "counter",
            "1",
            (LabelSpec("phase", max_distinct=8),),
            description="Scriere respinsă de fencing — rezultat aruncat, nu livrat.",
        ),
        MetricSpec(
            "web_turn_deadline_total",
            "counter",
            "1",
            (LabelSpec("stage", DEADLINE_STAGES), LabelSpec("reason", max_distinct=8)),
            description="Deadline epuizat, pe faza NX-241 în care s-a întâmplat.",
        ),
        MetricSpec(
            "web_model_calls_total",
            "counter",
            "1",
            (
                LabelSpec("model_role", MODEL_ROLES),
                LabelSpec("model_id", max_distinct=24),
                _OUTCOME,
            ),
            description="Apeluri de model, pe rol și model exact.",
        ),
        MetricSpec(
            "web_model_latency_seconds",
            "histogram",
            "s",
            (LabelSpec("model_role", MODEL_ROLES), LabelSpec("model_id", max_distinct=24)),
            LATENCY_BUCKETS,
        ),
        MetricSpec(
            "web_model_tokens",
            "histogram",
            "1",
            (
                LabelSpec("model_role", MODEL_ROLES),
                # NX-275 felia 1: `cached` e un SUBSET al lui `in` (convenția OpenAI:
                # `prompt_tokens` include `cached_tokens`), nu o a treia cantitate. Nu se adună
                # peste `in` — cine face suma pe direcții numără de două ori.
                LabelSpec("direction", frozenset({"in", "out", "cached"})),
            ),
            TOKEN_BUCKETS,
        ),
        MetricSpec(
            "web_model_cost_usd",
            "histogram",
            "USD",
            (LabelSpec("model_role", MODEL_ROLES),),
            COST_BUCKETS,
        ),
        MetricSpec(
            "web_tool_calls_total",
            "counter",
            "1",
            (LabelSpec("tool_name", max_distinct=32), _OUTCOME),
        ),
        MetricSpec(
            "web_tool_latency_seconds",
            "histogram",
            "s",
            (LabelSpec("tool_name", max_distinct=32),),
            LATENCY_BUCKETS,
        ),
        MetricSpec(
            "web_retrieval_outcomes_total",
            "counter",
            "1",
            (_MODE, _OUTCOME),
        ),
        MetricSpec(
            "web_validation_total",
            "counter",
            "1",
            (LabelSpec("check", max_distinct=16), _OUTCOME),
            description="Validator/critic/fallback — pe tipul verificării, nu pe conținut.",
        ),
        MetricSpec(
            "web_feedback_submissions_total",
            "counter",
            "1",
            (
                LabelSpec("rating", frozenset({"positive", "negative", UNKNOWN})),
                LabelSpec("reason_code", max_distinct=16),
                LabelSpec("outcome", FEEDBACK_OUTCOMES),
                _RELEASE_TRACK,
            ),
            description="Voturi one-tap. `outcome` distinge un vot NOU de o corecție — altfel "
            "o răzgândire ar arăta ca încă un vot în agregate.",
        ),
        # ── NX-248: operare. Nu sunt metrici de tur — sunt metrici despre INSTANȚĂ. Trăiesc în
        # același registru fiindcă poarta de cardinalitate e aceeași, dar au prefix `ops_` ca să
        # nu se amestece în panelurile de produs: „de câte ori am ieșit din rotație" și „câte
        # ture au eșuat" sunt întrebări diferite, iar un dashboard care le adună răspunde greșit
        # la amândouă.
        MetricSpec(
            "ops_readiness_transitions_total",
            "counter",
            "1",
            (LabelSpec("role", HEALTH_ROLES), LabelSpec("state", frozenset({"ok", "unavailable"}))),
            description="TRANZIȚII de readiness (nu stări): o pană de o oră e 1, nu 3600.",
        ),
        MetricSpec(
            "ops_dependency_probe_total",
            "counter",
            "1",
            (
                LabelSpec("component", HEALTH_COMPONENTS),
                LabelSpec("state", HEALTH_STATES),
                LabelSpec("reason_code", HEALTH_REASONS),
            ),
            description="Rezultatul fiecărei sonde, pe componentă și cod de motiv stabil.",
        ),
        MetricSpec(
            "ops_dependency_probe_seconds",
            "histogram",
            "s",
            (LabelSpec("component", HEALTH_COMPONENTS),),
            ACCEPT_BUCKETS,
            "Durata sondelor. O sondă lentă e un semnal cu o tură înaintea unei sonde eșuate.",
        ),
        # ── NX-249: controllerul de release. Ce s-a asignat, cât de proaspăt e policy-ul, cine a
        # apăsat kill-switchul și cum au ieșit porțile. `policy_id` NU e etichetă: un release nou
        # aduce un id nou, deci seria ar crește nemărginit — el rămâne atribut de trace și câmp în
        # evidence packet (cardul permite explicit exact distincția asta).
        MetricSpec(
            "release_assignment_total",
            "counter",
            "1",
            (
                LabelSpec("decision", RELEASE_DECISIONS),
                LabelSpec("reason", RELEASE_ASSIGNMENT_REASONS),
                LabelSpec("mode", RELEASE_MODES),
            ),
            description="Asignări de release. `drain` e o valoare de sine stătătoare: o "
            "conversație drenată nu e nici control, nici candidate.",
        ),
        MetricSpec(
            "release_policy_refresh_total",
            "counter",
            "1",
            (
                LabelSpec("outcome", RELEASE_POLICY_OUTCOMES),
                LabelSpec("age_bucket", RELEASE_POLICY_AGE_BUCKETS),
            ),
            description="Citiri de policy, pe rezultat și vechimea cache-ului. Un `store_down` "
            "care nu se vede aici ar arăta identic cu un canary oprit deliberat.",
        ),
        MetricSpec(
            "release_override_total",
            "counter",
            "1",
            (LabelSpec("mode", RELEASE_MODES), LabelSpec("reason_code", max_distinct=16)),
            description="Aplicări de policy care schimbă modul (inclusiv kill-switch).",
        ),
        MetricSpec(
            "release_gate_total",
            "counter",
            "1",
            (
                LabelSpec("gate", max_distinct=32),
                LabelSpec("verdict", RELEASE_GATE_VERDICTS),
            ),
            description="Verdictul fiecărei porți de promovare, la fiecare evaluare.",
        ),
        MetricSpec(
            "web_observability_dropped_total",
            "counter",
            "1",
            (LabelSpec("signal", SIGNALS), LabelSpec("reason", DROP_REASONS)),
            description="Ce am aruncat și de ce. Fără el, tăcerea arată ca sănătate.",
        ),
        MetricSpec(
            "web_observability_queue_depth",
            "histogram",
            "1",
            buckets=(1, 8, 64, 256, 1024, 4096),
            description="Adâncimea cozii de export la enqueue — presiunea, nu doar drop-urile.",
        ),
        MetricSpec(
            "web_observability_flush_seconds",
            "histogram",
            "s",
            (_OUTCOME,),
            buckets=ACCEPT_BUCKETS,
        ),
    )
}


class ContractViolation(ValueError):
    """Instrumentare greșită: nume/etichetă în afara contractului. Ridicată DOAR în teste și la
    boot (`assert_contract`); în producție violarea se numără și se normalizează — observabilitatea
    nu are voie să pice turul (P6)."""


def normalize_open_value(value: Any) -> str | None:
    """Valoare „deschisă" (model_id, tool_name, safe_error_code) → formă sigură, sau `None`.

    `None` = respinsă. Refuzăm orice nu arată a identificator: spații, diacritice, lungime,
    caractere de control. Un `safe_error_code` construit din `str(exception)` cade exact aici,
    iar asta e intenția — mesajul unei excepții e text liber, adică potențial PII și cardinalitate
    infinită în același obiect.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if _OPEN_VALUE.match(v) else None


def normalize_attribute_value(key: str, value: Any) -> str | int | float | bool | None:
    """Valoarea unui atribut de span → formă sigură, sau `None` (respinsă).

    **A allowlista CHEIA nu e suficient.** `tool_name` e un atribut legitim; `tool_name` cu
    valoarea `"search_products?q=maria@example.ro"` e o scurgere de PII care poartă un nume
    aprobat. Poarta trebuie să fie pe valoare, nu doar pe nume — altfel allowlist-ul devine o
    listă de uși pe care scrie „intrare permisă", fără încuietori.

    (Găsit de `tests/test_observability_privacy.py::test_canary_ul_nu_ajunge_in_niciun_semnal`,
    care caută canary-ul în TOATĂ suprafața sink-ului, nu doar în atributele pe care le știe.)
    """
    if isinstance(value, (bool, int, float)):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if key in SELF_GENERATED_ATTRIBUTES:
        return text[:200] if _CHAIN_VALUE.match(text) else None
    if key in STRUCTURAL_ATTRIBUTES:
        return text if _STRUCTURAL_VALUE.match(text) else None
    return normalize_open_value(text)


def assert_contract() -> None:
    """Poarta de coerență a registrului — rulată la import de `metrics.py` și în teste.

    Verifică lucrurile pe care le poate strica o editare grăbită: un nume duplicat, o histogramă
    fără bucket-uri, un counter CU bucket-uri (semn că cineva a copiat greșit), bucket-uri
    nesortate (backend-ul le-ar accepta și ar produce percentile false).
    """
    for name, spec in METRICS.items():
        if spec.name != name:
            raise ContractViolation(f"metrica {name!r} are alt nume în spec: {spec.name!r}")
        if spec.kind not in ("counter", "histogram"):
            raise ContractViolation(f"{name}: tip necunoscut {spec.kind!r}")
        if spec.kind == "histogram" and not spec.buckets:
            raise ContractViolation(f"{name}: histogramă fără bucket-uri")
        if spec.kind == "counter" and spec.buckets:
            raise ContractViolation(f"{name}: counter cu bucket-uri")
        if list(spec.buckets) != sorted(spec.buckets):
            raise ContractViolation(f"{name}: bucket-uri nesortate")
        if len(spec.label_keys) != len(spec.labels):
            raise ContractViolation(f"{name}: etichetă duplicată")
