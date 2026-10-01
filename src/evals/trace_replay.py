"""NX-366 felia B — rejoacă un tur din `conversation_traces` pe codul de acum, fără model.

Intrarea turului (`turn_input`, `src/worker/turn_capture.py`) reface `TurnLoadSnapshot`, iar
ieșirile înregistrate ale modelului (`model_io`, `src/agent/model_io.py`) sunt servite de un client
fals peste `LLMClient`-ul REAL: codul nostru citește exact obiectele de pe sârmă (`ChatCompletion`).
Contextul se construiește prin `processor.prepare_turn_context`, aceeași funcție ca producția, iar
stagiile rulează prin `run_pipeline`. Commit-ul, outbox-ul și aftercare-ul nu rulează; starea nouă
se calculează prin `_build_new_state`, aceeași funcție pe care o scrie commit-ul.

Ce NU inventează: o cerere către model care nu se potrivește cu înregistrarea (alt scop, altă formă,
un apel în plus) nu primește un răspuns ghicit. Turul iese `diverged`, cu indexul apelului: o
reparație care schimbă ce se cere modelului se verifică pe o înregistrare nouă, nu pe una veche.

Izolare: `business_id` vine din rândul de trace (coloana), niciodată din conținutul înregistrat
(P7). DB-ul e citit printr-un provider tenant-scoped în care fiecare checkout e o tranzacție
`READ ONLY` anulată la final.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

from openai.types import CreateEmbeddingResponse, ModerationCreateResponse
from openai.types.chat import ChatCompletion
from openai.types.responses import Response

from src.agent import model_io
from src.agent.llm import LLMClient
from src.config import get_settings
from src.db.provider import DbProvider, tenant_db
from src.models import Author, Contact, Direction, Message
from src.worker import turn_capture
from src.worker.turn_uow import TurnLoadSnapshot

#: Tipul răspunsului pe endpoint, ca `model_validate` să refacă obiectul pe care îl citește codul.
_RESPONSE_TYPES: dict[str, Any] = {
    "chat.completions": ChatCompletion,
    "responses": Response,
    "moderations": ModerationCreateResponse,
    "embeddings": CreateEmbeddingResponse,
}

#: Setările impuse pe durata replay-ului, peste profilul înregistrat: o eroare înregistrată se
#: joacă o dată (fără retry și backoff), iar proxy-ul de cronometrare nu învelește conexiunea.
_REPLAY_FORCED = {"llm_retry_max": 0, "db_query_timing_enabled": False}

#: Setările care rămân ale MAȘINII care rulează replay-ul: identitatea mediului și capacitatea
#: poolurilor (`env=prod` ar porni gărzi de producție, iar poolurile sunt deja deschise).
_LOCAL_ONLY = frozenset(
    {
        "env",
        "release_sha",
        "release_track",
        "db_admin_pool_max",
        "db_bot_pool_max",
        "db_session_budget",
    }
)

#: Statusurile unui replay. Vocabular ÎNCHIS. `wrote` = codul a încercat o scriere (refuzată de
#: tranzacția READ ONLY), deci turul de producție a avut un efect pe care replay-ul nu-l are.
STATUSES = ("replayed", "diverged", "wrote", "db_aborted", "not_replayable")


class ReplayDivergence(Exception):
    """Codul de acum cere modelului altceva decât s-a înregistrat (sau un apel în plus)."""

    def __init__(self, index: int, wanted: Mapping[str, Any], recorded: Mapping[str, Any] | None):
        super().__init__(f"apelul {index}: cerut {dict(wanted)}, înregistrat {recorded}")
        self.index = index
        self.wanted = dict(wanted)
        self.recorded = dict(recorded) if recorded else None


class ReplayedProviderError(Exception):
    """O eroare înregistrată a cărei clasă nu se poate reconstrui (rezerva lui `rebuild_error`)."""


def rebuild_error(rec: Mapping[str, Any]) -> BaseException:
    """Eroarea înregistrată, cu ACEEAȘI clasă: codul turului ramifică pe ea (`failure_cause`,
    `_TRANSIENT_ERRORS`, `rich_error`), deci o clasă generică ar schimba evenimentele și traceul.
    O anulare (interpretarea dark tăiată de `wait_for`) se joacă drept `TimeoutError`, exact ce
    vede apelantul lui `wait_for`."""
    import builtins  # noqa: PLC0415

    import httpx  # noqa: PLC0415
    import openai  # noqa: PLC0415

    from src.runtime.deadline import DeadlineExhausted  # noqa: PLC0415

    name = str(rec.get("error") or "")
    if rec.get("cancelled") or name in ("TimeoutError", "CancelledError"):
        return TimeoutError()
    if name == "DeadlineExhausted":
        return DeadlineExhausted("replay")
    request = httpx.Request("POST", "https://replay.invalid/v1")
    cls = getattr(openai, name, None)
    if isinstance(cls, type) and issubclass(cls, openai.APIStatusError):
        status = int(rec.get("status") or 500)
        return cls("replay", response=httpx.Response(status, request=request), body=None)
    if cls is openai.APITimeoutError:
        return openai.APITimeoutError(request=request)
    if cls is openai.APIConnectionError:
        return openai.APIConnectionError(request=request)
    builtin = getattr(builtins, name, None)
    if isinstance(builtin, type) and issubclass(builtin, Exception):
        try:
            return builtin()
        except Exception:  # noqa: BLE001
            pass
    return ReplayedProviderError(name or "unknown")


class _Endpoint:
    def __init__(self, player: RecordedModel, name: str) -> None:
        self._player = player
        self._name = name

    async def create(self, **kwargs: Any) -> Any:
        return self._player.next(self._name, kwargs)


class RecordedModel:
    """Clientul fals: aceeași formă ca `AsyncOpenAI` pe cele patru endpointuri folosite."""

    def __init__(self, calls: list[Mapping[str, Any]]) -> None:
        self._calls = list(calls)
        self.cursor = 0
        self.divergence: ReplayDivergence | None = None
        #: Indicii apelurilor servite peste o intrare DIFERITĂ de cea înregistrată (alt prompt, alt
        #: set de produse, alt istoric). Nu e divergență (o reparație poate schimba promptul), dar
        #: răspunsul înregistrat a fost jucat peste altă întrebare, iar raportul trebuie s-o spună.
        self.inputs_changed: list[int] = []
        self.chat = NS(completions=_Endpoint(self, "chat.completions"))
        self.responses = _Endpoint(self, "responses")
        self.moderations = _Endpoint(self, "moderations")
        self.embeddings = _Endpoint(self, "embeddings")

    @property
    def unused(self) -> int:
        return len(self._calls) - self.cursor

    def next(self, endpoint: str, kwargs: dict[str, Any]) -> Any:
        wanted = {"endpoint": endpoint, **model_io.request_key(endpoint, kwargs)}
        if self.cursor >= len(self._calls):
            return self._diverge(wanted, None)
        rec = self._calls[self.cursor]
        if {k: rec.get(k) for k in wanted} != wanted:
            return self._diverge(wanted, rec)
        if rec.get("input_sha") != model_io.input_sha(_input_kwargs(endpoint, kwargs)):
            self.inputs_changed.append(self.cursor)
        self.cursor += 1
        if not rec.get("ok"):
            raise rebuild_error(rec)
        try:
            return _RESPONSE_TYPES[endpoint].model_validate(rec["response"])
        except Exception:  # noqa: BLE001 — înregistrarea nu se poate reface: instrumentul e defect
            # Fără asta, codul turului ar înghiți eroarea (moderarea e fail-open) și replay-ul ar
            # raporta un rezultat produs de un fallback, nu de răspunsul înregistrat.
            self.cursor -= 1
            return self._diverge({**wanted, "reconstruct": "failed"}, rec)

    def _diverge(self, wanted: Mapping[str, Any], rec: Mapping[str, Any] | None) -> Any:
        recorded = {k: rec.get(k) for k in wanted} if rec else None
        err = ReplayDivergence(self.cursor, wanted, recorded)
        # Prima divergență rămâne: codul turului poate înghiți excepția și continua pe fallback,
        # deci statusul nu se poate citi din ce iese din pipeline.
        if self.divergence is None:
            self.divergence = err
        raise err


def _input_kwargs(endpoint: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Aceeași parte a cererii pe care a amprentat-o captura (`llm.py` dă moderării și
    embeddingului doar `input`, iar Vision doar `messages`)."""
    if endpoint in ("moderations", "embeddings"):
        return {"input": kwargs.get("input")}
    return kwargs


