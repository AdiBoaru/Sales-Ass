"""NX-235 — `ConversationStateV2`: snapshotul curent al nevoilor, nu jurnalul presupunerilor.

`conversations.state` (v1) e un JSONB fără versiune de schemă în care convețuiesc `constraints`,
`search_constraints`, `asked_intents`, `pending_question`, `displayed_products`, `active_search`,
`cart` și `safety`, fiecare cu alt proprietar și alte reguli. Consecința nu e estetică: un fapt
corectat de client poate REAPĂREA, fiindcă nimic nu ține minte că a fost retras — nici istoricul de
8 mesaje, nici rezumatul, nu au unde să vadă o revocare.

Forma de aici repară exact asta. O nevoie are `strength` (hard/soft), `status` (active/revoked/
superseded/unknown), `source` (cine a afirmat-o) și `updated_revision`. O corecție lasă TOMBSTONE
(`revocations[]`): de acum înainte, „bugetul nu mai contează" nu e absența unui fapt, e prezența
unei retrageri — și un rezumat care re-afirmă bugetul se lovește de ea.

**Compatibilitate, în ambele sensuri.** `adapt_v1` citește orice rând vechi (conservator: numai ce
se normalizează curat devine `active`; restul devine `unknown`, niciodată `hard`). `project_v1`
face drumul invers, ca un rând scris în v2 să fie citit corect de tot codul care încă vorbește v1 —
inclusiv după un rollback. Se scrie UN SINGUR format; celălalt e o proiecție calculată la citire,
nu o a doua copie care poate diverge.

**Ce NU intră aici:** utterance brut, PII, chain-of-thought, linii de coș (rămân ale NX-237, purtate
mai departe ca `cart` neatins) și fapte de siguranță rescrise (`safety` rămâne al NX-173). Modulul e
PUR — fără DB, fără ceas, fără random.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from src.conversation.needs import (
    HARD,
    SOFT,
    NeedVocabulary,
    norm_key,
    normalize_need,
)

log = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 2

# Caps (P4 — bugetul e impus în cod). `enforce_caps` taie ÎNAINTE de commit, ca scrierea să nu
# eșueze pe CHECK-ul din DB.
MAX_NEEDS = 16
MAX_REVOCATIONS = 12
MAX_ASKED_QUESTIONS = 8
MAX_DISPLAYED = 8
MAX_COMPARED = 4
MAX_ACTIVE_SEARCH_KEYS = 8
MAX_ID_CHARS = 64
MAX_NAME_CHARS = 80  # numele afișat e o etichetă de referință, nu descrierea produsului

# Bugetul DB e `check (pg_column_size(state) < 8192)` (migrarea 003) — pe reprezentarea BINARĂ
# jsonb, care are overhead per intrare (JEntry + aliniere + numele cheii stocat în FIECARE obiect;
# jsonb nu le deduplică). Lungimea textului JSON, singura măsurabilă fără DB, e deci un PROXY
# OPTIMIST. Overhead-ul MĂSURAT pe Postgres real (`pg_column_size` vs text): +7% pe documente cu
# multe nevoi, +12% pe liste de referințe. Bugetul de mai jos lasă 33% rezervă — de două ori
# maximul observat — iar `tests/test_conversation_state_v2_db.py` verifică relația, ca cifra să
# rămână o măsurătoare, nu o presupunere care îmbătrânește.
DB_STATE_LIMIT_BYTES = 8192
MAX_STATE_BYTES = 6144

NeedStatus = Literal["active", "revoked", "superseded", "unknown"]
NeedSource = Literal[
    "user_explicit", "action", "page_context", "catalog", "policy", "model_inferred"
]

# Sursele care pot susține o nevoie HARD. `model_inferred` lipsește DELIBERAT (D7): o inferență nu
# se promovează singură la constrângere inviolabilă, oricât de sigur ar suna modelul.
HARD_CAPABLE_SOURCES: frozenset[str] = frozenset({"user_explicit", "action", "catalog", "policy"})
# Sursele care pot ÎNVIA o cheie revocată. Doar clientul își poate lua înapoi propria retragere;
# istoricul, rezumatul și modelul nu au voie (asta e bucla pe care o închide cardul).
REVIVE_CAPABLE_SOURCES: frozenset[str] = frozenset({"user_explicit", "action"})

_STATUSES: frozenset[str] = frozenset({"active", "revoked", "superseded", "unknown"})
_SOURCES: frozenset[str] = frozenset(
    {"user_explicit", "action", "page_context", "catalog", "policy", "model_inferred"}
)
_REASON_CODES: frozenset[str] = frozenset(
    {"user_explicit", "superseded", "topic_reset", "policy", "legacy"}
)
# Cheile v1 pe care v2 nu le modelează, dar nu are voie să le piardă: `cart` e al NX-237,
# `safety` al NX-173 (P3 — un singur proprietar per câmp; v2 le CARĂ, nu le rescrie).
PASSTHROUGH_KEYS: tuple[str, ...] = ("cart", "safety")
# Câmpurile pe care v2 le MODELEAZĂ. Orice altă cheie găsită într-un document e passthrough —
# scrisă de o cale pe care reducerul n-o cunoaște. O cărăm, n-o pierdem: o cheie necunoscută e
# aproape sigur date reale ale altui card, nu gunoi.
_MODELLED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "revision",
        "topic",
        "needs",
        "revocations",
        "pending_clarification",
        "asked_questions",
        "references",
        "active_search",
        "cart_ref",
    }
)


def _clip(value: object, limit: int = MAX_ID_CHARS) -> str:
    return str(value or "")[:limit]


def _compact(doc: dict[str, Any], *, always: tuple[str, ...] = ()) -> dict[str, Any]:
    """Documentul fără câmpurile care ar fi oricum default la citire.

    Nu e cosmetică: fiecare cheie păstrată costă în jsonb un JEntry + numele cheii, în FIECARE
    obiect (jsonb nu deduplică numele de chei între obiecte). Cu 16 nevoi × 11 câmpuri, câmpurile
    nule erau singura diferență între „încape" și „CHECK violation". `always` protejează câmpurile
    a căror absență ar schimba semantica (`status`, `source`), nu doar dimensiunea."""
    return {k: v for k, v in doc.items() if k in always or v not in (None, "", [], {}, False, 0)}


@dataclass(frozen=True)
class Topic:
    """Subiectul curent. `category_key` e trigger-ul de reset scope-uit (vezi reducerul):
    schimbarea lui retrage nevoile legate de vechea categorie, nu și faptele despre persoană."""

    category_key: str | None = None
    goal: str | None = None
    changed_at_revision: int = 0

    def to_jsonb(self) -> dict[str, Any]:
        return _compact(
            {
                "category_key": self.category_key,
                "goal": self.goal,
                "changed_at_revision": self.changed_at_revision,
            }
        )

    @classmethod
    def from_jsonb(cls, raw: object) -> Topic:
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            category_key=_clip(raw.get("category_key")) or None,
            goal=_clip(raw.get("goal")) or None,
            changed_at_revision=_int(raw.get("changed_at_revision")),
        )


@dataclass(frozen=True)
class Need:
    """O nevoie canonică. `normalized_value is None` + `status='unknown'` = „s-a discutat cheia,
    dar n-avem un fapt" — NU o negație (`UNKNOWN != MISMATCH`)."""

    key: str
    operator: str
    normalized_value: str | float | bool | None = None
    strength: str = SOFT
    status: str = "active"
    source: str = "user_explicit"
    source_turn_id: str | None = None
    confirmed: bool = False
    sensitive_class: str | None = None
    updated_revision: int = 0
    # Categoria sub care a fost declarată — mecanismul prin care topic switch-ul resetează DOAR
    # nevoile scope-uite. `None` = fapt despre persoană (mărime, restricție), nelegat de subiect.
    scope: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.normalized_value is not None

    def to_jsonb(self) -> dict[str, Any]:
        return _compact(
            {
                "key": self.key,
                "operator": self.operator,
                "normalized_value": self.normalized_value,
                "strength": self.strength,
                "status": self.status,
                "source": self.source,
                "source_turn_id": self.source_turn_id,
                "confirmed": self.confirmed,
                "sensitive_class": self.sensitive_class,
                "updated_revision": self.updated_revision,
                "scope": self.scope,
            },
            # `status`/`source` decid dacă nevoia e activă și cine a afirmat-o: absența lor ar fi
            # citită ca `unknown`/`model_inferred`, adică altceva decât s-a scris.
            always=("key", "operator", "normalized_value", "strength", "status", "source"),
        )

    @classmethod
    def from_jsonb(cls, raw: object) -> Need | None:
        if not isinstance(raw, dict):
            return None
        key = norm_key(raw.get("key"))
        if not key:
            return None
        value = raw.get("normalized_value")
        if not isinstance(value, (str, int, float, bool, type(None))):
            value = None  # containere = state corupt/importat; nu le lăsăm să crească starea
        if isinstance(value, str):
            value = _clip(value)
        status = raw.get("status")
        source = raw.get("source")
        return cls(
            key=key,
            operator=_clip(raw.get("operator"), 16) or "eq",
            normalized_value=value,
            strength=HARD if raw.get("strength") == HARD else SOFT,
            status=status if status in _STATUSES else "unknown",
            source=source if source in _SOURCES else "model_inferred",
            source_turn_id=_clip(raw.get("source_turn_id")) or None,
            confirmed=bool(raw.get("confirmed")),
            sensitive_class=_clip(raw.get("sensitive_class"), 32) or None,
            updated_revision=_int(raw.get("updated_revision")),
            scope=_clip(raw.get("scope")) or None,
        )


