-- 009_gdpr_svc_role.sql (NX-72) — rol gdpr_svc + grant EXECUTE pe gdpr_erase_contact.
--
-- Aditiv, idempotent, re-rulabil. NU modifică funcția existentă (schema_v2 o are deja
-- ca security definer). Aliniere cu modelul din CLAUDE.md: gdpr_svc e rolul autorizat
-- să execute erase-ul. În cod, până la provisioning-ul unui login separat, stratul GDPR
-- rulează pe `admin_conn` (control plane) — vezi src/gdpr/erase.py.

do $$ begin
  if not exists (select from pg_roles where rolname = 'gdpr_svc') then
    create role gdpr_svc nologin;
  end if;
end $$;

-- compat pooler Supabase: postgres trebuie să fie membru ca să poată „purta" rolul
grant gdpr_svc to postgres;

grant execute on function gdpr_erase_contact(uuid) to gdpr_svc;
