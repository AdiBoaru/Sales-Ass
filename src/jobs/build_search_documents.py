"""NX-207 — writer ADMIN pentru artefactele de search, numai shadow.

Nu este invocat de worker/read-path. Se rulează explicit de un job de conținut după aplicarea 035
și 036; lipsa tabelelor produce eroare vizibilă, nu fallback care ar ascunde un shadow incomplet.
"""

from __future__ import annotations

import json
from typing import Any

from src.domain.search_documents import SearchArtifacts, build_search_artifacts


def _decode_jsonb(row: Any) -> dict[str, Any]:
    """`dict(row)` + decodare jsonb.

    Fără asta, jobul nu putea procesa NICIUN produs: pool-ul nu înregistrează codec jsonb (doar
    unul de `vector`, vezi `db/connection.py`), deci asyncpg întoarce `attributes` și `variants` ca
    STRING. Contractul NX-205 le respinge corect („așteptat obiect, primit str") și
    `build_search_artifacts` crapă pe primul produs.

    Aceeași convenție ca `catalog.py::_row_to_product`, care o documentează explicit. Bug-ul a fost
    invizibil pentru teste fiindcă dublura de conexiune întorcea dict-uri Python — un stub care
    răspunde mai bine decât baza reală ascunde exact clasa asta de defect."""
    out = dict(row)
    for key, empty in (("attributes", {}), ("variants", [])):
        value = out.get(key)
        if isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except (ValueError, TypeError):
                out[key] = empty
        elif value is None:
            out[key] = empty
    return out


async def load_active_products(conn: Any, business_id: str) -> list[dict[str, Any]]:
    """Încarcă numai produse ale tenantului.

    Variantele sunt reconstruite în shape-ul contractului.
    """
    rows = await conn.fetch(
        """
        select p.id::text as id, p.slug, p.name, b.slug as "brandSlug",
               c.slug as "primaryCategorySlug", p.short_description as "shortDescription",
               p.description, p.attributes,
               coalesce(jsonb_agg(jsonb_build_object(
                 'label', pv.label, 'sku', pv.sku, 'gtin', pv.gtin,
                 'net_content', case when pv.net_content_value is null then null else
                   jsonb_build_object('value', pv.net_content_value,
                     'unit', pv.net_content_unit) end
               ) order by pv.label) filter (where pv.id is not null), '[]'::jsonb) as variants
        from products p
        left join brands b on b.id = p.brand_id and b.business_id = p.business_id
        left join categories c on c.id = p.primary_category_id and c.business_id = p.business_id
        left join product_variants pv on pv.product_id = p.id and pv.business_id = p.business_id
        where p.business_id = $1 and p.status = 'active'
        group by p.id, b.slug, c.slug
        order by p.id
        """,
        business_id,
    )
    return [_decode_jsonb(row) for row in rows]