class _GuardedConn:
    """Conexiunea de replay: numără fiecare scriere refuzată (SQLSTATE 25006), chiar dacă apelantul
    o înghite. Sonda de la finalul checkout-ului nu ajunge: o scriere făcută printr-un `db_tx`
    rulează într-un SAVEPOINT, al cărui rollback lasă tranzacția exterioară sănătoasă."""

    _QUERY_METHODS = frozenset({"execute", "executemany", "fetch", "fetchrow", "fetchval"})

    def __init__(self, conn: Any, state: dict[str, int]) -> None:
        self._conn = conn
        self._state = state

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._conn, name)
        if name not in self._QUERY_METHODS:
            return attr

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return await attr(*args, **kwargs)
            except Exception as exc:
                if getattr(exc, "sqlstate", None) == "25006":
                    self._state["wrote"] += 1
                raise

        return guarded


def readonly_db(business_id: str, base: DbProvider | None = None) -> DbProvider:
    """Providerul de replay: fiecare checkout e o tranzacție `READ ONLY` anulată la final. O scriere
    refuzată se numără în `wrote` (vezi `_GuardedConn`); orice altă instrucțiune eșuată care lasă
    tranzacția anulată se vede la sonda finală și se numără în `aborted`."""
    inner = base or tenant_db(business_id)
    state = {"aborted": 0, "wrote": 0}

    @asynccontextmanager
    async def _cm(operation: str = "unlabeled") -> AsyncIterator[Any]:
        async with inner(operation) as conn:
            tx = conn.transaction(readonly=True)
            await tx.start()
            try:
                yield _GuardedConn(conn, state)
            finally:
                try:
                    await conn.fetchval("select 1")
                except Exception:  # noqa: BLE001 — tranzacția anulată de o instrucțiune eșuată
                    state["aborted"] += 1
                await tx.rollback()

    _cm.shared_connection = False  # type: ignore[attr-defined]
    _cm.state = state  # type: ignore[attr-defined]
    return _cm


