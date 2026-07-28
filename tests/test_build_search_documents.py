import contextlib

import pytest

from src.domain.search_documents import build_search_artifacts
from src.jobs.build_search_documents import build_for_business, plan_for_business, upsert_artifacts


class _Conn:
    def __init__(self, rows=()):
        self.rows, self.calls = rows, []
        self.tx_depth, self.in_tx = 0, []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self.rows

    async def execute(self, sql, *params):
        self.calls.append((sql, params))
        self.in_tx.append(self.tx_depth > 0)

    @contextlib.asynccontextmanager
    async def transaction(self):
        self.tx_depth += 1
        try:
            yield self
        finally:
            self.tx_depth -= 1


def _product():
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "slug": "p",
        "name": "Ser X",
        "brandSlug": "b",
        "primaryCategorySlug": "seruri-pentru-ten",
        "attributes": {},
        "variants": [],
    }


@pytest.mark.asyncio
async def test_writer_is_tenant_scoped_and_uses_weighted_shadow_fts():
    conn = _Conn()
    artifacts = build_search_artifacts(_product(), business_id="biz", locale="ro")
    await upsert_artifacts(conn, artifacts)
    sql = "\n".join(call[0] for call in conn.calls)
    assert "where business_id=$1 and product_id=$2::uuid" in sql
    # `'simple'` + `ro_unaccent`, IDENTIC cu products.search_tsv (033) și cu calea lexicală live.
    # `unaccent()` nu e instalată în baza asta — un writer care o apelează nu rulează deloc.
    assert "to_tsvector('simple', ro_unaccent($7))" in sql
    assert "unaccent($7)" in sql and "to_tsvector('romanian'" not in sql
    assert "setweight" in sql and "product_embeddings" not in sql
    assert all(call[1][0] == "biz" for call in conn.calls)


@pytest.mark.asyncio
async def test_evidence_refresh_is_atomic_and_idempotent():
    """delete + insert pe evidence trebuie să fie ÎN tranzacție: altfel o eroare între ele lasă
    produsul cu evidence șters și nescris la loc — pierdere tăcută exact în stratul citabil."""
    product = _product()
    product["shortDescription"] = "Ser cu niacinamidă."
    product["description"] = "Descriere lungă."
    conn = _Conn()
    await upsert_artifacts(conn, build_search_artifacts(product, business_id="biz", locale="ro"))

    evidence = [
        (sql, inside)
        for (sql, _), inside in zip(conn.calls, conn.in_tx, strict=True)
        if "product_evidence_chunks" in sql
    ]
    assert evidence, "nu s-a scris niciun fragment de evidence"
    assert all(inside for _, inside in evidence), "evidence atins în afara tranzacției"
    assert any("delete from product_evidence_chunks" in sql for sql, _ in evidence)
    assert all(
        "on conflict (business_id, product_id, role, locale, content_hash) do nothing" in sql
        for sql, _ in evidence
        if sql.lstrip().startswith("insert")
    )


@pytest.mark.asyncio
async def test_blurb_absent_is_deleted_not_written_as_the_product_name():
    """Un blurb egal cu numele produsului trece de `check length > 0` și arată ca unul bun pentru
    orice consumator. Absența se scrie ca absență."""
    artifacts = build_search_artifacts(_product(), business_id="biz", locale="ro")
    assert artifacts.card_blurb is None  # produsul nu are shortDescription/key_benefit/best_for

    conn = _Conn()
    await upsert_artifacts(conn, artifacts)
    blurb_sql = [sql for sql, _ in conn.calls if "product_card_blurbs" in sql]
    assert blurb_sql and all(sql.lstrip().startswith("delete") for sql in blurb_sql)


@pytest.mark.asyncio
async def test_jsonb_columns_arrive_as_strings_and_must_be_decoded():
    """Regresie, descoperită rulând pe un Postgres REAL: pool-ul nu înregistrează codec jsonb (doar
    unul de `vector`), deci asyncpg întoarce `attributes`/`variants` ca STRING. Contractul le
    respinge corect („așteptat obiect, primit str") și jobul crăpa pe PRIMUL produs — nu putea
    procesa niciunul.

    Testele nu-l puteau vedea pentru că dublura de conexiune întorcea dict-uri Python, adică
    răspundea mai bine decât baza reală. De aceea rândul de aici e string, ca în producție."""
    raw = _product()
    raw["attributes"] = '{"concerns": ["oily"], "texture": "gel"}'
    raw["variants"] = "[]"
    conn = _Conn([raw])

    artifacts = await plan_for_business(conn, "biz")

    assert len(artifacts) == 1
    assert "oily" in artifacts[0].positive_search_document
    assert "gel" in artifacts[0].fts_document.b


@pytest.mark.asyncio
async def test_null_jsonb_becomes_empty_not_a_contract_error():
    raw = _product()
    raw["attributes"], raw["variants"] = None, None

    artifacts = await plan_for_business(_Conn([raw]), "biz")

    assert len(artifacts) == 1 and artifacts[0].positive_search_document


@pytest.mark.asyncio
async def test_plan_is_read_only_and_generates_artifacts():
    conn = _Conn([_product()])
    artifacts = await plan_for_business(conn, "biz")
    assert len(artifacts) == 1 and artifacts[0].business_id == "biz"
    assert len(conn.calls) == 1
    assert "where p.business_id = $1 and p.status = 'active'" in conn.calls[0][0]


@pytest.mark.asyncio
async def test_build_loads_only_active_products_for_requested_tenant():
    conn = _Conn([_product()])
    assert await build_for_business(conn, "biz") == 1
    assert "where p.business_id = $1 and p.status = 'active'" in conn.calls[0][0]
    assert conn.calls[0][1] == ("biz",)
