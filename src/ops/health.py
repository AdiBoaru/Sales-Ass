"""NX-248 — health/startup/readiness: un socket deschis nu mai înseamnă „sănătos".

Healthcheckul de dinainte de cardul ăsta era `socket.create_connection(('localhost',8000))`. El
răspunde „healthy" pentru un proces cu DSN greșit, cu schema veche cu două migrări, cu pool-ul
tenant nefuncțional și cu inelul de chei absent — adică exact stările în care Traefik ar trebui
să NU trimită trafic. Aici întrebarea se pune pe înțeles.

## Trei întrebări diferite, nu trei nume pentru aceeași

  • **live** — procesul și bucla de evenimente răspund. ZERO I/O. Motivul e operațional, nu
    estetic: liveness e legat de RESTART. Dacă live ar atinge Postgres, o întrerupere de 30s la
    provider ar reporni fiecare container din flotă simultan, adică ar transforma o degradare
    într-o pană. Un Postgres jos nu e un motiv să omori un proces sănătos.
  • **startup** — condițiile care nu se pot repara singure la runtime: config obligatoriu, schemă
    în intervalul tolerat, registre/politici încărcate, chei prezente. Eșecul aici oprește
    TRAFICUL, nu procesul. Odată trecut, rămâne trecut (se latch-uiește): un startup care
    oscilează ar face proxy-ul să scoată și să bage instanța în rotație la fiecare tick.
  • **ready** — poate servi ACUM. Probe MĂRGINITE pe dependențele declarate ale rolului. Se
    reevaluează, cu un cache scurt ca o rafală de probe să nu devină ea însăși sarcina.

## Required vs optional se CITEȘTE din cod, nu se declară din obișnuință

Cardul cere ca dependențele optional/degraded să nu fie „transformate arbitrar în required".
Clasificarea de mai jos e derivată din comportamentul real la eroare:

  • `api` + Redis = **required**, fiindcă rate-limitul de pe calea de accept e `fail_closed=True`
    (`src/web/app.py::_accept_turn_v2`): cu Redis jos, marginea respinge 429, deci instanța chiar
    nu poate servi.
  • `worker` + Redis = **optional**, fiindcă executorul NX-233 tratează Redis ca scheduling
    (wake best-effort, `admission` cade pe local la orice excepție) iar autoritatea e Postgres.
    Un worker fără Redis lucrează mai încet, nu greșit — a-l scoate din rotație ar opri exact
    recuperarea turelor deja acceptate.

Aceeași diferență, exprimată în cod: `Requirement.REQUIRED` scoate instanța din rotație,
`Requirement.OPTIONAL` produce `degraded` + reason code intern, vizibil doar pe calea de operator.

## Ce NU face nicio sondă de aici

Nu apelează LLM. Nu creează conversație/contact. Nu scrie un rând de business. Nu inventează un
tenant ca să testeze o scriere (de-asta capacitatea de scriere pe ledger se verifică prin
`has_table_privilege`, nu printr-un INSERT cu ROLLBACK: un INSERT ar avea nevoie de FK-uri
valide, adică de date fabricate — o sondă care fabrică date ca să se testeze pe sine e chiar
scenariul pe care îl vrem imposibil). Nu ține o conexiune peste deadline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

from src.observability import metrics
from src.observability.contract import (
    HEALTH_COMPONENTS,
    HEALTH_REASONS,
    HEALTH_ROLES,
)
from src.ops.build_info import BuildInfo, cached_build_info

log = logging.getLogger(__name__)

# ── Vocabulare ÎNCHISE ────────────────────────────────────────────────────────────────────────
# Definite ÎN `observability/contract.py` și importate aici, nu copiate: sunt ȘI etichete de
# metrică, iar o valoare care există într-un loc și nu în celălalt ar fi normalizată tăcut la
# `other` — adică exact semnalul pe care îl cauți ar dispărea din grafic (vezi contract.py).

ROLE_API = "api"
ROLE_WORKER = "worker"
ROLE_SCHEDULER = "scheduler"
ROLES = HEALTH_ROLES


class State(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class Requirement(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "n/a"


#: Codurile de motiv. Set ÎNCHIS: ajung în metrici (cardinalitate) și pe calea de operator
#: (privacy). Un motiv construit din `str(exception)` ar duce în ambele locuri text liber.
REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_UNREACHABLE = "unreachable"
REASON_AUTH_FAILED = "auth_failed"
REASON_SCHEMA_TOO_OLD = "schema_too_old"
REASON_SCHEMA_TOO_NEW = "schema_too_new"
REASON_SCHEMA_UNKNOWN = "schema_unknown"
REASON_PRIVILEGE_MISSING = "privilege_missing"
REASON_LEDGER_MISSING = "ledger_missing"
REASON_POOL_EXHAUSTED = "pool_exhausted"
REASON_STALE_HEARTBEAT = "stale_heartbeat"
REASON_NOT_CONFIGURED = "not_configured"
REASON_DISABLED = "disabled"
REASON_PROBE_ERROR = "probe_error"
REASON_KEYS_MISSING = "keys_missing"
REASON_REGISTRY_INVALID = "registry_invalid"

REASON_CODES = HEALTH_REASONS

#: Componentele sondate. Set ÎNCHIS din același motiv ca numele de span (NX-246).
COMPONENTS = HEALTH_COMPONENTS


@dataclass(frozen=True, slots=True)
class ProbeResult:
    component: str
    state: State
    reason: str
    duration_ms: int = 0
    #: Detaliu NUMERIC/enumerat, niciodată text liber (vezi `operator()`).
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.state in (State.OK, State.SKIPPED)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Verdictul + probele. `public()` e minimal, `operator()` are detaliile."""

    kind: str  # "startup" | "ready"
    ok: bool
    build: BuildInfo
    probes: tuple[ProbeResult, ...]
    checked_at: str

    @property
    def degraded(self) -> tuple[str, ...]:
        return tuple(p.component for p in self.probes if p.state is State.DEGRADED)

    def public(self) -> dict[str, Any]:
        """Răspunsul NEAUTENTIFICAT: status, release, schemă, timestamp. Atât.

        Fără nume de dependență, fără reason code, fără DSN, fără hostname. Un endpoint de health
        e cel mai ușor de atins loc din sistem; „postgres_tenant: auth_failed" îi spune unui
        atacator exact ce să încerce mai departe și când.
        """
        return {
            "status": "ok" if self.ok else "unavailable",
            **self.build.public(),
            "checked_at": self.checked_at,
        }

    def operator(self) -> dict[str, Any]:
        """Vederea de operator (cale autorizată): probe + reason codes + numere. Tot fără secrete:
        `detail` conține doar valori pe care le-am construit noi (versiuni, contoare, booleeni)."""
        return {
            "status": "ok" if self.ok else "unavailable",
            "kind": self.kind,
            **self.build.operator(),
            "checked_at": self.checked_at,
            "probes": [
                {
                    "component": p.component,
                    "state": p.state.value,
                    "reason": p.reason,
                    "duration_ms": p.duration_ms,
                    **({"detail": p.detail} if p.detail else {}),
                }
                for p in self.probes
            ],
        }


