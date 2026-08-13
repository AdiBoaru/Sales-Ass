"""NX-234 — `TurnSnapshot`: tot ce știe turul despre lume, ca DATE IMUABILE.

`TurnLoadSnapshot` (NX-231) a stabilit principiul pentru citirile din DB: rezultatul fazei de load
e un obiect frozen, nu un handle — nimeni nu poate „mai trage un query" din el peste un await
extern. Cardul ăsta îl extinde la tot contextul turului, inclusiv la partea care vine din afară:
suprafața paginii, produsul ancoră, varianta, categoria, coșul.

**De ce imuabil, nu un dict care se completează pe drum.** Un context mutabil ar însemna că
promptul, tool-urile și projectorul pot vedea trei stări diferite ale aceluiași turn — fiecare
făcând propriul lookup, la momente diferite, cu prețuri care se pot schimba între ele. Faptele
comerciale se rehidratează O SINGURĂ DATĂ per turn, cu `source` și `fetched_at` atașate; cine are
nevoie de altceva cere explicit, nu descoperă accidental.

**Ce NU are voie să conțină** (și `snapshot_safety_violations` o dovedește, nu o promite):
`asyncpg.Connection`, client Redis, sesiune HTTP, ORM lazy, callback, JWT/action token brut,
`RawText` scurs ca `str`, sau orice container mutabil partajat. Un snapshot care ține o conexiune
nu mai e un snapshot, e un handle deghizat — iar handle-urile trec granițele de fază.

**`RawInbound` e singura excepție, și e o excepție tipizată.** D6 cere ca agentul principal să
vadă query-ul BRUT; `RawText` refuză însă `str()`, `repr()`, f-string și `json.dumps`, deci nu
poate ajunge accidental într-un log sau într-un event. `to_safe_dict()` nu îl emite deloc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from src.catalog.context_resolver import (
    CartSnapshot,
    SurfaceContext,
    resolve_surface_context,
)
from src.config import get_settings
from src.db.provider import DbProvider
from src.models import BusinessConfig, ConversationState
from src.privacy import RawInbound, RawText, SafeInbound
from src.web.context import NormalizedContext, negotiate_locale

log = logging.getLogger(__name__)

# Versiunea FORMEI snapshotului. Un consumator (NX-235/240) care se așteaptă la altceva o vede
# explicit, în loc să deducă din prezența unui câmp.
SNAPSHOT_SCHEMA_VERSION = "turn-snapshot.v1"
# Bugetul de refs proiectate în snapshot (P4: impus în cod, nu în prompt).
MAX_DISPLAYED_REFS = 8
MAX_REASONS = 12


@dataclass(frozen=True)
class TenantRef:
    """Identitatea SERVER-OWNED a turului. `business_id` intră aici o singură dată, derivat din
    sesiune — niciodată din request, niciodată din outputul modelului (P7)."""

    business_id: str
    channel_id: str | None = None
    channel_kind: str = "webchat"
    channel_account_id: str = ""


@dataclass(frozen=True)
class ActorRef:
    """Cine vorbește, ca REFERINȚE. `session_ref` e deja hash-uit la sursă (NX-232); nici
    `visitor_id`, nici telefonul, nici `id_token`-ul nu ajung aici (P12)."""

    contact_id: str
    session_ref: str | None = None
    verified: bool = False


@dataclass(frozen=True)
class ConversationRef:
    conversation_id: str
    revision: int = 0
    state_schema_version: int = 1


@dataclass(frozen=True)
class SafeInput:
    """Proiecția IMUABILĂ a lui `SafeInbound` (NX-230) — forma persistabilă a inputului.

    De ce o proiecție și nu obiectul: `SafeInbound` e `frozen`, dar poartă `counts: dict`, adică
    un container mutabil. Într-un snapshot care se plimbă prin tot turul, „frozen cu un dict
    înăuntru" e frozen doar pe hârtie. Din `counts` păstrăm exact ce folosește telemetria —
    bucketul low-cardinality — iar categoriile devin `tuple`."""

    text: str = ""
    categories: tuple[str, ...] = ()
    count_bucket: str = "0"
    degraded: bool = False

    @property
    def had_pii(self) -> bool:
        return bool(self.categories)

    @classmethod
    def of(cls, safe: SafeInbound | None) -> SafeInput:
        if safe is None:
            return cls()
        return cls(
            text=safe.text,
            categories=tuple(safe.categories),
            count_bucket=safe.count_bucket(),
            degraded=safe.degraded,
        )


@dataclass(frozen=True)
class InputSnapshot:
    """Inputul turului în AMBELE forme, cu granița vizibilă în tipuri: `raw` e request-scoped
    (`RawText`, refuză să se afișeze), `safe` e forma persistabilă (NX-230)."""

    raw: RawInbound | None
    safe: SafeInput = field(default_factory=SafeInput)
    content_type: str = "text"

    @property
    def safe_text(self) -> str:
        return self.safe.text


@dataclass(frozen=True)
class DisplayedRef:
    """Ce a arătat ASISTENTUL în tururile anterioare (din state, P8: ref-uri, nu obiecte).
    Distinct de ancora paginii: una e memoria conversației, cealaltă e unde se află clientul."""

    product_id: str
    name: str
    price: float


@dataclass(frozen=True)
class MemorySnapshot:
    """Adapter TEMPORAR peste `ConversationState` v1 până la `ConversationStateV2` (NX-235).
    Există ca tip separat exact ca să fie ușor de înlocuit: consumatorii citesc de aici, nu
    din `ctx.state`, deci schimbarea de sursă nu se propagă în call-site-uri."""

    displayed: tuple[DisplayedRef, ...] = ()
    pending_field: str | None = None
    constraints_keys: tuple[str, ...] = ()
    schema_version: int = 1

    @classmethod
    def from_state(cls, state: ConversationState) -> MemorySnapshot:
        pending = state.pending_question if isinstance(state.pending_question, dict) else {}
        return cls(
            displayed=tuple(
                DisplayedRef(p.product_id, p.name, float(p.price))
                for p in (state.displayed_products or [])[:MAX_DISPLAYED_REFS]
            ),
            pending_field=(pending.get("field") or None),
            constraints_keys=tuple(sorted((state.constraints or {}).keys()))[:MAX_REASONS],
        )


@dataclass(frozen=True)
class TurnSnapshot:
    """Contractul imuabil al turului. Construit O DATĂ, înainte de pipeline; citit de toată lumea.

    `pipeline_version` + `conversation.revision` sunt legătura anti-stale: un context calculat
    pentru conversația de dinainte de un reset nu se poate atașa turului nou (`matches`)."""

    schema_version: str
    turn_id: str
    tenant: TenantRef
    actor: ActorRef
    conversation: ConversationRef
    input: InputSnapshot
    surface: SurfaceContext
    memory: MemorySnapshot
    locale: str
    locale_reason: str | None = None
    deadline_at: datetime | None = None
    pipeline_version: str | None = None
    created_at: datetime | None = None

    @property
    def cart(self) -> CartSnapshot:
        return self.surface.cart

    @property
    def page_product_id(self) -> str | None:
        """Ancora canonică a paginii, sau None. Singurul drum prin care „produsul acesta" de pe
        PDP devine un id — și el trece prin rehidratarea tenant-scoped, nu prin browser."""
        return self.surface.anchor_product_id

    def matches(self, *, conversation_id: str, revision: int | None = None) -> bool:
        """Snapshotul aparține ACESTEI conversații (și, dacă se cere, acestei revizii)?

        Failure matrix: la reset / schimbare de conversație, contextul vechi NU se poate atașa
        turului nou. Verificarea e explicită fiindcă un snapshot e un obiect care poate
        supraviețui unui `await` — deci și unei schimbări de stare."""
        if self.conversation.conversation_id != conversation_id:
            return False
        return revision is None or self.conversation.revision == revision

    def to_safe_dict(self) -> dict[str, Any]:
        """Serializare DETERMINISTĂ și SAFE — pentru loguri, evenimente, teste și manual drive.

        Nu conține inputul brut, niciun token și nicio valoare de identitate. Aceleași date
        produc aceiași bytes (chei sortate, fără timestamps de moment), ca un diff de snapshot să
        însemne o schimbare reală, nu zgomot de rulare."""
        p = self.surface.product
        v = self.surface.variant
        c = self.surface.category
        return {
            "schema_version": self.schema_version,
            "turn_id": self.turn_id,
            "tenant": {
                "business_id": self.tenant.business_id,
                "channel_kind": self.tenant.channel_kind,
            },
            "actor": {"contact_id": self.actor.contact_id, "verified": self.actor.verified},
            "conversation": {
                "id": self.conversation.conversation_id,
                "revision": self.conversation.revision,
            },
            "input": {
                "content_type": self.input.content_type,
                "safe_len": len(self.input.safe_text),
                "had_pii": self.input.safe.had_pii,
            },
            "locale": self.locale,
            "locale_reason": self.locale_reason,
            "surface": {
                "surface": self.surface.surface,
                "status": self.surface.status,
                "reasons": list(self.surface.reasons),
                "relation_rejections": [list(r) for r in self.surface.relation_rejections],
                "product": None
                if p is None
                else {
                    "id": p.product_id,
                    "name": p.name,
                    "price": p.price,
                    "list_price": p.list_price,
                    "currency": p.currency,
                    "availability": p.availability,
                    "rating": p.rating,
                    "review_count": p.review_count,
                    "source": p.source,
                    "freshness": None
                    if p.freshness is None
                    else {"bucket": p.freshness.bucket, "stale": p.freshness.stale},
                    "unknown": sorted(p.unknown),
                },
                "variant": None
                if v is None
                else {
                    "id": v.variant_id,
                    "product_id": v.product_id,
                    "label": v.label,
                    "price": v.price,
                    "stock": v.stock,
                    "source": v.source,
                    "unknown": sorted(v.unknown),
                },
                "category": None
                if c is None
                else {"id": c.category_id, "slug": c.slug, "path": c.path, "source": c.source},
                "cart": {"status": self.surface.cart.status, "reason": self.surface.cart.reason},
            },
            "memory": {
                "displayed": [d.product_id for d in self.memory.displayed],
                "pending_field": self.memory.pending_field,
                "constraints_keys": list(self.memory.constraints_keys),
            },
            "pipeline_version": self.pipeline_version,
        }


# ── Builder ─────────────────────────────────────────────────────────────────────────────────


async def build_turn_snapshot(
    db: DbProvider,
    *,
    turn_id: str,
    business: BusinessConfig,
    contact_id: str,
    conversation_id: str,
    conversation_revision: int,
    state: ConversationState,
    raw_inbound: RawInbound | None,
    safe_inbound: SafeInbound | None,
    context: NormalizedContext | None,
    channel_id: str | None = None,
    channel_kind: str = "webchat",
    channel_account_id: str = "",
    session_ref: str | None = None,
    verified: bool = False,
    content_type: str = "text",
    deadline_at: datetime | None = None,
    pipeline_version: str | None = None,
    now: datetime | None = None,
) -> TurnSnapshot:
    """Construiește snapshotul în FAZE, în ordinea în care informația devine disponibilă:
    refs de auth → conversație/state → hidratare catalog → validare de relație → proiecție safe.

    Singurul I/O e rehidratarea de catalog (`resolve_surface_context`): UN checkout scurt,
    etichetat, care se închide înainte ca snapshotul să existe. Un snapshot nu poate „mai citi
    ceva" mai târziu, fiindcă n-are cu ce (NX-231).

    Fără context de pagină (orice canal non-web, sau web fără `context`) drumul e pur: zero
    query, `surface.status='absent'`, restul snapshotului identic."""
    now = now or datetime.now(UTC)
    locale, locale_reason = negotiate_locale(
        context.locale if context else None,
        supported=business.supported_locales or [],
        default=business.default_locale or "ro",
    )
    surface = await resolve_surface_context(db, business.id, context, now=now)
    return TurnSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        turn_id=turn_id,
        tenant=TenantRef(
            business_id=business.id,
            channel_id=channel_id,
            channel_kind=channel_kind,
            channel_account_id=channel_account_id,
        ),
        actor=ActorRef(contact_id=contact_id, session_ref=session_ref, verified=verified),
        conversation=ConversationRef(
            conversation_id=conversation_id, revision=conversation_revision
        ),
        input=InputSnapshot(
            raw=raw_inbound, safe=SafeInput.of(safe_inbound), content_type=content_type
        ),
        surface=surface,
        memory=MemorySnapshot.from_state(state),
        locale=locale,
        locale_reason=locale_reason,
        deadline_at=deadline_at,
        pipeline_version=pipeline_version or get_settings().web_context_pipeline_version,
        created_at=now,
    )


def without_evidence(snapshot: TurnSnapshot) -> TurnSnapshot:
    """Același snapshot, cu contextul de pagină golit de fapte (păstrând statusul și motivele).

    Folosit de rollout: cu `WEB_CONTEXT_PROMPT_ENABLED` stins, hidratarea rulează și se măsoară
    (shadow), dar nimic din ea nu poate ajunge în prompt — separarea cerută de card între
    „validation enforcement" și „prompt exposure", impusă de tip, nu de un `if` la fiecare
    consumator."""
    return replace(
        snapshot,
        surface=replace(snapshot.surface, product=None, variant=None, category=None),
    )


# ── Poarta de siguranță (folosită de teste și de manual drive) ──────────────────────────────

_FORBIDDEN_TYPE_MARKERS: tuple[str, ...] = (
    "Connection",
    "ConnectionPool",
    "Pool",
    "Redis",
    "ClientSession",
    "Cursor",
    "Session",
    "Transaction",
    "Task",
    "Future",
)
_MUTABLE_TYPES = (list, dict, set, bytearray)


def _looks_like_token(value: str) -> bool:
    """Euristică pentru JWT/action token scurs ca string. `eyJ` = `{"` în base64url — prefixul
    practic universal al unui JWT. Nu e un detector complet și nu pretinde să fie: e o alarmă
    pentru cazul „cineva a pus tokenul în snapshot ca să-l aibă la îndemână"."""
    return value.startswith("eyJ") or value.count(".") >= 2 and len(value) > 120


