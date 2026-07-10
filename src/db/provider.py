"""Felia 0B (NX-161) — provider DB tenant-scoped: conexiunea aparține OPERAȚIEI, nu turului.

Ținta epic-ului (docs/CONN-HOLD-ANALYSIS-2026.md): stagiile/tool-urile nu mai primesc un `conn` viu
lung, ci un PROVIDER de la care iau o conexiune DOAR cât ține operația:

    async with deps.db() as conn:   # checkout scurt
        ...                          # între operații (LLM) — ZERO conn ținut

Felia 0B adaugă DOAR infrastructura — niciun stagiu nu apelează încă `deps.db()` (zero schimbare de
runtime). Migrarea propriu-zisă (aftercare → free layers → agent tools) vine în feliile următoare.

Contract: un provider = un callable fără argumente care întoarce un async context manager ce
yield-uiește o `Connection`. `business_id` e LEGAT la construcție (providerul e tenant-scoped, ca
`tenant_conn`), deci `db()` nu-l primește ca argument.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import asyncpg

from src.db.connection import tenant_conn

DbProvider = Callable[[], AbstractAsyncContextManager[asyncpg.Connection]]


def tenant_db(business_id: str) -> DbProvider:
    """PROD: fiecare `db()` = checkout scurt REAL din `bot_pool` prin `tenant_conn` (setează
    `app.business_id` + assert izolare NX-04 + reset la release). `business_id` legat aici →
    providerul e tenant-scoped, exact ca `tenant_conn`."""

    @asynccontextmanager
    async def _cm() -> AsyncIterator[asyncpg.Connection]:
        async with tenant_conn(business_id) as conn:
            yield conn

    return _cm


def static_db(conn: object) -> DbProvider:
    """COMPAT/TEST: `db()` yield-uiește un conn DEJA deschis (injectat), FĂRĂ checkout nou și fără
    să-l închidă. Folosit de puntea de compat (`PipelineDeps.__post_init__` pentru cele 114
    `PipelineDeps(conn=...)` din teste) și de testele care injectează un conn/fake explicit."""

    @asynccontextmanager
    async def _cm() -> AsyncIterator[object]:
        yield conn

    return _cm