# ── Matricea de dependențe per rol ────────────────────────────────────────────────────────────

ProbeFn = Callable[[], Awaitable[ProbeResult]]

#: `component → {rol → cerință}`. Ce nu apare pentru un rol e `NOT_APPLICABLE` (sonda nici nu
#: rulează). Explicit peste tot: un default „required" ar scoate din rotație workerul pentru o
#: dependență de care nu depinde.
DEPENDENCY_MATRIX: dict[str, dict[str, Requirement]] = {
    "postgres_control": {
        ROLE_API: Requirement.REQUIRED,
        ROLE_WORKER: Requirement.REQUIRED,
        ROLE_SCHEDULER: Requirement.REQUIRED,
    },
    "postgres_tenant": {
        ROLE_API: Requirement.REQUIRED,
        ROLE_WORKER: Requirement.REQUIRED,
        ROLE_SCHEDULER: Requirement.NOT_APPLICABLE,
    },
    "schema": {
        ROLE_API: Requirement.REQUIRED,
        ROLE_WORKER: Requirement.REQUIRED,
        ROLE_SCHEDULER: Requirement.REQUIRED,
    },
    "ledger": {
        ROLE_API: Requirement.REQUIRED,
        ROLE_WORKER: Requirement.REQUIRED,
        ROLE_SCHEDULER: Requirement.NOT_APPLICABLE,
    },
    # Vezi docstringul modulului: `fail_closed=True` la accept vs `admission` care cade pe local.
    "redis": {
        ROLE_API: Requirement.REQUIRED,
        ROLE_WORKER: Requirement.OPTIONAL,
        ROLE_SCHEDULER: Requirement.NOT_APPLICABLE,
    },
    "executor": {
        ROLE_API: Requirement.NOT_APPLICABLE,
        ROLE_WORKER: Requirement.OPTIONAL,
        ROLE_SCHEDULER: Requirement.NOT_APPLICABLE,
    },
}


