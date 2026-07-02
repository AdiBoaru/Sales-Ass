"""Pool-uri asyncpg + izolare multi-tenant (NX-50: rol de LOGIN `bot_runtime`).

Două căi de acces, două pool-uri (vezi docs/db_connections.md):

  • TENANT PATH (hot path, ~tot traficul) — `tenant_conn(business_id)` pe
    `bot_pool`. Pool-ul se loghează DIRECT ca `bot_runtime` (rol de LOGIN, fără
    bypassrls, parolă proprie). Identitatea e fixată de credențiale, NU de un
    `SET ROLE` de sesiune → nu se mai poate scurge sub multiplexarea poolerului
    (P0-A din audit). La checkout setăm doar `app.business_id`; la release îl
    resetăm. RLS devine plasă: un query fără filtru de tenant → 0 rânduri.

  • CONTROL PLANE (o dată/mesaj, înainte de a ști tenantul) — `admin_conn(pool)`
    pe `admin_pool` (rol privilegiat, SUPABASE_DB_URL). Singura excepție de la
    „business_id pe tot": lookup `provider_account_id → business_id` (channels.py)
    precede tenantul. Tot aici rulează joburile admin (cleanup, embed).

Folosire (tenant):
    async with tenant_conn(business_id) as conn:
        rows = await conn.fetch("select * from products limit 5")
        # rândurile sunt DEJA filtrate la business_id de RLS

Provisioning (o dată, manual — vezi docs/005_bot_runtime_login.sql + TODO-MANUAL):
    ALTER ROLE bot_runtime LOGIN PASSWORD '<din vault>';  + setezi DATABASE_URL_BOT.
    Fără DATABASE_URL_BOT, `bot_pool` cade grațios pe SUPABASE_DB_URL + `SET ROLE`
    (compat dev/test înainte de provisioning; NU pentru prod — vezi docstring-ul).
"""

import logging
import socket
import ssl
import sys
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlparse

import asyncpg

from src.config import get_settings
from src.db.errors import IsolationError

log = logging.getLogger(__name__)

# admin_pool (control plane + joburi) și bot_pool (tenant path). Singleton/proces.
_pool: asyncpg.Pool | None = None
_bot_pool: asyncpg.Pool | None = None
# True dacă bot_pool s-a logat DIRECT ca bot_runtime (DATABASE_URL_BOT setat).
# False = mod compat: logat ca admin, coborâm rolul în init (dev/test).
_bot_login_mode: bool = False


def _connect_kwargs(dsn: str) -> dict:
    """Pe Windows, resolverul async al asyncpg (getaddrinfo) e flaky, iar
    conexiunea directă Supabase nu se rezolvă pe IPv4. Rezolvăm host-ul în IPv4
    sincron și conectăm pe IP, cu SSL fără verificare de hostname.
    Pe Linux/VPS (prod) folosim DSN-ul direct, curat.
    """
    if sys.platform != "win32":
        return {"dsn": dsn}

    p = urlparse(dsn)
    ipv4 = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][
        0
    ]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {
        "host": ipv4,
        "port": p.port or 5432,
        "user": unquote(p.username),
        "password": unquote(p.password),
        "database": (p.path or "/postgres").lstrip("/"),
        "ssl": ctx,
    }


async def get_pool() -> asyncpg.Pool:
    """Pool ADMIN (control plane + joburi). Privilegiat — NU pentru date de tenant.

    Folosit DOAR de `admin_conn` (resolve_channel) și de joburile admin. Pentru
    orice e tenant-scoped folosește `tenant_conn` (bot_pool). Lazy-init.
    """
    global _pool
    if _pool is None:
        dsn = get_settings().supabase_db_url
        _pool = await asyncpg.create_pool(
            **_connect_kwargs(dsn),
            min_size=1,
            max_size=10,
            # fără prepared statement cache: pooler-ul Supabase (pgbouncer)
            # nu garantează aceeași sesiune backend între statement-uri
            statement_cache_size=0,
        )
    return _pool


async def _assert_bot_role(conn: asyncpg.Connection) -> None:
    """Plasă la boot: după init, identitatea efectivă TREBUIE să fie bot_runtime.

    Dacă DATABASE_URL_BOT a fost setat greșit (ex. spre un rol privilegiat),
    pool-ul NU pornește — eroare explicită la boot, nu un drum de cod care
    rulează ca superuser și ocolește RLS în tăcere (P0-A)."""
    user = await conn.fetchval("select current_user")
    if user != "bot_runtime":
        raise RuntimeError(
            f"bot_pool: rol efectiv {user!r}, aștept 'bot_runtime'. "
            "Verifică DATABASE_URL_BOT (login bot_runtime) sau grant-ul bot_runtime→postgres."
        )


