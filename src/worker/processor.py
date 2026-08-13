"""Procesarea unui mesaj inbound — de la eveniment la răspuns în outbox.

`handle_turn` e miezul determinist al unui tur. NX-231: primește un PROVIDER de conexiuni, nu o
conexiune. Turul e împărțit explicit în patru faze (contractul din `turn_uow.py`):

  1. **load**    (checkout scurt) — dedupe claim → contact → conversație → mesaj inbound →
                 `last_inbound_at` → istoric/rezumat/facts. Rezultat: `TurnLoadSnapshot`, date
                 imutabile, fără connection handle.
  2. **compute** (ZERO conexiune ținută) — pipeline-ul (runner). Stagiile iau checkout-uri scurte
                 pentru operațiile lor; triajul, agentul, tool-urile și embed-urile rulează cu
                 poolul liber.
  3. **commit**  (checkout scurt + O tranzacție) — mesaje outbound + outbox (idempotent pe turn_id)
                 + patch de stare (state_version) + `mark_inbound_completed`. Atomic sau deloc.
  4. **aftercare** — primește ID-uri și un snapshot, niciodată o conexiune.

Înainte, toate cele patru rulau pe UNA și aceeași conexiune, ținută de la primul query până la
ultimul — inclusiv peste apelurile la OpenAI. Poolul (max 10) devenea frâna accidentală a
sistemului, iar ~79% din hold era timp în care conexiunea nu făcea nimic
(docs/CONN-HOLD-ANALYSIS-2026.md).

Dispatcher-ul (separat) citește outbox, trimite la canal și leagă provider_msg_id.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from redis.asyncio import Redis

from src.agent.llm import get_llm
from src.channels.base import IDENTIFIED_CHANNELS
from src.channels.media import get_media_registry
from src.config import get_settings
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import ReducerPolicy, StateUpdateProposal, reduce_all
from src.conversation.state_v2 import (
    PASSTHROUGH_KEYS,
    STATE_SCHEMA_VERSION,
    hydrate_state_v2,
    is_v2,
    project_v1,
    serialize,
    size_bucket,
    state_diff,
)
from src.db import op_metrics
from src.db.connection import bot_pool_stats
from src.db.pool_metrics import take_acquire_stats
from src.db.provider import DbProvider
from src.models import (
    BusinessConfig,
    ConversationState,
    InboundMessage,
    Reply,
    TurnContext,
    TurnUsage,
)
from src.privacy import RawInbound, RawText, SafeInbound, apply_boundary
from src.web.action_models import ActionCommand
from src.web.context import from_payload as page_context_from_payload
from src.worker.admission import tenant_bucket
from src.worker.aftercare import AftercareWork, persist_events, run_aftercare
from src.worker.compose import ensure_disclaimer
from src.worker.limits import (
    CONTACT_COST_WINDOW_S,
    contact_scope_key,
    cost_add_and_total,
    cost_over_budget,
    seed_daily_cost,
    spend_capped,
    spend_over_cap,
)
from src.worker.reply_split import split_reply
from src.worker.runner import DEFAULT_STAGES, PipelineDeps, Stage, run_pipeline
from src.worker.turn_snapshot import build_turn_snapshot, snapshot_events, without_evidence
from src.worker.turn_uow import (
    OutboundFragment,
    TurnCommit,
    TurnLoadSnapshot,
    commit_turn,
    complete_without_reply,
    load_turn,
)

log = logging.getLogger(__name__)

# NX-232: hook-ul de commit al MARGINII — `(conn tranzacție, reply, language, facts)`. Rulează în
# tranzacția `TurnCommit`; dacă ridică, tot commit-ul face rollback (vezi handle_turn).
# NX-236: al patrulea argument e OPȚIONAL (hook-urile vechi cu trei parametri rămân valide) și
# poartă faptele pe care marginea nu le poate deduce din `Reply` — vezi `CommitFacts`.
CommitHook = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class CommitFacts:
    """NX-236 — ce știe TURUL și marginea nu poate citi din `Reply`.

    Marginea web planifică acțiunile opace din ViewModel-ul pe care tocmai l-a randat, dar două
    dintre ele au nevoie de context: răspunsul la o clarificare trebuie legat de ÎNTREBAREA exactă
    (slot + a câta oară), iar paginarea de amprenta sesiunii de căutare. Fără ele, un buton ar
    putea răspunde la altă întrebare sau pagina altă listă — exact ce previne cardul.

    Se transmite prin hook, nu prin `Reply`: `Reply` e contractul NEUTRU de canal, iar starea
    turului n-are ce căuta în el."""

    pending_field: str | None = None
    pending_attempts: int = 1
    active_search_ref: str | None = None


@dataclass
class TurnResult:
    """Rezultatul procesării unui tur (pentru logging/teste).

    `reply` + `language` sunt populate DOAR pe calea sincronă (`deliver=False`, gateway web
    request/response): apelantul mapează `reply` (text + produse + chips) direct în răspunsul HTTP,
    fără outbox/dispatcher. Pe calea async (WhatsApp/Telegram/SSE) rămân None — livrarea e prin
    outbox, iar `reply_text` (text PUR, fără disclaimer) e suficient pt log/teste."""

    conversation_id: str | None
    contact_id: str | None
    turn_id: str | None
    reply_text: str | None
    outbox_id: str | None
    deduped: bool = False
    reply: Reply | None = None  # obiectul complet (sync); None pe calea async (outbox)
    language: str | None = None  # limba turului (pt re-aplicarea disclaimer-ului la mapare sync)
    # NX-161 F1: cu `defer_aftercare=True`, handle_turn NU rulează aftercare inline — întoarce
    # AICI munca de făcut, iar apelantul o rulează cu `run_aftercare(tenant_db(biz), ...)` DUPĂ ce a
    # închis `tenant_conn` (conn eliberat pe durata LLM). None = aftercare deja rulat inline.
    aftercare: "AftercareWork | None" = None


async def _llm_within_budget(
    ctx: TurnContext, redis: Redis | None, business: BusinessConfig, *, channel_kind: str
):
    """Cost guard (G2c): dacă businessul SAU contactul a depășit plafonul, întoarce None →
    pipeline-ul rulează FĂRĂ LLM (triaj/agent degradează grațios, cache L1 încă servește), dar
    gates rămân intacte. Best-effort: eșec Redis → fail-open (LLM normal). Plafoane:
    businesses.daily_cost_cap_usd | settings.daily_cost_cap_usd (business) și
    settings.contact_daily_cost_cap_usd (per-contact, canale identificate — NX-125)."""
    llm = get_llm()
    settings = get_settings()
    if llm is None or redis is None or not settings.cost_guard_enabled:
        return llm
    cap = business.daily_cost_cap_usd or settings.daily_cost_cap_usd
    contact_cap = settings.contact_daily_cost_cap_usd
    if not cap and not contact_cap:
        return llm
    try:
        if cap and await cost_over_budget(redis, business.id, cap):
            ctx.emit("cost_guard_tripped", cap_usd=cap)
            log.warning(
                "cost guard: business %s peste plafon ($%.2f) → LLM dezactivat", business.id, cap
            )
            return None
        # NX-125: plafon SOFT per-contact (canale identificate; web = NX-120). Pre-check read-only.
        if contact_cap and channel_kind in IDENTIFIED_CHANNELS:
            scope = contact_scope_key(business.id, ctx.contact.id)
            if await spend_capped(redis, scope, contact_cap):
                ctx.emit("contact_spend_capped", cap_usd=contact_cap)
                log.warning(
                    "cost guard: contact peste plafon per-contact ($%.4f) → LLM off", contact_cap
                )
                return None
    except Exception as e:  # noqa: BLE001 — check eșuat → fail-open
        log.warning("cost guard: check eșuat (%s) → fail-open", type(e).__name__)
    return llm


async def _record_turn_cost(
    redis: Redis | None,
    business: BusinessConfig,
    ctx: TurnContext,
    *,
    llm_used: bool,
    channel_kind: str,
) -> None:
    """Adaugă costul EXACT al turului în contorul zilnic (G2c) — DOAR dacă LLM-ul a fost folosit.
    NX-125: costul vine din tokeni reali (`ctx.usage.cost_usd`, cifra dashboard/usage_daily), nu
    din euristica `estimate_turn_cost`. Increment ATOMIC + enforcement POST-increment (fără TOCTOU):
    dacă noul total ≥ plafon, emite `cost_guard_tripped` → turul URMĂTOR e blocat de pre-check.
    Aplică și plafonul per-contact (canale identificate). Best-effort; facturare = usage_daily."""
    settings = get_settings()
    if redis is None or not settings.cost_guard_enabled or not llm_used:
        return
    cost = ctx.usage.cost_usd if ctx.usage else 0.0
    if cost <= 0:  # tur fără apeluri LLM (cache L1/gates) → nimic de contorizat
        return
    try:
        total = await cost_add_and_total(redis, business.id, cost)
        cap = business.daily_cost_cap_usd or settings.daily_cost_cap_usd
        if cap and total >= cap:
            ctx.emit("cost_guard_tripped", cap_usd=cap, total_usd=round(total, 6))
    except Exception as e:  # noqa: BLE001 — contor best-effort, turul a răspuns deja
        log.warning("cost guard: add eșuat (%s)", type(e).__name__)
    # NX-125: plafon per-contact (canale identificate; web = NX-120). Increment atomic + compară.
    contact_cap = settings.contact_daily_cost_cap_usd
    if contact_cap and channel_kind in IDENTIFIED_CHANNELS:
        try:
            scope = contact_scope_key(business.id, ctx.contact.id)
            if await spend_over_cap(redis, scope, cost, contact_cap, CONTACT_COST_WINDOW_S):
                ctx.emit("contact_spend_capped", cap_usd=contact_cap)
        except Exception as e:  # noqa: BLE001 — fail-open pe scope
            log.warning("cost guard: spend per-contact eșuat (%s)", type(e).__name__)


def _message_usage_kwargs(turn_usage: TurnUsage | None) -> dict:
    """Câmpurile de observabilitate (NX-103) atașate pe rândul `messages` outbound al botului:
    tokeni/cost/latență/model. DOAR pe primul fragment al reply-ului (split-ul e același reply).

    CONV-COMMERCE: salvăm pentru ORICE tur (nu doar cele cu LLM) — timpul + tokenii + costul intră
    în DB la FIECARE răspuns. Tokeni/cost = 0 când n-a fost apel LLM (cache/free-layer/welcome —
    corect 0, nu NULL); `model_route` = modelele folosite sau None. `None` = niciun tur prin runner
    (ex. mesaj proactiv pus direct în outbox)."""
    if turn_usage is None:
        return {}
    return {
        "model_route": ",".join(turn_usage.models) or None,
        "tokens_in": turn_usage.tokens_in,
        "tokens_out": turn_usage.tokens_out,
        "cost_usd": round(turn_usage.cost_usd, 6),
        "latency_ms": int(round(turn_usage.latency_ms)),
    }


def _displayed_product_refs(products: list[dict] | None) -> list[dict]:
    """Persist only compact refs in conversation state; rich card payload may contain variants."""
    refs: list[dict] = []
    for p in products or []:
        pid = p.get("product_id") or p.get("id")
        if pid is None or p.get("name") is None or p.get("price") is None:
            continue
        refs.append({"product_id": pid, "name": p["name"], "price": float(p["price"])})
    return refs


def _attach_state_v2(ctx: TurnContext, raw_state: dict | None) -> None:
    """Hidratează `ctx.state_v2` din `conversations.state` (orice versiune). NX-235.

    Migrarea e LAZY prin contract: un rând v1 se citește ca v2 în memorie și se rescrie în format
    nou abia la următorul commit, dacă flagul de scriere e aprins. Fără bulk rewrite în deploy —
    conversațiile moarte nu se ating niciodată, iar cele vii migrează singure la primul mesaj."""
    if not get_settings().conversation_state_v2_enabled:
        return
    legacy = not is_v2(raw_state)
    ctx.state_v2 = hydrate_state_v2(raw_state, _need_vocabulary(ctx))
    ctx.emit(
        "conversation_state_loaded",
        schema=1 if legacy else STATE_SCHEMA_VERSION,
        outcome="adapted" if legacy else "native",
    )
    if legacy and get_settings().conversation_state_v2_write_enabled:
        ctx.emit("conversation_state_lazy_upgraded", **{"from": 1, "to": STATE_SCHEMA_VERSION})


def _need_vocabulary(ctx: TurnContext) -> "NeedVocabulary":
    """Vocabularul de nevoi al businessului (nucleu universal + DomainPack). Construit din
    `ctx.business.domain_pack` — P9: ce poate memora conversația e configurație, nu cod."""
    return NeedVocabulary.from_pack(getattr(ctx.business, "domain_pack", None))


def _reducer_policy(ctx: TurnContext) -> ReducerPolicy:
    s = get_settings()
    return ReducerPolicy(
        vocabulary=_need_vocabulary(ctx),
        sensitive_consent=s.conversation_sensitive_memory_enabled,
        max_clarification_attempts=s.clarify_max_attempts,
    )


def _turn_proposals(
    ctx: TurnContext, *, is_rich: bool, has_products: bool
) -> list[StateUpdateProposal]:
    """Deltele turului, ca PROPUNERI typed — aceleași delte pe care `_build_new_state` le scrie
    în v1, doar exprimate în vocabularul reducerului.

    Subiectul (`set_topic`) și nevoile din filtrele turului sunt propuse de stagiul AGENT — el
    știe ruta și e cel care azi rulează `merge_constraints` (NX-133: doar pe SALES). Aici rămân
    deltele deținute de Sender: setul afișat, sesiunea de căutare, prune-ul de siguranță."""
    proposals: list[StateUpdateProposal] = list(ctx.state_proposals)

    if (is_rich or has_products) and ctx.reply is not None and ctx.reply.products:
        proposals.append(
            StateUpdateProposal(
                "set_references",
                source="catalog",
                turn_id=ctx.turn_id,
                payload={"displayed_products": _displayed_product_refs(ctx.reply.products)},
            )
        )
    # NX-119b: sesiunea de căutare — patch-ul tool-ului are întâietate; altfel un reply care nu e
    # o căutare o închide (fără sesiuni zombi).
    if "active_search" in ctx.state_patch:
        proposals.append(
            StateUpdateProposal(
                "set_active_search", source="catalog", payload=ctx.state_patch["active_search"]
            )
        )
    elif not (is_rich or has_products):
        proposals.append(StateUpdateProposal("set_active_search", source="catalog", payload=None))
    if "displayed_products" in ctx.state_patch:
        # NX-173: prune-ul de siguranță scoate produse din setul afișat — trece prin aceeași
        # operație, ca lista și revizia ei să rămână consistente. Ultimul, ca în v1: `state_patch`
        # are întâietate peste merge-ul canonic (parity, nu preferință).
        proposals.append(
            StateUpdateProposal(
                "set_references",
                source="policy",
                turn_id=ctx.turn_id,
                payload={"displayed_products": ctx.state_patch["displayed_products"]},
            )
        )
    return proposals


# Cheile de `state_patch` pe care traducerea de mai sus le acoperă. Ce nu e aici e o cale NOUĂ,
# scrisă după cardul ăsta — și nu are voie să dispară în tăcere doar fiindcă reducerul n-o știe.
_MAPPED_PATCH_KEYS = frozenset({"active_search", "displayed_products", *PASSTHROUGH_KEYS})


def _build_state_v2(base_state: dict, ctx: TurnContext, *, is_rich: bool, has_products: bool):
    """Starea v2 de persistat: hidratează BAZA (proaspătă la retry), reduce deltele turului.

    Funcția e la fel de pură ca `_build_new_state` și din același motiv: la conflict optimistic se
    re-aplică ACELEAȘI propuneri pe starea proaspăt citită. Reducerul fiind determinist, rezultatul
    e re-derivat — fără să rerulăm modelul și fără să repetăm un efect deja produs (P6)."""
    vocab = _need_vocabulary(ctx)
    hydrated = hydrate_state_v2(base_state, vocab)
    reduced = reduce_all(
        hydrated,
        _turn_proposals(ctx, is_rich=is_rich, has_products=has_products),
        _reducer_policy(ctx),
    )
    state = reduced.state
    # `cart` (NX-237) și `safety` (NX-173) NU sunt modelate de v2 — au proprietarii lor. Le CĂRĂM
    # neatinse din starea de bază + `state_patch`, ca migrarea de format să nu devină o rescriere
    # de date pe care cardul ăsta nu le deține (P3).
    passthrough = dict(state.passthrough)
    for key in PASSTHROUGH_KEYS:
        if key in ctx.state_patch:
            passthrough[key] = ctx.state_patch[key]
        elif isinstance(base_state, dict) and key in base_state and key not in passthrough:
            passthrough[key] = base_state[key]
    # O cheie de `state_patch` pe care traducerea n-o cunoaște (cale adăugată după cardul ăsta) e
    # cărată VERBATIM și semnalată. Alternativa — s-o ignorăm — ar face ca aprinderea flagului de
    # scriere să piardă tăcut date scrise de altcineva; un card viitor n-ar avea cum să observe.
    for key in sorted(set(ctx.state_patch) - _MAPPED_PATCH_KEYS):
        passthrough[key] = ctx.state_patch[key]
        ctx.emit("state_patch_unmapped", key=key)
    return replace(state, passthrough=passthrough), reduced


def _emit_state_v2_events(ctx: TurnContext, reduced, doc: dict, size: int, degraded: bool) -> None:
    """Observabilitate low-cardinality (P10/P12): CE s-a aplicat și DE CE s-a respins, niciodată
    cheia sau valoarea brută în labels."""
    for record in reduced.applied:
        if record.op in ("set_need", "supersede", "confirm"):
            ctx.emit(
                "need_update",
                operation=record.op,
                strength=record.strength,
                source=record.source,
                outcome=record.outcome,
            )
        elif record.op == "revoke":
            ctx.emit("constraint_revoked", reason=record.source)
        elif record.op == "set_topic" and record.outcome == "reset":
            ctx.emit("topic_reset", scope="category")
    for rejected in reduced.rejected:
        ctx.emit("need_update_rejected", reason=rejected.reason, operation=rejected.op)
    ctx.emit(
        "conversation_state_serialized",
        schema=STATE_SCHEMA_VERSION,
        state_size_bytes_bucket=size_bucket(size),
        degraded=degraded,
        needs=len(doc.get("needs") or []),
    )


def _build_new_state(
    base_state: dict, ctx: TurnContext, *, is_rich: bool, has_products: bool
) -> dict:
    """Construiește state-ul de persistat din starea de bază + deltele turului (Sender, P3).

    Extras ca funcție PURĂ (NX-221) ca retry-ul de `StateConflict` să poată re-aplica
    ACELEAȘI delte pe starea PROASPĂTĂ re-citită (last-writer-wins per cheie), nu pe
    snapshot-ul stale de la începutul turului.

    NX-235: cu `conversation_state_v2_enabled`, aceleași delte trec și prin reducer. SHADOW cât
    timp `..._write_enabled` e stins — v2 se calculează, se compară pe valori canonice și se
    măsoară, dar autoritatea la scriere rămâne v1. Cu flagul de scriere aprins se persistă DOAR
    documentul v2; cititorii v1 primesc proiecția (`ConversationState.from_jsonb`), deci nu există
    două formate care pot diverge."""
    s = get_settings()
    if s.conversation_state_v2_enabled:
        state_v2, reduced = _build_state_v2(
            base_state, ctx, is_rich=is_rich, has_products=has_products
        )
        doc, size, degraded = serialize(state_v2)
        _emit_state_v2_events(ctx, reduced, doc, size, degraded)
        if s.conversation_state_v2_write_enabled:
            return doc
        diff = state_diff(base_state, project_v1(state_v2))
        ctx.emit("conversation_state_shadow_diff", fields=sorted(diff), differs=bool(diff))

    new_state = base_state
    if (is_rich or has_products) and ctx.reply.products:
        # Recomandare BOGATĂ (iZi) / carusel (R2): persistăm setul afișat → navigarea
        # caruselului (handle_callback) îl citește din state (ref-uri, principiul 8).
        new_state = {
            **base_state,
            "displayed_products": _displayed_product_refs(ctx.reply.products),
        }
    # NX-130: persistă slotul de clarificare (reply CLARIFY) sau curăță-l (orice alt reply →
    # pending_question default None) → nu lăsăm întrebări zombi în state.
    new_state = {**new_state, "pending_question": ctx.reply.pending_question}
    # NX-112 (P3: processor = singurul scriitor explicit): merge canonic din ctx.state pentru
    # câmpurile pe care stagiile le mută IN-PLACE (clarify umple constraints; clarify scrie
    # asked_intents). Fără asta, un slot NOU (ex. „buget 200") trăiește doar pe ctx.state
    # (dict detașat de from_jsonb) și se pierde silențios la write-back → botul „uită".
    # `cart` rămâne owned de Agent prin state_patch (ctx.state.cart NU e ținut sincron de
    # cart_add) → NU îl includem aici; persistă via starea de bază brută + state_patch mai jos.
    new_state = {
        **new_state,
        "constraints": ctx.state.constraints,
        "asked_intents": ctx.state.asked_intents,
        # NX-133: stiva de constrângeri de căutare (mutată in-place de agent) — la fel ca
        # `constraints`, trebuie merge-uită canonic aici, altfel se pierde la write-back.
        "search_constraints": ctx.state.search_constraints,
    }
    # NX-119b: resetează sesiunea de căutare dacă reply-ul NU e o căutare de produse (fără
    # sesiuni zombi — un „mai arată-mi" ulterior nu trebuie să reia o sesiune veche, fără
    # legătură). Dacă tool-ul/agentul a scris `active_search` în state_patch (căutare nouă sau
    # pagină), acela are întâietate prin merge-ul de mai jos.
    if not (is_rich or has_products):
        new_state = {**new_state, "active_search": None}
    # NX-79: mutații de state cerute de tool-uri (ex. cart_add → {"cart": [...]}), acumulate
    # în stagiul Agent. Owner unic = Agent; processor-ul doar le merge-uiește la scriere (P3).
    # Rămâne ULTIMUL → state_patch (Agent) are întâietate peste merge-ul canonic.
    if ctx.state_patch:
        new_state = {**new_state, **ctx.state_patch}
    return new_state


async def handle_turn(
    db: DbProvider,
    business: BusinessConfig,
    channel_id: str,
    event: dict,
    *,
    redis: Redis | None = None,
    stages: list[Stage] | None = None,
    deliver: bool = True,
    defer_aftercare: bool = False,
    commit_hook: "CommitHook | None" = None,
    turn_id: str | None = None,
    preinserted_inbound_msg_id: str | None = None,
    stage_hook: Callable[[str], None] | None = None,
) -> TurnResult:
    """Procesează un mesaj inbound cu un provider tenant-scoped pe `business.id`.

    `db` (NX-231) = PROVIDER, nu conexiune: fiecare fază ia conexiunea doar cât are nevoie.
    Testele pot pasa `static_db(fake_conn)` ca să injecteze un conn unic (comportament de
    dinainte de migrare).

    `event` = envelope-ul neutru (InboundEvent.to_dict): channel_kind,
    channel_account_id, sender_external_id, provider_msg_id, content_type, body, ...

    `deliver` (NX-25b — gateway web sincron): True (default, calea async) = Sender-ul scrie
    reply-ul în `outbox` → dispatcher-ul îl livrează (WhatsApp/Telegram/SSE), eventual spart în 2.
    False (request/response: răspunsul HTTP E transportul) = persistăm mesajul outbound (status
    `sent`, un singur fragment) + state, dar NU punem în outbox (n-ar avea cine-l livra) și
    întoarcem `ctx.reply` în `TurnResult` ca apelantul să-l mapeze în răspuns. Restul (dedupe,
    history, analytics, cache, profil) e identic — sincronul nu pierde nimic din pipeline.

    `commit_hook` (NX-232) = scrierea marginii atașată la tranzacția de commit a turului
    (`turn_uow.commit_turn(on_commit=...)`): primește `(conn, reply, language)` și, dacă
    ridică, TOT commit-ul (mesaj + stare + outbox) face rollback. Folosit de ledgerul web ca
    rezultatul terminal, mesajul asistentului și patch-ul de stare să se vadă împreună sau
    deloc. Pipeline-ul nu știe CE scrie hook-ul (cuplajul de canal rămâne la margine).

    NX-233 (executorul web async): `turn_id` poate veni de la MARGINE (= id-ul rândului din
    `web_turns`) ca traiectoria turului să fie corelabilă cu ledgerul; absent → uuid nou, ca
    până acum. `preinserted_inbound_msg_id` = mesajul inbound a fost DEJA persistat la accept
    (în același checkout cu rândul de ledger) → `load_turn` nu-l mai inserează o dată.
    `stage_hook` = seam-ul PUR de observabilitate al runner-ului (phase working/validating).
    """
    stages = stages or DEFAULT_STAGES
    turn_id = turn_id or str(uuid4())
    channel_kind = event.get("channel_kind", "whatsapp")
    sender_external_id = event["sender_external_id"]
    provider_msg_id = event.get("provider_msg_id")
    # NX-129: login passthrough — dacă marginea de canal a verificat o identitate stabilă
    # (`customer_ref` din JWT host-signed), rezolvăm contactul pe EA (verified=true → contact stabil
    # peste sesiuni/device-uri), nu pe visitor_id-ul anonim. Absent → comportament anonim (ca azi).
    verified_customer_ref = event.get("verified_customer_ref")

    # NX-230 — FRONTIERA DE PRIVACY. Aici, înainte de PRIMA scriere durabilă, nu în gates.
    #
    # Până acum ordinea era inversă: `insert_message` scria `event["body"]` BRUT, iar masca de PII
    # (NX-121) rula abia în gates. Comentariul din `gates.py` recunoștea deschis consecința —
    # istoricul reintroduce PII-ul în promptul turului următor. Bucla era închisă: turul 1
    # maschează telefonul pentru model, îl scrie brut în `messages.body`, turul 2 îl citește înapoi
    # din DB prin `context.conversation_transcript`. Masca era o perdea în fața unei uși deschise.
    #
    # `raw_inbound` rămâne disponibil în memoria turului (D6); `safe_inbound` e singurul care are
    # voie să atingă un disc.
    raw_inbound, safe_inbound = apply_boundary(
        event.get("body"), content_type=event.get("content_type", "text")
    )
    if safe_inbound.degraded:
        # Fail-safe: detectorul a picat, deci NU scriem textul original „ca să nu pierdem mesajul".
        log.warning("privacy_guard_failed stage=ingress reason=degraded")

    _s = get_settings()
    # NX-231 (P10): contabilizarea checkout-urilor turului. Deschis ÎNAINTE de faza 1 → prinde și
    # `turn_load`; închis în `finally`, ca un tur care crapă să nu lase acumulatorul agățat.
    db_acc, db_token = op_metrics.push()
    try:
        # === FAZA 1 — LOAD (un checkout scurt) ==============================================
        snap = await load_turn(
            db,
            business,
            channel_id,
            event,
            turn_id=turn_id,
            safe_body=safe_inbound.text,
            identity_external_id=verified_customer_ref or sender_external_id,
            verified_customer_ref=verified_customer_ref,
            load_facts=_s.conversation_facts_enabled and _s.memory_safe_injection_enabled,
            preinserted_msg_id=preinserted_inbound_msg_id,
        )
        if snap.deduped:
            log.info("dedupe_hit_db: %s deja procesat (business %s)", provider_msg_id, business.id)
            return TurnResult(None, None, None, None, None, deduped=True)

        return await _run_turn(
            db,
            business,
            event,
            snap,
            turn_id=turn_id,
            raw_body=raw_inbound.body.value,
            safe_body=safe_inbound.text,
            channel_id=channel_id,
            channel_kind=channel_kind,
            sender_external_id=sender_external_id,
            provider_msg_id=provider_msg_id,
            verified_customer_ref=verified_customer_ref,
            redis=redis,
            stages=stages,
            deliver=deliver,
            defer_aftercare=defer_aftercare,
            db_acc=db_acc,
            commit_hook=commit_hook,
            stage_hook=stage_hook,
        )
    finally:
        op_metrics.pop(db_token)


async def _attach_snapshot(
    db: DbProvider,
    ctx: TurnContext,
    business: BusinessConfig,
    event: dict,
    snap: TurnLoadSnapshot,
    *,
    channel_id: str,
    raw_body: str,
    safe_body: str | None,
) -> None:
    """NX-234: construiește `TurnSnapshot` și îl atașează la `ctx` (owner UNIC: processor, P3).

    Poarta e în DOUĂ trepte, ca rollout-ul cerut de card:
      • `web_context_enabled` OFF → nu se construiește nimic. `ctx.snapshot` rămâne None și
        fiecare consumator cade pe comportamentul de dinainte. Zero query, zero eveniment.
      • `web_context_prompt_enabled` OFF (cu primul ON) → SHADOW: rehidratarea rulează și se
        măsoară, dar faptele sunt scoase din snapshot (`without_evidence`) înainte ca cineva
        să le poată citi. Nu e un `if` la fiecare consumator, e o proprietate a obiectului.

    Best-effort prin contract: un eșec de construcție NU oprește turul (P6) — pierdem contextul
    de pagină, nu răspunsul."""
    s = get_settings()
    if not s.web_context_enabled:
        return
    try:
        snapshot = await build_turn_snapshot(
            db,
            turn_id=ctx.turn_id,
            business=business,
            contact_id=ctx.contact.id,
            conversation_id=ctx.conversation_id,
            conversation_revision=snap.state_version,
            state=ctx.state,
            raw_inbound=RawInbound(RawText(raw_body), event.get("content_type", "text")),
            safe_inbound=SafeInbound(text=safe_body or ""),
            context=page_context_from_payload(event.get("page_context")),
            channel_id=channel_id,
            channel_kind=ctx.message.channel_kind,
            channel_account_id=ctx.message.channel_account_id,
            verified=bool(ctx.verified_customer_ref),
            content_type=ctx.message.content_type,
        )
    except Exception:  # noqa: BLE001 — contextul de pagină e opțional; turul răspunde oricum
        log.exception("web_context: construcția snapshotului a eșuat (turul continuă)")
        ctx.emit("web_context_validated", surface="unknown", outcome="unavailable", reason="build")
        return
    for event_type, props in snapshot_events(snapshot).items:
        ctx.emit(event_type, **props)
    ctx.snapshot = snapshot if s.web_context_prompt_enabled else without_evidence(snapshot)


def _attach_action(ctx: TurnContext, event: dict) -> None:
    """NX-236 — atașează comanda de acțiune deja autorizată la marginea web. Owner UNIC: processor.

    Se citește din envelope-ul turului (pus acolo din `messages.payload`, persistat la accept), nu
    dintr-un token: verificarea criptografică s-a întâmplat O SINGURĂ dată, la consum. A re-verifica
    aici ar însemna ca o rotație de chei între accept și execuție să omoare un turn deja acceptat —
    adică o operație de securitate de rutină să producă pierdere de răspuns.

    Un payload nerecunoscut (rând editat manual, format vechi) NU devine turn de text tăcut: e o
    respingere vizibilă în metrici, iar turul continuă fără acțiune (P6).

    Atașarea NU e gardată de kill-switch, deliberat. Flagul oprește ACCEPTUL, deci un payload de
    acțiune poate exista doar pe un turn acceptat cât timp era aprins; dacă între accept și execuție
    s-a făcut rollback, kernelul trebuie să VADĂ comanda ca să refuze onest — altfel ar rula un turn
    de text cu mesaj gol, adică exact tăcerea pe care o interzice P6."""
    raw = event.get("action")
    if not raw:
        return
    command = ActionCommand.from_jsonb(raw, conversation_id=ctx.conversation_id or "")
    if command is None:
        log.warning("web_action: payload de acțiune nerecunoscut — turul continuă fără el")
        ctx.emit("web_action_consumed", kind="unknown", outcome="action_invalid")
        return
    ctx.action = command


def _commit_facts(ctx: TurnContext) -> CommitFacts:
    """Faptele turului pentru hook-ul de commit (NX-236). Citite din `ctx.reply`/`ctx.state`, nu
    din snapshotul de la începutul turului: întrebarea care contează e cea pe care o punem ACUM.

    Sesiunea de căutare vine din `state_patch` dacă turul tocmai a creat una (tool-ul o scrie
    acolo), altfel din starea curentă — altfel butonul de paginare al primei căutări ar lipsi
    exact la turul care a produs lista."""
    pending = getattr(ctx.reply, "pending_question", None) if ctx.reply is not None else None
    pending = pending if isinstance(pending, dict) else None
    session = ctx.state_patch.get("active_search")
    if not isinstance(session, dict):
        session = ctx.state.active_search if isinstance(ctx.state.active_search, dict) else None
    return CommitFacts(
        pending_field=str(pending.get("field")) if pending and pending.get("field") else None,
        pending_attempts=int((pending or {}).get("attempts") or 1),
        active_search_ref=str((session or {}).get("fp") or "") or None,
    )


def _emit_db_metrics(ctx: TurnContext, db_acc: op_metrics.DbOpAccumulator) -> None:
    """NX-231 (P10): cât a ținut fiecare OPERAȚIE o conexiune, plus ocuparea poolului.

    `pool_metrics` (NX-161 Felia 0A) raporta un singur acquire-wait, fiindcă exista un singur
    checkout pe tur. Acum sunt N: raportăm maximul (vârful de contenție) și totalul, iar `db_ops`
    dă defalcarea pe operație — altfel „poolul e plin" rămâne o observație fără vinovat."""
    s = get_settings()
    if s.pool_metrics_enabled:
        stats = take_acquire_stats()
        n, total, mx = stats if stats is not None else (0, 0.0, 0.0)
        ctx.emit(
            "pool_metrics",
            acquire_wait_ms=round(mx, 2) if n else None,
            db_pool_wait_ms=round(mx, 2) if n else None,
            db_pool_wait_total_ms=round(total, 2) if n else None,
            db_checkouts=n,
            **bot_pool_stats(),
        )
    if s.db_op_metrics_enabled and db_acc.checkouts:
        ctx.emit("db_ops", **db_acc.as_event_props())