def requirement_for(component: str, role: str) -> Requirement:
    return DEPENDENCY_MATRIX.get(component, {}).get(role, Requirement.NOT_APPLICABLE)


# ── Sonde ─────────────────────────────────────────────────────────────────────────────────────


async def _timed(component: str, coro: Awaitable[ProbeResult], timeout_s: float) -> ProbeResult:
    """Rulează o sondă cu deadline propriu. Timeout = verdict, nu excepție care urcă.

    `asyncio.wait_for` stă AICI, în afara oricărui checkout de conexiune (regula NX-231: nimic
    extern între acquire și release). Sonda își deschide și își închide singură conexiunea.
    """
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=timeout_s)
    except (TimeoutError, asyncio.TimeoutError):
        return ProbeResult(component, State.FAILED, REASON_TIMEOUT, _ms(t0))
    except Exception as e:  # noqa: BLE001 — o sondă nu are voie să arunce spre handler
        log.warning("health: sonda %s a eșuat (%s)", component, type(e).__name__)
        return ProbeResult(component, State.FAILED, _classify(e), _ms(t0))
    return ProbeResult(result.component, result.state, result.reason, _ms(t0), result.detail)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _classify(exc: BaseException) -> str:
    """Excepție → cod STABIL. Pe TIPUL excepției (și pe câteva tipuri asyncpg cunoscute), niciodată
    pe mesaj: mesajul unei erori de conectare conține host, port și uneori user."""
    name = type(exc).__name__
    if "Password" in name or "Authentication" in name or "InsufficientPrivilege" in name:
        return REASON_AUTH_FAILED
    if "UndefinedTable" in name:
        return REASON_LEDGER_MISSING
    if "TooManyConnections" in name or "PoolTimeout" in name:
        return REASON_POOL_EXHAUSTED
    if "Connection" in name or "OSError" in name or "Redis" in name or "Timeout" in name:
        return REASON_UNREACHABLE
    return REASON_PROBE_ERROR


async def probe_postgres_control() -> ProbeResult:
    """Pool-ul de control plane răspunde. Un `select 1` — o sondă nu citește date de business."""
    from src.db.connection import admin_conn, get_pool  # noqa: PLC0415 — import leneș (boot)

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        await conn.fetchval("select 1")
    return ProbeResult("postgres_control", State.OK, REASON_OK)


