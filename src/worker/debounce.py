"""Debounce adaptiv (stagiul 2, R1) — mesajele rapide ale aceluiași user se
coalesc într-un SINGUR tur, ca botul să răspundă o dată cu tot contextul, nu de
N ori (lot de mesaje, nu N tururi independente).

Mecanism: per (canal, cont, expeditor) ținem un buffer de evenimente + un timer.
Fiecare mesaj nou resetează timerul; după `delay` secunde fără mesaj nou, golim
buffer-ul și procesăm evenimentele combinate (body = mesajele lipite cu newline,
provider_msg_id = ultimul, restul câmpurilor din ultimul). Statusurile NU se
debounce-uiesc — doar mesajele.

Durabilitate (NX-87): mesajele NU se ACK-uiesc pe stream la citire, ci DUPĂ ce lotul a fost
procesat cu succes (`ack` callback primește toate `msg_id`-urile lotului). Un crash între
buffering și flush lasă mesajele NE-ACK-uite în PEL → reclamate de reaper (XAUTOCLAIM, NX-86) sau
re-livrate, NU pierdute tăcut (P6). Un flush eșuat → fără ACK (rămân pending). Buffer-ul rămâne în
memorie (coalescing-ul e best-effort); durabilitatea stă pe ACK-after-flush, nu pe buffer.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 3.0


async def _noop_ack(msg_ids: list[str]) -> None:
    """ACK implicit (no-op) — pentru caller-ii fără stream (teste vechi / non-Redis)."""


class Debouncer:
    """Coalesce evenimente inbound per expeditor, cu timer resetabil + ACK după flush."""

    def __init__(
        self,
        process: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        delay: float = DEBOUNCE_SECONDS,
        ack: Callable[[list[str]], Awaitable[None]] = _noop_ack,
    ) -> None:
        self._process = process
        self._delay = delay
        self._ack = ack
        # buffer: per expeditor, perechi (event, msg_id) — msg_id pt ACK-after-flush (NX-87)
        self._buffers: dict[tuple, list[tuple[dict[str, Any], str | None]]] = {}
        self._timers: dict[tuple, asyncio.Task] = {}

    @staticmethod
    def _key(event: dict[str, Any]) -> tuple:
        return (
            event.get("channel_kind"),
            event.get("channel_account_id"),
            event.get("sender_external_id"),
        )

    async def add(self, event: dict[str, Any], msg_id: str | None = None) -> None:
        """Adaugă un mesaj (+ `msg_id` de stream) în buffer și (re)pornește timerul. `msg_id` se
        ACK-uiește DOAR după ce lotul e procesat cu succes (durabilitate, NX-87)."""
        key = self._key(event)
        self._buffers.setdefault(key, []).append((event, msg_id))
        old = self._timers.get(key)
        if old is not None:
            old.cancel()
        self._timers[key] = asyncio.create_task(self._flush_later(key))

    async def _flush_later(self, key: tuple) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return  # a venit un mesaj nou → timerul vechi e anulat, nu procesăm
        items = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        if not items:
            return
        events = [e for e, _ in items]
        msg_ids = [m for _, m in items if m is not None]
        combined = self._combine(events)
        try:
            await self._process(combined)
        except Exception:  # noqa: BLE001 — un lot stricat nu omoară worker-ul
            # FĂRĂ ACK: mesajele rămân pending (PEL) → reaper/re-livrare (P6), nu pierdute.
            log.exception("debounce: procesarea lotului a eșuat — fără ACK (rămâne pending)")
            return
        if msg_ids:
            try:
                await self._ack(msg_ids)
            except Exception:  # noqa: BLE001 — ACK eșuat → mesajele rămân pending (re-livrare)
                log.warning("debounce: ACK lot eșuat — mesajele rămân pending")

    @staticmethod
    def _combine(events: list[dict[str, Any]]) -> dict[str, Any]:
        """Un singur eveniment din lot: body = mesajele lipite cu newline; restul
        câmpurilor (provider_msg_id, timestamp, ...) din ULTIMUL mesaj."""
        base = dict(events[-1])
        bodies = [e.get("body") for e in events if (e.get("body") or "").strip()]
        if bodies:
            base["body"] = "\n".join(bodies)
        return base
