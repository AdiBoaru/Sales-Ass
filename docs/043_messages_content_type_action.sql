-- ============================================================================
-- 043 — `messages.content_type` acceptă 'action' (fix NX-236/237)
--
-- DEFECT (găsit de gate-ul E2E NX-247, pe Postgres real):
--   `src/web/app.py` persistă mesajul inbound al unui tur pornit dintr-un buton cu
--   `content_type = 'action'` (accept_web_turn + persist_inbound). CHECK-ul din
--   schema_v2 permitea doar vocabularul de canal Meta:
--     ('text','image','audio','video','document','interactive','template','location','sticker')
--   Consecință cu `WEB_ACTIONS_ENABLED=true`: INSERT-ul rupe TRANZACȚIA DE ACCEPT, deci
--   ORICE click pe un buton opac crapă cu CheckViolationError. Nu degradare — eșec total.
--   Invizibil până acum: flagul e OFF în producție, iar suitele existente foloseau
--   monkeypatch în loc de DB real.
--
-- DE CE se extinde vocabularul și nu se schimbă valoarea scrisă:
--   Un click pe o acțiune NU e text — `body` e gol deliberat (NX-236: eticheta rămâne
--   display-only, inputul e comanda typed). A-l înregistra ca 'text' ar fi o minciună în
--   ledger. 'interactive' e termenul Meta pentru mesaje cu butoane trimise de PROVIDER —
--   împrumutat aici, ar amesteca un concept de canal cu unul web-only și ar face
--   analytics-ul incapabil să distingă „a scris" de „a apăsat". Vocabularul e cel care a
--   rămas în urmă când NX-236 a introdus un tip nou de input, nu codul.
--   Nimic din `src/` nu ramifică pe `content_type == 'action'` (verificat) — valoarea e
--   purtată și stocată, deci extinderea e strict aditivă în comportament.
--
-- IMPACT OPERAȚIONAL:
--   `messages` e PARTIȚIONAT pe lună. DROP CONSTRAINT pe părinte îl scoate și din partiții;
--   ADD CONSTRAINT recursează și VALIDEAZĂ fiecare partiție (seq scan). Lock ACCESS
--   EXCLUSIVE pe părinte + partiții cât ține validarea. Noul set e un SUPERSET al celui
--   vechi, deci niciun rând existent nu poate încălca noua constrângere — validarea nu
--   poate eșua, doar durează. La volumul actual (partiții lunare mici) e sub o secundă;
--   pe un volum mare, rulează în fereastră de mentenanță.
--
-- ROLLBACK (revine la vocabularul vechi — DOAR dacă nu s-au scris rânduri 'action',
-- altfel ADD CONSTRAINT eșuează, corect, pe rândurile existente):
--   alter table messages drop constraint messages_content_type_check;
--   alter table messages add constraint messages_content_type_check
--     check (content_type in ('text','image','audio','video','document',
--                             'interactive','template','location','sticker'));
--
-- IDEMPOTENT: `drop constraint if exists` + `add` cu același nume; re-rularea dă aceeași stare.
-- ============================================================================

alter table messages drop constraint if exists messages_content_type_check;

alter table messages add constraint messages_content_type_check
  check (content_type in ('text','image','audio','video','document',
                          'interactive','template','location','sticker',
                          -- NX-236: tur pornit dintr-un token de acțiune opac (web widget).
                          -- `body` e gol; înțelesul stă în `payload->'action'`.
                          'action'));