@dataclass(frozen=True)
class Revocation:
    """Tombstone. Păstrează AMPRENTA valorii retrase, nu valoarea: destul cât să recunoaștem
    re-afirmarea aceluiași fapt, prea puțin cât să reconstituim ce era."""

    key: str
    prior_value_fingerprint: str | None = None
    source_turn_id: str | None = None
    revision: int = 0
    reason_code: str = "user_explicit"

    def to_jsonb(self) -> dict[str, Any]:
        return _compact(
            {
                "key": self.key,
                "prior_value_fingerprint": self.prior_value_fingerprint,
                "source_turn_id": self.source_turn_id,
                "revision": self.revision,
                "reason_code": self.reason_code,
            },
            always=("key", "reason_code"),
        )

    @classmethod
    def from_jsonb(cls, raw: object) -> Revocation | None:
        if not isinstance(raw, dict):
            return None
        key = norm_key(raw.get("key"))
        if not key:
            return None
        reason = raw.get("reason_code")
        return cls(
            key=key,
            prior_value_fingerprint=_clip(raw.get("prior_value_fingerprint"), 32) or None,
            source_turn_id=_clip(raw.get("source_turn_id")) or None,
            revision=_int(raw.get("revision")),
            reason_code=reason if reason in _REASON_CODES else "legacy",
        )


