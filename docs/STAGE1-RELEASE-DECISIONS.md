# NX-249 — deciziile de design ale controllerului de release

Documentul explică **de ce** arată așa controllerul. Runbookul operațional e
[`STAGE1-CANARY-RUNBOOK.md`](STAGE1-CANARY-RUNBOOK.md); închiderea rutei v1 e
[`STAGE1-CUTOVER.md`](STAGE1-CUTOVER.md); ritualul de calitate e
[`STAGE1-QUALITY-RITUAL.md`](STAGE1-QUALITY-RITUAL.md).

---

## 1. Inventarul care a declanșat clauza STOP din card

Cardul cere migrare NOUĂ doar dacă „inventarul dovedește că assignmentul nu poate fi durabil și
neambiguu". Inventarul, făcut pe `origin/main@520c150`:

| Cine decide pipeline-ul | Persistat? | La retry/reclaim | Rollback adapter |
|---|---|---|---|
| `WEB_TURN_V2_ENABLED` | ❌ env per proces, citit per request (`src/web/app.py`) | se recitește | rutele v1/v2 sunt separate |
| `SINGLE_BRAIN_ENABLED` | ❌ citit ÎN TIMPUL turului (`src/worker/stages/agent.py:357`, `runner.py:124`) | un reclaim după deploy rulează **alt creier** pe același turn | niciunul |
| `WEB_VIEW_V2_PROJECTOR_ENABLED` | ❌ citit la proiecție (`turn_events.py:524`) | se recitește la fiecare GET | payload-ul v1 rămâne |
| provider retrieval (NX-238) | ❌ `select_provider` per tur | stabil prin hash, dar necapturat | `current_live` |
| `RELEASE_TRACK` (NX-246) | ❌ env static, citit de executor | constantă de proces | — |
| `pipeline_version` | ✅ pe `web_turns`, la accept | păstrat | proiecția NX-233 |
| `deadline_at` | ✅ la accept, neprelungit | păstrat | — |

Concluzia: singurul lucru capturat durabil e `pipeline_version`, iar el înseamnă **contractul
răspunsului** (`web-chat.v1`), nu releaseul — proiecția NX-233 ramifică pe valoarea lui. Tot ce
decide CARE pipeline rulează e un env global citit la execuție.

Două consecințe măsurabile, nu teoretice:

1. Rândul „retry/reclaim după deploy → același pipeline" din failure matrix era **încălcat prin
   construcție**. Nu din neatenție: nu exista niciun loc unde s-ar fi putut respecta.
2. `src/observability/slo.py::_missing_capabilities` declara deja `per_row_release_sha` drept
   capabilitate LIPSĂ. Raportul candidate-vs-control cerut de card (cohorturi comparabile pe
   aceeași fereastră) nu se putea calcula din ledger.

Alternativa fără DDL — reconstrucția din `conversations.created_at` + istoricul de policy — a fost
respinsă fiindcă depinde de ceas și cade exact pe cazul care contează (un epoch aplicat între
crearea conversației și tur). **Un turn trebuie să știe singur ce a rulat.**

Migrarea aprobată: [`044_release_policy_and_turn_capture.sql`](044_release_policy_and_turn_capture.sql),
expand-only (coloane nullable + tabel nou, zero backfill) — imaginea precedentă rulează neschimbată
peste ea.

---

## 2. De ce etapa e DECLARATĂ, nu dedusă din (mod, procent)

