"""Erori de nivel DB.

`IsolationError` (NX-04): o conexiune scoasă din `bot_pool` care NU e
`bot_runtime` sau care NU are `app.business_id` setat la business-ul cerut e
respinsă ÎNAINTE de primul query. Fail-fast explicit (principiul 6): o regresie
de izolare pică zgomotos în staging, nu produce date greșite în tăcere în prod.

Mesajul conține DOAR rolul găsit + business_id-urile (id-uri de tenant, NU date
de client / PII — principiul 12).
"""


class IsolationError(RuntimeError):
    """Izolare de tenant invalidă la checkout din pool (vezi tenant_conn)."""