@dataclass(frozen=True)
class PendingClarification:
    """Întrebarea în așteptare. Maximum UNA (invariant impus de reducer). `options_refs` sunt
    REFERINȚE (chei canonice / id-uri), nu textul chips-urilor: copy-ul e server-owned și se
    recompune la afișare, nu se îngheață în stare."""

    question_id: str
    target_key: str
    reason: str = "missing_required"
    options_refs: tuple[str, ...] = ()
    asked_at_revision: int = 0
    expires_after_turns: int = 3
    attempts: int = 1
    resume_route: str | None = None

    def expired_at(self, revision: int) -> bool:
        return revision - self.asked_at_revision >= self.expires_after_turns

    def to_jsonb(self) -> dict[str, Any]:
        return _compact(
            {
                "question_id": self.question_id,
                "target_key": self.target_key,
                "reason": self.reason,
                "options_refs": list(self.options_refs),
                "asked_at_revision": self.asked_at_revision,
                "expires_after_turns": self.expires_after_turns,
                "attempts": self.attempts,
                "resume_route": self.resume_route,
            },
            always=("question_id", "target_key"),
        )

    @classmethod
    def from_jsonb(cls, raw: object) -> PendingClarification | None:
        if not isinstance(raw, dict):
            return None
        target = norm_key(raw.get("target_key"))
        if not target:
            return None
        options = tuple(
            _clip(o) for o in (raw.get("options_refs") or [])[:6] if isinstance(o, (str, int))
        )
        return cls(
            question_id=_clip(raw.get("question_id")) or f"q:{target}",
            target_key=target,
            reason=_clip(raw.get("reason"), 32) or "missing_required",
            options_refs=options,
            asked_at_revision=_int(raw.get("asked_at_revision")),
            expires_after_turns=max(1, _int(raw.get("expires_after_turns")) or 3),
            attempts=max(1, _int(raw.get("attempts")) or 1),
            resume_route=_clip(raw.get("resume_route"), 32) or None,
        )


@dataclass(frozen=True)
class AskedQuestion:
    """Semnal anti-buclă: ce chei am întrebat deja și de câte ori. Fără copy brut (P12) — doar
    cheia canonică și revizia la care s-a întâmplat."""

    key: str
    revision: int = 0
    attempts: int = 1

    def to_jsonb(self) -> dict[str, Any]:
        return _compact(
            {"key": self.key, "revision": self.revision, "attempts": self.attempts},
            always=("key",),
        )

    @classmethod
    def from_jsonb(cls, raw: object) -> AskedQuestion | None:
        if isinstance(raw, str):  # forma v1: `asked_intents` era o listă de chei
            key = norm_key(raw)
            return cls(key) if key else None
        if not isinstance(raw, dict):
            return None
        key = norm_key(raw.get("key"))
        return (
            cls(key, _int(raw.get("revision")), max(1, _int(raw.get("attempts")) or 1))
            if key
            else None
        )


@dataclass(frozen=True)
class DisplayedRef:
    """Un produs ARĂTAT clientului (P8: ref, nu obiect). `price` e prețul cu care produsul a apărut
    pe ecran — o proprietate a conversației, nu o afirmație de catalog: căile care servesc fapte
    rehidratează canonic (NX-234), iar aici păstrăm doar ce s-a văzut, ca „ceva mai ieftin" să aibă
    un reper."""

    product_id: str
    name: str = ""
    price: float | None = None

    def to_jsonb(self) -> dict[str, Any]:
        return _compact(
            {"product_id": self.product_id, "name": self.name, "price": self.price},
            always=("product_id",),
        )

    @classmethod
    def from_jsonb(cls, raw: object) -> DisplayedRef | None:
        if not isinstance(raw, dict):
            return None
        pid = _clip(raw.get("product_id") or raw.get("id"))
        if not pid:
            return None
        price = raw.get("price")
        return cls(
            product_id=pid,
            name=_clip(raw.get("name"), MAX_NAME_CHARS),
            price=float(price)
            if isinstance(price, (int, float)) and not isinstance(price, bool)
            else None,
        )