async def _run_turn(  # noqa: PLR0913 — o fază, mulți parametri deja validați de apelant
    db: DbProvider,
    business: BusinessConfig,
    event: dict,
    snap: TurnLoadSnapshot,
    *,
    turn_id: str,
    raw_body: str,
    safe_body: str | None,
    channel_id: str,
    channel_kind: str,
    sender_external_id: str,
    provider_msg_id: str | None,
    verified_customer_ref: str | None,
    redis: Redis | None,
    stages: list[Stage],
    deliver: bool,
    defer_aftercare: bool,
    db_acc: op_metrics.DbOpAccumulator,
    commit_hook: "CommitHook | None" = None,
    stage_hook: Callable[[str], None] | None = None,
) -> TurnResult:
    """Fazele 2-4 pe un `TurnLoadSnapshot` deja citit: compute (fără conexiune) → commit →
    aftercare. Separată de `handle_turn` ca faza de load să se vadă ca fază, nu ca preambul."""
    _s = get_settings()
    contact = snap.contact
    conversation_id = snap.conversation_id
    ctx = TurnContext(
        turn_id=turn_id,
        business=business,
        contact=contact,
        message=InboundMessage(
            provider_msg_id=event.get("provider_msg_id", ""),
            content_type=event.get("content_type", "text"),
            # D6: agentul principal vede query-ul BRUT, în memoria turului. Ce s-a schimbat e că
            # forma brută nu mai e și forma persistată — `safe_body` merge la orice sink durabil.
            body=raw_body,
            safe_body=safe_body,
            media_ref=event.get("media_id"),
            channel_kind=channel_kind,
            channel_account_id=event.get("channel_account_id", ""),
        ),
        conversation_id=conversation_id,
        history=snap.history,
        state=ConversationState.from_jsonb(snap.state),  # G6-2: agentul vede ce-a afișat
        summary=snap.summary,  # G6-2 felia 2
        facts=snap.facts,  # NX-148: memorie structurată (facts_block)
        language=snap.locale or business.default_locale,
        bot_active=snap.bot_active,
        handoff_until=snap.handoff_until,
        verified_customer_ref=verified_customer_ref,  # NX-129: login passthrough (None = anonim)
    )

    # NX-235 — starea REDUSĂ, hidratată din același jsonb, ÎNAINTE de pipeline (stagiile o
    # propun, nu o descoperă). Owner UNIC: processor (P3). Flag OFF → `None` peste tot.
    _attach_state_v2(ctx, snap.state)

    # NX-236 — acțiunea opacă a turului, din payload-ul DURABIL al mesajului (nu din request).
    _attach_action(ctx, event)

    # NX-234 — `TurnSnapshot`, construit ÎNTRE load și pipeline. Aici, nu mai devreme (are nevoie
    # de contact/conversație/state) și nu mai târziu (stagiile trebuie să-l găsească gata). Un
    # checkout SCURT propriu pentru rehidratarea de catalog, închis înainte de compute — un
    # snapshot nu ține niciodată o conexiune (NX-231).
    await _attach_snapshot(
        db,
        ctx,
        business,
        event,
        snap,
        channel_id=channel_id,
        raw_body=raw_body,
        safe_body=safe_body,
    )

    # NX-148: acoperirea memoriei (chei/contoare, nu valori — P12).
    if snap.facts:
        ctx.emit("facts_injected", n_injected=len(snap.facts))

    # NX-221 (P10): telemetria lock-ului de tur — măsurată la MARGINEA web (calea sincronă) și
    # pasată prin envelope; stagiile nu știu de lock, processor-ul doar o emite pe traiectoria
    # turului (turn_id atașat de ctx.emit). `turn_lock_wait` DOAR când s-a așteptat efectiv (>0);
    # bypass = lock neobținut în fereastră / Redis jos → procesat oricum (principiul 6).
    lock_wait_ms = event.get("turn_lock_wait_ms")
    if event.get("turn_lock_bypass"):
        ctx.emit("turn_lock_bypass", waited_ms=round(float(lock_wait_ms or 0.0), 1))
    elif lock_wait_ms:
        ctx.emit("turn_lock_wait", wait_ms=round(float(lock_wait_ms), 1))

    # NX-231 (P10): cât a stat turul în coada de admission, pe traiectoria LUI. Marginea (worker /
    # web) măsoară și pasează prin envelope — stagiile nu știu de frână. Respingerile n-au tur, deci
    # rămân contoare + log în `admission.py`. `tenant_bucket` = etichetă low-cardinality (P12).
    admission_wait_ms = event.get("admission_wait_ms")
    if admission_wait_ms:
        ctx.emit(
            "admission_wait",
            wait_ms=round(float(admission_wait_ms), 1),
            tenant_bucket=tenant_bucket(business.id),
            degraded=bool(event.get("admission_degraded")),
        )

    # NX-129: observabilitate login passthrough (P12: fără PII — doar succes/motiv, nu valoarea).
    if verified_customer_ref:
        ctx.emit("web_identity_verified")
    else:
        reject = (event.get("payload") or {}).get("identity_rejected")
        if reject:
            ctx.emit("web_identity_rejected", reason=reject)

    # Cost guard (G2c): peste plafonul zilnic → llm=None (degradare). Gates rulează oricum.
    # NX-125: reseed LAZY al contorului zilei din usage_daily (supraviețuiește pierderii Redis),
    # ÎNAINTE de pre-check; santinelă internă → o singură dată/zi, best-effort.
    if redis is not None and _s.cost_guard_enabled:
        async with db("seed_daily_cost") as conn:
            await seed_daily_cost(conn, redis, business.id)
    llm = await _llm_within_budget(ctx, redis, business, channel_kind=channel_kind)
    # Media routing (NX-76): registry de fetchers (singleton, ca llm) → gate-ul descarcă poza.
    media = get_media_registry()

    # === FAZA 2 — COMPUTE (ZERO conexiune ținută) ==========================================
    # Stagiile primesc DOAR providerul: fiecare operație își ia conexiunea și o dă înapoi. Între
    # ele (triaj nano, agent mini, tool loop, embed) poolul e liber (NX-231).
    deps = PipelineDeps(db=db, redis=redis, llm=llm, media=media, stage_hook=stage_hook)
    await run_pipeline(ctx, deps, stages)
    await persist_events(db, business.id, conversation_id, contact.id, ctx.events)
    # Evenimentele emise de aici încolo (metrici DB, conflict de stare, reply_split) apar DUPĂ
    # persistarea principală → coada listei se scrie separat, o singură dată, la final.
    events_persisted = len(ctx.events)
    await _record_turn_cost(
        redis, business, ctx, llm_used=llm is not None, channel_kind=channel_kind
    )

    if ctx.reply is None:
        if ctx.halt:
            # tăcere INTENȚIONATĂ (Gates): handoff activ / bot oprit — omul se ocupă.
            log.info("tăcere intenționată (handoff): conv=%s turn=%s", conversation_id, turn_id)
        else:
            # „niciodată tăcere" (principiul 6) e responsabilitatea stagiilor reale;
            # aici doar raportăm că turul n-a produs reply.
            log.info("tur procesat fără reply: conv=%s turn=%s", conversation_id, turn_id)
        # NX-86: tur DONE (halt/no-reply) → finalizează claim-ul (altfel reaper-ul l-ar reprocesa).
        await complete_without_reply(db, business.id, provider_msg_id)
        _emit_db_metrics(ctx, db_acc)
        await persist_events(
            db, business.id, conversation_id, contact.id, ctx.events[events_persisted:]
        )
        return TurnResult(conversation_id, contact.id, turn_id, None, None, language=ctx.language)

    # Sender (P5): garantează disclaimer-ul AI (art. 50) pe text, o singură dată, pt TOATE
    # rutele (simple/clarify/prose/fallback/cache). Idempotent — rich/welcome și-l aplatizează
    # deja → nu dublează. `ctx.reply.text` rămâne PUR (owner = stagiul; cache-ul îl stochează fără
    # disclaimer și-l re-aplică la hit). NX-134.
    reply_text = ensure_disclaimer(ctx.reply.text, ctx.language)  # self-gated

    # Sender (P5): 1-2 mesaje outbound + outbox + state, în aceeași tranzacție. NX-90: reply lung
    # de TEXT PUR → spart în max 2 fragmente (citire ușoară pe telefon); carusel/rich → un singur
    # fragment (spargerea lead-in-ului ar strica ordinea cardurilor). Spargerea NU atinge state-ul.
    is_rich = ctx.reply.rich is not None
    has_products = bool(ctx.reply.products)
    # Sync (deliver=False): un singur `content` în răspunsul HTTP — nu spargem (frontendul randează
    # o bulă). Rich/carusel rămân un fragment oricum (spargerea lead-in-ului ar strica ordinea).
    if is_rich or has_products or not deliver:
        fragments = [reply_text]
    else:
        fragments = split_reply(reply_text, limit=_s.reply_split_chars)

    # === FAZA 3 — COMMIT (un checkout, O tranzacție) =======================================
    commit = TurnCommit(
        business_id=business.id,
        conversation_id=conversation_id,
        contact_id=contact.id,
        fragments=[
            _build_fragment(
                ctx,
                frag,
                index=i,
                turn_id=turn_id,
                to=sender_external_id,
                deliver=deliver,
                is_rich=is_rich,
                has_products=has_products,
            )
            for i, frag in enumerate(fragments)
        ],
        new_state=_build_new_state(snap.state, ctx, is_rich=is_rich, has_products=has_products),
        expected_version=snap.state_version,
        provider_msg_id=provider_msg_id,
        deliver=deliver,
    )
    commit_result = await commit_turn(
        db,
        commit,
        # NX-221: la conflict de versiune, deltele turului se re-aplică pe starea PROASPĂTĂ
        # (last-writer-wins per cheie), nu pe snapshotul stale de la începutul turului.
        rebuild_state=lambda fresh: _build_new_state(
            fresh, ctx, is_rich=is_rich, has_products=has_products
        ),
        # NX-232: ledgerul web (sau orice scriere de margine) intră în ACEEAȘI tranzacție.
        on_commit=(lambda conn: commit_hook(conn, ctx.reply, ctx.language, _commit_facts(ctx)))
        if commit_hook is not None
        else None,
    )
    outbox_id = commit_result.first_outbox_id
    if commit_result.state_conflict == "retried":
        ctx.emit("state_conflict_retried")
    elif commit_result.state_conflict == "dropped":
        ctx.emit("state_conflict_dropped")
    if len(fragments) > 1:
        # Observabilitate (P10): emis după persistarea principală de events.
        # NX-122: prin ctx.emit → primește turn_id (parte din traiectoria aceluiași tur).
        ctx.emit("reply_split", parts=len(fragments))
    _emit_db_metrics(ctx, db_acc)
    # Persistăm coada de evenimente emise după persistarea principală (metrici DB +
    # state_conflict_* + reply_split) — o singură scriere, fără dubluri, în afara tranzacției de
    # commit (observabilitatea nu are voie să dea rollback pe răspuns).
    await persist_events(
        db, business.id, conversation_id, contact.id, ctx.events[events_persisted:]
    )

    # Log per-tur la succes (fără PII: doar id-uri + lungimea reply-ului, nu corpul).
    log.info(
        "tur procesat: conv=%s turn=%s reply=%dch outbox=%s",
        conversation_id,
        turn_id,
        len(reply_text),
        outbox_id,
    )
    # === FAZA 4 — AFTERCARE (cache write-back + summarizer + profil/facts) =================
    # Best-effort, DUPĂ commit, nu întârzie livrarea. Primește ID-uri + snapshot, NICIODATĂ o
    # conexiune (`aftercare.py` ia checkout-uri scurte, cu LLM-ul de fundal între ele).
    # INLINE = rulat aici, pe ACELAȘI provider. DEFERRED (`defer_aftercare`) → apelantul îl rulează
    # după ce a livrat răspunsul. Un eșec NU afectează reply/outbox/completed (deja commise).
    work = AftercareWork(
        business=business,
        conversation_id=conversation_id,
        contact_id=contact.id,
        ctx=ctx,
        inbound_msg_id=snap.inbound_msg_id,
        shadow_mode=snap.shadow_mode,
        llm=llm,
        language=ctx.language,
    )
    if not defer_aftercare:
        await run_aftercare(db, redis, work)
        work = None  # rulat inline → nimic de întors apelantului
    return TurnResult(
        conversation_id,
        contact.id,
        turn_id,
        ctx.reply.text,
        outbox_id,
        reply=ctx.reply,  # sync: apelantul mapează text+produse+chips în răspunsul HTTP
        language=ctx.language,
        aftercare=work,  # deferred → apelantul rulează run_aftercare; inline → None
    )