def _scalar_fields(settings: Any) -> dict[str, Any]:
    from src.ops.build_info import is_secret_field  # noqa: PLC0415

    out: dict[str, Any] = {}
    for name, f in type(settings).model_fields.items():
        if is_secret_field(name) or name in _LOCAL_ONLY:
            continue
        if isinstance(getattr(settings, name, None), (bool, int, float, str)):
            out[name] = f
    return out


@contextmanager
def settings_profile(recorded: Mapping[str, Any] | None) -> Iterator[None]:
    """Profilul turului: fiecare setare scalară ne-secretă ia valoarea ÎNREGISTRATĂ, iar una pe care
    înregistrarea n-o are (câmp nou, adus de codul testat) ia IMPLICITUL CODULUI, nu valoarea din
    `.env`-ul mașinii. `None` = profilul local (`--flags current`). Restaurat la ieșire."""
    s = get_settings()
    saved: dict[str, Any] = {}
    try:
        if recorded is not None:
            for name, f in _scalar_fields(s).items():
                if name in recorded:
                    value = recorded[name]
                else:
                    value = f.get_default(call_default_factory=True)
                    if not isinstance(value, (bool, int, float, str)):
                        continue
                saved[name] = getattr(s, name)
                setattr(s, name, value)
        for name, value in _REPLAY_FORCED.items():
            if name in type(s).model_fields and name not in saved:
                saved[name] = getattr(s, name)
            setattr(s, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(s, name, value)


def _dt(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def snapshot_from(doc: Mapping[str, Any]) -> TurnLoadSnapshot:
    """`TurnLoadSnapshot` din `turn_input.snapshot` (inversul lui `turn_capture.turn_input`)."""
    contact = dict(doc.get("contact") or {})
    history = [
        Message(
            direction=Direction(m["direction"]),
            author=Author(m["author"]),
            body=m.get("body"),
            content_type=m.get("content_type") or "text",
            created_at=_dt(m.get("created_at")),
            id=m.get("id"),
            payload=m.get("payload"),
        )
        for m in doc.get("history") or []
    ]
    facts = [
        {k: _dt(v) if k in ("last_seen_at", "expires_at") else v for k, v in dict(f).items()}
        for f in doc.get("facts") or []
    ]
    return TurnLoadSnapshot(
        deduped=False,
        contact=Contact(
            id=str(contact.get("id") or ""),
            business_id=str(contact.get("business_id") or ""),
            locale=contact.get("locale"),
            profile=dict(contact.get("profile") or {}),
            lead_score=float(contact.get("lead_score") or 0.0),
            lifecycle=contact.get("lifecycle") or "new",
            consent=dict(contact.get("consent") or {}),
        ),
        conversation_id=doc.get("conversation_id"),
        state=dict(doc.get("state") or {}),
        state_version=int(doc.get("state_version") or 0),
        locale=doc.get("locale"),
        bot_active=bool(doc.get("bot_active", True)),
        shadow_mode=bool(doc.get("shadow_mode", False)),
        history=history,
        summary=doc.get("summary"),
        facts=facts,
        inbound_msg_id=doc.get("inbound_msg_id"),
    )


def _plain(value: Any) -> Any:
    """Forma persistată (JSON) a unui obiect, ca replay-ul și trace-ul să se compare la fel."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def reply_fingerprint(reply: Mapping[str, Any] | None) -> dict[str, Any]:
    """Ce vede clientul, pe care se judecă fidelitatea: textul, cardurile (id-uri, în ordine),
    chips-urile și coloanele comparației. Nu motivele (un text, deja în `text` pe calea bogată)."""
    if not reply:
        return {"text": None, "products": [], "suggestions": [], "comparison": []}
    products = [str(p.get("product_id") or p.get("id")) for p in reply.get("products") or []]
    comparison = reply.get("comparison") or {}
    columns = comparison.get("columns") if isinstance(comparison, Mapping) else None
    return {
        "text": reply.get("text"),
        "products": products,
        "suggestions": list(reply.get("suggestions") or []),
        "comparison": [str(c.get("product_id")) for c in columns or []],
    }


@dataclass
class ReplayResult:
    turn_id: str
    status: str
    reason: str | None = None
    reply: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    new_state: dict[str, Any] | None = None
    divergence: dict[str, Any] | None = None
    unused_calls: int = 0
    #: Apelurile servite peste altă intrare decât cea înregistrată (`RecordedModel.inputs_changed`).
    inputs_changed: list[int] = field(default_factory=list)
    ignored_settings: list[str] = field(default_factory=list)
    drift: dict[str, Any] = field(default_factory=dict)


def not_replayable_reason(diagnostics: Mapping[str, Any]) -> str | None:
    ti = diagnostics.get("turn_input")
    mio = diagnostics.get("model_io")
    if not isinstance(ti, Mapping):
        return "no_turn_input"
    if ti.get("v") != turn_capture.FORMAT_VERSION or ti.get("error"):
        return "turn_input_unusable"
    if not isinstance(mio, Mapping):
        return "no_model_io"
    if mio.get("v") != model_io.FORMAT_VERSION:
        return "model_io_unknown_version"
    if not mio.get("replayable"):
        reasons = mio.get("unreplayable") or ["incomplete"]
        return "model_io_" + "+".join(str(r) for r in reasons)
    return None


async def replay_turn(
    row: Mapping[str, Any],
    *,
    db: DbProvider | None = None,
    flags: str = "recorded",
    business: Any = None,
    stages: list[Any] | None = None,
) -> ReplayResult:
    """Rejoacă un rând din `conversation_traces` (`turn_id`, `business_id`, `client_text`,
    `diagnostics`). `flags="recorded"` = profilul de setări al turului; `"current"` = cel local.
    `stages` există pentru teste; implicit, stagiile producției (`DEFAULT_STAGES`)."""
    from src.channels.media import get_media_registry  # noqa: PLC0415
    from src.db.queries.businesses import load_business  # noqa: PLC0415
    from src.worker.processor import _build_new_state, prepare_turn_context  # noqa: PLC0415
    from src.worker.runner import DEFAULT_STAGES, PipelineDeps, run_pipeline  # noqa: PLC0415

    turn_id = str(row["turn_id"])
    business_id = str(row["business_id"])
    diagnostics = row.get("diagnostics") or {}
    if isinstance(diagnostics, str):
        diagnostics = json.loads(diagnostics)
    reason = not_replayable_reason(diagnostics)
    if reason is not None:
        return ReplayResult(turn_id, "not_replayable", reason=reason)
    ti = diagnostics["turn_input"]
    calls = diagnostics["model_io"]["calls"]
    provider = readonly_db(business_id, db)
    if business is None:
        async with provider("replay_load_business") as conn:
            business = await load_business(conn, business_id)
    if business is None or str(business.id) != business_id:
        return ReplayResult(turn_id, "not_replayable", reason="business_not_found")

    recorded_settings = dict(ti["env"].get("settings") or {}) if flags == "recorded" else None
    fields = type(get_settings()).model_fields
    ignored = sorted(k for k in recorded_settings or {} if k not in fields)
    snap = snapshot_from(ti["snapshot"])
    ev = ti["event"]
    event = {
        "provider_msg_id": "",
        "content_type": ev.get("content_type") or "text",
        "channel_kind": ev.get("channel_kind") or "webchat",
        "channel_account_id": ev.get("channel_account_id") or "",
        "media_id": ev.get("media_id"),
        "action": ev.get("action"),
        "page_context": ev.get("page_context"),
    }
    body = row.get("client_text") or ""
    player = RecordedModel(calls)

    with settings_profile(recorded_settings):
        s = get_settings()
        llm = LLMClient(
            player,  # type: ignore[arg-type]
            model_agent=s.model_agent,
            model_embed=s.model_embed,
            model_moderation=s.model_moderation,
            model_vision=s.model_vision,
        )
        ctx = await prepare_turn_context(
            provider,
            business,
            event,
            snap,
            turn_id=turn_id,
            raw_body=body,
            safe_body=body,
            channel_id=ev.get("channel_id") or "",
            channel_kind=event["channel_kind"],
            # Identitatea verificată nu se înregistrează (P12), doar bitul. Uneltele de comandă
            # vor căuta un client inexistent: limită declarată.
            verified_customer_ref="replay-verified" if ev.get("verified") else None,
        )
        deps = PipelineDeps(db=provider, redis=None, llm=llm, media=get_media_registry())
        await run_pipeline(ctx, deps, stages if stages is not None else DEFAULT_STAGES)
        new_state = None
        if ctx.reply is not None:
            new_state = _build_new_state(
                snap.state,
                ctx,
                is_rich=ctx.reply.rich is not None,
                has_products=bool(ctx.reply.products),
            )

    db_state = provider.state  # type: ignore[attr-defined]
    status = "replayed"
    if player.divergence is not None or player.unused:
        status = "diverged"
    elif db_state["wrote"]:
        status = "wrote"
    elif db_state["aborted"]:
        status = "db_aborted"
    drift = {"pack": turn_capture.pack_sha(business) != ti["env"].get("pack_sha")}
    return ReplayResult(
        turn_id=turn_id,
        status=status,
        reply=_plain(asdict(ctx.reply)) if ctx.reply is not None else None,
        events=[{"type": e.type, **_plain(e.properties)} for e in ctx.events],
        trace=_plain(ctx.trace),
        new_state=_plain(new_state) if new_state is not None else None,
        divergence=(
            {
                "index": player.divergence.index,
                "wanted": player.divergence.wanted,
                "recorded": player.divergence.recorded,
            }
            if player.divergence is not None
            else None
        ),
        unused_calls=player.unused,
        inputs_changed=list(player.inputs_changed),
        ignored_settings=ignored,
        drift=drift,
    )


async def catalog_drift(
    db: DbProvider, business_id: str, recorded: Mapping[str, list[Any]]
) -> list[str]:
    """Produsele turului al căror preț/disponibilitate s-a schimbat de la înregistrare."""
    if not recorded:
        return []
    async with db("replay_catalog_drift") as conn:
        rows = await conn.fetch(
            """
            select p.id::text as id, p.price, p.sale_price, p.availability
              from products p
             where p.business_id = $1::uuid and p.id = any($2::uuid[])
            """,
            business_id,
            list(recorded),
        )
    now = {r["id"]: [_num(r["price"]), _num(r["sale_price"]), r["availability"]] for r in rows}
    changed = []
    for pid, values in recorded.items():
        then = [_num(values[0]), _num(values[1]), values[2]]
        if pid not in now or (then[2] is not None and now[pid] != then):
            changed.append(pid)
    return sorted(changed)


def _num(v: Any) -> float | None:
    try:
        return None if v is None else round(float(v), 2)
    except (TypeError, ValueError):
        return None