@dataclass(frozen=True)
class References:
    """Tot ce poate fi ținta unui „acesta" / „al doilea" / „ăla de dinainte".

    `displayed_revision` e ce face ordinalul VERIFICABIL: o ancoră emisă peste lista de la revizia
    5 nu are voie să selecteze din lista de la revizia 6 (failure matrix: „ordinal pe listă veche/
    reordonată → ambiguous/stale, nu produs greșit")."""

    selected_product: str | None = None
    page_product: str | None = None
    displayed_products: tuple[DisplayedRef, ...] = ()
    displayed_revision: int = 0
    compared_products: tuple[str, ...] = ()
    last_action: str | None = None

    def to_jsonb(self) -> dict[str, Any]:
        return _compact(
            {
                "selected_product": self.selected_product,
                "page_product": self.page_product,
                "displayed_products": [d.to_jsonb() for d in self.displayed_products],
                "displayed_revision": self.displayed_revision,
                "compared_products": list(self.compared_products),
                "last_action": self.last_action,
            }
        )

    @classmethod
    def from_jsonb(cls, raw: object) -> References:
        raw = raw if isinstance(raw, dict) else {}
        displayed = tuple(
            d
            for d in (DisplayedRef.from_jsonb(x) for x in (raw.get("displayed_products") or []))
            if d is not None
        )[:MAX_DISPLAYED]
        return cls(
            selected_product=_clip(raw.get("selected_product")) or None,
            page_product=_clip(raw.get("page_product")) or None,
            displayed_products=displayed,
            displayed_revision=_int(raw.get("displayed_revision")),
            compared_products=tuple(
                _clip(c) for c in (raw.get("compared_products") or [])[:MAX_COMPARED] if c
            ),
            last_action=_clip(raw.get("last_action"), 32) or None,
        )


@dataclass(frozen=True)
class ConversationStateV2:
    """Starea conversației, versionată și redusă. IMUABILĂ: singurul mod de a o schimba e
    `state_reducer.reduce`, care validează propunerea înainte să o aplice."""

    schema_version: int = STATE_SCHEMA_VERSION
    revision: int = 0
    topic: Topic = field(default_factory=Topic)
    needs: tuple[Need, ...] = ()
    revocations: tuple[Revocation, ...] = ()
    pending_clarification: PendingClarification | None = None
    asked_questions: tuple[AskedQuestion, ...] = ()
    references: References = field(default_factory=References)
    active_search: Mapping[str, Any] | None = None
    cart_ref: Mapping[str, Any] | None = None
    # Cheile v1 nemodelate (NX-237 `cart`, NX-173 `safety`) — cărate neatinse, nu rescrise.
    passthrough: Mapping[str, Any] = field(default_factory=dict)

    # --- interogări (citite de proiecții, prompt, query spec) ---------------------------------

    def active_needs(self) -> tuple[Need, ...]:
        return tuple(n for n in self.needs if n.is_active)

    def need_for(self, key: object) -> Need | None:
        """Nevoia ACTIVĂ pe o cheie (cel mult una — reducerul menține invariantul)."""
        normalized = norm_key(key)
        for need in self.needs:
            if need.key == normalized and need.is_active:
                return need
        return None

    def revoked_keys(self) -> frozenset[str]:
        """Cheile cu tombstone care NU au fost re-afirmate de atunci. Ce e aici nu are voie să
        reintre din istoric/rezumat — poarta e în reducer, lista e citită de proiecții."""
        active = {n.key for n in self.needs if n.is_active}
        return frozenset(r.key for r in self.revocations if r.key not in active)

    def asked(self, key: object) -> AskedQuestion | None:
        normalized = norm_key(key)
        return next((q for q in self.asked_questions if q.key == normalized), None)

    # --- serializare -------------------------------------------------------------------------

    def to_jsonb(self) -> dict[str, Any]:
        doc: dict[str, Any] = _compact(
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "revision": self.revision,
                "topic": self.topic.to_jsonb(),
                "needs": [n.to_jsonb() for n in self.needs],
                "revocations": [r.to_jsonb() for r in self.revocations],
                "pending_clarification": (
                    self.pending_clarification.to_jsonb() if self.pending_clarification else None
                ),
                "asked_questions": [q.to_jsonb() for q in self.asked_questions],
                "references": self.references.to_jsonb(),
                "active_search": dict(self.active_search) if self.active_search else None,
                "cart_ref": dict(self.cart_ref) if self.cart_ref else None,
            },
            always=("schema_version",),
        )
        for key, value in self.passthrough.items():
            if key not in _MODELLED_KEYS:
                doc[key] = value
        return doc

    @classmethod
    def from_jsonb(cls, raw: object) -> ConversationStateV2:
        """Hidratare DEFENSIVĂ a unui document deja v2. Orice câmp corupt cade pe default —
        un rând stricat degradează memoria, nu turul (P6)."""
        raw = raw if isinstance(raw, dict) else {}
        needs = tuple(
            n for n in (Need.from_jsonb(x) for x in (raw.get("needs") or [])) if n is not None
        )[:MAX_NEEDS]
        revocations = tuple(
            r
            for r in (Revocation.from_jsonb(x) for x in (raw.get("revocations") or []))
            if r is not None
        )[:MAX_REVOCATIONS]
        asked = tuple(
            q
            for q in (AskedQuestion.from_jsonb(x) for x in (raw.get("asked_questions") or []))
            if q is not None
        )[-MAX_ASKED_QUESTIONS:]
        return cls(
            revision=_int(raw.get("revision")),
            topic=Topic.from_jsonb(raw.get("topic")),
            needs=needs,
            revocations=revocations,
            pending_clarification=PendingClarification.from_jsonb(raw.get("pending_clarification")),
            asked_questions=asked,
            references=References.from_jsonb(raw.get("references")),
            active_search=bounded_map(raw.get("active_search")),
            cart_ref=bounded_map(raw.get("cart_ref")),
            passthrough={k: v for k, v in raw.items() if k not in _MODELLED_KEYS},
        )


