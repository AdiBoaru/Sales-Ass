"""NX-248 — health pentru procesele FĂRĂ HTTP (worker, scheduler).

Un container fără port nu poate fi întrebat „ești gata?" printr-un GET, așa că Docker îl întreabă
printr-o comandă. Problema cunoscută a heartbeat-urilor pe fișier e că **un fișier scris de un
proces mort rămâne pe disc**: dacă bucla asyncio s-a blocat sau firul a murit, ultimul `touch`
continuă să pară proaspăt exact atâta timp cât e fereastra de freshness, iar dacă cineva pune
fereastra prea largă (ca să evite fals-pozitivele), heartbeat-ul devine decor.

Cardul cere explicit mai mult: „un fișier heartbeat scris de un process mort nu este suficient:
probele verifică freshness, PID/process lifecycle și capacitatea de a obține bounded următorul
lease **fără a muta un turn real**".

Aici sunt cele trei verificări, în ordinea în care se ieftinesc:

  1. **freshness** — `last_success` mai vechi decât fereastra ⇒ nesănătos;
  2. **PID viu** — heartbeat-ul conține PID-ul care l-a scris; sonda verifică (în ACELAȘI
     namespace de PID-uri, adică în container) că procesul există. Un fișier orfan de la un
     proces ucis pică aici, oricât de recent ar fi;
  3. **boot id** — un PID se REFOLOSEȘTE. După un restart, PID 1 e alt proces cu același număr,
     deci „PID-ul există" ar fi din nou adevărat degeaba. Heartbeat-ul poartă un `boot_id`
     generat o dată per proces; sonda din interiorul procesului îl compară cu al ei, iar sonda
     din afară (`docker exec`) verifică `/proc/<pid>` — combinația face fișierul orfan detectabil
     în ambele direcții.

**Capacitatea de lease NU se testează mutând un tur.** `lease_loop_alive` e raportat de bucla
executorului (îl atinge la fiecare ciclu, inclusiv pe ciclurile fără muncă), deci „bucla
funcționează" e o observație, nu un experiment. O sondă care ar face un claim real ar rula turul
unui client ca să afle dacă poate rula turul unui client.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.observability.contract import HEALTH_ROLES

log = logging.getLogger(__name__)

#: Rolurile fără HTTP. Verificate contra vocabularului unic din `observability/contract.py`, ca o
#: greșeală de scriere să pice la IMPORT, nu la primul healthcheck dintr-un container de prod.
ROLE_WORKER = "worker"
ROLE_SCHEDULER = "scheduler"
if not {ROLE_WORKER, ROLE_SCHEDULER} <= HEALTH_ROLES:  # pragma: no cover — invariant de import
    raise RuntimeError("rol non-HTTP absent din HEALTH_ROLES (observability/contract.py)")

#: Un id per PROCES. Fixat la import: două procese nu-l pot avea egal, același proces nu-l schimbă.
BOOT_ID = uuid4().hex[:16]

DEFAULT_PATH = Path("/tmp/nativx_health")  # noqa: S108 — tmpfs montat explicit în compose


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Ce scrie un proces non-HTTP despre sine. NUMERE și enumerate, zero text liber: fișierul
    ăsta ajunge în `docker inspect`/loguri de operator, deci n-are voie să poarte date de client."""

    role: str
    pid: int
    boot_id: str
    release_sha: str
    #: `time.time()` la ultimul ciclu ÎNCHEIAT cu succes (nu la pornirea ciclului: altfel un
    #: ciclu care se blochează la jumătate ar arăta ca unul reușit).
    last_success: float
    #: Bucla de lease a executorului a mai făcut un ciclu (chiar și gol). `None` = rol fără buclă.
    lease_loop_alive: bool | None = None
    schema_compatible: bool | None = None
    #: Vocabular ÎNCHIS, nu adâncimea exactă: adâncimea e o valoare de client (câți vizitatori
    #: sunt la coadă acum), bucketul e o stare operațională.
    queue_lag_bucket: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)


QUEUE_LAG_BUCKETS = ("empty", "low", "high", "unknown")


def lag_bucket(depth: int | None) -> str:
    """Adâncime → bucket. Praguri fixe: un bucket configurabil ar face două instanțe să raporteze
    stări diferite pentru aceeași coadă."""
    if depth is None:
        return "unknown"
    if depth <= 0:
        return "empty"
    return "low" if depth < 50 else "high"


