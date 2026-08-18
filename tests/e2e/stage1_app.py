"""NX-247 — app factory TEST-ONLY peste aplicația de producție, cu gărzi STRUCTURALE.

Ideea centrală: harnessul nu construiește o aplicație paralelă. Ia EXACT obiectul FastAPI din
`src.webhook.app` (cu middleware-ul de body cap, lifespan-ul de observabilitate și montarea
condiționată a routerului web, toate reale) și îi atașează un singur router de control. Dacă ar
reasambla routerele, ar putea trece testul cu un stack care nu există în producție — exact clasa
de „gate fals" pe care cardul o interzice.

**De ce gărzile nu sunt convenții.** Un flag de env care activează un provider fake ar fi
exploatabil în producție; de aceea nu există niciun flag. Există o FUNCȚIE, în `tests/`, care refuză
să se execute dacă:

  • `settings.env != "test"` — și `is_prod` e verificat separat, nu doar prin egalitate;
  • hostul de bind nu e loopback;
  • secretul de control nu e furnizat, sau e prea scurt ca să fie random;
  • routerul web real nu e montat (adică `WEB_ENABLED` e stins) — mai bine refuz decât un test
    care „trece" fără suprafața pe care pretinde că o exersează.

`tests/` nu intră în imaginea de producție (`Dockerfile` copiază `src/`, `scripts/migrate.py`,
`docs/*.sql`), iar un test verifică mecanic asta — nu ne bazăm pe memoria nimănui.

**Zero rețea.** `deny_outbound_network()` lasă să treacă numai loopbackul și hosturile de DB/Redis
citite din configurație. Orice altă rezolvare DNS sau conexiune e refuzată ȘI numărată: contorul e
dovada pozitivă cerută de `outbound_provider_network_attempt_count = 0`.
"""

from __future__ import annotations

import hmac
import logging
import os
import socket
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from tests.e2e import stage1_probes as probes
from tests.e2e.stage1_scenarios import (
    FAULTS,
    MODEL_SCRIPTS,
    Stage1FakeLLM,
    SyntheticTenant,
)

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CONTROL_PREFIX = "/__test__"
CONTROL_HEADER = "X-Stage1-Control"
MIN_CONTROL_SECRET_LEN = 32
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

#: Setul ÎNCHIS de rute de control. Testul de vocabular compară routerul construit cu lista asta:
#: o rută adăugată fără să fie declarată aici rupe suita. NU există rută de ceas și nici de ID —
#: cardul cere ca acelea să rămână seam de fixture, nu suprafață HTTP.
CONTROL_ROUTES: frozenset[str] = frozenset(
    {
        f"{CONTROL_PREFIX}/health",
        f"{CONTROL_PREFIX}/reset",
        f"{CONTROL_PREFIX}/scenario",
        f"{CONTROL_PREFIX}/fault",
        f"{CONTROL_PREFIX}/probe/counters",
        f"{CONTROL_PREFIX}/probe/turn",
        f"{CONTROL_PREFIX}/probe/conversation",
    }
)


class HarnessRefused(RuntimeError):
    """Harnessul a refuzat să pornească. Niciodată prins de codul de harness: dacă pornirea e
    nesigură, testul trebuie să moară zgomotos, nu să continue pe o cale degradată."""


class OutboundNetworkDenied(OSError):
    """Tentativă de rețea în afara allowlistului. `OSError` deliberat: bibliotecile de provider o
    tratează ca eroare de conexiune și nu blochează suita — iar noi păstrăm contorul."""


# ── Profiluri de flag-uri ───────────────────────────────────────────────────────────────────
# Un singur loc care spune ce configurație se certifică. NX-247 nu aprinde trafic real; el
# stabilește pe CE combinație de flag-uri s-a măsurat, ca NX-249 să nu poată face canary pe alta.

