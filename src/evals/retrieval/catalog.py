"""NX-203 — încărcarea catalogului pentru evaluarea `hard_constraints`.

Un singur loc care ştie CE înseamnă „catalogul contra căruia se măsoară". Filtrul e acelaşi peste
tot: `status='active'` + `content_status='published'`. Dacă scriptul de derivare a excepţiilor şi
benchmark-ul ar folosi filtre diferite, o parte din produse ar fi „interzise" într-un loc şi
inexistente în celălalt — iar diferenţa n-ar apărea nicăieri ca eroare.

`version` include marca de timp a ultimei modificări, nu doar numărul de produse: un re-seed care
schimbă preţuri fără să adauge produse trebuie să producă altă versiune, altfel două rapoarte
incomparabile ar trece drept comparabile.
"""

from __future__ import annotations

import json
from typing import Any

from src.evals.retrieval.harness import CatalogSnapshot

_SQL = """
select p.id::text as id, p.name, p.price, p.attributes, c.slug as category_slug,
       p.updated_at
from products p
left join categories c on c.id = p.primary_category_id
where p.business_id = $1::uuid
  and p.status = 'active'
  and p.content_status = 'published'
"""


async def load_catalog(conn: Any, business_id: str) -> CatalogSnapshot:
    """Snapshot-ul catalogului live, în forma consumată de `constraints.evaluate`."""
    rows = await conn.fetch(_SQL, business_id)
    products: dict[str, dict] = {}
    latest = None
    for r in rows:
        d = dict(r)
        # asyncpg întoarce jsonb ca STRING (nu e înregistrat codec) — fără decodare, `attributes`
        # ar fi un şir, deci fiecare atribut ar ieşi „necunoscut" şi nicio constrângere n-ar fi
        # verificată, în tăcere.
        attrs = d.get("attributes")
        if isinstance(attrs, str):
            attrs = json.loads(attrs)
        updated = d.pop("updated_at", None)
        if updated is not None and (latest is None or updated > latest):
            latest = updated
        products[d["id"]] = {
            "name": d.get("name"),
            "price": d.get("price"),
            "category_slug": d.get("category_slug"),
            "attributes": attrs or {},
        }
    stamp = latest.isoformat(timespec="seconds") if latest is not None else "unknown"
    return CatalogSnapshot(version=f"live:{len(products)}@{stamp}", products=products)