async def probe_postgres_tenant() -> ProbeResult:
    """Pool-ul tenant e utilizabil ȘI are rolul corect.

    `current_user` nu e cosmetică: dacă `DATABASE_URL_BOT` a fost pus greșit spre un rol
    privilegiat, procesul ar rula cu RLS ocolit. `get_bot_pool` are deja plasa la boot; sonda o
    reconfirmă pe fiecare instanță, fiindcă un pool poate fi recreat după o pană.
    """
    from src.db.connection import get_bot_pool  # noqa: PLC0415

    pool = await get_bot_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchval("select current_user")
    if user != "bot_runtime":
        return ProbeResult(
            "postgres_tenant", State.FAILED, REASON_AUTH_FAILED, detail={"role_ok": False}
        )
    return ProbeResult("postgres_tenant", State.OK, REASON_OK, detail={"role_ok": True})


async def probe_schema(build: BuildInfo) -> ProbeResult:
    """Schema aplicată e în intervalul pe care ARTEFACTUL ăsta îl tolerează.

    Trei verdicte distincte, fiindcă cer trei acțiuni diferite:
      • prea VECHE → rulează jobul de migrare (imaginea cere ceva ce DB-ul n-are);
      • prea NOUĂ → promovează imaginea corectă sau oprește rollbackul (DB-ul a trecut dincolo de
        ce știe imaginea asta — cazul în care un rollback naiv ar rula cod orb pe coloane noi);
      • necunoscută → `schema_migrations` nu se poate citi; nu presupunem nimic.
    """
    from src.db.connection import admin_conn, get_pool  # noqa: PLC0415

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        applied = await conn.fetchval(
            "select coalesce(max(version::int), 0) from schema_migrations"
        )
    if applied is None:
        return ProbeResult("schema", State.FAILED, REASON_SCHEMA_UNKNOWN)
    detail = {
        "applied": int(applied),
        "requires": build.schema_requires,
        "tolerates": build.schema_tolerates,
    }
    if applied < build.schema_requires:
        return ProbeResult("schema", State.FAILED, REASON_SCHEMA_TOO_OLD, detail=detail)
    if applied > build.schema_tolerates:
        return ProbeResult("schema", State.FAILED, REASON_SCHEMA_TOO_NEW, detail=detail)
    return ProbeResult("schema", State.OK, REASON_OK, detail=detail)


async def probe_ledger() -> ProbeResult:
    """Rolul de runtime poate CITI și SCRIE ledgerul `web_turns` — fără să scrie nimic.

    `has_table_privilege` răspunde exact la întrebarea de care depinde acceptul durabil (NX-232),
    fără să inventeze un tenant, o conversație și un FK doar ca să facă un INSERT pe care apoi
    să-l dea ROLLBACK. Un grant lipsă e cea mai probabilă cauză de „ready dar nu poate accepta",
    fiindcă apare la restore/migrare — adică fix în situațiile după care întrebi „e gata?".
    """
    from src.db.connection import get_bot_pool  # noqa: PLC0415

    pool = await get_bot_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("select to_regclass('public.web_turns') is not null")
        if not exists:
            return ProbeResult("ledger", State.FAILED, REASON_LEDGER_MISSING)
        row = await conn.fetchrow(
            "select has_table_privilege('public.web_turns', 'select') as can_read, "
            "has_table_privilege('public.web_turns', 'insert') as can_insert, "
            "has_table_privilege('public.web_turns', 'update') as can_update"
        )
    detail = {k: bool(row[k]) for k in ("can_read", "can_insert", "can_update")}
    if not all(detail.values()):
        return ProbeResult("ledger", State.FAILED, REASON_PRIVILEGE_MISSING, detail=detail)
    return ProbeResult("ledger", State.OK, REASON_OK, detail=detail)


async def probe_redis() -> ProbeResult:
    """Redis răspunde la PING. Nimic mai mult: o sondă care ar scrie o cheie ar produce trafic de
    scriere proporțional cu numărul de sonde × frecvența lor."""
    from src.redis_bus import get_redis  # noqa: PLC0415

    redis = await get_redis()
    await redis.ping()
    return ProbeResult("redis", State.OK, REASON_OK)