#: Configurația LIVRABILĂ azi: tot transportul v2 + acțiuni + coș + context + feedback +
#: observabilitate + deadline, cu pipeline-ul curent și proiecția v1→v2 (NX-233).
PROFILE_V2_TRANSPORT: dict[str, str] = {
    "ENV": "test",
    "WEB_ENABLED": "true",
    "WEB_TURN_LEDGER_ENABLED": "true",
    "WEB_TURN_V2_ENABLED": "true",
    "WEB_TURN_EXECUTOR_ENABLED": "true",
    "WEB_TURN_RECOVERY_ENABLED": "true",
    "WEB_TURN_SSE_ENABLED": "true",
    # Sesiuni v2 (cu TTL + legare de origin): fără ele, expirarea și reînnoirea din R10 n-ar avea
    # ce testa, iar gate-ul ar certifica o configurație mai slabă decât cea de release.
    "WEB_SESSION_V2_ENABLED": "true",
    "WEB_SESSION_ORIGIN_BINDING": "true",
    "WEB_CONTEXT_ENABLED": "true",
    "WEB_ACTIONS_ENABLED": "true",
    "CONVERSATION_CART_ENABLED": "true",
    "WEB_FEEDBACK_ENABLED": "true",
    "OBSERVABILITY_ENABLED": "true",
    "TURN_LATENCY_SPANS_ENABLED": "true",
    "TURN_DEADLINE_ENABLED": "true",
    "ADMISSION_ENABLED": "false",
}

#: Configurația de creier unic (NX-239/240). Rulabilă, dar NU certificabilă acum: promovarea
#: `search_entities` are verdict `NOT-READY` (`docs/NX-238-DECISION.md`), iar NX-247 nu are voie să
#: emită un GO pe care gate-ul de retrieval nu l-a dat. Există ca gate-ul să fie pregătit, nu ca să
#: pretindă că a măsurat ce nu s-a decis.
PROFILE_V2_SINGLE_BRAIN: dict[str, str] = {
    **PROFILE_V2_TRANSPORT,
    "SINGLE_BRAIN_ENABLED": "true",
    "WEB_VIEW_V2_PROJECTOR_ENABLED": "true",
}

FLAG_PROFILES: dict[str, dict[str, str]] = {
    "v2_transport": PROFILE_V2_TRANSPORT,
    "v2_single_brain": PROFILE_V2_SINGLE_BRAIN,
}

#: Profilul pe care rulează gate-ul obligatoriu. Schimbarea lui e o decizie de release, nu un
#: detaliu de test — de aceea e o constantă cu nume, nu un default de parametru.
CERTIFIED_PROFILE = "v2_transport"


def apply_flag_profile(name: str, *, env: dict[str, str] | None = None) -> dict[str, str]:
    """Scrie profilul în `env` (default `os.environ`) ÎNAINTE de primul `get_settings()`.

    `Settings` e cache-uit (lru_cache): un flag pus după primul apel n-ar avea efect și testul ar
    rula pe altă configurație decât cea declarată. De aceea funcția e apelată de launcher/fixture
    înainte de orice import de `src.*`, iar valorile existente NU se suprascriu decât explicit.
    """
    if name not in FLAG_PROFILES:
        raise HarnessRefused(f"profil de flag-uri necunoscut: {name!r}")
    target = env if env is not None else os.environ
    for key, value in FLAG_PROFILES[name].items():
        target[key] = value
    return dict(FLAG_PROFILES[name])


# ── Poarta de pornire ───────────────────────────────────────────────────────────────────────


def assert_harness_allowed(*, bind_host: str, control_secret: str) -> None:
    """Poarta structurală. Ridică `HarnessRefused` — niciodată un warning, niciodată un default."""
    from src.config import get_settings

    settings = get_settings()
    if settings.env != "test":
        raise HarnessRefused(
            f"harnessul Stage 1 pornește DOAR cu ENV=test (acum: {settings.env!r})"
        )
    if settings.is_prod:
        raise HarnessRefused("harnessul Stage 1 nu pornește pe o configurație de producție")
    if bind_host not in LOOPBACK_HOSTS:
        raise HarnessRefused(
            f"harnessul se leagă DOAR la loopback (cerut: {bind_host!r}); "
            "un harness accesibil din rețea e o rută de control publică"
        )
    if len(control_secret or "") < MIN_CONTROL_SECRET_LEN:
        raise HarnessRefused(
            f"secretul de control trebuie să aibă ≥{MIN_CONTROL_SECRET_LEN} caractere "
            "și să fie generat per proces"
        )
    if not settings.web_enabled:
        raise HarnessRefused("WEB_ENABLED=false — routerul web real nu s-ar monta")
    if not settings.web_turn_v2_enabled:
        raise HarnessRefused("WEB_TURN_V2_ENABLED=false — rutele v2 ar răspunde 404")


