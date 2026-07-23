"""NX-216 — `prompt_version` ca dimensiune de cheie în `semantic_cache`.

Bug-ul reparat: indexul unic live avea 4 coloane (`+ prompt_version`, via migrarea 030 din
NX-181, ajunsă în Supabase dar nu în `main`), iar `ON CONFLICT`-ul din cod avea 3 → FIECARE
write-back arunca `InvalidColumnReferenceError`, înghițit de best-effort-ul din aftercare.
Cache-ul rămăsese înghețat (citirile vechi mergeau, scrierile nu). Vezi `tasks/NX-216.md`.

Testele de aici păzesc trei lucruri distincte:
  1. simetria citire/scriere pe aceeași sursă de versiune (altfel: cache mort SAU servire
     încrucișată între versiuni de prompt);
  2. izolarea reală v1 ↔ vnext pe DB (nu se suprascriu, nu se citesc încrucișat);
  3. faptul că un `ON CONFLICT` desincronizat de indexul unic e prins de teste, nu de producție.
"""

from __future__ import annotations

import pytest

DEMO = "6098812a-50fc-44bd-a1ba-bc77e6399158"


# --- Sursa unică de versiune (pur, fără DB) ----------------------------------


def test_read_and_write_use_the_same_version_source():
    """Citirea (cache_stage) și scrierea (aftercare) trebuie să ceară versiunea din ACELAȘI loc.

    Divergența dintre capete e exact modul în care s-a produs NX-216; un default implicit
    într-un capăt și o valoare hardcodată în celălalt nu s-ar vedea până în producție.
    """
    import inspect

    from src.cache.version import DEFAULT_PROMPT_VERSION, cache_prompt_version
    from src.worker import aftercare
    from src.worker.stages import cache as cache_stage

    assert cache_prompt_version(None) == DEFAULT_PROMPT_VERSION

    # Ambele capete apelează helper-ul — nu construiesc versiunea local.
    assert "cache_prompt_version(" in inspect.getsource(cache_stage)
    assert "cache_prompt_version(" in inspect.getsource(aftercare)


def test_queries_accept_prompt_version_on_all_three_paths():
    """Cele DOUĂ citiri + scrierea sunt parametrizate. Dacă una lipsește, izolarea e falsă:
    scrii în namespace-ul corect dar citești din altul (sau invers)."""
    import inspect

    from src.db.queries.semantic_cache import exact_lookup, semantic_lookup, upsert_entry

    for fn in (exact_lookup, semantic_lookup, upsert_entry):
        assert "prompt_version" in inspect.signature(fn).parameters, fn.__name__


def test_upsert_conflict_target_matches_unique_index():
    """`ON CONFLICT` trebuie să enumere EXACT coloanele indexului unic `idx_semcache_exact`.

    Test anti-regresie direct pe cauza NX-216: Postgres nu potrivește un conflict target de 3
    coloane cu un index unic pe 4 → eroare la fiecare scriere. Ținem cele două în sincron aici,
    ca divergența să pice în CI, nu tăcut în producție.
    """
    import inspect
    import re

    from src.db.queries import semantic_cache

    src = inspect.getsource(semantic_cache.upsert_entry)
    m = re.search(r"on conflict \(([^)]*)\)", src, re.IGNORECASE)
    assert m, "upsert_entry nu mai are ON CONFLICT — contractul de idempotență s-a rupt"
    target = {c.strip() for c in m.group(1).split(",")}
    assert target == {"business_id", "locale", "canonical_hash", "prompt_version"}

    # Aceleași coloane trebuie create de migrarea 034 (sursa de adevăr a indexului).
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1] / "docs" / "034_semantic_cache_prompt_version.sql"
    )
    ddl = migration.read_text(encoding="utf-8").lower()
    assert "create unique index if not exists idx_semcache_exact" in ddl
    for col in target:
        assert col in ddl


# --- Integration (DB real): izolarea v1 ↔ vnext ------------------------------


@pytest.fixture
async def pool():
    from src.db.connection import close_pool, get_pool

    p = await get_pool()
    yield p
    await close_pool()