Prima variantă deducea etapa din cifre. Un test a arătat că **nu se poate**: etapa 2 („demo",
tenant demo la 100%) și etapa 6 („default", toți eligibilii la 100%) au exact aceleași cifre și
diferă prin allowlist. Deducerea o nimerea pe prima și cerea 24h/100 de ture acolo unde cardul cere
14 zile/2.000 — adică poarta ar fi fost cea mai slabă exact la ultima etapă dinaintea cutoverului.

`ReleasePolicy.stage` e acum un câmp validat contra tabelului `STAGES`: modul trebuie să
corespundă, iar la `canary` și procentul. Un 7% „care pare sigur" e respins la construcție, nu
raportat ca `UNKNOWN` mai târziu. Bonus: cine aprobă policy-ul vede în el ce etapă aprobă.

Excepția: `force_control` **nu e o etapă**, e întreruperea uneia. Păstrează `stage`-ul din care a
oprit, fiindcă exact asta trebuie să citească cineva în istoric — nu „eram la etapa force_control",
ci „am oprit etapa 4".

---

## 3. De ce bucketul e HMAC, nu sha256 public

NX-238 folosește sha256 simplu pentru bucketul de retrieval. Aici e HMAC cu salt secret, pentru că
miza diferă: la NX-238 bucketul alege un provider de căutare; aici alege ce **versiune de produs**
primește un client. Cu hash public, oricine poate calcula bucketul oricărei conversații — și, mai
important, poate încerca `conversation_id`-uri până nimerește unul în canary.

Saltul nu intră niciodată în policy, în DB sau în loguri: policy-ul poartă doar `stable_salt_id`.
Un policy exfiltrat din DB nu permite nimănui să prezică bucketuri. `stable_salt_id` intră totuși
în mesajul HMAC, ca o rotire de salt să fie o reasignare **explicabilă**, nu o distribuție care
„s-a mutat".

În producție, `RELEASE_ASSIGNMENT_SALT` gol oprește procesul la boot (`src/config.py`); în dev e
permis, fiindcă face testele reproductibile fără secrete.

---

## 4. De ce sticky-ul trăiește în ledger

Cardul interzice explicit „sticky doar în Redis/cookie/localStorage". Motivele, pe rând:

- **cookie/localStorage** — frontendul e pasiv și nu are voie să știe de canary (boundary NX-244);
  un `useV2` persistat în browser ar fi exact „business logic în frontend";
- **Redis** — best-effort prin contract (bypass la timeout/FLUSHALL). Un FLUSHALL ar reasigna
  conversații în mijlocul dialogului;
- **ledger** — fiecare turn poartă track-ul cu care a rulat, deci asignarea unei conversații se
  re-derivă din propriul ei istoric (`latest_capture`). Un Redis pierdut nu pierde asignarea.

Consecință de design: `resolve()` primește `prior` ca ARGUMENT. Stabilitatea e o proprietate a
algoritmului, nu o convenție pe care trebuie s-o respecte apelanții.

---

## 5. De ce `drain` e o a treia ieșire, nu „control"

La `force_control`, o conversație deja servită de candidate NU se convertește tăcut la control.
Starea, referințele ordinale („al doilea") și acțiunile ei au fost produse de candidate; mutarea
i-ar schimba înțelesul sub picioarele clientului.

Conversia e permisă doar dacă policy-ul declară `rollback_compatible=True` — adică s-a DOVEDIT
(drill NX-248) că imaginea precedentă citește ce a scris candidate. Altfel conversația se
drenează: turele active termină normal, iar acceptul următor primește `503 release_draining` cu un
error-view localizat.

`Assignment.track` e `None` la drain, deliberat: un turn refuzat nu aparține niciunui cohort, iar
a-l eticheta `champion` ar polua raportul cu ture care n-au rulat.

**503, nu 409** — și da, intră în denominatorul de availability. În timpul unui kill-switch,
disponibilitatea CHIAR scade pentru conversațiile drenate; un SLI care ascunde asta e decorativ.

---

## 6. De ce policy-ul stă în DB și nu într-un artefact semnat

NX-238 pune verdictul într-un `decision.json` semnat, din imagine. Aici nu se poate: ținta
operațională e „≤5 minute de la decizie la zero accepturi candidate noi", iar un artefact din
imagine se schimbă doar prin deploy — care la NX-248 e o promovare umană pe GitHub Environments.
Kill-switchul ar fi mai lent decât incidentul.

Deci: tabel `release_policies`, append-only, cu CAS în SCHEMĂ (`unique (environment, revision)`).
Compare-and-set nu e o secvență citește-apoi-scrie — între cele două ar exista o fereastră în care
doi operatori aplică policy-uri diferite și ultimul câștigă tăcut. Aici al doilea PIERDE explicit.
Cazul care contează: primul tocmai a apăsat kill-switchul.

**Control plane, nu tenant.** Rândul poartă allowlistul de tenanți, deci nu are ce căuta pe o
conexiune tenant-scoped: un tenant nu trebuie să afle cine altcineva e în canary. Migrarea nu dă
grant lui `bot_runtime` — nu e o convenție, e o imposibilitate. Se citește pe `admin_conn`, a doua
excepție documentată de control plane după `provider_account_id → business_id`, cu cache bounded
(TTL din config, deci nu e un query per accept).

---

## 7. De ce `PASS` nu promovează

`src/release/gates.py` nu are acces la storeul de policy. Nu „nu ar trebui să promoveze" — **nu
poate**. Promovarea cere:

1. un evidence packet cu verdict `PASS` și amprentă care se **recalculează** din conținut
   (cine editează „FAIL" în „PASS" îi rupe amprenta);
2. `--expected-revision` (CAS);
3. `--actor` + `--reason`;
4. `--confirm` (fără el, `apply` e dry-run).

Oprirea, în schimb, NU cere evidence packet. A cere un raport ca să oprești traficul e exact invers
față de siguranță.

---

## 8. Ce NU poate face controllerul, spus pe față

- **Nu poate compara cost/tur pe cohort.** `web_turns` n-are coloană de cost (el trăiește pe
  `messages`/`usage_daily`). Raportul îl declară LIPSĂ (`cost_per_turn_by_track`), nu îl
  aproximează — un buget verificat pe o aproximare e un buget neverificat.
- **Nu poate defalca latența pe `turn_class`.** NX-241 ține clasa doar în runtime; ledgerul n-o
  are. Rămâne o capabilitate lipsă declarată, ca la NX-246.
- **Nu poate dovedi compatibilitatea imaginii precedente.** Aia cere două imagini și e treaba
  `scripts/release/migration_drill.py` + `smoke_web_v2.py` (NX-248). `rollback_drill.py` o
  declară la `not_verified_here`.
- **Nu poate măsura exact propagarea kill-switchului.** Raportează o limită SUPERIOARĂ
  (`apply_s + RELEASE_POLICY_REFRESH_S`), fiindcă o măsurătoare reală ar cere să întrebăm fiecare
  proces. Un drill care pretinde mai multă precizie decât are e mai rău decât unul modest.

---

## 9. Starea de azi (2026-08-18): controllerul e construit, releaseul e BLOCAT

Controllerul e complet și testat, dar **niciun trafic nu se mișcă**, fiindcă porțile upstream nu
sunt verzi:

| Poartă | Sursă | Stare |
|---|---|---|
| `deploy_evidence` | NX-248 `reports/nx248/evidence.json` | `NOT_READY` — 7/10 elemente critice cer CI + staging |
| `e2e_stage1` | NX-247 | `NO-GO` — lipsesc PR B/browser + ratificarea pragurilor |
| `quality_holdout` | NX-246 felia 3 | `NOT-READY` — 10/60 dev, holdout nesigilat |
| retrieval candidate | NX-238 | `NOT-READY` — H3 0/50 sigilate, qrels 18/100 familii |

`RELEASE_CONTROLLER_ENABLED=false` (default) ⇒ zero policy citit, zero captură scrisă, comportament
byte-identic. Deblocarea e a NX-203 (corpus) + NX-202 (H3) + CI/staging, nu a codului de aici.