# ── Garda de rețea ──────────────────────────────────────────────────────────────────────────


@dataclass
class NetworkGuard:
    """Allowlist de rețea + contor de refuzuri. `attempts` conține DOAR hostname/port, niciodată
    payload: e o dovadă de scope, nu un sniffer."""

    allowed_hosts: set[str]
    attempts: list[str] = field(default_factory=list)
    _original_getaddrinfo: Any = None
    _original_connect: Any = None

    def _allowed(self, host: Any) -> bool:
        return str(host) in self.allowed_hosts

    def install(self) -> NetworkGuard:
        self._original_getaddrinfo = socket.getaddrinfo
        self._original_connect = socket.socket.connect
        guard = self

        def getaddrinfo(host, port, *args, **kwargs):
            if not guard._allowed(host):
                guard.attempts.append(f"dns:{host}:{port}")
                raise OutboundNetworkDenied(f"rezolvare DNS refuzată de harness: {host}")
            return guard._original_getaddrinfo(host, port, *args, **kwargs)

        def connect(self_sock, address):  # noqa: ANN001 — semnătura socket-ului
            host = address[0] if isinstance(address, tuple) else address
            if not guard._allowed(host):
                guard.attempts.append(f"tcp:{host}")
                raise OutboundNetworkDenied(f"conexiune refuzată de harness: {host}")
            return guard._original_connect(self_sock, address)

        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = connect  # type: ignore[method-assign]
        return self

    def uninstall(self) -> None:
        if self._original_getaddrinfo is not None:
            socket.getaddrinfo = self._original_getaddrinfo  # type: ignore[assignment]
        if self._original_connect is not None:
            socket.socket.connect = self._original_connect  # type: ignore[method-assign]


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None


def deny_outbound_network(*, extra_hosts: tuple[str, ...] = ()) -> NetworkGuard:
    """Permite loopbackul + hosturile de DB/Redis din configurație. Restul e refuzat și numărat."""
    from src.config import get_settings

    settings = get_settings()
    allowed = set(LOOPBACK_HOSTS) | {"0.0.0.0", "::"} | set(extra_hosts)  # noqa: S104
    for url in (
        getattr(settings, "supabase_db_url", None),
        getattr(settings, "database_url_bot", None),
        getattr(settings, "redis_url", None),
    ):
        host = _host_of(url)
        if host:
            allowed.add(host)
    return NetworkGuard(allowed_hosts=allowed).install()


# ── Starea harnessului ──────────────────────────────────────────────────────────────────────


@dataclass
class HarnessState:
    """Starea MUTABILĂ a harnessului: modelul fals, defectul armat, tenanții seedați.

    Per proces, nu per request: harnessul rulează un scenariu la un moment dat, iar
    `/__test__/reset` e obligatoriu între teste (self-testul verifică izolarea).
    """

    control_secret: str
    llm: Stage1FakeLLM = field(default_factory=Stage1FakeLLM)
    fault: str = "none"
    fault_fired: int = 0
    tenants: dict[str, SyntheticTenant] = field(default_factory=dict)

    def arm_fault(self, name: str) -> None:
        if name not in FAULTS:
            raise HTTPException(status_code=400, detail="fault necunoscut")
        self.fault = name
        self.fault_fired = 0

    def take_fault(self, name: str) -> bool:
        """True O SINGURĂ dată pentru defectele tranzitorii: un „retry bounded" nu se poate
        dovedi dacă defectul se repetă la infinit (ar dovedi doar că renunțăm)."""
        if self.fault != name:
            return False
        self.fault_fired += 1
        if name in _ONE_SHOT_FAULTS:
            self.fault = "none"
        return True

    def reset(self) -> None:
        self.llm.counters.reset()
        self.llm.script.script = "recommend"
        self.fault = "none"
        self.fault_fired = 0