# ---------------------------------------------------------------------------
# Hidratare, adapter v1 → v2 și proiecție v2 → v1
# ---------------------------------------------------------------------------


def is_v2(raw: object) -> bool:
    """Un rând e v2 dacă își declară schema. Absența versiunii înseamnă v1 — nu ghicim după
    prezența unui câmp (exact ambiguitatea pe care o repară `schema_version`)."""
    return isinstance(raw, dict) and _int(raw.get("schema_version")) >= STATE_SCHEMA_VERSION


def hydrate_state_v2(
    raw: object, vocab: NeedVocabulary, *, revision: int = 0
) -> ConversationStateV2:
    """`conversations.state` (orice versiune) → `ConversationStateV2`. NU ridică niciodată.

    `revision` e contorul MONOTON al documentului (crește o dată per commit, în `reduce_all`) —
    ancora pentru „lista de la revizia 7", pentru expirarea unei întrebări și pentru tombstone-uri.
    E deliberat contorul documentului, nu coloana `state_version`: la re-aplicare pe o stare
    proaspătă (conflict optimistic) citim revizia din documentul proaspăt, deci n-avem nevoie de un
    al doilea contor care ar putea rămâne în urmă. Parametrul `revision` e doar un PLAFON MINIM,
    util pentru rândurile v1 care n-au încă document."""
    try:
        state = ConversationStateV2.from_jsonb(raw) if is_v2(raw) else adapt_v1(raw, vocab)
    except Exception:  # noqa: BLE001 — memorie coruptă ⇒ pornim curat, nu rupem turul (P6)
        log.warning(
            # domain-leak: ok — „hidratare" = rehidratarea STĂRII, nu a pielii
            "state_v2: hidratare eșuată — pornim de la starea goală"
        )
        return ConversationStateV2(revision=revision)
    return replace(state, revision=max(state.revision, revision))


def adapt_v1(raw: object, vocab: NeedVocabulary) -> ConversationStateV2:
    """Adapter CONSERVATOR v1 → v2.

    Regula (pasul 3 din card): numai ce se normalizează curat devine `active`; restul devine
    `unknown`, NICIODATĂ `hard`. Nu se inventează revocări retroactive — o stare veche nu conține
    dovada că un fapt a fost retras, iar a o presupune ar șterge memorie reală.

    `constraints` (v1) e cazul delicat: valorile lui sunt RĂSPUNSURI BRUTE (`clarify` scria
    `constraints[field] = answer`). Trec prin aceeași poartă de normalizare ca orice altceva, deci
    o propoziție nu devine valoare de stare — devine `unknown`."""
    raw = raw if isinstance(raw, dict) else {}
    needs: list[Need] = []
    seen: set[str] = set()

    def _add(key: object, value: object, *, source: str) -> None:
        normalized = normalize_need(key, value, vocab)
        if normalized is None or normalized.key in seen:
            return
        seen.add(normalized.key)
        needs.append(
            Need(
                key=normalized.key,
                operator=normalized.operator,
                normalized_value=normalized.value,
                # Fără dovada declarației explicite, o valoare moștenită nu poate fi `hard`.
                strength=normalized.default_strength if normalized.value is not None else SOFT,
                status="active" if normalized.value is not None else "unknown",
                source=source,
                confirmed=False,
                scope=None,
            )
        )

    search_constraints = raw.get("search_constraints")
    search_constraints = search_constraints if isinstance(search_constraints, dict) else {}
    category = _clip(search_constraints.get("category_key")) or None
    for key, value in search_constraints.items():
        if vocab.is_topic_key(key):
            continue
        if isinstance(value, list):
            for item in value[:MAX_NEEDS]:
                _add_list_item(needs, seen, key, item, vocab, source="user_explicit")
        else:
            _add(key, value, source="user_explicit")

    constraints = raw.get("constraints")
    for key, value in (constraints if isinstance(constraints, dict) else {}).items():
        if not vocab.is_topic_key(key):
            _add(key, value, source="user_explicit")

    asked = tuple(
        q
        for q in (AskedQuestion.from_jsonb(x) for x in (raw.get("asked_intents") or []))
        if q is not None
    )[-MAX_ASKED_QUESTIONS:]

    return ConversationStateV2(
        topic=Topic(category_key=category),
        needs=tuple(needs)[:MAX_NEEDS],
        revocations=(),
        pending_clarification=_pending_from_v1(raw.get("pending_question"), vocab),
        asked_questions=asked,
        references=References(
            displayed_products=tuple(
                d
                for d in (DisplayedRef.from_jsonb(x) for x in (raw.get("displayed_products") or []))
                if d is not None
            )[:MAX_DISPLAYED],
        ),
        active_search=bounded_map(raw.get("active_search")),
        # NX-237: ref-ul coșului canonic e MODELAT în v2 — dacă ar cădea în passthrough, ar fi
        # exclus la serializare (passthrough filtrează cheile modelate) și s-ar pierde tăcut.
        cart_ref=bounded_map(raw.get("cart_ref")),
        passthrough={k: raw[k] for k in PASSTHROUGH_KEYS if k in raw},
    )