def write(hb: Heartbeat, path: Path = DEFAULT_PATH) -> None:
    """Scrie heartbeat-ul ATOMIC (tmp + rename).

    Fără rename, o sondă care citește exact în timpul scrierii vede JSON trunchiat și declară
    procesul mort — un fals-pozitiv care, sub Docker `restart: unless-stopped`, chiar repornește
    un proces sănătos. Scrierea nu are voie să oprească bucla: eșecul se loghează, atât.
    """
    payload = json.dumps(asdict(hb), separators=(",", ":"), sort_keys=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("heartbeat: nu pot scrie %s (%s)", path, type(e).__name__)


def read(path: Path = DEFAULT_PATH) -> Heartbeat | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return Heartbeat(
            role=str(raw["role"]),
            pid=int(raw["pid"]),
            boot_id=str(raw["boot_id"]),
            release_sha=str(raw.get("release_sha", "unknown")),
            last_success=float(raw["last_success"]),
            lease_loop_alive=raw.get("lease_loop_alive"),
            schema_compatible=raw.get("schema_compatible"),
            queue_lag_bucket=str(raw.get("queue_lag_bucket", "unknown")),
            extra=dict(raw.get("extra") or {}),
        )
    except (KeyError, TypeError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """Procesul cu PID-ul ăsta există ÎN ACEST namespace?

    `os.kill(pid, 0)` nu trimite semnal, doar verifică existența + permisiunea. `PermissionError`
    înseamnă „există, dar e al altcuiva" — tot existent, deci `True`. Pe Windows (dev) `os.kill`
    cu semnal 0 nu e portabil, dar sonda rulează în container; local întoarcem `True` ca să nu
    inventăm un verdict din platforma greșită.
    """
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    reason: str
    age_s: float | None = None


def evaluate(
    hb: Heartbeat | None,
    *,
    max_age_s: float,
    now: float | None = None,
    own_boot_id: str | None = None,
) -> Verdict:
    """Heartbeat → verdict. PUR: primește ceasul, nu-l citește (testabil fără sleep).

    `own_boot_id` se dă doar când sonda rulează ÎN PROCES; din afară (docker exec) e `None`,
    fiindcă un alt proces nu poate ști boot-id-ul celui viu — acolo verificarea de lifecycle e PID.
    """
    clock = time.time() if now is None else now
    if hb is None:
        return Verdict(False, "no_heartbeat")
    age = clock - hb.last_success
    if age > max_age_s:
        return Verdict(False, "stale", age)
    if age < -max_age_s:
        # Heartbeat din viitor = ceas sărit între scriere și citire. Nu-l tratăm ca proaspăt:
        # ar fi exact modul în care un fișier vechi devine „valabil pentru totdeauna".
        return Verdict(False, "clock_skew", age)
    if not pid_alive(hb.pid):
        return Verdict(False, "dead_process", age)
    if own_boot_id is not None and hb.boot_id != own_boot_id:
        return Verdict(False, "foreign_boot", age)
    if hb.schema_compatible is False:
        return Verdict(False, "schema_incompatible", age)
    if hb.lease_loop_alive is False:
        return Verdict(False, "lease_loop_dead", age)
    return Verdict(True, "ok", age)


def main(argv: list[str] | None = None) -> int:
    """CLI: `python -m src.ops.worker_health --role worker --max-age 90`.

    Ăsta e healthcheckul din compose pentru procesele fără HTTP. Cod 0 = sănătos, 1 = nu.
    Scrie o linie JSON pe stdout (fără PII) ca `docker inspect` să arate ULTIMUL motiv, nu doar
    un cod de ieșire.
    """
    import argparse  # noqa: PLC0415 — doar pe calea de CLI

    ap = argparse.ArgumentParser(description="Health pentru procesele non-HTTP (NX-248)")
    ap.add_argument("--role", default="worker")
    ap.add_argument("--max-age", type=float, default=90.0)
    ap.add_argument("--path", default=None)
    args = ap.parse_args(argv)

    path = Path(args.path) if args.path else heartbeat_path(args.role)
    verdict = evaluate(read(path), max_age_s=args.max_age)
    print(  # noqa: T201 — CLI: stdout e chiar interfața
        json.dumps(
            {"role": args.role, "ok": verdict.ok, "reason": verdict.reason, "age_s": verdict.age_s},
            separators=(",", ":"),
        )
    )
    return 0 if verdict.ok else 1


def heartbeat_path(role: str) -> Path:
    """Un fișier per ROL: worker și scheduler pot rula în același container în dev, iar un fișier
    partajat ar face ca heartbeat-ul unuia să acopere moartea celuilalt."""
    return DEFAULT_PATH.with_name(f"nativx_health_{role}")


if __name__ == "__main__":
    raise SystemExit(main())
