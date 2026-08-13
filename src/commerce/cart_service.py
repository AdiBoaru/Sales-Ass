"""NX-237 — `CartService`: SINGURUL punct de mutație al coșului conversației.

Orice intrare — click pe acțiune opacă (kernel NX-236) sau tool LLM (`cart_add`/`checkout_link`)
— trimite ACEEAȘI comandă typed (`CartCommand`) aici. Serviciul rehidratează și reverifică
produsul, varianta, prețul, stocul și siguranța ÎNAINTE de scriere; orice succes are un
`MutationReceipt` idempotent și un `CartSnapshot` versionat. Un retry cu aceeași cheie primește
același receipt — zero a doua mutație, zero cantitate dublată, zero checkout dublu.

Forma unei mutații (contract NX-231 — conexiunea aparține operației):

    db_tx("cart_add") {                     # O tranzacție SCURTĂ, zero await extern
        lock cart (FOR UPDATE)              # frâna de concurență: rândul, nu disciplina
        replay? (receipt după cheie, SUB lock)
        expected_version? → conflict + snapshot fresh (fără merge în FE)
        rehidratează faptele (UN query, batch)
        validează: produs, variantă-membership, safety (NX-173), stoc, monedă, capuri
        aplică → version+1 → receipt succeeded
    }

Calea cu adaptor EXTERN (când va exista un storefront real) e diferită prin construcție:
receipt `pending` scris ÎNAINTE de call (cu cheie stabilă), conexiunea ELIBERATĂ pe durata
apelului, apoi finalizare/`unknown_reconcile` într-o a doua tranzacție. La răspuns pierdut NU se
repetă orb mutația: `reconcile()` întreabă providerul după cheie întâi.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Any

from src.commerce.adapters.base import StorefrontCartAdapter, configured_adapter
from src.commerce.cart_models import (
    CART_MAX_LINE_QUANTITY,
    CART_MAX_LINES,
    CartCommand,
    CartSnapshot,
    MutationOutcome,
    MutationReceipt,
    build_snapshot,
)
from src.commerce.facts_provider import FactsBatch, FactsKey, load_facts
from src.config import get_settings
from src.db.provider import DbProvider, db_tx
from src.db.queries import carts as q
from src.db.queries.commerce import create_checkout_link

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Praguri de bucket LOW-CARDINALITY (P10/P12: numărăm clase, nu valori).
_ITEM_BUCKETS = ((0, "0"), (3, "1-3"), (6, "4-6"), (CART_MAX_LINES, "7-10"))


def _bucket(n: int) -> str:
    for limit, label in _ITEM_BUCKETS:
        if n <= limit:
            return label
    return f">{CART_MAX_LINES}"


@dataclass
class CartService:
    """Orchestrarea coșului canonic. `business_id` e legat la construcție (SERVER-OWNED, P7) —
    niciun apelant nu îl poate rebinda; `db` e providerul tenant-scoped al turului."""

    db: DbProvider
    business_id: str
    contact_id: str | None = None
    language: str | None = "ro"
    # NX-173: poarta de siguranță a turului. `for_turn` (singura cale de runtime) o setează
    # MEREU; `None` există doar pentru teste de nivel jos care își injectează propriul fake.
    policy: Any = None
    emit: Callable[..., None] | None = None
    # Emiterea deciziei de siguranță (policy.emit are nevoie de ctx; serviciul nu îl ține).
    safety_emit: Callable[[Any, str], None] | None = None
    adapter: StorefrontCartAdapter | None = None
    sla_s: int | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @classmethod
    def for_turn(cls, ctx: TurnContext, deps: PipelineDeps) -> CartService:
        """Construcția CANONICĂ din tur: policy/emit/limba vin din context, adaptorul din
        configurația mediului (azi: None — coșul asistentului, vezi `adapters/base.py`)."""
        from src.safety.policy import SafetyPolicy  # noqa: PLC0415 — evită ciclul de import

        policy = SafetyPolicy.for_turn(ctx)
        return cls(
            db=deps.db,
            business_id=ctx.business.id,
            contact_id=ctx.contact.id,
            language=ctx.language,
            policy=policy,
            emit=ctx.emit,
            safety_emit=lambda decision, purpose: policy.emit(ctx, decision, purpose=purpose),
            adapter=configured_adapter(),
        )

    # ── utilitare interne ────────────────────────────────────────────────────────────────────

    def _sla(self) -> int:
        return self.sla_s if self.sla_s is not None else get_settings().commerce_facts_sla_s

    def _event(self, name: str, **props: Any) -> None:
        if self.emit is not None:
            self.emit(name, **props)

    def _receipt(self, row: Mapping[str, Any], *, replayed: bool = False) -> MutationReceipt:
        return MutationReceipt(
            receipt_id=str(row["id"]),
            operation=row["operation"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            before_version=int(row["before_version"] or 0),
            after_version=row.get("after_version"),
            result_code=row.get("result_code"),
            external_ref=row.get("external_ref"),
            url=row.get("url"),
            replayed=replayed,
        )

    async def _hydrate(
        self, conn: Any, refs: list[FactsKey], *, outcome: str = "mutation"
    ) -> FactsBatch:
        started = perf_counter()
        facts = await load_facts(conn, self.business_id, refs, now=self.now(), sla_s=self._sla())
        self._event(
            "cart_hydration_ms",
            outcome=outcome,
            elapsed_ms=round((perf_counter() - started) * 1000.0, 1),
        )
        self._event("cart_query_count_bucket", bucket=_bucket(facts.query_count))
        return facts

    def _build(
        self,
        cart: Mapping[str, Any] | None,
        items: list[dict[str, Any]],
        facts: FactsBatch,
        *,
        version: int | None = None,
    ) -> CartSnapshot:
        snapshot = build_snapshot(
            cart_id=str(cart["id"]) if cart else None,
            version=version if version is not None else int(cart["version"]) if cart else 0,
            status=str(cart["status"]) if cart else "empty",
            items=items,
            facts=facts.facts,
            language=self.language,
        )
        self._event("cart_items_bucket", bucket=_bucket(len(items)))
        for reason in snapshot.blocked_reasons:
            if reason != "cart_empty":
                self._event("commerce_cta_omitted", reason=reason)
        return snapshot

    def _replay(
        self,
        row: Mapping[str, Any],
        snapshot: CartSnapshot,
        *,
        products: tuple[Mapping[str, Any], ...] = (),
    ) -> MutationOutcome:
        """Cheia a mai fost consumată → rezultatul ORIGINAL, fără a doua mutație."""
        receipt = self._receipt(row, replayed=True)
        error: str | None = None
        if receipt.status in ("pending", "unknown_reconcile"):
            error = "receipt_pending"
        elif receipt.status == "failed":
            error = receipt.result_code or "internal_error"
        self._event(
            "cart_command", operation=receipt.operation, outcome="replayed", reason=error or ""
        )
        return MutationOutcome(snapshot=snapshot, receipt=receipt, error=error, products=products)

    async def _write_receipt(
        self,
        conn: Any,
        *,
        conversation_id: str,
        cart_id: str | None,
        operation: str,
        idempotency_key: str,
        status: str,
        before_version: int,
        after_version: int | None,
        result_code: str | None,
        turn_id: str | None,
        action_id: str | None,
        external_ref: str | None = None,
        url: str | None = None,
    ) -> MutationReceipt:
        row = await q.insert_receipt(
            conn,
            self.business_id,
            conversation_id=conversation_id,
            cart_id=cart_id,
            operation=operation,
            idempotency_key=idempotency_key,
            status=status,
            before_version=before_version,
            after_version=after_version,
            result_code=result_code,
            turn_id=turn_id,
            action_id=action_id,
            external_ref=external_ref,
            url=url,
        )
        if row is None:  # cheia a fost scrisă între timp (plasă — sub lock nu ar trebui)
            row = await q.get_receipt_by_key(conn, self.business_id, idempotency_key)
        self._event("cart_receipt", operation=operation, status=status)
        return self._receipt(row or {})

    def _reject(
        self,
        code: str,
        *,
        operation: str,
        snapshot: CartSnapshot,
        receipt: MutationReceipt | None = None,
    ) -> MutationOutcome:
        self._event("cart_command", operation=operation, outcome="rejected", reason=code)
        return MutationOutcome(snapshot=snapshot, receipt=receipt, error=code)

    # ── citire ───────────────────────────────────────────────────────────────────────────────

    async def get_snapshot(self, conversation_id: str) -> CartSnapshot:
        """Snapshot PROASPĂT: coș + linii + fapte rehidratate ACUM, într-un checkout scurt.
        Snapshotul vechi (afișat într-un tur anterior) nu e niciodată truth pentru citirea asta."""
        async with self.db("cart_snapshot") as conn:
            cart = await q.get_active_cart(conn, self.business_id, conversation_id)
            items = await q.get_cart_items(conn, self.business_id, cart["id"]) if cart else []
            refs: list[FactsKey] = [(it["product_id"], it.get("variant_id")) for it in items]
            facts = await self._hydrate(conn, refs, outcome="snapshot")
        return self._build(cart, items, facts)

    # ── mutații ──────────────────────────────────────────────────────────────────────────────

    async def mutate(
        self,
        conversation_id: str,
        command: CartCommand,
        *,
        idempotency_key: str,
        turn_id: str | None = None,
        action_id: str | None = None,
    ) -> MutationOutcome:
        """O mutație = o tranzacție scurtă. Vezi docstring-ul modulului pentru formă; aici doar
        dispatch-ul pe operație, cu TOATE verificările înaintea oricărei scrieri."""
        op = command.operation
        try:
            async with db_tx(self.db, f"cart_{op}") as conn:
                cart = await q.lock_active_cart(conn, self.business_id, conversation_id)
                if cart is None and op == "add":
                    await q.create_cart_if_absent(conn, self.business_id, conversation_id)
                    cart = await q.lock_active_cart(conn, self.business_id, conversation_id)
                # Replay-ul se verifică SUB lock: doi retry simultani se serializează pe rândul
                # de coș, deci al doilea VEDE receiptul primului (failure matrix, rândul 1).
                existing = await q.get_receipt_by_key(conn, self.business_id, idempotency_key)
                items = await q.get_cart_items(conn, self.business_id, cart["id"]) if cart else []
                refs: list[FactsKey] = [(it["product_id"], it.get("variant_id")) for it in items]
                if command.product_ref:
                    refs.append((command.product_ref, command.variant_ref))
                facts = await self._hydrate(conn, refs)
                if existing is not None:
                    return self._replay(existing, self._build(cart, items, facts))
                if (
                    command.expected_version is not None
                    and cart is not None
                    and int(cart["version"]) != command.expected_version
                ):
                    self._event("cart_version_conflict", operation=op)
                    self._event("cart_command", operation=op, outcome="conflict", reason="")
                    return MutationOutcome(snapshot=self._build(cart, items, facts), conflict=True)
                return await self._apply(
                    conn,
                    conversation_id,
                    command,
                    cart=cart,
                    items=items,
                    facts=facts,
                    idempotency_key=idempotency_key,
                    turn_id=turn_id,
                    action_id=action_id,
                )
        except Exception:  # noqa: BLE001 — o mutație crăpată nu are voie să lase turul mut (P6)
            log.exception("cart_service: mutația %s a eșuat", op)
            self._event("cart_command", operation=op, outcome="error", reason="internal_error")
            return MutationOutcome(
                snapshot=CartSnapshot(cart_id=None, version=0, status="empty"),
                error="internal_error",
            )

    async def _apply(
        self,
        conn: Any,
        conversation_id: str,
        command: CartCommand,
        *,
        cart: Mapping[str, Any] | None,
        items: list[dict[str, Any]],
        facts: FactsBatch,
        idempotency_key: str,
        turn_id: str | None,
        action_id: str | None,
    ) -> MutationOutcome:
        op = command.operation
        version = int(cart["version"]) if cart else 0
        cart_id = str(cart["id"]) if cart else None

        async def fail(code: str) -> MutationOutcome:
            receipt = await self._write_receipt(
                conn,
                conversation_id=conversation_id,
                cart_id=cart_id,
                operation=op,
                idempotency_key=idempotency_key,
                status="failed",
                before_version=version,
                after_version=version,
                result_code=code,
                turn_id=turn_id,
                action_id=action_id,
            )
            return self._reject(
                code, operation=op, snapshot=self._build(cart, items, facts), receipt=receipt
            )

        target = next(
            (
                it
                for it in items
                if it["product_id"] == command.product_ref
                and (it.get("variant_id") or None) == command.variant_ref
            ),
            None,
        )

        if op == "clear":
            if cart is not None and items:
                await q.clear_cart_items(conn, self.business_id, cart_id)
                version = await q.bump_cart_version(conn, self.business_id, cart_id)
            items_after: list[dict[str, Any]] = []
        elif op == "remove":
            if cart is None or target is None:
                return await fail("line_not_found")
            await q.delete_cart_item(
                conn, self.business_id, cart_id, command.product_ref, command.variant_ref
            )
            version = await q.bump_cart_version(conn, self.business_id, cart_id)
            items_after = [it for it in items if it is not target]
        elif op in ("add", "set_quantity"):
            outcome_code = self._validate_line(command, cart=cart, items=items, facts=facts)
            if outcome_code is not None:
                return await fail(outcome_code)
            if op == "add":
                new_qty = (int(target["quantity"]) if target else 0) + int(command.quantity or 1)
                if new_qty > CART_MAX_LINE_QUANTITY:
                    return await fail("quantity_invalid")
                code = self._stock_check(command, facts, new_qty)
                if code is not None:
                    return await fail(code)
                if target is None:
                    await q.insert_cart_item(
                        conn,
                        self.business_id,
                        cart_id,
                        command.product_ref,
                        command.variant_ref,
                        new_qty,
                        turn_id or "",
                    )
                else:
                    await q.set_cart_item_quantity(
                        conn,
                        self.business_id,
                        cart_id,
                        command.product_ref,
                        command.variant_ref,
                        new_qty,
                        turn_id or "",
                    )
            else:  # set_quantity
                if target is None:
                    return await fail("line_not_found")
                new_qty = int(command.quantity or 1)
                if new_qty > int(target["quantity"]):  # creșterea = un add deghizat
                    code = self._stock_check(command, facts, new_qty)
                    if code is not None:
                        return await fail(code)
                await q.set_cart_item_quantity(
                    conn,
                    self.business_id,
                    cart_id,
                    command.product_ref,
                    command.variant_ref,
                    new_qty,
                    turn_id or "",
                )
            version = await q.bump_cart_version(conn, self.business_id, cart_id)
            items_after = [dict(it) for it in items]
            for it in items_after:
                if it["product_id"] == command.product_ref and (
                    (it.get("variant_id") or None) == command.variant_ref
                ):
                    it["quantity"] = new_qty
                    break
            else:
                items_after.append(
                    {
                        "product_id": command.product_ref,
                        "variant_id": command.variant_ref,
                        "quantity": new_qty,
                    }
                )
        else:  # checkout trece prin create_checkout, nu pe aici
            return await fail("internal_error")

        receipt = await self._write_receipt(
            conn,
            conversation_id=conversation_id,
            cart_id=cart_id,
            operation=op,
            idempotency_key=idempotency_key,
            status="succeeded",
            before_version=int(cart["version"]) if cart else 0,
            after_version=version,
            result_code=None,
            turn_id=turn_id,
            action_id=action_id,
        )
        self._event("cart_command", operation=op, outcome="ok", reason="")
        raw = facts.product(command.product_ref) if command.product_ref else None
        return MutationOutcome(
            snapshot=self._build(cart, items_after, facts, version=version),
            receipt=receipt,
            products=(raw,) if raw is not None else (),
        )

    def _validate_line(
        self,
        command: CartCommand,
        *,
        cart: Mapping[str, Any] | None,
        items: list[dict[str, Any]],
        facts: FactsBatch,
    ) -> str | None:
        """Verificările de add/set_quantity, TOATE înaintea oricărei scrieri. None = valid."""
        raw = facts.product(command.product_ref or "")
        if raw is None:
            return "product_not_found"
        if not facts.variant_known(command.product_ref or "", command.variant_ref):
            return "variant_not_found"
        f = facts.get(command.product_ref or "", command.variant_ref)
        if f is None:
            return "variant_not_found"
        # NX-173 (P0): poarta de MUTAȚIE — un filtru de rezultat nu poate anula un rând scris.
        if self.policy is not None and not self.policy.allows(raw, purpose="cart_add"):
            if self.safety_emit is not None:
                self.safety_emit(self.policy.evaluate([raw], purpose="cart_add"), "cart_add")
            return "safety_excluded"
        sell = f.sellable
        if sell is not None:
            return sell
        if not f.price_known:
            # Un produs fără preț susținut nu intră în coș: totalul ar deveni neafirmabil, iar
            # checkout-ul (unde suma E necesară) s-ar bloca oricum (failure matrix).
            return "price_unknown"
        currencies = {
            facts.get(it["product_id"], it.get("variant_id")).currency
            for it in items
            if facts.get(it["product_id"], it.get("variant_id")) is not None
        }
        if currencies and f.currency not in currencies:
            return "currency_mismatch"
        is_new_line = command.operation == "add" and not any(
            it["product_id"] == command.product_ref
            and (it.get("variant_id") or None) == command.variant_ref
            for it in items
        )
        if is_new_line and len(items) >= CART_MAX_LINES:
            return "cart_full"
        return None

    @staticmethod
    def _stock_check(command: CartCommand, facts: FactsBatch, new_qty: int) -> str | None:
        """Stoc CUNOSCUT < cantitate ⇒ reject explicat. Stoc necunoscut nu blochează cantitatea
        (capul e doar al sursei care îl dă), dar disponibilitatea a fost deja verificată."""
        f = facts.get(command.product_ref or "", command.variant_ref)
        if f is not None and f.stock is not None and new_qty > f.stock:
            return "insufficient_stock"
        return None

    # ── checkout ─────────────────────────────────────────────────────────────────────────────

    async def create_checkout(
        self,
        conversation_id: str,
        *,
        idempotency_key: str,
        turn_id: str,
        base_url: str,
        lines: list[dict[str, Any]] | None = None,
        expected_version: int | None = None,
        action_id: str | None = None,
    ) -> MutationOutcome:
        """Checkout prin linkul CANONIC (`checkout_links`, `ref_code = turn_id` — idempotent per
        tur, ca înainte). `lines=None` = coșul canonic întreg (calea de acțiuni); linii explicite
        = calea LLM (modelul poate cere checkout direct pe un produs) — ambele validate identic.

        Cu adaptor EXTERN configurat, fluxul devine pending → call (fără conexiune) → finalize;
        azi adaptorul e None (vezi `adapters/base.py`), deci totul e o singură tranzacție."""
        if not base_url:
            return MutationOutcome(
                snapshot=await self.get_snapshot(conversation_id), error="checkout_unavailable"
            )
        try:
            if self.adapter is None:
                return await self._checkout_internal(
                    conversation_id,
                    idempotency_key=idempotency_key,
                    turn_id=turn_id,
                    base_url=base_url,
                    lines=lines,
                    expected_version=expected_version,
                    action_id=action_id,
                )
            return await self._checkout_external(
                conversation_id,
                idempotency_key=idempotency_key,
                turn_id=turn_id,
                base_url=base_url,
                lines=lines,
                expected_version=expected_version,
                action_id=action_id,
            )
        except Exception:  # noqa: BLE001 — P6
            log.exception("cart_service: checkout eșuat")
            self._event("checkout_created", outcome="error")
            return MutationOutcome(
                snapshot=CartSnapshot(cart_id=None, version=0, status="empty"),
                error="internal_error",
            )

    async def _checkout_prepare(
        self,
        conn: Any,
        conversation_id: str,
        *,
        idempotency_key: str,
        lines: list[dict[str, Any]] | None,
        expected_version: int | None,
    ) -> tuple[Any, ...]:
        """Partea comună (lock + replay + validare). Întoarce fie un outcome terminal, fie
        materialul validat pentru scriere: (None, cart, use_lines, facts, valid, fail)."""
        cart = await q.lock_active_cart(conn, self.business_id, conversation_id)
        cart_items = await q.get_cart_items(conn, self.business_id, cart["id"]) if cart else []
        use_lines = [
            {
                "product_id": str(ln["product_id"]),
                "variant_id": (str(ln["variant_id"]) if ln.get("variant_id") else None),
                "quantity": int(ln.get("quantity") or 1),
            }
            for ln in (lines if lines is not None else cart_items)
        ]
        refs: list[FactsKey] = [(ln["product_id"], ln["variant_id"]) for ln in use_lines] + [
            (it["product_id"], it.get("variant_id")) for it in cart_items
        ]
        facts = await self._hydrate(conn, refs, outcome="checkout")
        existing = await q.get_receipt_by_key(conn, self.business_id, idempotency_key)
        if existing is not None:
            return (self._replay(existing, self._build(cart, cart_items, facts)),)
        if (
            expected_version is not None
            and cart is not None
            and int(cart["version"]) != expected_version
        ):
            self._event("cart_version_conflict", operation="checkout")
            return (MutationOutcome(snapshot=self._build(cart, cart_items, facts), conflict=True),)
        return (None, cart, cart_items, use_lines, facts)

    def _checkout_validate(
        self, use_lines: list[dict[str, Any]], facts: FactsBatch
    ) -> tuple[str | None, list[dict[str, Any]], list[Mapping[str, Any]]]:
        """Validarea liniilor de checkout. Întoarce (cod_eroare, linii_valide, produse_brute)."""
        if not use_lines:
            return "cart_empty", [], []
        valid: list[dict[str, Any]] = []
        raw_products: list[Mapping[str, Any]] = []
        currencies: set[str | None] = set()
        for ln in use_lines:
            raw = facts.product(ln["product_id"])
            if raw is None:
                continue  # produs dispărut din catalog → sărit (paritate cu checkout-ul legacy)
            if not facts.variant_known(ln["product_id"], ln["variant_id"]):
                return "variant_not_found", [], []
            f = facts.get(ln["product_id"], ln["variant_id"])
            if f is None:
                return "variant_not_found", [], []
            sell = f.sellable
            if sell is not None:
                return sell, [], []
            if not f.price_known:
                return "price_unknown", [], []
            currencies.add(f.currency)
            valid.append({**ln, "name": f.name, "price": round(float(f.price or 0.0), 2)})
            raw_products.append(raw)
        if not valid:
            return "product_not_found", [], []
        if len(currencies) > 1:
            return "currency_mismatch", [], []
        # NX-173: refuzăm TOT checkout-ul dacă vreo linie e contraindicată — un checkout parțial,
        # tăcut, ar schimba comanda clientului fără să-i spună (paritate cu tool-ul legacy).
        if self.policy is not None:
            d = self.policy.evaluate(list(raw_products), purpose="checkout")
            if getattr(d, "blocked", None) or getattr(d, "unavailable", None):
                if self.safety_emit is not None:
                    self.safety_emit(d, "checkout")
                return "safety_excluded", [], []
        return None, valid, raw_products

    async def _checkout_internal(
        self,
        conversation_id: str,
        *,
        idempotency_key: str,
        turn_id: str,
        base_url: str,
        lines: list[dict[str, Any]] | None,
        expected_version: int | None,
        action_id: str | None,
    ) -> MutationOutcome:
        async with db_tx(self.db, "cart_checkout") as conn:
            prep = await self._checkout_prepare(
                conn,
                conversation_id,
                idempotency_key=idempotency_key,
                lines=lines,
                expected_version=expected_version,
            )
            if prep[0] is not None:
                return prep[0]
            _, cart, cart_items, use_lines, facts = prep
            code, valid, raw_products = self._checkout_validate(use_lines, facts)
            cart_id = str(cart["id"]) if cart else None
            version = int(cart["version"]) if cart else 0
            if code is not None:
                receipt = await self._write_receipt(
                    conn,
                    conversation_id=conversation_id,
                    cart_id=cart_id,
                    operation="checkout",
                    idempotency_key=idempotency_key,
                    status="failed",
                    before_version=version,
                    after_version=version,
                    result_code=code,
                    turn_id=turn_id,
                    action_id=action_id,
                )
                self._event("checkout_created", outcome="rejected")
                return self._reject(
                    code,
                    operation="checkout",
                    snapshot=self._build(cart, cart_items, facts),
                    receipt=receipt,
                )
            sep = "&" if "?" in base_url else "?"
            url = f"{base_url}{sep}ref={turn_id}"
            expires_at = self.now() + timedelta(days=get_settings().checkout_link_ttl_days)
            await create_checkout_link(
                conn,
                self.business_id,
                conversation_id,
                self.contact_id,
                turn_id,  # ref_code = turn_id → idempotent per tur (atribuirea, F2-2)
                valid,
                url,
                expires_at,
            )
            # Coșul canonic se închide DOAR dacă checkout-ul îl acoperă integral: un checkout pe
            # un singur produs explicit nu „consumă" restul coșului pe tăcute.
            covered = bool(cart_items) and all(
                any(
                    it["product_id"] == ln["product_id"]
                    and (it.get("variant_id") or None) == ln["variant_id"]
                    for ln in use_lines
                )
                for it in cart_items
            )
            if cart is not None and covered:
                await q.set_cart_status(conn, self.business_id, cart_id, "checked_out")
                version = await q.bump_cart_version(conn, self.business_id, cart_id)
            receipt = await self._write_receipt(
                conn,
                conversation_id=conversation_id,
                cart_id=cart_id,
                operation="checkout",
                idempotency_key=idempotency_key,
                status="succeeded",
                before_version=int(cart["version"]) if cart else 0,
                after_version=version,
                result_code=None,
                turn_id=turn_id,
                action_id=action_id,
                external_ref=turn_id,
                url=url,
            )
        self._event("checkout_created", outcome="ok")
        self._event("cart_command", operation="checkout", outcome="ok", reason="")
        status = (
            "checked_out"
            if (cart is not None and covered)
            else (str(cart["status"]) if cart else "empty")
        )
        snapshot = build_snapshot(
            cart_id=str(cart["id"]) if cart else None,
            version=version,
            status=status,
            items=cart_items,
            facts=facts.facts,
            language=self.language,
        )
        return MutationOutcome(
            snapshot=snapshot,
            receipt=receipt,
            products=tuple(raw_products),
            lines=tuple(valid),
        )

    async def _checkout_external(
        self,
        conversation_id: str,
        *,
        idempotency_key: str,
        turn_id: str,
        base_url: str,
        lines: list[dict[str, Any]] | None,
        expected_version: int | None,
        action_id: str | None,
    ) -> MutationOutcome:
        """Calea cu storefront REAL: receipt `pending` scris cu cheie stabilă ÎNAINTE de call,
        conexiunea eliberată pe durata apelului, finalizare într-o a doua tranzacție. La răspuns
        pierdut → `unknown_reconcile` + lookup înainte de retry — NICIODATĂ repetare oarbă."""
        async with db_tx(self.db, "cart_checkout_pending") as conn:
            prep = await self._checkout_prepare(
                conn,
                conversation_id,
                idempotency_key=idempotency_key,
                lines=lines,
                expected_version=expected_version,
            )
            if prep[0] is not None:
                return prep[0]
            _, cart, cart_items, use_lines, facts = prep
            code, valid, raw_products = self._checkout_validate(use_lines, facts)
            cart_id = str(cart["id"]) if cart else None
            version = int(cart["version"]) if cart else 0
            snapshot = self._build(cart, cart_items, facts)
            if code is not None:
                receipt = await self._write_receipt(
                    conn,
                    conversation_id=conversation_id,
                    cart_id=cart_id,
                    operation="checkout",
                    idempotency_key=idempotency_key,
                    status="failed",
                    before_version=version,
                    after_version=version,
                    result_code=code,
                    turn_id=turn_id,
                    action_id=action_id,
                )
                self._event("checkout_created", outcome="rejected")
                return self._reject(code, operation="checkout", snapshot=snapshot, receipt=receipt)
            receipt = await self._write_receipt(
                conn,
                conversation_id=conversation_id,
                cart_id=cart_id,
                operation="checkout",
                idempotency_key=idempotency_key,
                status="pending",
                before_version=version,
                after_version=None,
                result_code=None,
                turn_id=turn_id,
                action_id=action_id,
            )
        # ZERO conexiune ținută peste apelul extern (NX-231).
        try:
            result = await self.adapter.push_checkout(
                business_id=self.business_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                ref_code=turn_id,
                lines=valid,
            )
        except Exception:  # noqa: BLE001 — răspuns pierdut ≠ eșec: NU știm ce s-a întâmplat
            log.exception("cart_service: adaptor extern fără răspuns (unknown_reconcile)")
            async with db_tx(self.db, "cart_checkout_unknown") as conn:
                await q.finalize_receipt(
                    conn,
                    self.business_id,
                    receipt.receipt_id,
                    status="unknown_reconcile",
                )
            self._event("checkout_created", outcome="unknown")
            self._event("cart_receipt", operation="checkout", status="unknown_reconcile")
            return MutationOutcome(
                snapshot=snapshot,
                receipt=MutationReceipt(
                    receipt_id=receipt.receipt_id,
                    operation="checkout",
                    status="unknown_reconcile",
                    idempotency_key=idempotency_key,
                    before_version=version,
                ),
                error="receipt_pending",
            )
        async with db_tx(self.db, "cart_checkout_finalize") as conn:
            status = "succeeded" if result.ok else "failed"
            await q.finalize_receipt(
                conn,
                self.business_id,
                receipt.receipt_id,
                status=status,
                result_code=None if result.ok else (result.error or "internal_error"),
                after_version=version,
                external_ref=result.external_ref,
                url=result.url,
            )
        self._event("checkout_created", outcome="ok" if result.ok else "rejected")
        self._event("cart_receipt", operation="checkout", status=status)
        final = MutationReceipt(
            receipt_id=receipt.receipt_id,
            operation="checkout",
            status=status,
            idempotency_key=idempotency_key,
            before_version=version,
            after_version=version,
            result_code=None if result.ok else (result.error or "internal_error"),
            external_ref=result.external_ref,
            url=result.url,
        )
        return MutationOutcome(
            snapshot=snapshot,
            receipt=final,
            error=None if result.ok else (result.error or "internal_error"),
            products=tuple(raw_products),
            lines=tuple(valid),
        )

    # ── reconciliere (runbook: docs/CART-DATA-READINESS.md) ─────────────────────────────────

    async def reconcile(self, idempotency_key: str) -> MutationReceipt | None:
        """Rezolvă un receipt `pending`/`unknown_reconcile` întrebând providerul după CHEIE —
        pasul OBLIGATORIU înainte de orice retry al unei operații externe incerte. Fără adaptor
        (mediul curent) nu există operații externe, deci nu există nimic de reconciliat."""
        async with self.db("cart_reconcile_read") as conn:
            row = await q.get_receipt_by_key(conn, self.business_id, idempotency_key)
        if row is None:
            return None
        receipt = self._receipt(row)
        if receipt.status not in ("pending", "unknown_reconcile") or self.adapter is None:
            return receipt
        result = await self.adapter.lookup(
            business_id=self.business_id, idempotency_key=idempotency_key
        )
        if result is None:
            # Providerul n-a văzut cheia → operația NU s-a întâmplat: safe de marcat failed.
            async with db_tx(self.db, "cart_reconcile") as conn:
                await q.finalize_receipt(
                    conn,
                    self.business_id,
                    receipt.receipt_id,
                    status="failed",
                    result_code="external_not_found",
                )
            self._event("cart_receipt_reconcile", outcome="not_found")
            return await self.reconcile(idempotency_key)
        status = "succeeded" if result.ok else "failed"
        async with db_tx(self.db, "cart_reconcile") as conn:
            await q.finalize_receipt(
                conn,
                self.business_id,
                receipt.receipt_id,
                status=status,
                result_code=None if result.ok else (result.error or "internal_error"),
                external_ref=result.external_ref,
                url=result.url,
            )
        self._event("cart_receipt_reconcile", outcome=status)
        async with self.db("cart_reconcile_read") as conn:
            row = await q.get_receipt_by_key(conn, self.business_id, idempotency_key)
        return self._receipt(row) if row else None


def tool_idempotency_key(turn_id: str, command: CartCommand) -> str:
    """Cheia căii LLM: turn + operație + amprenta argumentelor. Re-rularea ACELUIAȘI tur (crash,
    retry de executor NX-233) regăsește receiptul; un tur nou = o cheie nouă = o mutație nouă."""
    return f"t:{turn_id}:{command.operation}:{command.fingerprint()}"


def action_idempotency_key(action_id: str) -> str:
    """Cheia căii de acțiuni: `action_id`-ul opac. Două tururi care ar apuca să consume aceeași
    acțiune (race pe one-shot) cad pe ACELAȘI receipt — o singură creștere (failure matrix)."""
    return f"a:{action_id}"


__all__ = [
    "CartService",
    "action_idempotency_key",
    "tool_idempotency_key",
]
