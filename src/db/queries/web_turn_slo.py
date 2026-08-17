"""NX-246 — faptele de SLI, citite din ledgerul `web_turns`. Tenant-scoped, fără conținut.

Ledgerul (NX-232) e singura sursă durabilă care știe ce s-a promis și ce s-a livrat: accept,
attempt, deadline, terminal, cod de eroare. Cardul cere explicit să NU construim un al doilea
ledger în analytics — deci raportul de SLO citește de aici, nu dintr-un flux de evenimente care
poate pierde rânduri fără să știe nimeni.

Două reguli care se văd direct în SQL:

  • **`business_id = $1` peste tot** (P7). Un raport operațional e tentat să fie cross-tenant
    „fiindcă e doar o metrică"; dar `response_json` e conținut de conversație, iar un query fără
    tenant care atinge coloana asta e exact bug-ul de izolare pe care RLS-ul îl prinde ultimul.
    Fleet-wide se face rulând raportul per tenant, nu relaxând poarta.

  • **`renderable` se calculează ÎN SQL.** Ar fi fost mai simplu să aducem `response_json` și să
    reevaluăm în Python cu `turn_service.renderable` — și ar fi însemnat să scoatem răspunsurile
    clienților din DB, în memoria unui proces de raportare, de unde ajung într-un artefact de CI
    sau într-un traceback. Expresia de mai jos oglindește exact acea funcție, dar nu scoate niciun
    octet de conținut din baza de date.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from src.observability.slo import TurnFact

log = logging.getLogger(__name__)


def _truthy(path: str) -> str:
    """Oglinda în SQL a adevărului „pythonic" folosit de `turn_service.renderable`.

    `view.get("products")` e falsy pentru `None`, `[]`, `{}`, `""`, `0`, `false`. Un simplu
    `is not null` ar declara randabil un `{"products": []}` — adică exact terminalul gol pe care
    P6 îl interzice, marcat verde de propriul nostru indicator.
    """
    return (
        f"({path} is not null and {path} <> 'null'::jsonb and {path} <> '\"\"'::jsonb "
        f"and {path} <> '[]'::jsonb and {path} <> '{{}}'::jsonb and {path} <> '0'::jsonb "
        f"and {path} <> 'false'::jsonb)"
    )


_RENDERABLE = (
    "case when w.response_json is null then false else ("
    + " or ".join(
        _truthy(f"w.response_json->'{key}'")
        for key in ("content", "products", "comparison", "offer")
    )
    + ") end"
)


@dataclass(frozen=True)
class TurnFactPage:
    """Faptele + dacă am văzut TOT. `truncated=True` degradează verdictele la `UNKNOWN`:
    un procent calculat pe primele N rânduri nu e procentul ferestrei."""

    facts: list[TurnFact]
    truncated: bool
    total: int


async def load_turn_facts(
    conn: asyncpg.Connection,
    business_id: str,
    *,
    window_from: datetime,
    window_to: datetime,
    limit: int = 50_000,
) -> TurnFactPage:
    """Faptele de SLI ale unei ferestre, pentru UN tenant. Zero conținut, zero PII.

    Fereastra se filtrează pe `accepted_at` (momentul promisiunii), nu pe `completed_at`: altfel
    turele care încă rulează la marginea ferestrei ar dispărea din numitor, iar disponibilitatea
    ar crește exact atunci când sistemul e lent — indicatorul ar minți fix în incident.
    """
    if not await conn.fetchval("select to_regclass('public.web_turns') is not null"):
        log.warning("SLO: web_turns lipsește (migrarea 040 neaplicată) — raport fără date")
        return TurnFactPage([], False, 0)
    total = await conn.fetchval(
        "select count(*) from web_turns "
        "where business_id = $1 and accepted_at >= $2 and accepted_at < $3",
        business_id,
        window_from,
        window_to,
    )
    rows = await conn.fetch(
        f"""
        select w.status,
               w.safe_error_code,
               w.attempt,
               w.accepted_at,
               w.completed_at,
               w.deadline_at,
               {_RENDERABLE} as renderable
          from web_turns w
         where w.business_id = $1
           and w.accepted_at >= $2
           and w.accepted_at < $3
         order by w.accepted_at
         limit $4
        """,
        business_id,
        window_from,
        window_to,
        limit,
    )
    facts = [
        TurnFact(
            status=r["status"],
            safe_error_code=r["safe_error_code"],
            attempt=int(r["attempt"]),
            accepted_at=r["accepted_at"],
            completed_at=r["completed_at"],
            deadline_at=r["deadline_at"],
            renderable=bool(r["renderable"]),
        )
        for r in rows
    ]
    return TurnFactPage(facts, truncated=int(total or 0) > len(facts), total=int(total or 0))
