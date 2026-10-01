"""NX-366 — ce a ieșit din model pe un tur, înregistrat ca turul să poată fi rejucat fără model.

De ce: verificarea unei reparații pe un tur real cerea până acum o nouă rulare cu modelul, care
formulează altfel de fiecare dată, deci un tur care „trecea" putea trece din noroc. Cu ieșirile
înregistrate, replay-ul (`src/evals/trace_replay.py`) rulează CODUL NOSTRU pe exact ce a scris
modelul atunci, iar un defect reparat se vede pe mecanism.

Contractul (P10, ca `src.agent.usage`): adaptorul de model raportează aici fiecare apel LOGIC (după
retry, nu pe încercare); runner-ul deschide acumulatorul pe durata calculului turului (`push`/`pop`)
și îl pune în `ctx.trace["model_io"]`. Stagiile nu știu că sunt înregistrate. Aftercare-ul nu e
înregistrat: nu face parte din tur.

Ce face un tur NErejucabil (`replayable: false`, cu motivele în `unreplayable`), fiindcă replay-ul
ar servi altceva decât a primit codul în producție:
  • `truncated` — un răspuns peste plafon (înlocuit cu un marcaj, nu tăiat la mijloc);
  • `failed` — un răspuns care n-a putut fi serializat;
  • `concurrent` — două apeluri în zbor deodată: înregistrarea urmează ordinea în care s-au
    TERMINAT, replay-ul pe cea în care s-au CERUT, deci două cereri de aceeași formă s-ar inversa;
  • `redaction_degraded` — frontiera NX-230 a pus un substituent în locul unui text;
  • `redacted` — frontiera a schimbat CONȚINUTUL scris de model (un telefon într-un argument sau
    într-un citat): replay-ul ar juca alt text decât cel pe care l-a citit codul.

Ce NU face: nu intră în analytics (P12). Rândul stă în `conversation_traces` (retenție 30 de zile,
GDPR prin `delete_traces_for_contact`). Promptul nu se înregistrează: doar o amprentă a lui
(`input_sha`), ca replay-ul să vadă când codul de acum îi dă modelului altă intrare.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
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
FORMAT_VERSION = 2

#: Ce din cerere intră în amprenta intrării (`input_sha`): exact ce citește modelul. Fără `model`
#: (configurație), fără `timeout` / `prompt_cache_key` (ale gărzilor).
_INPUT_KEYS = ("messages", "input", "tools", "response_format", "instructions")

#: Cheile care poartă TEXT SCRIS DE MODEL. O redactare care schimbă un astfel de text face turul
#: nerejucabil; pe restul (id-uri de răspuns, metadate) redactarea e inofensivă.
_CONTENT_KEYS = frozenset({"content", "arguments", "refusal", "text", "output_text"})

#: Cheile care nu se redactează deloc: identificatori tehnici ai furnizorului, nu vocea cuiva.
#: `chatcmpl-…` conține cifre pe care detectorul de telefon le-ar citi ca număr.
_NEVER_REDACT = frozenset({"id", "call_id", "system_fingerprint", "model", "object"})


@dataclass
class ModelIOAccumulator:
    calls: list[dict[str, Any]] = field(default_factory=list)
    bytes: int = 0
    truncated: bool = False
    #: Apeluri care n-au putut fi serializate: turul nu mai e rejucabil, dar nu se pierde tăcut.
    failed: int = 0
    redaction_degraded: int = 0
    redacted: int = 0
    concurrent: bool = False
    inflight: int = 0

    def unreplayable(self) -> list[str]:
        reasons = [
            ("truncated", self.truncated),
            ("failed", self.failed),
            ("concurrent", self.concurrent),
            ("redaction_degraded", self.redaction_degraded),
            ("redacted", self.redacted),
        ]
        return [name for name, hit in reasons if hit]

    def as_trace(self) -> dict[str, Any]:
        reasons = self.unreplayable()
        return {
            "v": FORMAT_VERSION,
            "calls": self.calls,
            "replayable": not reasons,
            "unreplayable": reasons,
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


@contextmanager
def in_flight() -> Iterator[None]:
    """Marchează un apel în zbor. Două deodată fac turul nerejucabil (vezi docstring-ul modulului):
    azi nu există apeluri concurente pe drumul turului, iar asta e garda care o spune dacă apar."""
    acc = _current.get()
    if acc is None:
        yield
        return
    acc.inflight += 1
    if acc.inflight > 1:
        acc.concurrent = True
    try:
        yield
    finally:
        acc.inflight -= 1


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


def input_sha(kwargs: dict[str, Any] | None) -> str | None:
    """Amprenta a ce citește modelul (mesaje, unelte, schemă), fără conținut. Replay-ul o compară:
    aceeași cerere cu altă intrare (alt prompt, alt set de produse, alt istoric) înseamnă că
    răspunsul înregistrat e jucat peste altă întrebare, iar raportul trebuie s-o spună."""
    if not kwargs:
        return None
    doc = {k: kwargs[k] for k in _INPUT_KEYS if k in kwargs}
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _redacted(value: Any, marks: dict[str, int], key: str | None = None) -> Any:
    """Frontiera NX-230 pe textele răspunsului. Import leneș: cu flagul stins nu costă nimic."""
    from src.privacy.boundary import make_safe  # noqa: PLC0415

    if isinstance(value, str):
        if key in _NEVER_REDACT:
            return value
        safe = make_safe(value)
        if safe.degraded:
            marks["degraded"] += 1
        elif safe.text != value and key in _CONTENT_KEYS:
            marks["content"] += 1
        return safe.text
    if isinstance(value, dict):
        return {k: _redacted(v, marks, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, marks, key) for v in value]
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
        marks = {"degraded": 0, "content": 0}
        row["response"] = _redacted(row["response"], marks)
        if marks["degraded"]:
            acc.redaction_degraded += 1
            row["redaction_degraded"] = True
        if marks["content"]:
            acc.redacted += 1
            row["redacted"] = True
    acc.bytes += size
    acc.calls.append(row)


def _row(endpoint: str, kwargs: dict[str, Any] | None, ok: bool) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        **request_key(endpoint, kwargs),
        "input_sha": input_sha(kwargs),
        "ok": ok,
    }


def record_ok(endpoint: str, kwargs: dict[str, Any] | None, resp: Any) -> None:
    """Un apel reușit. Best-effort (P6): o serializare picată marchează turul nerejucabil."""
    acc = _current.get()
    if acc is None:
        return
    try:
        # `by_alias`: unele răspunsuri au câmpuri cu alias (moderarea: „sexual/minors"), iar fără
        # alias `model_validate` nu le mai poate reface la replay.
        dump = resp.model_dump(mode="json", by_alias=True) if hasattr(resp, "model_dump") else resp
        row = _row(endpoint, kwargs, True)
        row["response"] = dump
        _append(acc, row)
    except Exception:  # noqa: BLE001 — captura nu are voie să rupă turul
        acc.failed += 1
        log.warning("model_io: răspunsul n-a putut fi înregistrat (endpoint=%s)", endpoint)


def record_error(endpoint: str, kwargs: dict[str, Any] | None, exc: BaseException) -> None:
    """Un apel eșuat definitiv (după retry): CLASA erorii și, unde există, codul HTTP, nu mesajul
    (poate purta conținut). O anulare se înregistrează și ea (`cancelled`): interpretarea dark
    tăiată de `wait_for` (NX-353) n-ar lăsa altfel niciun rând, iar replay-ul ar cere un apel
    inexistent.
    Replay-ul joacă eroarea cu aceeași clasă, ca turul să treacă prin același fallback."""
    acc = _current.get()
    if acc is None:
        return
    try:
        row = _row(endpoint, kwargs, False)
        row["error"] = type(exc).__name__
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            row["status"] = status
        if isinstance(exc, asyncio.CancelledError):
            row["cancelled"] = True
        _append(acc, row)
    except Exception:  # noqa: BLE001
        acc.failed += 1


def bytes_bucket(n: int) -> str:
    """Bucket cu cardinalitate mică pentru evenimentul de acoperire (P12: fără mărimi exacte)."""
    for limit, label in ((16_384, "0-16k"), (65_536, "16-64k"), (MAX_TURN_BYTES, "64-256k")):
        if n <= limit:
            return label
    return "256k+"
