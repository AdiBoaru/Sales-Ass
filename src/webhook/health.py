"""NX-248 — rutele de health. Trei endpointuri, trei semantici, un singur răspuns public minimal.

Separarea nu e cosmetică, e legată de ce se întâmplă când pică fiecare:

    /health/live     → orchestratorul REPORNEȘTE procesul
    /health/startup  → proxy-ul nu-l bagă în rotație (procesul rămâne viu)
    /health/ready    → proxy-ul îl scoate din rotație (procesul rămâne viu)

De-asta `live` nu are voie să atingă nimic extern: cu Postgres în probleme, un `live` care
verifică DB-ul repornește toată flota — o degradare devine o pană, iar repornirea nu repară
Postgresul.

## De ce răspunsul public e sărac

`/health/*` e neautentificat prin construcție (Docker și Traefik nu au credențiale). Un răspuns
care spune „postgres_tenant: auth_failed" e recon gratuit: confirmă tehnologia, confirmă că rolul
există și spune exact când e sistemul slăbit. Deci publicul primește status, release, config,
interval de schemă și un timestamp; detaliile per dependență ies DOAR pe calea de operator, care
cere un token dedicat și nu trece niciodată prin Traefik.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.ops import health

router = APIRouter(prefix="/health", tags=["ops"])

#: Rolul procesului care servește HTTP. Constant: `webhook` e API, indiferent ce rute montează.
ROLE = health.ROLE_API

#: Cache-control pe TOATE răspunsurile de health. Un proxy care cache-uiește „ok" 30 de secunde
#: transformă readiness într-o amintire — exact în intervalul în care contează că e o măsurătoare.
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _json(payload: dict, status: int) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


@router.get("/live")
def live() -> Response:
    """Procesul și event loop-ul răspund. ZERO I/O, zero config citită dinamic, zero DB.

    Dacă handlerul ăsta rulează, event loop-ul nu e blocat — asta e tot ce trebuie să dovedească.
    """
    return _json({"status": "ok"}, 200)


@router.get("/startup")
async def startup() -> Response:
    """Condițiile care nu se repară singure: schemă în interval, registre, chei.

    503 până trec. Docker are `start_period` propriu pe sonda asta, distinct de readiness, ca o
    migrare care rulează în paralel să nu fie confundată cu un container stricat.
    """
    report = await health.check_startup(ROLE)
    return _json(report.public(), 200 if report.ok else 503)


@router.get("/ready")
async def ready() -> Response:
    """Poate servi ACUM. Probe mărginite pe dependențele REQUIRED ale rolului `api`.

    Dependențele `optional` care pică apar ca `degraded` pe calea de operator și NU scot instanța
    din rotație — clasificarea vine din comportamentul real la eroare, vezi `src/ops/health.py`.
    """
    report = await health.check_ready(ROLE)
    return _json(report.public(), 200 if report.ok else 503)


@router.get("/detail")
async def detail(request: Request) -> Response:
    """Vederea de OPERATOR: fiecare sondă, cu reason code și durată.

    Autorizată cu un token dedicat (`OPS_HEALTH_TOKEN`), comparat în timp CONSTANT. Fără token
    configurat, endpointul nu există (404, nu 401): un 401 confirmă că ruta e acolo și invită la
    ghicit, iar asta e singura informație pe care o are de dat un endpoint de diagnostic.
    """
    token = get_settings().ops_health_token
    if not token:
        return _json({"detail": "not found"}, 404)
    presented = request.headers.get("x-ops-token", "")
    if not hmac.compare_digest(presented, token):
        return _json({"detail": "not found"}, 404)
    report = await health.check_ready(ROLE, cache_s=0.0)
    startup_report = await health.check_startup(ROLE)
    payload = report.operator()
    payload["startup"] = {
        "ok": startup_report.ok,
        "probes": startup_report.operator()["probes"],
    }
    return _json(payload, 200 if report.ok else 503)
