"""Debounce adaptiv (stagiul 2, R1) — mesajele rapide ale aceluiași user se
coalesc într-un SINGUR tur, ca botul să răspundă o dată cu tot contextul, nu de
N ori (lot de mesaje, nu N tururi independente).

Mecanism: per (canal, cont, expeditor) ținem un buffer de evenimente + un timer.
Fiecare mesaj nou resetează timerul; după `delay` secunde fără mesaj nou, golim
buffer-ul și procesăm evenimentele combinate (body = mesajele lipite cu newline,
provider_msg_id = ultimul, restul câmpurilor din ultimul). Statusurile NU se
debounce-uiesc — doar mesajele.

Trade-off (canal de TEST): buffer-ul e în memoria procesului. Un crash între
buffering și flush pierde mesajele ne-procesate (deja ACK-uite pe stream). O
variantă durabilă (buffer în Redis) = follow-up dacă mutăm pe producție.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0


class Debouncer:
    """Coalesce evenimente inbound per expeditor, cu timer resetabil."""

    def __init__(
        self,
        process: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        delay: float = DEBOUNCE_SECONDS,
    ) -> None:
        self._process = process
        self._delay = delay
        self._buffers: dict[tuple, list[dict[str, Any]]] = {}
        self._timers: dict[tuple, asyncio.Task] = {}

    @staticmethod
    def _key(event: dict[str, Any]) -> tuple:
        return (
            event.get("channel_kind"),
            event.get("channel_account_id"),
            event.get("sender_external_id"),
        )

    async def add(self, event: dict[str, Any]) -> None:
        """Adaugă un mesaj în buffer-ul expeditorului și (re)pornește timerul."""
        key = self._key(event)
        self._buffers.setdefault(key, []).append(event)
        old = self._timers.get(key)
        if old is not None:
            old.cancel()
        self._timers[key] = asyncio.create_task(self._flush_later(key))

    async def _flush_later(self, key: tuple) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return  # a venit un mesaj nou → timerul vechi e anulat, nu procesăm
        events = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        if not events:
            return
        combined = self._combine(events)
        try:
            await self._process(combined)
        except Exception:  # noqa: BLE001 — un lot stricat nu omoară worker-ul
            log.exception("debounce: procesarea lotului a eșuat")

    @staticmethod
    def _combine(events: list[dict[str, Any]]) -> dict[str, Any]:
        """Un singur eveniment din lot: body = mesajele lipite cu newline; restul
        câmpurilor (provider_msg_id, timestamp, ...) din ULTIMUL mesaj."""
        base = dict(events[-1])
        bodies = [e.get("body") for e in events if (e.get("body") or "").strip()]
        if bodies:
            base["body"] = "\n".join(bodies)
        return base