_ONE_SHOT_FAULTS: frozenset[str] = frozenset({"db_transient_at_commit", "kill_worker_after_claim"})

_STATE: HarnessState | None = None


def state() -> HarnessState:
    if _STATE is None:
        raise HarnessRefused("harnessul nu e construit (build_stage1_app nu a rulat)")
    return _STATE


# ── Injecția dependențelor externe ──────────────────────────────────────────────────────────


def install_fake_model(st: HarnessState) -> None:
    """Înlocuiește adaptorul OpenAI pe SINGURA cale prin care îl ia turul (`processor.get_llm`).

    Nu e un flag și nu e un parametru de request: e o rescriere de atribut de modul, făcută din
    `tests/`, după ce poarta a trecut. Un binar de producție nu are cum să ajungă aici, fiindcă
    modulul nu există în imagine.
    """
    from src.worker import processor

    processor.get_llm = lambda: st.llm  # type: ignore[assignment]


def install_fault_hooks(st: HarnessState) -> None:
    """Împachetează DOUĂ funcții ale executorului ca să putem injecta defecte fără să atingem
    `src/`: claim-ul (worker omorât după claim) și commitul (eroare DB tranzitorie).

    Ambele împachetări cheamă funcția REALĂ înainte de a decide: pentru R5 claim-ul trebuie să fie
    deja DURABIL când „moare" workerul, altfel n-am testat reclaim-ul, ci absența unui claim.
    """
    from src.web import turn_executor as te

    real_claim = te.claim_web_turn
    real_complete = te.complete_web_turn_on_conn

    async def claim(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        if claimed is not None and st.take_fault("kill_worker_after_claim"):
            raise _HarnessWorkerKilled("worker omorât după claim (injectat)")
        return claimed

    async def complete(*args, **kwargs):
        if st.take_fault("db_transient_at_commit"):
            raise _HarnessDbTransient("eroare DB tranzitorie la commit (injectat)")
        return await real_complete(*args, **kwargs)

    te.claim_web_turn = claim  # type: ignore[assignment]
    te.complete_web_turn_on_conn = complete  # type: ignore[assignment]


class _HarnessWorkerKilled(RuntimeError):
    """Moartea simulată a workerului. Executorul o prinde ca orice excepție de iterație și lasă
    turul pe lease — exact traiectoria unui proces care a dispărut."""


class _HarnessDbTransient(RuntimeError):
    """Eroare de DB la commit. Nu e `asyncpg.PostgresError` fiindcă nu vrem să pretindem că știm
    ce cod ar întoarce Postgres; ce contează e că tranzacția NU se închide cu succes."""


# ── Routerul de control ─────────────────────────────────────────────────────────────────────


def _authorize(request: Request, st: HarnessState) -> None:
    """Două condiții, ambele necesare: client pe loopback ȘI secretul per proces.

    Loopback singur n-ar fi de-ajuns (orice proces de pe mașină ar putea muta scenariul); secretul
    singur n-ar fi de-ajuns (un reverse proxy pus greșit l-ar expune). Comparația e
    `compare_digest`: un test de timing pe secretul de harness ar fi o glumă, dar tiparul greșit
    copiat mai departe nu.
    """
    host = request.client.host if request.client else ""
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=404, detail="not found")
    supplied = request.headers.get(CONTROL_HEADER) or ""
    if not hmac.compare_digest(supplied, st.control_secret):
        raise HTTPException(status_code=404, detail="not found")


