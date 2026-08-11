# Conexiuni DB — cine se conectează cu ce rol, pe ce port, de ce

_Referință NX-50 (P0-A din audit). Sursa de adevăr pentru codul de conectare:
[`src/db/connection.py`](../src/db/connection.py)._

## De ce două pool-uri

Workerul face două tipuri de query-uri, fundamental diferite:

| | Tenant path | Control plane |
|---|---|---|
| **Exemple** | contacts, conversations, messages, outbox, products | `resolve_channel` (canal→business), due-tenants, joburi admin |
| **Știe `business_id`?** | DA (l-a rezolvat deja) | NU — exact ce caută (precede tenantul) |
| **RLS** | trebuie activ (izolare) | trebuie ocolit (cross-tenant prin definiție) |
| **Volum** | ~tot traficul | o dată/mesaj la intrare |

Nu pot împărți aceeași conexiune: una are nevoie de RLS strict, cealaltă de
cross-tenant. → două pool-uri, două roluri.

## Pool-urile

### `bot_pool` — TENANT PATH
- **Rol:** `bot_runtime` — **LOGIN**, parolă proprie, **fără `bypassrls`**, non-superuser.
- **Conexiune:** DIRECTĂ, port **5432** (sesiune), `DATABASE_URL_BOT`.
- **Acces:** `tenant_conn(business_id)`. La checkout setează `app.business_id`;
  la release îl golește (echivalent `RESET`). **Niciun `SET ROLE`** — identitatea
  vine din credențialele de login.
- **De ce:** auditul (P0-A) — vechiul model intra ca `postgres` + `SET ROLE
  bot_runtime` per conexiune. Sub poolerul Supabase (transaction-mode) acel
  `SET ROLE`/`SET app.business_id` se poate scurge la alt client → query pe
  tenantul greșit, „date dispărute" (GUC gol → RLS fail-closed), sau drum
  superuser (bypass RLS total). Cu login direct, identitatea e fixă → scurgerea
  dispare structural.
- **Plasă la boot:** `init=_assert_bot_role` verifică `current_user='bot_runtime'`
  la fiecare conexiune nouă. DSN greșit (rol privilegiat) → pool-ul NU pornește,
  eroare explicită la boot, nu la primul mesaj. `consumer`/`dispatcher` creează
  `bot_pool` eager în `_main` exact pentru asta.
- **Plasă la checkout (NX-04):** `tenant_conn` verifică rol + `app.business_id`
  ÎNAINTE de a da conexiunea apelantului — set + verificare într-un singur
  round-trip (`set_config(...) as biz, current_user as usr`), zero latență în
  plus. Orice abatere → `IsolationError` (fail-fast, log `critical`), conexiunea
  nu ajunge la query. Flag `DB_ISOLATION_ASSERT=off` o sare (cu WARNING la boot).

### `admin_pool` — CONTROL PLANE + JOBURI
- **Rol:** privilegiat (`postgres`/`service_role`), `SUPABASE_DB_URL`.
- **Acces:** `admin_conn(pool)`. Folosit DOAR pentru:
  - `resolve_channel` (`provider_account_id → business_id`) — singura excepție
    documentată de la „business_id pe tot": îl DERIVĂM aici.
  - due-tenants în dispatcher (`business_ids_with_due_outbox`, cross-tenant).
  - joburi admin: `cleanup_dedupe`, `embed_products` (scrie catalog), migrări.
- **Regulă:** NU citi/scrie date de client pe `admin_conn`. Orice tenant-scoped
  → `tenant_conn`. Suprafața e limitată intenționat la maparea canal→business +
  mentenanță non-PII.

## Cine deține conexiunea: OPERAȚIA, nu turul (NX-231)

Până la NX-231, `handle_turn` primea o conexiune și o ținea de la primul query până la ultimul —
inclusiv peste triaj, agent, tool-uri și embed-uri. Conexiunea aparținea TURULUI. Efectul:
`bot_pool.max_size` devenea, accidental, plafonul de concurență al întregului sistem, iar ~79% din
timpul cât o conexiune era ocupată nu se executa niciun query (măsurat: `scripts/sim/pool_probe.py`,
docs/CONN-HOLD-ANALYSIS-2026.md).

