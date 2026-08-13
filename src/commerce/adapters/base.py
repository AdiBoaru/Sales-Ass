"""NX-237 — portul de storefront: ce AR trebui să implementeze un magazin real, și ce avem azi.

Decizia explicită a cardului (Definition of Ready): în mediul curent NU există API de storefront
(catalogul demo trăiește în Supabase, checkout-ul e linkul canonic `checkout_links` cu `?ref=`).
Sistemul canonic ales e deci **coșul asistentului** — `conversation_carts` în Postgres, prin
`CartService` — iar UI-ul/copy-ul îl numește onest „coșul conversației": NU pretinde că a
modificat coșul global al magazinului (boundary-ul nenegociabil din card).

Portul de mai jos există ca SEAM, nu ca promisiune: când un client real oferă un cart API,
adaptorul lui se conectează aici, cu contractul exact-once deja construit în serviciu (receipt
`pending` ÎNAINTE de call, cheie stabilă de idempotency, `unknown_reconcile` la răspuns pierdut,
`lookup` înainte de orice retry). Testele exersează contractul cu un adaptor fake cu fault
injection — nu inventăm integrarea (D15/rollout pct. 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AdapterResult:
    """Rezultatul unui apel de adaptor. `ok=False` = REFUZ CLAR al providerului (devine receipt
    `failed`). Un timeout/răspuns pierdut NU produce `AdapterResult` — ridică, iar serviciul
    marchează `unknown_reconcile`: „nu știu" nu se raportează drept „a eșuat"."""

    ok: bool
    external_ref: str | None = None
    url: str | None = None
    error: str | None = None  # cod low-cardinality al providerului, nu mesaj brut


class StorefrontCartAdapter(Protocol):
    """Contractul unui coș de storefront REAL. Toate apelurile primesc `idempotency_key` stabil:
    providerul trebuie ori să deduplice pe ea, ori să ofere `lookup` (read-after-write) — altfel
    exact-once e imposibil și integrarea nu se aprinde (Definition of Ready)."""

    name: str

    async def push_checkout(
        self,
        *,
        business_id: str,
        conversation_id: str,
        idempotency_key: str,
        ref_code: str,
        lines: list[dict[str, Any]],
    ) -> AdapterResult:
        """Creează checkout-ul în storefront. Poate fi apelat de mai multe ori cu aceeași cheie."""
        ...

    async def lookup(self, *, business_id: str, idempotency_key: str) -> AdapterResult | None:
        """Starea CANONICĂ a unei operații după cheie — obligatoriu înainte de orice retry al
        unei operații `unknown_reconcile`. None = providerul nu a văzut niciodată cheia."""
        ...


def configured_adapter() -> StorefrontCartAdapter | None:
    """Adaptorul mediului curent: **None** — nu există storefront API (vezi docstring-ul
    modulului). Checkout-ul folosește exclusiv linkul canonic existent; coșul e al asistentului.
    Testele injectează adaptoare fake direct în `CartService` (parametrul `adapter`)."""
    return None


__all__ = ["AdapterResult", "StorefrontCartAdapter", "configured_adapter"]
