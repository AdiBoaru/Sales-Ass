"""Rezolvarea canalului — control plane (NU date de tenant).

Problema de bootstrap: un mesaj inbound vine cu `phone_number_id` (canalul Meta),
dar pentru a deschide o conexiune tenant-scoped avem nevoie de `business_id` —
exact ce încercăm să aflăm. Lookup-ul phone_number_id → business e deci o
operație de CONTROL PLANE, rulată pe o conexiune admin (`admin_conn`), nu pe una
de tenant. E singura excepție de la „business_id pe tot": aici îl DERIVĂM.

`channels` e o tabelă de infrastructură (mapare canal→business), nu date de
client. Lookup-ul e parametrizat (zero injection) și întoarce strict id-urile.
"""

import json

import asyncpg


async def resolve_web_session(
    conn: asyncpg.Connection, public_token: str
) -> dict[str, str | None] | None:
    """`public_token → {business_id, session_secret, identity_secret, default_locale}` pt un canal
    `webchat` ACTIV.

    Control plane (`admin_conn`): derivă tenantul ÎNAINTE de a-l ști, ca `resolve_channel`.
    `session_secret` (NX-20) semnează `visitor_id`-ul anonim; `identity_secret` (NX-129, opțional)
    verifică JWT-ul de login passthrough — DOUĂ chei separate. None dacă tokenul nu mapează la un
    canal activ SAU canalul n-are `session_secret` (seed incomplet); `identity_secret` lipsă =
    login passthrough inactiv pe acel tenant (nu invalidează sesiunea anonimă).

    NX-244: `default_locale` vine din `businesses`, nu din `channels` — limba e a tenantului, iar
    copy-ul de shell servit la bootstrap trebuie să fie în ea (D3: locale-aware, nu română
    hardcodată). JOIN, nu al doilea query: rezultatul e oricum cache-uit de `SessionSecretCache`,
    deci costul e o coloană în plus la un miss, nu un round-trip per request."""
    row = await conn.fetchrow(
        """
        select c.business_id::text as business_id,
               c.settings->>'session_secret' as session_secret,
               c.settings->>'session_secret_prev' as session_secret_prev,
               c.settings->>'identity_secret' as identity_secret,
               c.settings->>'allowed_origins' as allowed_origins,
               b.default_locale as default_locale
        from channels c
        join businesses b on b.id = c.business_id
        where c.kind = 'webchat'
          and c.provider_account_id = $1
          and c.status = 'active'
        """,
        public_token,
    )
    if row is None or not row["session_secret"]:
        return None
    return {
        "business_id": row["business_id"],
        "session_secret": row["session_secret"],
        # NX-229: cheia PRECEDENTĂ, pentru overlapul de rotație. Absentă = nicio rotație în curs;
        # sesiunile semnate cu ea rămân valide până le expiră singure, deci rotația nu mai
        # deconectează pe toată lumea deodată.
        "session_secret_prev": row["session_secret_prev"],
        "identity_secret": row["identity_secret"],
        # NX-229: allowlist de origini PER CANAL (CSV). Absent → se folosește cel global din
        # settings. Un tenant nu trebuie să poată vedea sau moșteni originile altuia.
        "allowed_origins": row["allowed_origins"],
        # NX-244: limba tenantului, pentru copy-ul de shell servit la bootstrap. `normalize_locale`
        # decide fallbackul la consumator — aici întoarcem exact ce e în DB, inclusiv `None`.
        "default_locale": row["default_locale"],
    }


async def resolve_channel(
    conn: asyncpg.Connection,
    channel_kind: str,
    provider_account_id: str,
) -> dict[str, str] | None:
    """(channel_kind, provider_account_id) → {business_id, channel_id}, sau None.

    Generic pe canal (NX-60): phone_number_id la WhatsApp, bot id la Telegram, ...
    A se rula pe `admin_conn` (cross-tenant): la momentul apelului încă nu avem
    un tenant scope. Filtrăm pe canal activ — un canal dezactivat nu primește
    procesare (mesajele lui se ignoră, nu crapă worker-ul).
    """
    row = await conn.fetchrow(
        """
        select id::text as channel_id, business_id::text as business_id
        from channels
        where kind = $1
          and provider_account_id = $2
          and status = 'active'
        """,
        channel_kind,
        provider_account_id,
    )
    return dict(row) if row else None


async def resolve_channel_by_phone(
    conn: asyncpg.Connection,
    phone_number_id: str,
) -> dict[str, str] | None:
    """Wrapper WhatsApp peste `resolve_channel` (compat)."""
    return await resolve_channel(conn, "whatsapp", phone_number_id)


async def upsert_channel(
    conn: asyncpg.Connection,
    business_id: str,
    kind: str,
    provider_account_id: str,
    *,
    display_name: str | None = None,
    settings: dict | None = None,
) -> dict:
    """Creează/actualizează un canal (idempotent pe unique(kind, provider_account_id)).

    Operație de ONBOARDING — a se rula cu rol ADMIN (postgres), NU bot_runtime:
    `channels` e read-only pentru bot. Întoarce {id, created} (created=False dacă
    rândul exista deja și a fost reactivat). Folosit de scripturile de seed.

    `settings` (opțional, NX-20): la conflict, cheile EXISTENTE câștig (`$5 || channels.settings`)
    → un re-seed NU suprascrie `session_secret`-ul deja emis (altfel sigurile vizitatorilor
    devin invalide); adaugă doar cheile noi. `None` (ex. seed Telegram) lasă settings neatins."""
    row = await conn.fetchrow(
        """
        insert into channels
            (business_id, kind, provider_account_id, display_name, status, settings)
        values ($1, $2, $3, $4, 'active', coalesce($5::jsonb, '{}'::jsonb))
        on conflict (kind, provider_account_id) do update
            set status = 'active',
                business_id = excluded.business_id,
                display_name = coalesce(excluded.display_name, channels.display_name),
                settings = case
                    when $5::jsonb is null then channels.settings
                    else coalesce($5::jsonb, '{}'::jsonb) || channels.settings
                end
        returning id::text as id, (xmax = 0) as created
        """,
        business_id,
        kind,
        provider_account_id,
        display_name,
        json.dumps(settings) if settings is not None else None,
    )
    return {"id": row["id"], "created": row["created"]}
