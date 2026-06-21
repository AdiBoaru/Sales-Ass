-- ============================================================================
-- 011 — FIX: bot_runtime poate CITI intent_aliases + faqs (regresie de izolare)
-- ----------------------------------------------------------------------------
-- Descoperit la testarea pre-producție (driver de simulare, 2026-06-19): sub rolul
-- REAL de runtime (`bot_runtime` + app.business_id), pipeline-ul NU putea citi
-- `intent_aliases` și `faqs` → alias_stage, faq_stage ȘI agent_stage (care citește
-- aliasele aprobate ca hint de prompt — agent.py `_build_prompt_inputs` →
-- list_routing_aliases) cădeau cu InsufficientPrivilegeError. Efect: ORICE mesaj
-- de tip sales/order degrada la fallback-ul generic. (Producția folosește aceeași
-- cale `set role bot_runtime`, deci era afectată identic.)
--
-- Două cauze independente:
--   BUG 1 — 003 a acordat doar INSERT/UPDATE pe intent_aliases (candidates din shadow),
--           dar a UITAT SELECT. Lookup-ul aliasurilor aprobate (alias_stage + prompt
--           builder) are nevoie de SELECT.
--   BUG 2 — politicile de DASHBOARD „admin write faqs" / „admin write aliases" sunt
--           `FOR ALL TO public` și au în USING un subselect din `business_users`. Fiind
--           `ALL` + `public`, se evaluează ȘI la un SELECT al lui bot_runtime; cum
--           bot_runtime NU are SELECT pe business_users (corect — e tabel de dashboard),
--           evaluarea crapă cu „permission denied for table business_users", în loc să
--           întoarcă fals. Sunt politici pentru owner/admin → rolul corect e
--           `authenticated`, nu `public`. ALTER POLICY schimbă DOAR rolurile (USING /
--           WITH CHECK rămân neatinse) → fix fidel + reversibil.
--
-- Accesul botului rămâne garantat de `bot_runtime_tenant` (003, ALL, business_id =
-- current_business_id()) — neschimbat. Scrierea de candidates pe intent_aliases
-- (003 grant insert/update) curge tot prin bot_runtime_tenant, deci NU e afectată.
--
-- Aditiv + idempotent (grant + ALTER POLICY sunt re-rulabile). Reversibil:
--   revoke select on intent_aliases from bot_runtime;
--   alter policy "admin write faqs"   on faqs           to public;
--   alter policy "admin write aliases" on intent_aliases to public;
-- ============================================================================

-- BUG 1: lookup-ul aliasurilor aprobate are nevoie de SELECT (003 a dat doar ins/upd).
grant select on intent_aliases to bot_runtime;

-- BUG 2: re-scopează politicile de dashboard de la `public` la `authenticated`, ca să
-- NU se mai evalueze (și crape) pe SELECT-ul lui bot_runtime/anon. Owner/admin de
-- dashboard sunt autentificați → comportamentul intenționat e păstrat.
alter policy "admin write faqs"    on faqs           to authenticated;
alter policy "admin write aliases" on intent_aliases to authenticated;