def _add_list_item(
    needs: list[Need],
    seen: set[str],
    key: object,
    item: object,
    vocab: NeedVocabulary,
    *,
    source: str,
) -> None:
    """O cheie de tip listă (`concerns`) devine O nevoie per valoare canonică — `contains` e un
    predicat pe element, nu pe listă. Cheia de unicitate include valoarea, ca două nevoi de acnee
    și pori să coexiste."""
    normalized = normalize_need(key, item, vocab)
    if normalized is None or normalized.value is None:
        return
    marker = f"{normalized.key}={normalized.value}"
    if marker in seen:
        return
    seen.add(marker)
    needs.append(
        Need(
            key=normalized.key,
            operator=normalized.operator,
            normalized_value=normalized.value,
            strength=normalized.default_strength,
            status="active",
            source=source,
        )
    )


def _pending_from_v1(raw: object, vocab: NeedVocabulary) -> PendingClarification | None:
    """`pending_question` (v1) → `pending_clarification`. `question_id` e DERIVAT determinist din
    slot + încercare: replay-ul aceleiași stări dă același id (fără ceas, fără random).

    Slotul se CANONIZEAZĂ prin vocabular („budget" → „budget_max"): altfel întrebarea pusă și
    nevoia pe care o umple ar trăi sub două chei diferite, iar semnalul anti-buclă ar căuta o
    cheie pe care nimeni n-o scrie. Un slot fără corespondent (`intent`) rămâne ca atare."""
    if not isinstance(raw, dict):
        return None
    target = norm_key(raw.get("field"))
    if not target:
        return None
    spec = vocab.spec_for(target)
    target = spec.key if spec is not None else target
    attempts = max(1, _int(raw.get("attempts")) or 1)
    return PendingClarification(
        question_id=f"legacy:{target}:{attempts}",
        target_key=target,
        reason="legacy",
        asked_at_revision=0,
        attempts=attempts,
        resume_route=_clip(raw.get("resume_route"), 32) or None,
    )


def active_needs(ctx: object) -> tuple[Need, ...]:
    """Nevoile active ale unui `TurnContext`, sau `()`.

    Tolerant la `state_v2=None`/obiect străin DELIBERAT, ca `page_anchor_from_snapshot` (NX-234):
    `TurnContext.state_v2` e tipizat `Any` (ca să nu importăm `src.conversation` în models), iar
    consumatorii sunt pe hot path. Absența stării înseamnă „fără nevoi moștenite", nu o excepție."""
    state = getattr(ctx, "state_v2", None)
    return state.active_needs() if isinstance(state, ConversationStateV2) else ()