async def upsert_artifacts(conn: Any, artifacts: SearchArtifacts) -> None:
    """Upsert idempotent al documentului/blurb-ului și refresh sigur al evidence generat.

    Toate operațiile filtrează `business_id`; writerul trebuie apelat prin `admin_conn`, niciodată
    din worker.

    `fts_document` folosește **`to_tsvector('simple', ro_unaccent(...))`**, identic cu
    `products.search_tsv` (033) și cu calea lexicală live (`search_products_lexical`). Două motive,
    ambele descoperite la verificare:
      • `unaccent()` NU EXISTĂ în baza asta — 033 spune explicit că extensia e disponibilă dar
        neinstalată, și de aceea proiectul are `ro_unaccent()` scrisă cu `translate` (imutabilă,
        deci utilizabilă într-o coloană generată). Un writer care apelează `unaccent()` nu rulează.
      • configurația `'romanian'` ar fi dat alt stemmer decât indexul live, pe text deja lipsit de
        diacritice — adică benchmarkul NX-203 ar fi măsurat diferența de NORMALIZARE, nu documentul
        nou. Un experiment pe stemmer se face deliberat și separat, nu ca efect secundar.
    Ponderile A/B/C se păstrează.
    """
    fts = artifacts.fts_document
    await conn.execute(
        """
        insert into product_search_documents
          (business_id, product_id, locale, document_version, schema_version,
           positive_search_document, fts_document, content_hash)
        values ($1, $2::uuid, $3, $4, $5, $6,
          setweight(to_tsvector('simple', ro_unaccent($7)), 'A') ||
          setweight(to_tsvector('simple', ro_unaccent($8)), 'B') ||
          setweight(to_tsvector('simple', ro_unaccent($9)), 'C'), $10)
        on conflict (business_id, product_id, locale, document_version) do update set
          schema_version = excluded.schema_version,
          positive_search_document = excluded.positive_search_document,
          fts_document = excluded.fts_document,
          content_hash = excluded.content_hash,
          updated_at = now()
        where product_search_documents.content_hash is distinct from excluded.content_hash
        """,
        artifacts.business_id,
        artifacts.product_id,
        artifacts.locale,
        artifacts.document_version,
        artifacts.schema_version,
        artifacts.positive_search_document,
        " ".join(fts.a),
        " ".join(fts.b),
        " ".join(fts.c),
        artifacts.content_hash,
    )
    # Blurb ABSENT ≠ blurb gol: dacă produsul n-are din ce compune un blurb util, nu scriem un rând
    # care ar trece `check length > 0` fiind doar numele produsului. Un rând inutil arată la fel ca
    # unul bun pentru orice consumator; absența se vede.
    if artifacts.card_blurb:
        await conn.execute(
            """
            insert into product_card_blurbs
              (business_id, product_id, locale, document_version, schema_version, text,
               content_hash)
            values ($1, $2::uuid, $3, $4, $5, $6, $7)
            on conflict (business_id, product_id, locale, document_version) do update set
              schema_version = excluded.schema_version, text = excluded.text,
              content_hash = excluded.content_hash, updated_at = now()
            where product_card_blurbs.content_hash is distinct from excluded.content_hash
            """,
            artifacts.business_id,
            artifacts.product_id,
            artifacts.locale,
            artifacts.document_version,
            artifacts.schema_version,
            artifacts.card_blurb,
            artifacts.content_hash,
        )
    else:
        await conn.execute(
            """delete from product_card_blurbs where business_id=$1 and product_id=$2::uuid
               and locale=$3 and document_version=$4""",
            artifacts.business_id,
            artifacts.product_id,
            artifacts.locale,
            artifacts.document_version,
        )

    # delete + insert ÎNTR-O SINGURĂ tranzacție: fără ea, o eroare între cele două lăsa produsul
    # cu evidence ȘTERS și nescris la loc — pierdere tăcută de date, exact în stratul care există
    # ca să fie citabil. `on conflict do nothing` ține pasul idempotent chiar dacă două fragmente
    # ajung la același (rol, locale, hash).
    async with conn.transaction():
        await conn.execute(
            """delete from product_evidence_chunks where business_id=$1 and product_id=$2::uuid
               and locale=$3 and source in ('catalog.shortDescription', 'catalog.description',
               'catalog.not_recommended_for')""",
            artifacts.business_id,
            artifacts.product_id,
            artifacts.locale,
        )
        for chunk in artifacts.evidence_chunks:
            await conn.execute(
                """insert into product_evidence_chunks
                  (business_id, product_id, role, text, source, locale, schema_version,
                   content_hash)
                  values ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
                  on conflict (business_id, product_id, role, locale, content_hash) do nothing""",
                chunk.business_id,
                chunk.product_id,
                chunk.role,
                chunk.text,
                chunk.source,
                chunk.locale,
                chunk.schema_version,
                json.dumps(chunk.model_dump(), ensure_ascii=False, sort_keys=True),
            )


async def plan_for_business(
    conn: Any, business_id: str, *, locale: str = "ro"
) -> list[SearchArtifacts]:
    """Construiește shadow-ul în memorie, pentru dry-run/inspectare fără niciun INSERT/UPDATE."""
    return [
        build_search_artifacts(product, business_id=business_id, locale=locale)
        for product in await load_active_products(conn, business_id)
    ]


async def build_for_business(conn: Any, business_id: str, *, locale: str = "ro") -> int:
    """Construiește artefactele pentru un tenant. Callerul decide tranzacția/job scheduling."""
    products = await load_active_products(conn, business_id)
    for product in products:
        artifacts = build_search_artifacts(product, business_id=business_id, locale=locale)
        await upsert_artifacts(conn, artifacts)
    return len(products)