def _build_fragment(
    ctx: TurnContext,
    body: str,
    *,
    index: int,
    turn_id: str,
    to: str,
    deliver: bool,
    is_rich: bool,
    has_products: bool,
) -> OutboundFragment:
    """Un fragment de răspuns → descrierea lui de persistat (mesaj + eventual rând de outbox).

    Funcție PURĂ: alege payload-ul, nu scrie nimic. Extras-urile bogate (rich/comparison/carusel/
    offer) stau pe PRIMUL fragment — rich/carusel sunt un fragment oricum, iar spargerea unui
    lead-in ar strica ordinea cardurilor."""
    message_payload = {
        # NX-146 felia 2: turn_id + fragment_index. Fragmentele outbound se scriu în ACEEAȘI
        # tranzacție → `created_at` poate fi identic (now() e constant per tranzacție) →
        # `created_at asc` NU garantează ordinea. `fragment_index` = poziția reală (split NX-90).
        "turn_id": turn_id,
        "fragment_index": index,
    }
    # NX-103: cost/tokeni/latență/model pe PRIMUL fragment. Split-ul (frag 2) e același reply →
    # nu dublăm costul. `messages.cost_usd` devine real.
    usage_kwargs = _message_usage_kwargs(ctx.usage) if index == 0 else {}
    if not deliver:
        # Sync (deliver=False): NU punem în outbox — răspunsul HTTP e transportul.
        return OutboundFragment(body, message_payload, usage_kwargs, None, "")

    payload: dict = {
        "type": "text",
        "to": to,
        "text": body,
        "language": ctx.language,  # NX-127: randorul de canal re-aplică disclaimer/locale
    }
    if index == 0 and is_rich:
        payload["rich"] = asdict(ctx.reply.rich)
    elif index == 0 and ctx.reply.comparison is not None:
        # IZI-compare: tabelul structurat + cardurile produselor comparate. `type` rămâne 'text'
        # (floor = tabelul aplatizat pe canalele fără COMPARISON); web rutează pe send_rich după
        # payload['comparison']. reply_from_outbox îl reconstruiește.
        payload["comparison"] = asdict(ctx.reply.comparison)
        if ctx.reply.products:
            payload["products"] = ctx.reply.products
    elif index == 0 and has_products:
        payload["type"] = "carousel"
        payload["products"] = ctx.reply.products
    # NX-127: offer neutru (NX-114) pe primul fragment → randat NATIV (buton web / CTA), nu doar
    # floor-uit în text. reply_from_outbox îl reconstruiește pe ruta async.
    if index == 0 and ctx.reply.offer is not None:
        payload["offer"] = asdict(ctx.reply.offer)
    # idempotency_key per fragment (turn:0 / turn:1)
    return OutboundFragment(body, message_payload, usage_kwargs, payload, f"{turn_id}:{index}")