def project_v1(state: ConversationStateV2) -> dict[str, Any]:
    """`ConversationStateV2` → forma v1 a JSONB-ului, pentru cititorii nemigrați.

    Nu e un al doilea format persistat: e o PROIECȚIE calculată la citire dintr-o singură sursă de
    adevăr. De asta un rollback e sigur — codul vechi vede exact ce se aștepta să vadă, iar
    revocările nu se pierd (o cheie revocată pur și simplu nu apare în proiecție)."""
    search_constraints: dict[str, Any] = {}
    constraints: dict[str, Any] = {}
    for need in state.active_needs():
        if need.operator == "contains":
            search_constraints.setdefault(need.key, []).append(need.normalized_value)
        else:
            search_constraints[need.key] = need.normalized_value
        constraints[need.key] = need.normalized_value
    if state.topic.category_key:
        search_constraints["category_key"] = state.topic.category_key

    pending = state.pending_clarification
    doc: dict[str, Any] = {
        "displayed_products": [d.to_jsonb() for d in state.references.displayed_products],
        "active_search": dict(state.active_search) if state.active_search else None,
        "pending_question": (
            {
                "field": pending.target_key,
                "resume_route": pending.resume_route,
                "attempts": pending.attempts,
                "question_id": pending.question_id,
            }
            if pending
            else None
        ),
        "asked_intents": [q.key for q in state.asked_questions],
        "constraints": constraints,
        "search_constraints": search_constraints,
        # NX-237: cititorii v1 văd ref-ul coșului canonic exact ca într-un rând v1.
        "cart_ref": dict(state.cart_ref) if state.cart_ref else None,
    }
    for key, value in state.passthrough.items():
        if key not in _MODELLED_KEYS:
            doc[key] = value
    return doc


# ---------------------------------------------------------------------------
# Caps + serializare mărginită
# ---------------------------------------------------------------------------


def enforce_caps(state: ConversationStateV2) -> ConversationStateV2:
    """Taie starea la caps-urile din cod, cu PRIORITATE: `hard` și `sensitive` nu se pierd
    niciodată; se sacrifică întâi soft-urile vechi, apoi tombstone-urile vechi.

    Ordinea contează: a tăia un `hard` ca să încapă un `soft` ar transforma bugetul de memorie
    într-o portiță de relaxare a constrângerilor."""

    def _priority(indexed: tuple[int, Need]) -> tuple[int, int, int]:
        index, need = indexed
        keep = 0 if (need.strength == HARD or need.sensitive_class) else 1
        return (keep, -need.updated_revision, -index)

    # Selecția e pe INDECȘI, nu pe identitate de obiect: două nevoi egale (aceeași cheie, aceeași
    # valoare, în stări diferite) sunt dataclass-uri egale, iar o comparație pe valoare le-ar
    # confunda. Ordinea de afișare rămâne cea de inserție — stabilă pentru diff-uri și prompt.
    kept = {index for index, _ in sorted(enumerate(state.needs), key=_priority)[:MAX_NEEDS]}
    ordered = tuple(need for index, need in enumerate(state.needs) if index in kept)
    return replace(
        state,
        needs=ordered,
        revocations=state.revocations[-MAX_REVOCATIONS:],
        asked_questions=state.asked_questions[-MAX_ASKED_QUESTIONS:],
        references=replace(
            state.references,
            displayed_products=state.references.displayed_products[:MAX_DISPLAYED],
            compared_products=state.references.compared_products[:MAX_COMPARED],
        ),
        active_search=bounded_map(state.active_search),
    )


def serialize(state: ConversationStateV2) -> tuple[dict[str, Any], int, bool]:
    """Documentul de persistat + dimensiunea lui + dacă a fost nevoie de degradare.

    Bugetul DB (CHECK-ul din 003) se verifică ÎNAINTE de commit: o stare peste limită ar face
    UPDATE-ul să eșueze cu `CheckViolationError`, iar tranzacția de commit ar da rollback — adică
    am pierde RĂSPUNSUL din cauza memoriei. Nu e ipotetic: pe calea v1 exact asta se întâmplă azi
    cu un `cart` corupt/importat.

    Degradarea taie în ordinea inversă a valorii — referințe afișate → întrebări puse →
    tombstone-uri → nevoi `soft` → sesiune/coș — și **niciodată** nevoi `hard`/sensibile. Ultimele
    sacrificate sunt cheile altor carduri: `cart` (NX-237) înaintea lui `safety` (NX-173), care
    hrănește un gate P0 și pleacă doar dacă singur depășește bugetul."""
    capped = enforce_caps(state)
    doc = capped.to_jsonb()
    size = _byte_size(doc)
    if size <= MAX_STATE_BYTES:
        return doc, size, False

    for shrink in (
        lambda s: replace(s, references=replace(s.references, displayed_products=())),
        lambda s: replace(s, asked_questions=()),
        lambda s: replace(s, revocations=s.revocations[-3:]),
        lambda s: replace(
            s, needs=tuple(n for n in s.needs if n.strength == HARD or n.sensitive_class)
        ),
        lambda s: replace(s, active_search=None, cart_ref=None),
        # Coșul e al NX-237 și de aceea îl cărăm neatins — dar „neatins" nu poate însemna „chiar
        # dacă blochează scrierea". Un coș care singur depășește bugetul e deja corupt (cart_add
        # plafonează la 10 linii); îl pierdem VIZIBIL (`degraded=True`), nu pierdem turul.
        lambda s: replace(s, passthrough={k: v for k, v in s.passthrough.items() if k != "cart"}),
    ):
        capped = shrink(capped)
        doc = capped.to_jsonb()
        size = _byte_size(doc)
        if size <= MAX_STATE_BYTES:
            return doc, size, True

    # Ultima plasă: revizia + siguranța, atât. Mai bine memorie pierdută decât un tur care eșuează
    # la scriere (P6).
    minimal = ConversationStateV2(
        revision=capped.revision,
        passthrough={k: v for k, v in capped.passthrough.items() if k == "safety"},
    )
    doc = minimal.to_jsonb()
    size = _byte_size(doc)
    if size <= MAX_STATE_BYTES:
        return doc, size, True
    # Chiar și siguranța singură depășește bugetul ⇒ rândul e corupt dincolo de reparație. Scriem
    # documentul gol: gate-ul de siguranță redetectează din mesaj (NX-173), turul supraviețuiește.
    doc = ConversationStateV2(revision=capped.revision).to_jsonb()
    return doc, _byte_size(doc), True