async def _init_bot_conn_compat(conn: asyncpg.Connection) -> None:
    """Mod compat (fără DATABASE_URL_BOT): ne-am logat ca admin → coborâm o
    singură dată la rol, la crearea conexiunii (NU per-checkout, ca să nu existe
    un `SET ROLE` care se scurge sub multiplexare). Apoi verificăm."""
    await conn.execute("set role bot_runtime")
    await _assert_bot_role(conn)


def _vector_encode(value) -> str:
    """list[float] → literal text pgvector `[a,b,c]` (formatul trimis ca `::vector`).

    NX-137: acceptă ȘI un literal DEJA formatat (str) — `faqs.py`/`semantic_cache.py` pre-formatau
    cu `_vec()` (convenția pre-NX-113c), iar codecul făcea `float('[')` → DataError pe ORICE
    lookup FAQ/cache → ambele straturi gratuite mureau TĂCUT (best-effort → miss). Nedetectat cât
    `faqs` era gol pe demo; a ieșit la iveală la primul seed real (diagnostic live pe sim)."""
    if isinstance(value, str):
        return value
    return "[" + ",".join(f"{float(x):.7f}" for x in value) + "]"


def _vector_decode(value: str) -> list[float]:
    """Literal pgvector `[a,b,c]` → list[float] (rar folosit: nu citim coloana embedding)."""
    s = (value or "").strip()
    if len(s) <= 2:
        return []
    return [float(x) for x in s[1:-1].split(",")]


async def register_vector_codec(conn: asyncpg.Connection) -> None:
    """NX-113c: codec pgvector (text) pe conexiune → trimitem `list[float]` DIRECT ca `$n::vector`,
    fără literalul text de ~15KB inline pe fiecare query semantic (hot path).

    DEFENSIV: dacă tipul `vector` nu există / introspecția pică, NU rupem pool init — boot-ul nu
    trebuie să cadă pe un codec opțional. Fără codec, query-ul semantic ar pica la encode, dar e
    prins de orchestrator (search_products_tool) → degradare lexical-only, fără tăcere (P6)."""
    try:
        # Rezolvă schema REALĂ a tipului `vector`: `set_type_codec` face match EXACT pe namespace
        # (NU pe search_path), iar migrarea creează extensia fără clauză de schemă → pe Supabase
        # tipul poate ajunge în `extensions`, nu `public`. Hardcodarea 'public' ar rata tipul →
        # codec neînregistrat în tăcere → optimizarea 113c defeated odată ce apar embeddings.
        ns = await conn.fetchval(
            "select n.nspname from pg_type t "
            "join pg_namespace n on n.oid = t.typnamespace "
            "where t.typname = 'vector' limit 1"
        )
        if ns is None:
            log.warning("register_vector_codec: tipul pgvector 'vector' nu există (fallback)")
            return
        await conn.set_type_codec(
            "vector",
            schema=ns,
            encoder=_vector_encode,
            decoder=_vector_decode,
            format="text",
        )
    except Exception:  # noqa: BLE001 — tip absent / pooler → rămânem fără codec (fallback la query)
        log.warning("register_vector_codec: codec pgvector neînregistrat (fallback text-inline)")


async def _init_bot_login(conn: asyncpg.Connection) -> None:
    """Init bot_pool (login direct): verifică rolul (boot fail-loud pe rol greșit), apoi codec."""
    await _assert_bot_role(conn)
    await register_vector_codec(conn)


async def _init_bot_compat(conn: asyncpg.Connection) -> None:
    """Init bot_pool (compat): coboară+verifică rolul, apoi codec."""
    await _init_bot_conn_compat(conn)
    await register_vector_codec(conn)


def _isolation_enabled() -> bool:
    """NX-04: asserturile de izolare la checkout sunt active (default) decât dacă
    DB_ISOLATION_ASSERT='off'."""
    return get_settings().db_isolation_assert != "off"


