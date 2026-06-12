# Backlog compact NX — restul taskurilor din audit (spec scurt per task)
Format: context → scop → DoD → dependențe. Cardurile complete (13) sunt în fișierele separate; aici e tot ce mai trebuie ca un dezvoltator să pornească fără alte întrebări. Estimările sunt în Excel (sheet „Audit NX detalii").

## NX-03 · Alerte consumer lag + outbox depth (MVP · Z13 · 4h · dep T167)
Job la 60s: `XINFO GROUPS` pe stream-ul inbound (lag per grup) + `SELECT count(*), max(now()-created_at) FROM outbox WHERE status='pending'`. Praguri în config (lag>100 sau age>120s) → eveniment + alertă Slack. DoD: lag simulat (consumer oprit + 150 mesaje) declanșează alerta într-un minut.

## NX-11 · systemd units + healthchecks (P1-O1 · 3h · dep T156)
webhook / worker / dispatcher / scheduler ca unități systemd separate (sau servicii compose cu restart: always + healthcheck-uri distincte). DoD: `kill -9` pe worker → repornit <10s, celelalte neafectate; `systemctl status` arată verde per serviciu.

## NX-14 · Supabase regiune UE + decizie OpenAI EU (P1-O1 · 4h · dep T018)
Verifică regiunea proiectului; dacă nu e UE → plan de migrare ÎNAINTE de primul contact real în DB (după aceea e transfer de date personale). Decizie scrisă în `docs/decisions/` privind EU data residency la OpenAI (cost ~+10% pe nano) — se poate amâna, dar decizia trebuie datată. DoD: regiunea confirmată în dashboard + ADR comis.

## NX-13 · Registru procesatori + notă de informare (P1-O2 · 3h · dep T181)
Tabel în docs: OpenAI, Supabase, Meta, Cloudflare, Slack (după NX-08) — ce date văd, temei, DPA link. Actualizează politica de confidențialitate; linkul e cel din T180. DoD: document publicat la privacy_url-ul de demo, registru comis în repo.

## NX-41 · create_tenant.py idempotent (P1-O1 · 6h · dep T044)
Script CLI: business + channel + channel_identity + plafoane + FAQ seed + orders_webhook_secret (NX-52) + validare privacy_url (T180). Rerulabil: ON CONFLICT peste tot, output „created/exists" per resursă. DoD: 2 rulări consecutive = stare identică; devine API-ul de onboarding la faza B.

## NX-43 · Whitelist chei contacts.profile (P1-O2 · 4h · dep T130)
Per vertical (beauty/hvac/auto/salon) o listă de chei permise în profil (skin_type, vehicle_model...). Extractorul aruncă cheile necunoscute + emite `profile_key_dropped`. DoD: cheie inventată de model nu intră în DB; lista e în taxonomie, nu hardcodată.

## NX-42 · pip-audit + renovate + digest pin (P1-O3 · 4h · dep T005)
pip-audit în CI (fail pe severity high), config renovate pe requirements + actions, imaginile prod referite prin digest. DoD: CVE high cunoscut într-un pachet de test pică CI-ul.

## NX-15 · Moderation gate inbound (P1-O1 · 3h · dep T101)
Apel la endpointul de moderation (gratuit la OpenAI) în gates, înainte de triaj. Flagged → răspuns neutru template + `message_moderated`, conversația nu ajunge la agent; după 3 flag-uri în 24h → abuse blocklist (m19 din diagramă). DoD: mesaj flagged nu generează niciun apel de agent.

## NX-17 · schema_version în payload-urile din stream (P1-O2 · 3h · dep T049)
Câmp `v: 1` în payload inbound/outbound; consumerii acceptă N și N-1 (chei noi opționale, niciodată redenumiri). DoD: deploy cu v2 procesează mesaje v1 rămase în coadă, test cu ambele formate.

## NX-07 · Pacing proactiv + quiet hours (P1-O2 · 6h · dep T201 + NX-05)
Cap per contact (max 2 proactive/săpt), cap per business/oră (spread, nu burst), quiet hours 21:00–09:00 pe `businesses.timezone`. Mesajele amânate primesc `next_attempt_at`, nu se pierd. DoD: jobul rulat la 23:00 nu trimite nimic; coada se golește dimineața în ritmul setat.

## NX-54 · Tarife Meta per piață în config (P1-O2 · 4h · dep T196)
`config/meta_rates.yaml`: per țară+categorie (marketing/utility/auth), cu date de valabilitate — RO și HU au rate card propriu de la 1 iul 2026. Cost guard și raportul lunar citesc de aici. DoD: schimbarea unui tarif = doar editare YAML + test care prinde tarif expirat.

## NX-33 · Funnel + cohorte în Metabase (P1-O3 · 8h · dep T195)
Funnel: conversații → intent vânzare → recomandare → checkout link → comandă atribuită; cohortă de revenire pe lună. Definiția „assisted revenue" scrisă în dashboard (textbox) — aceeași formulare intră în contracte. DoD: ambele vizualizări pe datele demo.

## NX-06 · CTWA referral → atribuire ads (P2 · 6h · dep T062)
Parse `referral` din webhook (source_id, ad_id, headline) → `analytics_events('ctwa_entry')` + marchează fereastra gratuită de 72h în window tracker. Raport per campanie în Metabase. DoD: payload CTWA simulat apare în raport cu ad_id corect.

## NX-09 · Retry failed re-engagement (P2 · 4h · dep T099)
Doar pentru mesaje PROACTIVE failed cu eroare de fereastră: după 24h, o singură reîncercare pe template aprobat. DoD: failed simulat → exact un retry, marcat `reengagement_retry`.

## NX-30 · Promotions: tabel + tool + regulă validator (P2 · 20h · dep T094)
`promotions(business_id, title, discount_type, value, scope jsonb, starts_at, ends_at, active)`; tool read-only `get_promotions`; regula de validator: orice procent/sumă de reducere din răspuns trebuie să existe într-o promoție activă, altfel reformulare fără cifre. DoD: golden „ce reduceri aveți?" răspunde doar din tabel; discount inventat = blocat.

## NX-32 · Cross-sell map + free-shipping gap (P2 · 16h · dep T130)
Mapare complementaritate per categorie în taxonomie (`goes_with`); context builderul adaugă semnalul „mai are nevoie de X lei până la transport gratuit" (prag per business). DoD: golden de upsell recomandă complementar corect; semnalul apare doar sub prag.

## NX-31 · Export CRM: webhook + CSV zilnic (P2 · 12h · dep T120 + NX-52)
Eveniment `lead.qualified` POST-at către URL-ul clientului, semnat HMAC cu același mecanism ca NX-52 (secret separat, outbound de data asta) + CSV zilnic per tenant în storage privat. DoD: lead calificat ajunge la endpointul de test cu semnătură validă; fișierul zilnic există și e descărcabil.

## Epicul E26 · Web Widget (V1.5 — după clientul 1 stabil pe WhatsApp)
Ordinea W1→W6; zona 1b din diagrama v4. Estimări în Excel.
- **NX-20 · Gateway SSE (W1):** POST /web/messages + GET /web/stream; sesiune semnată cu token public per tenant; rate limit IP+visitor; reconectare Last-Event-ID. SSE, nu WebSocket: trece prin orice proxy/CDN. DoD: 200 sesiuni simultane stabile 10 min pe staging.
- **NX-21 · widget.js (W2):** shadow DOM (zero conflict CSS), embed 1 linie cu data-token, temă din settings, i18n RO/HU/EN, disclosure AI vizibil permanent (art. 50). DoD: funcționează pe un site terț de test.
- **NX-22 · Canal web în gates/sender (W3):** `channel_kind='web'`: fără fereastră 24h, debounce 800ms, typing = eveniment SSE; senderul scrie în stream-ul sesiunii. DoD: golden echo pe web verde.
- **NX-25 · CORS/CSP + allowlist (W3):** allowlist domenii per tenant; token public ≠ secret (doar identifică tenantul, rate-limitat agresiv). DoD: embed de pe domeniu neautorizat respins cu 403.
- **NX-23 · Identitate vizitator + merge (W4):** visitor_id semnat în cookie first-party → channel_identities; la capturarea telefonului → merge cu identitatea WhatsApp, logat în audit_log, reversibil. DoD: istoricul unificat, zero contacte duplicate pe demo.
- **NX-24 · Context pagină (W5):** page_url/product_id din widget → hint de retrieval prioritar în context builder. DoD: „cât costă asta?" pe pagina unui produs răspunde despre ACEL produs.
- **NX-26 · Golden web + load SSE (W6):** suita golden rulată pe canalul web + load 200 sesiuni/10 min; p95 și mesaje pierdute măsurate. DoD: p95 < țintă, 0 pierdute.