Regula de azi:

```python
async with deps.db("nume_operație") as conn:   # checkout SCURT, etichetat
    rows = await conn.fetch(...)               # doar muncă de DB
# conexiunea e deja înapoi în pool
emb = await deps.llm.embed([q])                # apelul extern rulează FĂRĂ conexiune
async with deps.db("write_back") as conn:      # alt checkout scurt
    await conn.execute(...)
```

- **Provider, nu conexiune.** Stagiile și tool-urile primesc `deps.db` (`src/db/provider.py`),
  tenant-scoped la construcție. `business_id` NU e argument al lui `db()` → un tool controlat de
  model nu-l poate schimba (P7: server-owned).
- **Unit of Work per tur** (`src/worker/turn_uow.py`): `load` (un checkout) → `compute` (zero
  conexiune) → `commit` (un checkout + O tranzacție) → `aftercare` (checkout-uri proprii). Faza de
  commit e atomică: mesaje outbound + outbox + `state_version` + `mark_inbound_completed`.
- **Query-uri atomice = o metodă cu tranzacție internă** (`db_tx(deps.db, "op")`), nu o conexiune
  ținută între apeluri de tool.
- **Interzis:** orice await extern (LLM/embed/moderation/media/HTTP de provider/backoff/SSE/
  așteptare de coadă) în interiorul unui checkout. Guard mecanic în CI:
  `python scripts/check_no_raw_conn.py` (3 reguli, excepțiile cer motiv scris în
  `scripts/conn_allowlist.json`).
- **Frâna e acum explicită.** Poolul nu mai limitează concurența, deci limitează `admission`
  (`src/worker/admission.py`): lease-uri Redis cu plafon global + per-tenant, comune tuturor
  replicilor. Aceeași poartă pe worker și pe ruta web sincronă.
- **Observabilitate:** `db_ops` per tur (`db_checkout_ms`/`db_hold_ms` defalcate pe operație) +
  `pool_metrics` (`db_pool_wait_ms`, `db_pool_active`). `idle_held` cere modul diagnostic
  `DB_QUERY_TIMING_ENABLED=1` — fără el raportăm „nu știm", nu 0.

## Mod compat (dev/test înainte de provisioning)

Dacă `DATABASE_URL_BOT` lipsește, `bot_pool` cade pe `SUPABASE_DB_URL` și coboară
rolul cu `SET ROLE bot_runtime` o singură dată **în `init`** (la crearea
conexiunii, nu per-checkout). Funcționează pentru iterare locală, dar **NU e
sigur în prod**: pe un pooler transaction-mode `SET ROLE`-ul din init se poate
pierde. În producție setezi întotdeauna `DATABASE_URL_BOT` (login direct, 5432).

## Provisioning (o dată, manual)

1. `BOT_RUNTIME_PASSWORD='<din vault>' python scripts/apply_005.py`
   (face `ALTER ROLE bot_runtime LOGIN PASSWORD` + verifică login/bypassrls/super).
   GRANT-urile + politicile RLS există deja din 003 + 004 — niciun obiect nou.
2. Pune `DATABASE_URL_BOT=postgresql://bot_runtime:...@db.<proj>.supabase.co:5432/postgres`
   în `.env` (worker). Lasă `SUPABASE_DB_URL` (postgres) pentru admin + migrări.

## Rezumat

| Pool | Rol | Port | DSN | RLS | Folosit de |
|---|---|---|---|---|---|
| `bot_pool` | `bot_runtime` (login) | 5432 direct | `DATABASE_URL_BOT` | activ (plasă) | `tenant_conn` (tot pipeline-ul) |
| `admin_pool` | privilegiat | 5432/6543 | `SUPABASE_DB_URL` | ocolit | `admin_conn` (resolve_channel, joburi) |
