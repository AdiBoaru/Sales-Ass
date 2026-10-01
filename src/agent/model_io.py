"""NX-366 — ce a ieșit din model pe un tur, înregistrat ca turul să poată fi rejucat fără model.

De ce: verificarea unei reparații pe un tur real cerea până acum o nouă rulare cu modelul, care
formulează altfel de fiecare dată, deci un tur care „trecea" putea trece din noroc. Cu ieșirile
înregistrate, replay-ul (`src/evals/trace_replay.py`) rulează CODUL NOSTRU pe exact ce a scris
modelul atunci, iar un defect reparat se vede pe mecanism.

Contractul (P10, ca `src.agent.usage`): adaptorul de model raportează aici fiecare apel LOGIC (după
retry, nu pe încercare); runner-ul deschide acumulatorul pe durata calculului turului (`push`/`pop`)
și îl pune în `ctx.trace["model_io"]`. Stagiile nu știu că sunt înregistrate. Aftercare-ul nu e
înregistrat: nu face parte din tur.

Ce NU face: nu intră în analytics (P12). Rândul stă în `conversation_traces` (retenție 30 de zile,
GDPR prin `delete_traces_for_contact`), iar textele trec prin frontiera NX-230 (`make_safe`), ca
restul traceului. Id-urile de produs (UUID) nu sunt atinse de redactare (verificat în test).
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from src.agent import usage

log = logging.getLogger(__name__)

#: Plafoanele (P4). Un apel de compunere bogată are ~3.000 de tokeni de ieșire (~12 KB), iar un tur
#: de recomandare face 3-5 apeluri: 64 KB pe apel și 256 KB pe tur prind doar cazurile patologice
#: (embeddinguri, o buclă). Peste plafon nu se taie JSON-ul la mijloc: răspunsul se înlocuiește cu
#: un marcaj, iar turul se declară nerejucabil.
MAX_CALL_BYTES = 64 * 1024
MAX_TURN_BYTES = 256 * 1024

#: Endpointurile furnizorului pe care le folosește `LLMClient`. Vocabular ÎNCHIS: replay-ul
#: reconstruiește tipul de răspuns după el, iar testul AST din `test_nx366_model_io_capture` cere ca
#: fiecare `self._client.<endpoint>.create` din `llm.py` să fie aici și să fie înregistrat.
ENDPOINTS = ("chat.completions", "responses", "moderations", "embeddings")

#: Versiunea formei înregistrate. Replay-ul refuză o formă pe care n-o cunoaște (`not_replayable`).
FORMAT_VERSION = 1


@dataclass
class ModelIOAccumulator:
    calls: list[dict[str, Any]] = field(default_factory=list)
    bytes: int = 0
    truncated: bool = False
    #: Apeluri care n-au putut fi serializate: turul nu mai e rejucabil, dar nu se pierde tăcut.
    failed: int = 0

    def as_trace(self) -> dict[str, Any]:
        return {
            "v": FORMAT_VERSION,
            "calls": self.calls,
            "replayable": not self.truncated and not self.failed,
            "truncated": self.truncated,
            "failed": self.failed,
        }


_current: contextvars.ContextVar[ModelIOAccumulator | None] = contextvars.ContextVar(
    "model_io", default=None
)


def push() -> tuple[ModelIOAccumulator, contextvars.Token]:
    acc = ModelIOAccumulator()
    return acc, _current.set(acc)


def pop(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> ModelIOAccumulator | None:
    return _current.get()


def endpoint_for(kwargs: dict[str, Any]) -> str:
    """Endpointul unei cereri de GENERARE, din argumentele ei (PUR). `input` există doar pe
    `/v1/responses` (aceeași regulă ca `usage.request_shape`)."""
    return "responses" if "input" in kwargs else "chat.completions"


def request_key(endpoint: str, kwargs: dict[str, Any] | None) -> dict[str, Any]:
    """Ce identifică o cerere la replay, fără conținutul ei: forma, scopul, schema și numele
    uneltelor. Promptul NU intră: o reparație îl poate schimba legitim, iar replay-ul trebuie să
    joace aceeași ieșire peste el. Ce schimbă NATURA cererii (alt scop, altă formă) e divergență."""
    if not kwargs or endpoint not in ("chat.completions", "responses"):
        return {"shape": None, "purpose": None, "schema": None, "tools": []}
    fmt = kwargs.get("response_format")
    schema = None
    if isinstance(fmt, dict) and isinstance(fmt.get("json_schema"), dict):
        schema = fmt["json_schema"].get("name")
    tools = []
    for t in kwargs.get("tools") or []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str):
                tools.append(name)
    return {
        "shape": usage.request_shape(kwargs),
        "purpose": usage.request_purpose(kwargs),
        "schema": schema,
        "tools": sorted(tools),
    }


def _redacted(value: Any, degraded: list[bool]) -> Any:
    """Frontiera NX-230 pe fiecare text al răspunsului. O redactare degradată (detector picat,
    text peste plafonul de scanare) pune un substituent în locul textului: răspunsul înregistrat
    nu mai e cel al modelului, deci turul nu mai e rejucabil (`degraded` o spune apelantului).
    Import leneș: cu flagul stins modulul de privacy nu costă nimic."""
    from src.privacy.boundary import make_safe  # noqa: PLC0415

    if isinstance(value, str):
        safe = make_safe(value)
        if safe.degraded:
            degraded.append(True)
        return safe.text
    if isinstance(value, dict):
        return {k: _redacted(v, degraded) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, degraded) for v in value]
    return value


def _size(row: dict[str, Any]) -> int:
    return len(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str).encode())


def _append(acc: ModelIOAccumulator, row: dict[str, Any]) -> None:
    """Plafoanele se judecă pe răspunsul BRUT, înaintea redactării: un text peste plafon nu se
    redactează degeaba și nu se taie la mijloc, ci se înlocuiește cu un marcaj."""
    row["i"] = len(acc.calls)
    size = _size(row)
    if size > MAX_CALL_BYTES or acc.bytes + size > MAX_TURN_BYTES:
        row = {k: v for k, v in row.items() if k != "response"}
        row["response"] = {"truncated": True, "bytes": size}
        acc.truncated = True
        size = _size(row)
    elif "response" in row:
        degraded: list[bool] = []
        row["response"] = _redacted(row["response"], degraded)
        if degraded:
            acc.failed += 1
            row["redaction_degraded"] = True
    acc.bytes += size
    acc.calls.append(row)


def record_ok(endpoint: str, kwargs: dict[str, Any] | None, resp: Any) -> None:
    """Un apel reușit. Best-effort (P6): o serializare picată marchează turul nerejucabil."""
    acc = _current.get()
    if acc is None:
        return
    try:
        # `by_alias`: unele răspunsuri au câmpuri cu alias (moderarea: „sexual/minors"), iar fără
        # alias `model_validate` nu le mai poate reface la replay.
        dump = resp.model_dump(mode="json", by_alias=True) if hasattr(resp, "model_dump") else resp
        row = {"endpoint": endpoint, **request_key(endpoint, kwargs), "ok": True}
        row["response"] = dump
        _append(acc, row)
    except Exception:  # noqa: BLE001 — captura nu are voie să rupă turul
        acc.failed += 1
        log.warning("model_io: răspunsul n-a putut fi înregistrat (endpoint=%s)", endpoint)


def record_error(endpoint: str, kwargs: dict[str, Any] | None, exc: BaseException) -> None:
    """Un apel eșuat definitiv (după retry): doar CLASA erorii, nu mesajul (poate purta conținut).
    Replay-ul îl joacă drept eroare, ca turul să treacă prin același fallback."""
    acc = _current.get()
    if acc is None or not isinstance(exc, Exception):
        # O anulare (deadline, client deconectat) nu e un răspuns al furnizorului.
        return
    try:
        row = {"endpoint": endpoint, **request_key(endpoint, kwargs), "ok": False}
        row["error"] = type(exc).__name__
        _append(acc, row)
    except Exception:  # noqa: BLE001
        acc.failed += 1


def bytes_bucket(n: int) -> str:
    """Bucket cu cardinalitate mică pentru evenimentul de acoperire (P12: fără mărimi exacte)."""
    for limit, label in ((16_384, "0-16k"), (65_536, "16-64k"), (MAX_TURN_BYTES, "64-256k")):
        if n <= limit:
            return label
    return "256k+"
