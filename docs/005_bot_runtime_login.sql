-- ============================================================================
-- 005 — NX-50: bot_runtime devine rol de LOGIN (P0-A din audit)
-- ----------------------------------------------------------------------------
-- STATUS: de aplicat o singură dată, ca ADMIN (postgres/service_role).
--   Rulează: BOT_RUNTIME_PASSWORD='<din vault>' python scripts/apply_005.py
--   (sau manual în SQL editor — vezi mai jos, înlocuind parola).
-- ----------------------------------------------------------------------------
-- CONTEXT: până acum workerul se conecta prin pooler ca `postgres` și făcea
-- `SET ROLE bot_runtime` per conexiune. Sub multiplexarea poolerului
-- (transaction-mode) acel `SET ROLE` se poate scurge la alt client → query pe
-- tenantul greșit / drum superuser care ocolește RLS. NX-50: workerul se
-- loghează DIRECT ca `bot_runtime` (conexiune directă, port 5432) → identitatea
-- e fixată de credențiale, nu de o comandă de sesiune.
--
-- Acest fișier face DOAR `ALTER ROLE bot_runtime LOGIN PASSWORD`. Toate GRANT-urile
-- și politicile RLS există deja (003_bot_runtime_role.sql + 004_inbound_dedupe.sql)
-- → NICIUN obiect nou, nicio politică nouă.
-- ============================================================================

-- Rolul există deja (003) ca `nologin`. Îl facem LOGIN + parolă.
-- Parola NU se comite în repo: o injectează apply_005.py din BOT_RUNTIME_PASSWORD,
-- sau o pui manual aici DOAR într-o sesiune locală (nu salvezi fișierul cu ea).
do $$
begin
  if not exists (select from pg_roles where rolname = 'bot_runtime') then
    raise exception 'bot_runtime lipsește — rulează întâi 003_bot_runtime_role.sql';
  end if;
end $$;

-- apply_005.py înlocuiește :'bot_password' prin parametru (zero injection).
-- Manual: înlocuiește :'bot_password' cu 'parola-ta' între apostrofuri.
alter role bot_runtime login password :'bot_password';

-- `bot_runtime` nu primește bypassrls (rămâne plasa RLS) și nu e superuser.
-- GRANT-urile de membru (bot_runtime → postgres din 003) rămân valide pentru
-- modul compat (dev fără DATABASE_URL_BOT): postgres + SET ROLE în init.

-- ============================================================================
-- VERIFICARE POST-APLICARE (ca admin):
--   select rolcanlogin, rolbypassrls, rolsuper from pg_roles where rolname='bot_runtime';
--     -- aștept: rolcanlogin=t, rolbypassrls=f, rolsuper=f
-- Apoi, conectat DIRECT ca bot_runtime (DATABASE_URL_BOT, port 5432):
--   select current_user;                              -- aștept: bot_runtime
--   select count(*) from products;                    -- aștept: 0 (fără app.business_id → RLS fail-closed)
--   select set_config('app.business_id', '<demo>', false);
--   select count(*) from products;                    -- aștept: 500 (doar tenantul)
-- ============================================================================
