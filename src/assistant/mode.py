"""Cine deține turul: agentul unic sau calea de dinainte (NX-396). Modul e MIC și fără dependențe
grele, fiindcă îl citesc și straturile de dinaintea agentului (salut, alias, cache, reluarea unei
clarificări): într-o conversație a agentului ele nu răspund, ca tot textul să-l scrie agentul.

Agentul e calea PRINCIPALĂ (decizia lui Adi, 2026-10-09): pornit implicit, pe toate conversațiile
(`ASSISTANT_CANARY_PERCENT=100`). Kernelul și calea v1 rămân plasa unui tur pe care agentul nu-l
poate servi. Rămân înaintea lui doar porțile (moderare, `bot_active`, rate limit) și butoanele
semnate (`action_kernel`, NX-236: o decizie deja luată, nu text de interpretat).
"""

from __future__ import annotations

import hashlib
from typing import Any


def canary_bucket(business_id: str, conversation_id: str | None) -> int:
    """Bucket-ul STICKY al conversației, cu salt propriu (independent de canary-ul kernelului)."""
    raw = f"nx396:{business_id}:{conversation_id or ''}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) % 100


def assistant_mode(settings: Any, business: Any, conversation_id: str | None) -> str:
    """`serve` pe conversațiile agentului, altfel `off`. PUR.

    Creierul unic și bugetele de tur (NX-241) revendică și ele turul, respectiv îl taie la 1-3
    runde: cu oricare aprins, agentul se dă la o parte în loc să pice la boot (agentul e pornit
    implicit, deci un `.env` vechi nu are voie să oprească serviciul)."""
    if not getattr(settings, "assistant_agent_enabled", False):
        return "off"
    if getattr(settings, "single_brain_enabled", False) or getattr(
        settings, "turn_budget_enforced", False
    ):
        return "off"
    tenants = {t.strip() for t in (settings.assistant_tenants or "").split(",") if t.strip()}
    if tenants and getattr(business, "slug", None) not in tenants:
        return "off"
    bucket = canary_bucket(str(business.id), conversation_id)
    return "serve" if bucket < settings.assistant_canary_percent else "off"


def owns_turn(ctx: Any, deps: Any, settings: Any) -> bool:
    """Turul e al agentului: conversația e a lui, există un model și nu e un buton semnat."""
    if deps is None or getattr(deps, "llm", None) is None:
        return False
    from src.web.action_models import action_command  # noqa: PLC0415

    if action_command(ctx) is not None:
        return False
    return assistant_mode(settings, ctx.business, ctx.conversation_id) == "serve"
