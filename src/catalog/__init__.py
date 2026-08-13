"""Stratul de catalog CANONIC: ce e adevărat despre un produs, indiferent cine a întrebat.

Deocamdată un singur modul (`context_resolver`, NX-234): rehidratarea tenant-scoped a
referințelor afirmate de pagina gazdă. Query-urile SQL rămân în `src/db/queries/catalog.py`;
aici stau REGULILE (relații, freshness, ce e UNKNOWN și de ce)."""
