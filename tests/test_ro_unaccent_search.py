"""NX-178 — căutarea nu are voie să depindă de diacritice.

Un client român care scrie „sampon" trebuie să găsească exact ce găsește cel care scrie „șampon".
Înainte de migrarea 033: **20 vs 0** rezultate. Nu relevanță slabă — inexistență.

Testele rulează pe DB reală (`integration`), pentru că exact asta e garanția care nu poate fi
dovedită fără ea: coloana generată și interogarea trebuie normalizate cu ACEEAȘI funcție.
"""

from __future__ import annotations

import pytest

from src.db.connection import admin_conn, close_pool, get_pool

pytestmark = [pytest.mark.integration]

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

#: exact condiția din `search_products_lexical` (match FTS SAU fuzzy pe nume), ambele normalizate
_MATCH_SQL = (
    "select count(*) from products p "
    "where p.business_id=$1 and p.status='active' "
    "and (p.search_tsv @@ websearch_to_tsquery('simple', ro_unaccent($2)) "
    "or ro_unaccent(p.name) % ro_unaccent($2))"
)

PERECHI = [
    ("păr uscat", "par uscat"),
    ("șampon", "sampon"),
    ("cremă hidratantă", "crema hidratanta"),
    ("mască de față", "masca de fata"),
    ("ulei de păr", "ulei de par"),
    ("protecție solară", "protectie solara"),
]


# pytest-asyncio dă fiecărui test propriul event loop, iar `get_pool()` memorează pool-ul global —
# legat de PRIMUL loop. Al doilea test ar crăpa cu „attached to a different loop". De aceea fiecare
# test își ia pool-ul și îl închide la final. (Individual treceau toate; doar rulate împreună se
# vedea problema — exact genul de lucru pe care un test rulat izolat îl ascunde.)
@pytest.fixture(autouse=True)
async def _fresh_pool():
    yield
    await close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("cu_diacritice,fara", PERECHI)
async def test_paritate_cu_si_fara_diacritice(cu_diacritice, fara):
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        a = await conn.fetchval(_MATCH_SQL, DEMO_BIZ, cu_diacritice)
        b = await conn.fetchval(_MATCH_SQL, DEMO_BIZ, fara)
    assert a > 0, f"«{cu_diacritice}» nu găsește nimic — testul n-ar demonstra nimic"
    assert a == b, f"«{cu_diacritice}»={a} vs «{fara}»={b}"


@pytest.mark.asyncio
async def test_functia_acopera_si_forma_cu_sedila():
    """ş/ţ (sedilă, U+015F/U+0163) apar în text copiat din surse vechi și se tastează des —
    dacă le ratăm, jumătate din corpusul RO real rămâne negăsibil."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        assert await conn.fetchval("select ro_unaccent($1)", "Şampon Ţeapă") == "sampon teapa"
        assert await conn.fetchval("select ro_unaccent($1)", "Șampon Țeapă") == "sampon teapa"
        assert await conn.fetchval("select ro_unaccent($1)", "ÎNGRIJIRE Păr") == "ingrijire par"


@pytest.mark.asyncio
async def test_coloana_generata_e_normalizata():
    """Dacă doar interogarea ar fi normalizată, potrivirea tot n-ar avea loc — coloana generată
    trebuie construită peste text normalizat. Verificăm pe definiția REALĂ din DB."""
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        expr = await conn.fetchval(
            "select pg_get_expr(d.adbin, d.adrelid) from pg_attrdef d "
            "join pg_attribute a on a.attrelid=d.adrelid and a.attnum=d.adnum "
            "where a.attrelid='products'::regclass and a.attname='search_tsv'"
        )
    assert "ro_unaccent" in (expr or ""), expr