def snapshot_safety_violations(obj: Any, *, path: str = "snapshot", depth: int = 0) -> list[str]:
    """Traversează snapshotul și întoarce ÎNCĂLCĂRILE găsite (listă goală = curat).

    Există ca funcție, nu ca listă de assert-uri într-un test, ca să poată fi folosită și de
    manual drive: „dovedește că snapshotul nu conține X" e o afirmație verificabilă, nu o
    convingere. Un `RawText` e PERMIS (e bariera, nu scurgerea); un `str` care arată a token, nu."""
    if depth > 12:
        return [f"{path}: adâncime peste limită (structură ciclică?)"]
    out: list[str] = []
    if obj is None or isinstance(obj, (bool, int, float, datetime)):
        return out
    if isinstance(obj, RawText):
        return out  # bariera de privacy: nu se afișează, deci nu se scurge
    if isinstance(obj, str):
        if _looks_like_token(obj):
            out.append(f"{path}: string care arată a token/JWT")
        return out
    if isinstance(obj, _MUTABLE_TYPES):
        out.append(f"{path}: container MUTABIL ({type(obj).__name__}) într-un snapshot frozen")
        return out
    if callable(obj) and not isinstance(obj, type):
        out.append(f"{path}: callable ({getattr(obj, '__name__', '?')}) — un snapshot nu execută")
        return out
    type_name = type(obj).__name__
    if any(marker in type_name for marker in _FORBIDDEN_TYPE_MARKERS):
        out.append(f"{path}: tip interzis {type_name} (handle, nu dată)")
        return out
    if isinstance(obj, (tuple, frozenset)):
        for i, item in enumerate(sorted(obj, key=repr) if isinstance(obj, frozenset) else obj):
            out.extend(snapshot_safety_violations(item, path=f"{path}[{i}]", depth=depth + 1))
        return out
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is not None:
        for name in fields:
            out.extend(
                snapshot_safety_violations(
                    getattr(obj, name, None), path=f"{path}.{name}", depth=depth + 1
                )
            )
        return out
    slots = getattr(obj, "__slots__", None)
    if slots is None and getattr(obj, "__dict__", None) is not None:
        out.append(f"{path}: obiect cu `__dict__` mutabil ({type_name})")
    return out


