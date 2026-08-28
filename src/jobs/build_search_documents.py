"""NX-207 — writer ADMIN pentru artefactele de search, numai shadow.

Nu este invocat de worker/read-path. Se rulează explicit de un job de conținut după aplicarea 035
și 036; lipsa tabelelor produce eroare vizibilă, nu fallback care ar ascunde un shadow incomplet.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from src.domain.search_documents import SearchArtifacts, build_search_artifacts

log = logging.getLogger(__name__)


def _hash_chunk(chunk: Any) -> str:
    """Amprenta unui `EvidenceChunk`, în aceeași formă canonică folosită de NX-207.

    Aceeași rețetă ca `src/domain/search_documents._hash_payload`: JSON sortat, separatori
    compacți, sha256. Deliberat identică, nu doar „un hash": dacă cele două ar diverge, două
    reprezentări ale aceluiași fragment ar da amprente diferite și deduplicarea ar tăcea.
    """
    raw = json.dumps(chunk.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
        select p.id::text as id, p.slug, p.name, p.updated_at as source_version,
               b.slug as "brandSlug",
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


async def upsert_artifacts(
    conn: Any, artifacts: SearchArtifacts, *, source_version: Any = None
) -> bool:
    """Scrie TOATE artefactele unui produs — document, blurb, evidence — ATOMIC.

    Întoarce `True` dacă s-a scris, `False` dacă produsul s-a schimbat între snapshot și scriere.

    **`source_version` închide cursa produs → document** (review #251, runda 3). Artefactele se
    construiesc dintr-un SNAPSHOT al produsului; între citire și scriere produsul poate fi
    actualizat, iar noi publicam un document derivat din date VECHI, ștampilat cu un `content_hash`
    care spune „ăsta e curent". Verificarea e concurență optimistă pe `products.updated_at` (are
    trigger `trg_products_upd`), făcută ÎN tranzacție: dacă produsul a mișcat, nu scriem nimic.

    Eșuează în SIGURANȚĂ: scrierea se sare, nu se reîncearcă. Rularea următoare o prinde oricum,
    fiindcă va construi din datele noi și va obține alt hash. Fără lock-uri, fără retry.

    `source_version=None` dezactivează verificarea (dry-run, teste pe artefacte construite manual).

    **Tranzacția acoperă tot produsul, nu doar evidence** (review #251, runda 2). Înainte începea
    abia la evidence, deci documentul și blurb-ul erau deja scrise când pica ceva: produsul rămânea
    cu un `positive_search_document` NOU și evidence VECHI (sau șters). Cele trei artefacte descriu
    același produs la aceeași `document_version` — dacă pot diverge, `content_hash` nu mai înseamnă
    nimic, iar un cititor n-are cum să afle că se uită la o combinație care n-a existat niciodată.

    Granularitatea e per PRODUS, nu per rulare: un job peste tot catalogul într-o singură tranzacție
    ar ține un lock lung și ar arunca la gunoi munca bună a mii de produse pentru un singur rând
    stricat. Produsul e unitatea de consistență pentru că e unitatea pe care o citește cineva.

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
    async with conn.transaction():
        if source_version is not None:
            current = await conn.fetchval(
                "select updated_at from products where id = $1::uuid and business_id = $2",
                artifacts.product_id,
                artifacts.business_id,
            )
            if current != source_version:
                # Produsul s-a schimbat sub noi. Un document construit din snapshot-ul vechi ar fi
                # o minciună cu ștampilă de prospețime — mai rău decât lipsa lui, pentru că
                # `content_hash` l-ar declara actual.
                return False
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
        # Blurb ABSENT ≠ blurb gol: dacă produsul n-are din ce compune un blurb util, nu scriem un
        # rând care ar trece `check length > 0` fiind doar numele produsului. Un rând inutil arată
        # la fel ca unul bun pentru orice consumator; absența se vede.
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

        # `on conflict do nothing` ține pasul idempotent chiar dacă două fragmente ajung la același
        # (rol, locale, hash).
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
                # HASH, nu payloadul serializat. `content_hash` intră în constrângerea unică
                # `(business_id, product_id, role, locale, content_hash)`, iar un index btree
                # nu acceptă chei peste ~2.704 octeți. Cu payloadul brut acolo, orice produs cu
                # descriere lungă rupe jobul:
                #   „index row size 3072 exceeds btree version 4 maximum 2704".
                #
                # N-a explodat până acum fiindcă descrierile catalogului demo erau scurte. Prima
                # rulare pe catalogul real (2.758 de produse, descriere medie 1.618 caractere) a
                # picat pe produsul 1.648. Un sha256 are lungime fixă și aceeași semantică de
                # deduplicare: două fragmente identice dau același hash.
                _hash_chunk(chunk),
            )
    return True


async def plan_for_business(
    conn: Any, business_id: str, *, locale: str = "ro"
) -> list[SearchArtifacts]:
    """Construiește shadow-ul în memorie, pentru dry-run/inspectare fără niciun INSERT/UPDATE."""
    return [
        build_search_artifacts(product, business_id=business_id, locale=locale)
        for product in await load_active_products(conn, business_id)
    ]


async def build_for_business(conn: Any, business_id: str, *, locale: str = "ro") -> int:
    """Construiește artefactele pentru un tenant. Callerul decide tranzacția/job scheduling.

    Întoarce câte produse au fost SCRISE efectiv — nu câte au fost citite. Cele sărite (produsul s-a
    schimbat sub snapshot) sunt logate și rămân pentru rularea următoare; a le număra ca procesate
    ar fi exact raportarea care ascunde problema."""
    products = await load_active_products(conn, business_id)
    written = 0
    for product in products:
        artifacts = build_search_artifacts(product, business_id=business_id, locale=locale)
        if await upsert_artifacts(conn, artifacts, source_version=product.get("source_version")):
            written += 1
        else:
            log.info(
                "search_documents: produs sărit, s-a schimbat între snapshot și scriere (id=%s)",
                artifacts.product_id,
            )
    if skipped := len(products) - written:
        log.warning("search_documents: %d produse sărite din %d", skipped, len(products))
    return written
