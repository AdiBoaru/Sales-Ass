"""NX-173 — `SafetyPolicy`: **singurul** API prin care se decide ce poate fi expus clientului.

Review Codex pe #229: gate-ul împrăștiat pe câteva call-site-uri a scăpat exact pe căile care NU
trec prin tool loop (rehidratare din `displayed_products`, cross-sell, „mai ieftin", superlativ) și
pe mutațiile de comerț (cart/checkout/back-in-stock scriau ÎNAINTE de orice filtrare). Concluzia:
nu „mai multe filtre", ci **un singur punct de decizie** cu rezultat TIPIZAT, chemat de toate căile.

    policy = SafetyPolicy.for_turn(ctx)          # o dată pe tur (context din state, nu din istoric)
    d = policy.evaluate(products, purpose="search")
    d.kept / d.blocked / d.contexts / d.rule_ids / d.must_refer / d.message_key

Reguli de folosire (invarianți):
  - **Expunere**: orice cale care ajunge în `ctx.retrieval` / `ctx.reply` / carduri / offer trece
    prin `evaluate` ÎNAINTE de a compune.
  - **Mutație**: cart / checkout / back-in-stock cheamă `allows(product)` ÎNAINTE de scriere. O
    filtrare de rezultat NU poate anula un rând deja scris în DB.
  - **Copy**: policy-ul NU produce text. Întoarce chei; `messages.py` randează localizat, o dată.
  - **Fail-CLOSED**: registru invalid + context de siguranță activ → `unavailable` → nu expunem
    nimic (nu servim catalog nefiltrat pretinzând că e verificat). Fără context → neafectat.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.config import get_settings
from src.safety.contraindications import (
    Block,
    RegistryError,
    detect_contexts_in_turn,
    filter_products,
    has_verifiable_ingredients,
    load_registry,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    """Rezultatul TIPIZAT al unei evaluări. Aceeași decizie alimentează retrieval, side effects,
    observabilitate și compunere — nu se re-derivă nicăieri."""

    kept: list[dict[str, Any]]
    blocked: list[Block] = field(default_factory=list)
    contexts: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    # Contract de răspuns: contextul e activ → codul GARANTEAZĂ fraza de siguranță (recunoaștere +
    # trimitere la medic), o singură dată. Nu depinde de model.
    must_refer: bool = False
    # Cheia de copy (localizată în `messages.py`); None = nimic de spus.
    message_key: str | None = None
    # Registrul e stricat → n-am putut decide. Cine cheamă NU are voie să expună (fail-closed).
    unavailable: bool = False
    # Produse rămase pe care catalogul nu ne lasă să le judecăm → nu le declarăm potrivite.
    unverifiable: int = 0

    @property
    def active(self) -> bool:
        """Există un context de siguranță în acest tur? (False ⇒ tur normal, zero efect.)"""
        return bool(self.contexts)

    @property
    def blocked_ids(self) -> tuple[str, ...]:
        return tuple(sorted({b.product_id for b in self.blocked if b.product_id}))


_NO_CONTEXT = Decision(kept=[])


@dataclass(frozen=True)
class SafetyPolicy:
    """Decizia de siguranță pentru UN tur. Imutabilă: contextele se calculează o dată, la intrarea
    în tur, și nu se schimbă în timpul lui (altfel două căi ar putea decide diferit — P3)."""

    contexts: frozenset[str]
    registry_ok: bool = True
    registry_error: str = ""

    # --- construcție ---------------------------------------------------------------------------

    @classmethod
    def for_turn(cls, ctx: Any) -> SafetyPolicy:
        """Policy-ul turului. Contextul ACTIV = ce e persistat în `state.safety` ∪ ce s-a declarat
        acum (istoricul de 8 mesaje nu e invariant de producție — vezi `safety_state`).

        Kill-switch OFF → policy inert (contexte goale) — comportamentul de dinainte de NX-173."""
        if not get_settings().safety_contraindications_enabled:
            return cls(contexts=frozenset())
        try:
            load_registry()
        except RegistryError as e:
            # Registrul e stricat. NU putem decide → orice tur cu context de siguranță e blocat.
            # Dacă registrul nu se încarcă, nici pattern-urile de detecție nu există → singurul
            # context de încredere e cel deja PERSISTAT în state.
            log.error("safety: registru invalid → FAIL-CLOSED. %s", e)
            persisted = frozenset(_persisted_contexts(ctx))
            return cls(contexts=persisted, registry_ok=False, registry_error=str(e))
        contexts = frozenset(_persisted_contexts(ctx)) | detect_contexts_in_turn(ctx)
        return cls(contexts=contexts)

    # --- decizie -------------------------------------------------------------------------------

    def evaluate(self, products: list[dict[str, Any]], *, purpose: str = "expose") -> Decision:
        """Ce poate fi expus din `products`. `purpose` e doar pentru observabilitate (search/page/
        details/compare/link/cross_sell/cheaper/rehydrate/cart/checkout/back_in_stock/reorder)."""
        if not self.contexts:
            return _NO_CONTEXT if not products else Decision(kept=products)
        ctxs = tuple(sorted(self.contexts))
        if not self.registry_ok:
            # Fail-closed: context activ + registru stricat → nu expunem NIMIC. Clientul primește
            # un mesaj onest („nu pot verifica acum"), nu o listă nefiltrată.
            return Decision(
                kept=[],
                blocked=[],
                contexts=ctxs,
                must_refer=True,
                message_key="safety.unavailable",
                unavailable=True,
            )
        kept, blocked = filter_products(products, self.contexts)
        return Decision(
            kept=kept,
            blocked=blocked,
            contexts=ctxs,
            rule_ids=tuple(sorted({b.rule_id for b in blocked})),
            # context activ → fraza de siguranță e obligatorie, chiar dacă n-am blocat nimic
            must_refer=True,
            message_key="safety.blocked" if blocked else "safety.ack",
            unverifiable=sum(1 for p in kept if not has_verifiable_ingredients(p)),
        )

    def allows(self, product: dict[str, Any] | None, *, purpose: str = "mutate") -> bool:
        """Poarta pentru MUTAȚII (cart/checkout/back-in-stock/reorder): `False` ⇒ NU scrie.
        Produs lipsă → True (nu e treaba noastră; tool-ul are propriul not_found)."""
        if product is None or not self.contexts:
            return True
        return not self.evaluate([product], purpose=purpose).blocked and (
            self.registry_ok or not self.contexts
        )

    # --- observabilitate -----------------------------------------------------------------------

    def emit(self, ctx: Any, d: Decision, *, purpose: str) -> None:
        """Un singur loc care emite evenimentul de siguranță (P10/P12: doar ref-uri, fără PII)."""
        if not d.active or (not d.blocked and not d.unavailable):
            return
        ctx.emit(
            "safety_contraindication_block",
            purpose=purpose,
            contexts=list(d.contexts),
            blocked=len(d.blocked),
            kept=len(d.kept),
            rules=list(d.rule_ids),
            product_ids=list(d.blocked_ids),
            registry_unavailable=d.unavailable,
        )

    def gate(
        self, ctx: Any, products: list[dict[str, Any]], *, purpose: str
    ) -> tuple[list[dict[str, Any]], Decision]:
        """`evaluate` + `emit` + păstrarea deciziei pe tur — helperul folosit de call-site-uri.
        Decizia se ACUMULEAZĂ pe `ctx` (`ctx.safety_decision`) → compunerea o citește o dată, la
        final, ca să garanteze fraza (fără să depindă de ce cale a produs produsele)."""
        d = self.evaluate(products, purpose=purpose)
        self.emit(ctx, d, purpose=purpose)
        if d.active:
            _merge_decision(ctx, d)
        return d.kept, d


def _persisted_contexts(ctx: Any) -> set[str]:
    state = getattr(ctx, "state", None)
    safety = getattr(state, "safety", None) if state is not None else None
    if not isinstance(safety, dict):
        return set()
    return {str(c) for c in (safety.get("contexts") or [])}


def _merge_decision(ctx: Any, d: Decision) -> None:
    """Decizia turului = uniunea evaluărilor (o cale poate bloca, alta nu). `must_refer` e sticky:
    dacă un context a fost activ măcar o dată în tur, fraza de siguranță se datorează."""
    prev: Decision | None = getattr(ctx, "safety_decision", None)
    if prev is None:
        ctx.safety_decision = d
        return
    ctx.safety_decision = Decision(
        kept=d.kept,
        blocked=list(prev.blocked) + list(d.blocked),
        contexts=tuple(sorted(set(prev.contexts) | set(d.contexts))),
        rule_ids=tuple(sorted(set(prev.rule_ids) | set(d.rule_ids))),
        must_refer=prev.must_refer or d.must_refer,
        message_key=(
            "safety.blocked" if (prev.blocked or d.blocked) else (d.message_key or prev.message_key)
        ),
        unavailable=prev.unavailable or d.unavailable,
        unverifiable=max(prev.unverifiable, d.unverifiable),
    )


def safety_state(ctx: Any, policy: SafetyPolicy) -> dict[str, Any] | None:
    """Patch-ul de state care PERSISTĂ contextul (sursă + timestamp), ca invariantul să nu depindă
    de fereastra de 8 mesaje a istoricului (review Codex).

    Revocare: contextul e sticky pe conversație — nu-l ștergem singuri. E o alegere deliberată:
    un client care spune „sunt însărcinată" nu încetează să fie între tururi, iar auto-expirarea
    ar reintroduce exact bug-ul. Ștergerea explicită = `clear_safety_context` (operator/GDPR)."""
    if not policy.contexts:
        return None
    existing = _persisted_contexts(ctx)
    if existing == set(policy.contexts):
        return None  # nimic nou → fără scriere inutilă de state
    return {
        "contexts": sorted(policy.contexts),
        "source": "declared_by_contact",
        "updated_at": _now_iso(ctx),
    }


def clear_safety_context() -> dict[str, Any]:
    """Revocare EXPLICITĂ (operator / cerere client / GDPR). Nu se cheamă din pipeline."""
    return {"contexts": [], "source": "cleared", "updated_at": ""}


def _now_iso(ctx: Any) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    started = getattr(ctx, "started_at", None)
    if isinstance(started, datetime):
        return started.astimezone(UTC).isoformat()
    return datetime.now(UTC).isoformat()