def _check_isolation(current_user: str | None, current_biz: str | None, expected: str) -> None:
    """Plasa NX-04: conexiunea scoasă din bot_pool TREBUIE să fie `bot_runtime`
    și să poarte exact `app.business_id = expected`. Orice abatere → IsolationError
    ÎNAINTE de primul query (rol greșit, GUC nesetat, sau reuse murdar de la alt
    tenant). Mesajul are doar id-uri de tenant + rolul, fără date de client."""
    if current_user != "bot_runtime":
        raise IsolationError(
            f"checkout bot_pool cu rol {current_user!r}, aștept 'bot_runtime' (business {expected})"
        )
    if not current_biz:
        raise IsolationError(
            f"checkout bot_pool fără app.business_id setat (business cerut {expected})"
        )
    if current_biz != expected:
        raise IsolationError(
            f"checkout bot_pool cu app.business_id={current_biz}, dar s-a cerut {expected} "
            "(reuse murdar de conexiune)"
        )


async def get_bot_pool() -> asyncpg.Pool:
    """Pool TENANT (bot_runtime). Login direct dacă DATABASE_URL_BOT e setat;
    altfel compat pe SUPABASE_DB_URL + SET ROLE în init (dev/test).

    Eager la boot-ul workerului (consumer/dispatcher) ca o parolă greșită să
    crape la pornire, nu la primul mesaj."""
    global _bot_pool, _bot_login_mode
    if _bot_pool is None:
        s = get_settings()
        if not _isolation_enabled():
            log.warning(
                "DB_ISOLATION_ASSERT=off — asserturile de izolare la checkout sunt "
                "DEZACTIVATE; o regresie de izolare NU mai pică zgomotos (NX-04)"
            )
        _bot_login_mode = bool(s.database_url_bot)
        dsn = s.database_url_bot or s.supabase_db_url
        init = _init_bot_login if _bot_login_mode else _init_bot_compat
        _bot_pool = await asyncpg.create_pool(
            **_connect_kwargs(dsn),
            min_size=2,
            max_size=10,
            # login direct (conexiune de sesiune) → cache OK; compat pe pooler → 0
            statement_cache_size=100 if _bot_login_mode else 0,
            init=init,
        )
    return _bot_pool


async def close_pool() -> None:
    """Închide ambele pool-uri (la oprirea procesului)."""
    global _pool, _bot_pool
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _bot_pool is not None:
        await _bot_pool.close()
        _bot_pool = None


@asynccontextmanager
async def admin_conn(pool: asyncpg.Pool):
    """Conexiune de CONTROL PLANE — fără scope de tenant (rol privilegiat).

    Folosită DOAR pentru lookup-uri de infrastructură care preced rezolvarea
    tenantului — în practică `provider_account_id → business_id` (channels.py) —
    și pentru joburile admin. NU citi/scrie date de client pe ea: pentru orice e
    tenant-scoped folosește `tenant_conn`. RLS e bypass-at aici (rol privilegiat),
    de aceea suprafața e limitată intenționat la maparea canal→business + mentenanță.
    """
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def tenant_conn(business_id: str):
    """Conexiune tenant-scoped (RLS activ) din `bot_pool`.

    Conexiunea ESTE deja `bot_runtime` (login direct sau coborât în init) — aici
    NU mai facem `SET ROLE` (asta era scurgerea P0-A). Setăm doar `app.business_id`
    pentru durata checkout-ului și îl resetăm la release, ca să nu „murdărim"
    conexiunea întoarsă în pool (un checkout ulterior pe alt tenant n-o vede)."""
    pool = await get_bot_pool()
    async with pool.acquire() as conn:
        # set_config(..., is_local=false) → ține pe checkout; quoting safe pe param.
        # Plasa NX-04 (strict): set + verificare ÎNTR-UN SINGUR round-trip —
        # set_config întoarce valoarea setată, current_user confirmă rolul. Zero
        # latență în plus față de set-ul pe care oricum îl făceam.
        if _isolation_enabled():
            row = await conn.fetchrow(
                "select set_config('app.business_id', $1, false) as biz, current_user as usr",
                business_id,
            )
            try:
                _check_isolation(row["usr"], row["biz"], business_id)
            except IsolationError as e:
                # alertă maximă: o conexiune neizolată a ajuns la checkout. Logăm
                # CRITIC (fără PII) și resetăm GUC-ul; NU scriem în analytics_events
                # (ar fi un write pe exact conexiunea declarată ne-de-încredere).
                log.critical("isolation_assert_failed: %s", e)
                await conn.execute("select set_config('app.business_id', '', false)")
                raise
        else:
            await conn.execute("select set_config('app.business_id', $1, false)", business_id)
        try:
            yield conn
        finally:
            # echivalent RESET app.business_id → următorul checkout fail-closed
            await conn.execute("select set_config('app.business_id', '', false)")