@pytest.mark.integration
async def test_v1_and_vnext_coexist_without_overwrite_or_cross_read(pool):
    """Același query canonic, scris sub v1 ȘI vnext: două rânduri distincte, fiecare citit
    doar din namespace-ul lui. Fără asta, activarea Prompt vNext ar servi răspunsuri compuse
    cu promptul vechi (și invers) — motivul pentru care coloana există."""
    from src.db.queries.semantic_cache import exact_lookup, upsert_entry

    emb = [0.013] * 1536
    h = "nx216-probe-hash"

    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("set role bot_runtime")
            await conn.execute("select set_config('app.business_id', $1, true)", DEMO)

            common = dict(
                canonical_str="proba nx216 namespace",
                canonical_hash=h,
                embedding=emb,
                volatility_class="static",
                embedding_model="text-embedding-3-small",
                quality_score=1.0,
                ttl_days=1,
            )
            await upsert_entry(conn, DEMO, "ro", answer="raspuns v1", prompt_version="v1", **common)
            await upsert_entry(
                conn, DEMO, "ro", answer="raspuns vnext", prompt_version="vnext", **common
            )

            # 1. Scrierea a doua NU a suprascris-o pe prima (altfel ON CONFLICT ar fi prins).
            n = await conn.fetchval(
                "select count(*) from semantic_cache "
                "where business_id = $1 and canonical_hash = $2",
                DEMO,
                h,
            )
            assert n == 2, "v1 și vnext trebuie să fie rânduri DISTINCTE"

            # 2. Fiecare citire vede DOAR namespace-ul ei (zero servire încrucișată).
            hit_v1 = await exact_lookup(
                conn, DEMO, "ro", h, volatility_class="static", prompt_version="v1"
            )
            hit_vnext = await exact_lookup(
                conn, DEMO, "ro", h, volatility_class="static", prompt_version="vnext"
            )
            assert hit_v1 is not None and hit_v1["answer"] == "raspuns v1"
            assert hit_vnext is not None and hit_vnext["answer"] == "raspuns vnext"

            # 3. Un namespace inexistent = miss curat, nu fallback pe altă versiune.
            assert (
                await exact_lookup(
                    conn, DEMO, "ro", h, volatility_class="static", prompt_version="v-inexistent"
                )
                is None
            )

            # 4. Re-scrierea în ACELAȘI namespace actualizează (idempotență păstrată).
            await upsert_entry(
                conn, DEMO, "ro", answer="raspuns v1 refresh", prompt_version="v1", **common
            )
            again = await conn.fetchval(
                "select count(*) from semantic_cache "
                "where business_id = $1 and canonical_hash = $2",
                DEMO,
                h,
            )
            assert again == 2, "upsert în namespace existent nu trebuie să adauge rând nou"
            refreshed = await exact_lookup(
                conn, DEMO, "ro", h, volatility_class="static", prompt_version="v1"
            )
            assert refreshed is not None and refreshed["answer"] == "raspuns v1 refresh"
        finally:
            await tr.rollback()


@pytest.mark.integration
async def test_migration_034_is_idempotent_on_livelike_schema(pool):
    """Migrarea trebuie să fie corectă în AMBELE stări (cerință A+):
      • DB live — coloana + indexul pe 4 coloane există deja (via 030, aplicată în Supabase);
      • DB proaspăt — nu există.
    Rulăm DDL-ul de două ori într-o tranzacție rollback-uită: a doua rulare pe schema deja
    migrată nu trebuie să arunce, iar indexul rămâne cel pe 4 coloane.
    """
    from pathlib import Path

    ddl = (
        Path(__file__).resolve().parents[1] / "docs" / "034_semantic_cache_prompt_version.sql"
    ).read_text(encoding="utf-8")

    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute(ddl)  # peste starea live (coloana există deja) → no-op
            await conn.execute(ddl)  # a doua oară → tot no-op (idempotent)

            idx = await conn.fetchval(
                "select indexdef from pg_indexes where indexname = 'idx_semcache_exact'"
            )
            assert idx is not None
            for col in ("business_id", "locale", "canonical_hash", "prompt_version"):
                assert col in idx

            col = await conn.fetchrow(
                "select is_nullable, column_default from information_schema.columns "
                "where table_name = 'semantic_cache' and column_name = 'prompt_version'"
            )
            assert col is not None and col["is_nullable"] == "NO"
        finally:
            await tr.rollback()