def size_bucket(size: int) -> str:
    """Bucket low-cardinality pentru `state_size_bytes_bucket` (P10/P12: fără valori exacte în
    labels). Ultimul bucket e „peste bugetul de cod" — semnalul că degradarea a intrat în joc."""
    for edge, label in ((1024, "0-1k"), (2048, "1-2k"), (4096, "2-4k"), (MAX_STATE_BYTES, "4-6k")):
        if size <= edge:
            return label
    return "over-budget"


def _byte_size(doc: Mapping[str, Any]) -> int:
    try:
        return len(json.dumps(doc, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_STATE_BYTES + 1  # neserializabil = tratat ca oversize → degradare controlată


def bounded_map(raw: object) -> dict[str, Any] | None:
    """Dict mărginit (ref-uri de sesiune / coș): maximum `MAX_ACTIVE_SEARCH_KEYS` chei, valori
    scalare sau liste scurte. Ce nu e mărginit nu intră — memoria nu e un depozit."""
    if not isinstance(raw, dict) or not raw:
        return None
    out: dict[str, Any] = {}
    for key in list(raw)[:MAX_ACTIVE_SEARCH_KEYS]:
        value = raw[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[_clip(key, 32)] = _clip(value, 128) if isinstance(value, str) else value
        elif isinstance(value, list):
            out[_clip(key, 32)] = [
                _clip(v, 64) if isinstance(v, str) else v
                for v in value[: MAX_DISPLAYED * 3]
                if isinstance(v, (str, int, float, bool))
            ]
        elif isinstance(value, dict):
            out[_clip(key, 32)] = {
                _clip(k, 32): (_clip(v, 64) if isinstance(v, str) else v)
                for k, v in list(value.items())[:MAX_ACTIVE_SEARCH_KEYS]
                if isinstance(v, (str, int, float, bool))
            }
    return out or None


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def state_diff(v1_raw: object, projected: Mapping[str, Any]) -> dict[str, list[str]]:
    """Diff SHADOW între starea v1 reală și proiecția v2, pe VALORI CANONICE (rollout, pasul 1).

    Întoarce doar CHEI, niciodată valori (P12: diff-ul intră în telemetrie). Diferența de formă
    (v1 ținea răspunsuri brute) e așteptată — de aceea comparăm mulțimile de chei active, nu
    textul."""
    left = v1_raw if isinstance(v1_raw, dict) else {}
    result: dict[str, list[str]] = {}
    for field_name in ("constraints", "search_constraints"):
        old = {norm_key(k) for k, v in (left.get(field_name) or {}).items() if v not in (None, "")}
        new = {
            norm_key(k) for k, v in (projected.get(field_name) or {}).items() if v not in (None, "")
        }
        if old - new:
            result[f"{field_name}_missing"] = sorted(old - new)
        if new - old:
            result[f"{field_name}_added"] = sorted(new - old)
    old_ids = [
        _clip(p.get("product_id") or p.get("id")) for p in (left.get("displayed_products") or [])
    ]
    new_ids = [_clip(p.get("product_id")) for p in (projected.get("displayed_products") or [])]
    if old_ids != new_ids:
        result["displayed_products"] = ["differs"]
    if bool(left.get("pending_question")) != bool(projected.get("pending_question")):
        result["pending_question"] = ["differs"]
    return result


__all__ = [
    "HARD_CAPABLE_SOURCES",
    "MAX_ASKED_QUESTIONS",
    "MAX_COMPARED",
    "MAX_DISPLAYED",
    "MAX_NEEDS",
    "MAX_REVOCATIONS",
    "DB_STATE_LIMIT_BYTES",
    "MAX_STATE_BYTES",
    "PASSTHROUGH_KEYS",
    "REVIVE_CAPABLE_SOURCES",
    "STATE_SCHEMA_VERSION",
    "AskedQuestion",
    "ConversationStateV2",
    "DisplayedRef",
    "Need",
    "NeedSource",
    "NeedStatus",
    "PendingClarification",
    "References",
    "Revocation",
    "Topic",
    "adapt_v1",
    "bounded_map",
    "enforce_caps",
    "hydrate_state_v2",
    "is_v2",
    "project_v1",
    "serialize",
    "size_bucket",
    "state_diff",
]
