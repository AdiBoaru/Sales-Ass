"""NX-148 — conversation_facts: memorie structurată per contact.

Facts stabile despre client (buget, tip de piele, mărime, brand, restricții), extrase post-tur
și injectate țintit în prompt. `select_whitelisted_facts` e PUR (whitelist + dedupe + cap,
testabil fără DB); `upsert_facts` / `fetch_relevant_facts` sunt tenant-scoped (P7). PII: nimic
din `fact_value` nu conține telefon/id canal (whitelist de tipuri + extractorul aruncă restul).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.models import Author, Direction, Message
from src.worker.summarizer import _redact_pii

if TYPE_CHECKING:
    import asyncpg

# Cap dur de facts injectate/persistate per contact (P4 — buget de context în cod).
MAX_FACTS = 10
# NX-148: fereastra extractorului post-tur de facts — 10 tururi = 20 mesaje (bot+client). Mai
# lată decât istoricul de context (8, P4), ca memoria să prindă fapte spuse mai devreme.
EXTRACTION_WINDOW = 20


def _clamp01(value: Any) -> float:
    """confidence forțat în [0, 1] — un extractor buggy/adversarial (confidence=999) nu poate
    domina sortarea sau injecta „memorie sigură" falsă."""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _redact_fact_value(value: Any) -> Any:
    """Redactare PII recursivă a valorii (P12): un `fact_type` PERMIS (ex. `restriction`) poate
    conține totuși telefon în text („nu suna la 0722…") — whitelist-ul de tip nu apără valoarea."""
    if isinstance(value, str):
        return _redact_pii(value)
    if isinstance(value, dict):
        return {k: _redact_fact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_fact_value(v) for v in value]
    return value


def select_whitelisted_facts(
    facts: list[dict[str, Any]], whitelist: frozenset[str] | set[str], *, cap: int = MAX_FACTS
) -> list[dict[str, Any]]:
    """PUR: păstrează doar `fact_type` din whitelist, dedupe per tip (ține confidence maxim +
    valoarea PERECHE cu el), clampează confidence în [0,1], redactează PII din valoare, ordonează
    pe confidence desc și taie la `cap`.

    **Fail-CLOSED**: whitelist gol → NICIUN fact (nu presupune permisiv — un `fact_type` inventat
    de model, ex. `phone`, e ARUNCAT). Plasa anti-halucinație + anti-PII de memorie (P12)."""
    best: dict[str, dict[str, Any]] = {}
    for f in facts:
        ftype = f.get("fact_type")
        if not ftype or ftype not in whitelist:  # fail-closed
            continue
        value = f.get("fact_value")
        if value in (None, "", {}, []):
            continue
        conf = _clamp01(f.get("confidence"))
        prev = best.get(ftype)
        # valoarea + confidence rămân PERECHE: o observație cu confidence mai mic NU suprascrie
        # valoarea celei cu confidence mai mare (altfel: „oily @ 0.95" = memorie falsă sigură).
        if prev is None or conf >= prev["confidence"]:
            best[ftype] = {**f, "confidence": conf, "fact_value": _redact_fact_value(value)}
    ordered = sorted(best.values(), key=lambda f: f["confidence"], reverse=True)
    return ordered[:cap]


def _row_memory_key(f: dict[str, Any]) -> str:
    """Cheia de deduplicare a unui rând de upsert. NX-160: `memory_key` explicit (calea v2 din
    `memory.process_facts`) sau derivat backcompat din `fact_type` (apelanți vechi / NX-148)."""
    mk = f.get("memory_key")
    if mk:
        return str(mk)
    base = f.get("canonical_key") or f.get("raw_key") or f.get("fact_type") or ""
    kind = "canonical" if (f.get("canonical_key") or f.get("fact_type")) else "raw"
    return f"{kind}:{str(base).strip().lower()}"


