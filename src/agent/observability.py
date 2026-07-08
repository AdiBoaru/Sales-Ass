"""NX-146 felia 2 — evenimentul `agent_prompt` pentru Turn Replay.

Corelează, per tur, VERSIUNEA promptului (hash) cu grounding-ul (retrieval IDs) — fără a
persista corpul promptului în DB (PII + volum). `prompt_hash` permite legarea de o versiune de
prompt dintr-un registry (viitor); `retrieval_ids` arată exact ce produse au alimentat
răspunsul. Corpul redactat se scrie DOAR sub kill-switch (`replay_store_prompt_enabled`,
default OFF; P12). Funcție PURĂ → testabilă fără pipeline; `agent_stage` doar o cheamă + emite.
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.worker.summarizer import _redact_pii


def _retrieval_ids(retrieved: Any) -> list[str]:
    """Product IDs din setul retrieval al turului (păstrează ordinea, dedupe stabil)."""
    ids: list[str] = []
    seen: set[str] = set()
    for p in retrieved or []:
        pid = p.get("product_id") or p.get("id") if isinstance(p, dict) else None
        if pid and str(pid) not in seen:
            seen.add(str(pid))
            ids.append(str(pid))
    return ids


def agent_prompt_event(
    system: str, user: str, retrieved: Any, *, store_prompt: bool = False
) -> dict[str, Any]:
    """Proprietățile evenimentului `agent_prompt` (PUR, fără I/O).

    `prompt_hash` = sha256(system + "\\n" + user) — corelare cu o versiune de prompt, fără
    corpul în DB. `retrieval_ids` = grounding-ul. `prompt_rendered` (redactat) DOAR când
    `store_prompt` e True (kill-switch OFF by default)."""
    rendered = f"{system}\n{user}"
    props: dict[str, Any] = {
        "prompt_hash": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "retrieval_ids": _retrieval_ids(retrieved),
    }
    if store_prompt:
        props["prompt_rendered"] = _redact_pii(rendered)
    return props
