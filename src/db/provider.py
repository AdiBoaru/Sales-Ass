"""Provider DB tenant-scoped: conexiunea aparține OPERAȚIEI, nu turului (NX-161 → NX-231).

NX-161 Felia 0B a adăugat infrastructura; NX-231 o face regulă. Stagiile/tool-urile nu mai
primesc un `conn` viu ținut cât ține turul, ci un PROVIDER de la care iau o conexiune DOAR cât
durează operația:

    async with deps.db("search_products") as conn:   # checkout scurt, ETICHETAT
        ...                                          # între operații (LLM/HTTP) — ZERO conn ținut

Contract: un provider = un callable care întoarce un async context manager ce yield-uiește o
`Connection`. `business_id` e LEGAT la construcție (providerul e tenant-scoped, ca `tenant_conn`),
deci `db()` nu-l primește ca argument — un tool controlat de model nu are cum să-l schimbe (P7:
`business_id` e SERVER-OWNED).

Eticheta `operation` e opțională la nivel de tip (compat cu apelurile vechi `db()`), dar toate
căile de runtime o dau: fără ea, `db_checkout_ms{operation}` / `db_hold_ms{operation}` (NX-231)
nu spun nimic despre CINE ține conexiunea.

Invariantul pe care îl apără cardul: niciun `async with db(...)` nu are voie să conțină un await
extern (LLM / embed / moderation / media / HTTP de provider / backoff / SSE). Structura e mereu
`read scurt → (extern, fără conn) → write scurt`. Guard mecanic: `scripts/check_no_raw_conn.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from time import perf_counter
from typing import Any

import asyncpg

from src.config import get_settings
from src.db import op_metrics
from src.db.connection import tenant_conn

# Callable[[str | None], ACM]: eticheta e pozițională/opțională → `db()` vechi rămâne valid.
DbProvider = Callable[..., AbstractAsyncContextManager[asyncpg.Connection]]


def tenant_db(business_id: str) -> DbProvider:
    """PROD: fiecare `db(op)` = checkout scurt REAL din `bot_pool` prin `tenant_conn` (setează
    `app.business_id` + assert de izolare NX-04 + reset la release). `business_id` legat aici →
    providerul e tenant-scoped, exact ca `tenant_conn`, și nu poate fi rebindat de apelant.

    Măsoară per checkout (NX-231): `checkout_ms` (acquire + set_config = contenția) și `hold_ms`
    (yield → release). În mod diagnostic (`DB_QUERY_TIMING_ENABLED`) adaugă `query_ms` printr-un
    proxy → `idle_held_ms = hold - query` devine măsurabil."""

    @asynccontextmanager
    async def _cm(operation: str = op_metrics.UNLABELED) -> AsyncIterator[Any]:
        timing = get_settings().db_query_timing_enabled
        t_checkout = perf_counter()
        async with tenant_conn(business_id) as conn:
            checkout_ms = (perf_counter() - t_checkout) * 1000.0
            handle = op_metrics.TimedConn(conn) if timing else conn
            t_hold = perf_counter()
            try:
                yield handle
            finally:
                hold_ms = (perf_counter() - t_hold) * 1000.0
                acc = op_metrics.current()
                if acc is not None:
                    if timing:
                        acc.timed = True
                    op_metrics.record(
                        operation,
                        checkout_ms=checkout_ms,
                        hold_ms=hold_ms,
                        query_ms=getattr(handle, "query_ms", 0.0),
                        queries=getattr(handle, "queries", 0),
                    )

    _cm.shared_connection = False  # NX-241: checkout REAL per operație → operații concurente OK
    return _cm


def is_shared_connection(db: DbProvider | None) -> bool:
    """NX-241: providerul yield-uiește ACEEAȘI conexiune tuturor apelanților?

    Un `static_db` (teste, sim, punte de compat) dă un singur `asyncpg.Connection`, care nu suportă
    două operații simultane — deci pe el paralelismul de citiri rămâne 1, oricât ar spune flagul.
    Necunoscut (provider din afara `src/db/provider.py`) = tratat ca partajat: conservator."""
    return bool(getattr(db, "shared_connection", True))


def static_db(conn: object) -> DbProvider:
    """COMPAT/TEST: `db()` yield-uiește un conn DEJA deschis (injectat), FĂRĂ checkout nou și fără
    să-l închidă. Folosit de puntea de compat din `PipelineDeps.__post_init__` (cele ~140 de
    `PipelineDeps(conn=...)` din teste) și de testele care injectează un fake explicit.

    Acceptă și `conn=None`: multe teste de stagiu construiesc `PipelineDeps()` fără conexiune și
    monkeypatch-uiesc query-ul. Providerul static yield-uiește atunci `None` — exact ce primea
    query-ul stub prin `deps.conn` înainte de migrare (compat byte-identic, nu un `TypeError`)."""

    @asynccontextmanager
    async def _cm(operation: str = op_metrics.UNLABELED) -> AsyncIterator[Any]:  # noqa: ARG001
        yield conn

    _cm.shared_connection = True  # NX-241: o singură conexiune pentru toți → fără paralelism
    return _cm


@asynccontextmanager
async def db_tx(db: DbProvider, operation: str) -> AsyncIterator[Any]:
    """Checkout scurt + tranzacție, într-un singur pas.

    Forma canonică pentru „mai multe query-uri care trebuie să fie atomice" (contractul UoW din
    NX-231): o metodă de repository/service deschide UN checkout și O tranzacție, în loc să țină
    conexiunea între apeluri de tool. Robust la conexiuni fake din teste care nu expun
    `transaction()` — atunci rulează fără tranzacție (comportamentul de dinainte)."""
    async with db(operation) as conn:
        tx = getattr(conn, "transaction", None)
        if tx is None:
            yield conn
            return
        async with tx():
            yield conn