@dataclass(frozen=True)
class SnapshotEvents:
    """Evenimentele derivate dintr-un snapshot — DATE, emise de processor (P10: stagiile nu
    știu că sunt măsurate). Low-cardinality prin construcție: statusuri, motive și buckets."""

    items: tuple[tuple[str, dict[str, Any]], ...] = field(default_factory=tuple)


def snapshot_events(snapshot: TurnSnapshot) -> SnapshotEvents:
    """Observabilitatea contextului, ca listă de `(tip, properties)`.

    P12: niciun ID extern, niciun text de user, nicio valoare de preț în labels — doar suprafață,
    outcome, motiv și buckets. `web_context_query_count` e în probe (bucket), nu per-turn ID."""
    s = snapshot.surface
    items: list[tuple[str, dict[str, Any]]] = [
        (
            "web_context_validated",
            {
                "surface": s.surface,
                "outcome": s.status,
                "reason": s.reasons[0] if s.reasons else None,
            },
        )
    ]
    if s.status != "absent":
        items.append(
            (
                "web_context_hydration_ms",
                {"source": "catalog", "outcome": s.status, "ms": s.hydration_ms},
            )
        )
        items.append(("web_context_query_count", {"bucket": "1" if s.query_count <= 1 else "2+"}))
    for relation, reason in s.relation_rejections:
        items.append(("web_context_relation_rejected", {"relation": relation, "reason": reason}))
    for entity, evidence in (("product", s.product), ("variant", s.variant)):
        fresh = getattr(evidence, "freshness", None)
        if fresh is not None and fresh.stale:
            items.append(("web_context_stale", {"entity_type": entity, "age_bucket": fresh.bucket}))
    if snapshot.locale_reason:
        items.append(("web_context_locale_fallback", {"reason": snapshot.locale_reason}))
    return SnapshotEvents(tuple(items))


__all__ = [
    "MAX_DISPLAYED_REFS",
    "SNAPSHOT_SCHEMA_VERSION",
    "ActorRef",
    "ConversationRef",
    "DisplayedRef",
    "InputSnapshot",
    "MemorySnapshot",
    "SafeInput",
    "SnapshotEvents",
    "TenantRef",
    "TurnSnapshot",
    "build_turn_snapshot",
    "snapshot_events",
    "snapshot_safety_violations",
    "without_evidence",
]