async def upsert_facts(
    conn: asyncpg.Connection,
    business_id: str,
    contact_id: str,
    conversation_id: str | None,
    facts: list[dict[str, Any]],
) -> int:
    """Upsert per (business_id, contact_id, **memory_key**): un fact re-menționat bump-uie
    `last_seen_at` + `max(confidence)`, nu duplică. Întoarce câte au fost scrise. `WHERE`-ul
    implicit e `business_id` (P7; RLS ca plasă). `fact_value` serializat în jsonb.

    NX-160: scrie coloanele v2 (`raw_key`/`canonical_key`/`memory_key`/`safety_class`/`visibility`).
    Backcompat: un apelant vechi care dă doar `fact_type` primește defaults sigure
    (raw=canonical=fact_type, visibility='inject', safety='safe') — comportamentul NX-148."""
    if not facts:
        return 0
    rows = []
    for f in facts:
        raw_key = f.get("raw_key") or f.get("fact_type")
        canonical_key = f.get("canonical_key")
        # fact_type e NOT NULL în schemă (alias backcompat) — îl ținem populat.
        fact_type = f.get("fact_type") or canonical_key or raw_key
        rows.append(
            (
                business_id,
                contact_id,
                conversation_id,
                fact_type,
                raw_key,
                canonical_key,
                _row_memory_key(f),
                json.dumps(_redact_fact_value(f.get("fact_value"))),  # P12: defensiv
                _clamp01(f.get("confidence") if f.get("confidence") is not None else 0.5),
                f.get("safety_class") or "safe",
                f.get("visibility") or "inject",
                f.get("source_message_id"),
                f.get("expires_at"),
            )
        )
    await conn.executemany(
        """
        insert into conversation_facts as cf
            (business_id, contact_id, conversation_id, fact_type, raw_key, canonical_key,
             memory_key, fact_value, confidence, safety_class, visibility,
             source_message_id, expires_at)
        values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13)
        on conflict (business_id, contact_id, memory_key) do update set
            -- valoarea/sursa/expirarea rămân PERECHE cu confidence-ul CÂȘTIGĂTOR: o observație cu
            -- confidence mai mic bump-uie doar last_seen, NU suprascrie valoarea sigură. `cf` =
            -- rândul existent (conversation_facts), `excluded` = cel nou.
            fact_value = case
                when excluded.confidence >= cf.confidence then excluded.fact_value
                else cf.fact_value end,
            confidence = greatest(cf.confidence, excluded.confidence),
            conversation_id = excluded.conversation_id,
            fact_type = excluded.fact_type,
            raw_key = excluded.raw_key,
            canonical_key = excluded.canonical_key,
            -- vizibilitatea/clasa urmează valoarea câștigătoare (o re-observație mai sigură poate
            -- promova un candidate la inject; una mai slabă nu retrogradează silent).
            safety_class = case
                when excluded.confidence >= cf.confidence then excluded.safety_class
                else cf.safety_class end,
            visibility = case
                when excluded.confidence >= cf.confidence then excluded.visibility
                else cf.visibility end,
            source_message_id = case
                when excluded.confidence >= cf.confidence then excluded.source_message_id
                else cf.source_message_id end,
            expires_at = case
                when excluded.confidence >= cf.confidence then excluded.expires_at
                else cf.expires_at end,
            last_seen_at = now()
        """,
        rows,
    )
    return len(rows)


async def fetch_relevant_facts(
    conn: asyncpg.Connection, business_id: str, contact_id: str, *, limit: int = MAX_FACTS
) -> list[dict[str, Any]]:
    """Facts INJECTABILE ne-expirate ale unui contact (tenant-scoped), ordonate pe confidence
    desc → last_seen desc. Pentru injectarea bugetată din `facts_block`.

    NX-160: DOAR `visibility='inject'` — PII/financial (drop) nici nu-s stocate, iar semnalele
    sensibile (`candidate`, ex. condiție medicală) sunt stocate dar NU ajung în prompt. Read
    path-ul e a doua plasă: chiar dacă ceva sensibil s-a strecurat, nu se injectează."""
    rows = await conn.fetch(
        """
        select fact_type, raw_key, canonical_key, fact_value, confidence, last_seen_at, expires_at
        from conversation_facts
        where business_id = $1 and contact_id = $2
          and visibility = 'inject'
          and (expires_at is null or expires_at > now())
        order by confidence desc, last_seen_at desc
        limit $3
        """,
        business_id,
        contact_id,
        min(limit, MAX_FACTS),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        value = r["fact_value"]
        if isinstance(value, str):
            value = json.loads(value) if value else None
        out.append(
            {
                # `fact_type` păstrat pt backcompat; `facts_block` preferă canonical_key/raw_key.
                "fact_type": r["fact_type"],
                "raw_key": r["raw_key"],
                "canonical_key": r["canonical_key"],
                "fact_value": value,
                "confidence": r["confidence"],
                "last_seen_at": r["last_seen_at"],
                "expires_at": r["expires_at"],
            }
        )
    return out


async def get_messages_for_extraction(
    conn: asyncpg.Connection, business_id: str, conversation_id: str, limit: int = EXTRACTION_WINDOW
) -> list[Message]:
    """Ultimele `limit` mesaje (cronologic crescător) pentru extractorul post-tur de facts
    (NX-148). Cap dur la EXTRACTION_WINDOW (20 = 10 tururi). Separat de `get_recent_messages`
    (contextul agentului rămâne la 8, P4) — fereastra mai lată e DOAR pentru extracția offline."""
    limit = min(limit, EXTRACTION_WINDOW)
    rows = await conn.fetch(
        """
        select direction, author, body, content_type, created_at
        from (
            select direction, author, body, content_type, created_at
            from messages
            where business_id = $1 and conversation_id = $2
            order by created_at desc
            limit $3
        ) recent
        order by created_at asc
        """,
        business_id,
        conversation_id,
        limit,
    )
    return [
        Message(
            direction=Direction(r["direction"]),
            author=Author(r["author"]),
            body=r["body"],
            content_type=r["content_type"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