async def probe_executor() -> ProbeResult:
    """Capacitatea executorului: câte sloturi de admission sunt ocupate acum.

    `degraded` la saturație — NU `failed`. Un executor plin lucrează; a-l scoate din rotație ar
    face exact opusul a ceea ce vrei sub sarcină. (Și oricum nu e rutabil: workerul n-are HTTP.)
    """
    from src.config import get_settings  # noqa: PLC0415
    from src.worker.admission import get_admission  # noqa: PLC0415

    admission = get_admission()
    inflight = admission.inflight
    cap = max(1, get_settings().admission_max_inflight)
    detail = {"inflight": int(inflight), "cap": int(cap)}
    if inflight >= cap:
        return ProbeResult("executor", State.DEGRADED, REASON_POOL_EXHAUSTED, detail=detail)
    return ProbeResult("executor", State.OK, REASON_OK, detail=detail)


#: `component → sondă`. `schema` primește `BuildInfo`, deci se leagă separat.
PROBES: dict[str, ProbeFn] = {
    "postgres_control": probe_postgres_control,
    "postgres_tenant": probe_postgres_tenant,
    "ledger": probe_ledger,
    "redis": probe_redis,
    "executor": probe_executor,
}


# ── Startup (latch) ───────────────────────────────────────────────────────────────────────────

#: Startupul trecut RĂMÂNE trecut. Motivul e în docstringul modulului: un startup care oscilează
#: bagă și scoate instanța din rotație la fiecare tick, ceea ce e mai rău decât ambele stări.
_startup_passed: dict[str, bool] = {}


def reset_startup_latch() -> None:
    """Doar pentru teste: fiecare caz începe cu un proces „proaspăt pornit"."""
    _startup_passed.clear()


async def check_startup(role: str, *, timeout_s: float = 5.0) -> HealthReport:
    """Condițiile care nu se repară singure la runtime.

    Config-ul obligatoriu a fost deja validat (dacă `Settings()` pica, procesul nu exista), deci
    aici verificăm ce boot-ul nu verifică: schema în interval, registrul de siguranță, inelul de
    chei al acțiunilor. Eșecul înseamnă „nu primi trafic", nu „sinucide-te".
    """
    from src.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    build = cached_build_info(role)
    probes: list[ProbeResult] = []

    if _startup_passed.get(role):
        return _report("startup", True, build, tuple(probes))

    probes.append(await _timed("schema", probe_schema(build), timeout_s))

    # NX-173 (P0): registrul de contraindicații. Workerul are deja poartă de boot pe el; aici îl
    # verificăm ȘI pentru API, fiindcă `/web/chat` rulează pipeline-ul IN-PROCES în `webhook`.
    from src.safety.contraindications import registry_healthy  # noqa: PLC0415

    ok, _info = registry_healthy()
    probes.append(
        ProbeResult(
            "safety_registry",
            State.OK if ok else State.FAILED,
            REASON_OK if ok else REASON_REGISTRY_INVALID,
        )
    )

    # NX-236: dacă acțiunile opace sunt aprinse, inelul de chei TREBUIE să parseze. `Settings` are
    # deja poarta la boot; o repetăm ca stare observabilă (o cheie rotită greșit e o cauză
    # frecventă de „pornește dar nu poate semna").
    probes.append(_probe_action_keys(settings))

    ok_all = all(p.healthy for p in probes)
    if ok_all:
        _startup_passed[role] = True
    return _report("startup", ok_all, build, tuple(probes))


