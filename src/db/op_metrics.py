"""NX-231 — contabilitatea checkout-urilor DB pe OPERAȚIE (nu pe tur).

Cu conn-per-op conexiunea nu mai aparține turului, ci operației. Măsurătoarea trebuie să se
mute odată cu proprietatea: `pool_metrics` (NX-161 Felia 0A) raporta UN acquire-wait per tur,
fiindcă exista un singur checkout. Acum sunt N, iar întrebarea utilă e „care operație ține
conexiunea și cât".

Trei mărimi, per operație:

  • `checkout_ms` — cât a durat obținerea conexiunii (acquire + `set_config` de izolare). Ăsta e
    semnalul de CONTENȚIE: crește când poolul e plin.
  • `hold_ms`     — cât a stat conexiunea checkout-uită (yield → release). Cu conn-per-op ar
    trebui să fie ~`query_ms`; orice diferență mare = un await extern strecurat înăuntru.
  • `query_ms`    — cât s-a executat efectiv SQL. Măsurat DOAR în mod diagnostic
    (`DB_QUERY_TIMING_ENABLED`), printr-un proxy peste conexiune: pe calea fierbinte nu punem
    un wrapper pe fiecare query pentru o cifră de raport.

`idle_held_ms = hold_ms - query_ms` e cifra care demonstrează fix-ul (înainte: ~79% din hold era
idle, docs/CONN-HOLD-ANALYSIS-2026.md). Fără timing e `None` — raportăm „nu știm", nu 0.

P10 (runner/processor măsoară, stagiile nu știu) + P12 (ZERO PII: nume de operație din cod,
milisecunde, contoare). ContextVar ca `src/agent/usage.py` → două tururi concurente nu-și
amestecă valorile.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

# Cap dur de chei raportate (P4): un bug care generează nume dinamice de operație nu are voie
# să umfle un event de analytics. Operațiile sunt literali din cod → în practică ~25.
MAX_OPERATIONS = 40

# Nume folosit când apelantul nu etichetează checkout-ul. Vizibil în raport ca datorie, nu ascuns.
UNLABELED = "unlabeled"


@dataclass
class OpStats:
    """Agregat per operație (un singur tur)."""

    n: int = 0
    checkout_ms: float = 0.0
    hold_ms: float = 0.0
    query_ms: float = 0.0
    queries: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "checkout_ms": round(self.checkout_ms, 2),
            "hold_ms": round(self.hold_ms, 2),
            "query_ms": round(self.query_ms, 2),
            "queries": self.queries,
        }


@dataclass
class DbOpAccumulator:
    """Acumulator per tur. `timed` = a rulat cu proxy de timing → `query_ms` e real."""

    by_op: dict[str, OpStats] = field(default_factory=dict)
    timed: bool = False
    dropped: int = 0  # operații peste MAX_OPERATIONS (raportate ca număr, nu ca nume)

    def record(
        self, operation: str, *, checkout_ms: float, hold_ms: float, query_ms: float, queries: int
    ) -> None:
        row = self.by_op.get(operation)
        if row is None:
            if len(self.by_op) >= MAX_OPERATIONS:
                self.dropped += 1
                return
            row = self.by_op[operation] = OpStats()
        row.n += 1
        row.checkout_ms += checkout_ms
        row.hold_ms += hold_ms
        row.query_ms += query_ms
        row.queries += queries

    @property
    def checkouts(self) -> int:
        return sum(r.n for r in self.by_op.values())

    @property
    def hold_ms(self) -> float:
        return sum(r.hold_ms for r in self.by_op.values())

    @property
    def checkout_ms(self) -> float:
        return sum(r.checkout_ms for r in self.by_op.values())

    @property
    def query_ms(self) -> float:
        return sum(r.query_ms for r in self.by_op.values())

    @property
    def idle_held_ms(self) -> float | None:
        """Timp cu conexiunea ținută degeaba. `None` fără timing — nu inventăm un 0 liniștitor."""
        if not self.timed:
            return None
        return max(0.0, self.hold_ms - self.query_ms)

    def as_event_props(self) -> dict[str, Any]:
        """Props pentru evenimentul `db_ops` (un event per tur, ca `llm_usage`)."""
        props: dict[str, Any] = {
            "checkouts": self.checkouts,
            "db_checkout_ms": round(self.checkout_ms, 2),
            "db_hold_ms": round(self.hold_ms, 2),
            "by_operation": {k: v.as_dict() for k, v in sorted(self.by_op.items())},
        }
        if self.timed:
            props["db_query_ms"] = round(self.query_ms, 2)
            props["db_idle_held_ms"] = round(self.idle_held_ms or 0.0, 2)
        if self.dropped:
            props["operations_dropped"] = self.dropped
        return props


_current: contextvars.ContextVar[DbOpAccumulator | None] = contextvars.ContextVar(
    "db_op_accumulator", default=None
)


def push() -> tuple[DbOpAccumulator, contextvars.Token]:
    """Deschide un acumulator pentru turul curent. Simetric cu `usage.push()`."""
    acc = DbOpAccumulator()
    return acc, _current.set(acc)


def pop(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> DbOpAccumulator | None:
    """Acumulatorul turului curent, sau None (checkout în afara unui tur — job, script, boot)."""
    return _current.get()


def record(
    operation: str,
    *,
    checkout_ms: float,
    hold_ms: float,
    query_ms: float = 0.0,
    queries: int = 0,
) -> None:
    """Raportează un checkout încheiat. No-op în afara unui tur (nu ținem un registru global)."""
    acc = _current.get()
    if acc is None:
        return
    acc.record(
        operation or UNLABELED,
        checkout_ms=checkout_ms,
        hold_ms=hold_ms,
        query_ms=query_ms,
        queries=queries,
    )


class TimedConn:
    """Proxy diagnostic peste conexiune care ACUMULEAZĂ timpul petrecut în SQL.

    Există ca să putem separa `hold` de `query` (adică idle-held-ul, cifra care demonstrează
    conn-per-op). Nu e pe calea fierbinte by default: se activează cu `DB_QUERY_TIMING_ENABLED`,
    exact pentru rularea de raport (`scripts/sim/conn_hold_probe.py`).

    Tot ce nu e o metodă de execuție se delegă prin `__getattr__` — inclusiv `.transaction()`,
    care întoarce un `Transaction` legat de conexiunea REALĂ. BEGIN/COMMIT-ul emis de el rămâne
    nemăsurat (rezidual mic, contat ca idle → estimarea e conservatoare)."""

    __slots__ = ("_conn", "query_ms", "queries")

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.query_ms = 0.0
        self.queries = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def _timed(self, meth, *a, **kw):
        t0 = perf_counter()
        try:
            return await meth(*a, **kw)
        finally:
            self.query_ms += (perf_counter() - t0) * 1000.0
            self.queries += 1

    async def execute(self, *a, **kw):
        return await self._timed(self._conn.execute, *a, **kw)

    async def executemany(self, *a, **kw):
        return await self._timed(self._conn.executemany, *a, **kw)

    async def fetch(self, *a, **kw):
        return await self._timed(self._conn.fetch, *a, **kw)

    async def fetchrow(self, *a, **kw):
        return await self._timed(self._conn.fetchrow, *a, **kw)

    async def fetchval(self, *a, **kw):
        return await self._timed(self._conn.fetchval, *a, **kw)
