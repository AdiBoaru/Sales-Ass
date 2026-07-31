"""NX-203 — încărcarea catalogului pentru evaluarea `hard_constraints`.

Un singur loc care ştie CE înseamnă „catalogul contra căruia se măsoară". Filtrul e acelaşi peste
tot: `status='active'` + `content_status='published'`. Dacă scriptul de derivare a excepţiilor şi
benchmark-ul ar folosi filtre diferite, o parte din produse ar fi „interzise" într-un loc şi
inexistente în celălalt — iar diferenţa n-ar apărea nicăieri ca eroare.

`version` include marca de timp a ultimei modificări, nu doar numărul de produse: un re-seed care
schimbă preţuri fără să adauge produse trebuie să producă altă versiune, altfel două rapoarte
incomparabile ar trece drept comparabile.

CONSECINŢĂ de reţinut: preţul efectiv depinde de FEREASTRA promoţiei, deci de ziua rulării. Acelaşi
rând din DB dă alt preţ după expirarea unei promoţii — `updated_at` nu se schimbă, dar amprenta da,
fiindcă acoperă conţinutul. Un baseline măsurat înainte de expirare nu se mai poate compara cu un
candidat măsurat după, iar `compare_reports` va refuza. E corect: catalogul pe care îl vede
clientul chiar s-a schimbat. Practic — baseline şi candidat se rulează în aceeaşi zi.
"""

from __future__ import annotations

import json
from typing import Any

from src.db.queries.catalog import _EFFECTIVE_PRICE, _VARIANT_SALE_ON
from src.evals.retrieval.harness import CatalogSnapshot

# PREŢUL EFECTIV, nu `p.price`. Constrângerea „sub 90 lei" e despre preţul pe care îl vede
# clientul: promoţia activă (în fereastră) şi minimul variantelor. Un snapshot pe preţul de listă
# marchează drept încălcare un produs de 100 lei vândut cu 60 — un fals-pozitiv raportat ca
# `verified`, deci mai rău decât o stare neverificată: e un răspuns greşit cu încredere.
#
# Expresiile se IMPORTĂ din calea de producţie, nu se rescriu. O copie ar diverge la prima
# schimbare de reguli de promoţie, iar benchmarkul ar măsura contra unui catalog pe care nimeni
# nu-l vede.
_SQL = f"""
select p.id::text as id, p.name,
       {_EFFECTIVE_PRICE}::float8 as price,
       p.price::float8 as list_price,
       p.attributes, c.slug as category_slug,
       p.updated_at
from products p
left join categories c on c.id = p.primary_category_id
left join lateral (
    select min(case when {_VARIANT_SALE_ON} then v.sale_price else v.price end) as price
    from product_variants v
    where v.product_id = p.id and v.business_id = p.business_id
) vp on true
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
            "price": d.get("price"),  # EFECTIV — singurul citit de constrângeri
            # Diagnostic, nu input de evaluare: fără el, un fals-pozitiv raportat pe preţ nu se
            # poate investiga („de ce 60 şi nu 100?"). Exclus din amprentă, ca o schimbare de preţ
            # de listă fără efect asupra preţului real să nu invalideze o comparaţie.
            "list_price": d.get("list_price"),
            "category_slug": d.get("category_slug"),
            "attributes": attrs or {},
        }
    stamp = latest.isoformat(timespec="seconds") if latest is not None else "unknown"
    return CatalogSnapshot(version=f"live:{len(products)}@{stamp}", products=products)