def _probe_action_keys(settings: Any) -> ProbeResult:
    if not getattr(settings, "web_actions_enabled", False):
        return ProbeResult("action_keys", State.SKIPPED, REASON_DISABLED)
    from src.web.action_crypto import parse_key_ring  # noqa: PLC0415

    try:
        ring = parse_key_ring(settings.web_action_keys)
    except ValueError:
        return ProbeResult("action_keys", State.FAILED, REASON_KEYS_MISSING)
    # Numărul de chei, nu cheile: „2" spune operatorului că rotația e în fereastra de overlap.
    return ProbeResult("action_keys", State.OK, REASON_OK, detail={"keys": len(ring.keys)})


# ── Readiness ─────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Cache:
    report: HealthReport | None = None
    at: float = 0.0
    last_ok: bool | None = None


_ready_cache: dict[str, _Cache] = {}


def reset_ready_cache() -> None:
    _ready_cache.clear()


async def check_ready(
    role: str, *, timeout_s: float = 2.0, cache_s: float = 1.0, now: float | None = None
) -> HealthReport:
    """Poate servi ACUM? Probele rulează în PARALEL, fiecare cu deadline-ul ei.

    Serial ar însemna că bugetul total e suma celor mai proaste cazuri — exact când toate
    dependențele sunt lente, adică fix când proxy-ul are nevoie de un răspuns rapid ca să te
    scoată din rotație. Cache scurt (`cache_s`): trei probe de la Docker, Traefik și un operator
    în aceeași secundă nu trebuie să devină trei rafale de query-uri.
    """
    clock = time.monotonic() if now is None else now
    cache = _ready_cache.setdefault(role, _Cache())
    if cache.report is not None and clock - cache.at < cache_s:
        return cache.report

    build = cached_build_info(role)
    planned = [
        c for c in sorted(PROBES) if requirement_for(c, role) is not Requirement.NOT_APPLICABLE
    ]

    async def _run(component: str) -> ProbeResult:
        return await _timed(component, PROBES[component](), timeout_s)

    results = list(
        await asyncio.gather(
            _timed("schema", probe_schema(build), timeout_s),
            *(_run(c) for c in planned),
        )
    )

    # Cerința decide ce înseamnă un eșec. `optional` eșuat = degraded (rămâi în rotație, dar se
    # vede); `required` eșuat = out.
    adjusted: list[ProbeResult] = []
    ok = True
    for probe in results:
        req = requirement_for(probe.component, role)
        if probe.state is State.FAILED and req is Requirement.OPTIONAL:
            probe = ProbeResult(
                probe.component, State.DEGRADED, probe.reason, probe.duration_ms, probe.detail
            )
        if probe.state is State.FAILED:
            ok = False
        adjusted.append(probe)

    report = _report("ready", ok, build, tuple(sorted(adjusted, key=lambda p: p.component)))
    _emit_metrics(role, report, cache)
    cache.report, cache.at, cache.last_ok = report, clock, ok
    return report


def _emit_metrics(role: str, report: HealthReport, cache: _Cache) -> None:
    """Fiecare probă → un contor; TRANZIȚIA verdictului → alt contor.

    Diferența contează: contorul de probe spune „cât de des e Redis jos", tranziția spune „de câte
    ori am ieșit din rotație". Un dashboard care le confundă arată o pană de o oră ca 3600 de pene.
    """
    for probe in report.probes:
        metrics.record_counter(
            "ops_dependency_probe_total",
            component=probe.component,
            state=probe.state.value,
            reason_code=probe.reason,
        )
        metrics.record_histogram(
            "ops_dependency_probe_seconds", probe.duration_ms / 1000.0, component=probe.component
        )
    if cache.last_ok is not None and cache.last_ok != report.ok:
        metrics.record_counter(
            "ops_readiness_transitions_total", role=role, state="ok" if report.ok else "unavailable"
        )


def _report(kind: str, ok: bool, build: BuildInfo, probes: tuple[ProbeResult, ...]) -> HealthReport:
    from datetime import UTC, datetime  # noqa: PLC0415

    return HealthReport(
        kind=kind,
        ok=ok,
        build=build,
        probes=probes,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