def control_router(st: HarnessState) -> APIRouter:
    """Suprafața de control, mărginită la ce nu se poate face din afara procesului: alegerea
    scenariului, armarea unui defect, citirea contoarelor și a probelor."""
    router = APIRouter(prefix=CONTROL_PREFIX, include_in_schema=False)

    @router.get("/health")
    async def health(request: Request) -> dict:
        _authorize(request, st)
        return {"ok": True, "tenants": sorted(st.tenants)}

    @router.post("/reset")
    async def reset(request: Request) -> dict:
        _authorize(request, st)
        st.reset()
        return {"ok": True}

    @router.post("/scenario")
    async def scenario(request: Request) -> dict:
        _authorize(request, st)
        body = await request.json()
        script = str(body.get("script") or "")
        if script not in MODEL_SCRIPTS:
            raise HTTPException(status_code=400, detail="script necunoscut")
        st.llm.arm(script, stall_s=float(body.get("stall_s") or 0.0))
        return {"ok": True, "script": script}

    @router.post("/fault")
    async def fault(request: Request) -> dict:
        _authorize(request, st)
        body = await request.json()
        st.arm_fault(str(body.get("fault") or "none"))
        return {"ok": True, "fault": st.fault}

    @router.get("/probe/counters")
    async def counters(request: Request) -> dict:
        _authorize(request, st)
        return st.llm.counters.snapshot()

    @router.get("/probe/turn")
    async def probe_turn(
        request: Request, tenant: str, turn_id: str, client_turn_id: str, conversation_id: str
    ) -> JSONResponse:
        """Probele unui tur. `tenant` e cheia sintetică (`alpha`/`beta`): `business_id` NU vine
        niciodată din query, ci din tenanții seedați de harness — aceeași regulă ca în producție
        (P7, server-owned), ca ruta de control să nu devină un oracol cross-tenant."""
        _authorize(request, st)
        target = st.tenants.get(tenant)
        if target is None:
            raise HTTPException(status_code=404, detail="tenant necunoscut")
        from src.db.provider import tenant_db

        db = tenant_db(target.business_id)
        async with db("stage1_probe_turn") as conn:
            bundle = await probes.turn_bundle(
                conn,
                business_id=target.business_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                client_turn_id=client_turn_id,
            )
        return JSONResponse(content=bundle)

    @router.get("/probe/conversation")
    async def probe_conversation(
        request: Request, tenant: str, conversation_id: str
    ) -> JSONResponse:
        _authorize(request, st)
        target = st.tenants.get(tenant)
        if target is None:
            raise HTTPException(status_code=404, detail="tenant necunoscut")
        from src.db.provider import tenant_db

        db = tenant_db(target.business_id)
        async with db("stage1_probe_conversation") as conn:
            snapshot = await probes.conversation_state(conn, target.business_id, conversation_id)
        return JSONResponse(content=snapshot)

    return router


# ── Factory ─────────────────────────────────────────────────────────────────────────────────


def build_stage1_app(
    *,
    control_secret: str,
    bind_host: str = "127.0.0.1",
    tenants: dict[str, SyntheticTenant] | None = None,
) -> FastAPI:
    """Aplicația de PRODUCȚIE + routerul de control. Poarta rulează întâi; nimic nu se importă
    din `src.webhook.app` înainte ca ea să treacă."""
    global _STATE
    assert_harness_allowed(bind_host=bind_host, control_secret=control_secret)

    from src.webhook.app import app as production_app

    paths = {getattr(r, "path", "") for r in production_app.routes}
    if "/web/v2/turns" not in paths:
        raise HarnessRefused(
            "routerul web v2 nu e montat pe aplicația de producție — harnessul ar testa un stub"
        )
    if any(p.startswith(CONTROL_PREFIX) for p in paths):
        raise HarnessRefused(
            "routerul de control e deja montat (build_stage1_app rulat de două ori)"
        )

    st = HarnessState(control_secret=control_secret, tenants=dict(tenants or {}))
    install_fake_model(st)
    install_fault_hooks(st)
    production_app.include_router(control_router(st))
    _STATE = st
    log.warning("harness Stage 1 montat pe %s (test-only, loopback)", bind_host)
    return production_app
