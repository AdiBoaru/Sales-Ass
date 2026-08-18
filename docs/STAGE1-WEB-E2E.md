# STAGE 1 — Gate E2E cross-repo pentru WebWidget v2 (NX-247)

**Status:** PR A (backend) implementat · PR B (frontend, Playwright) NU e început
**Card:** [`tasks/stage1/NX-247.md`](../tasks/stage1/NX-247.md)
**Backend certificat pe:** profilul de flag-uri `v2_transport` (vezi §2)
**Verdict de gate azi:** **NO-GO pentru NX-249** — nu din cauza unui eșec, ci din cauza a două
lucruri care încă nu există: jumătatea de browser (PR B) și decizia de retrieval NX-238. Vezi §9.

---

## 1. Ce este și ce NU este

Este un harness care pornește **aplicația de producție** (`src.webhook.app:app` — middleware-ul de
body cap, lifespan-ul de observabilitate, montarea condiționată a routerului web, toate reale) cu
**Postgres și Redis reale** și cu **modelul/embedderul FALSE**, apoi conduce ture prin marginea v2
reală: accept durabil → executor cu lease/fencing → proiecție `web-view.v2`.

Nu este o suită happy-path. Miezul lui e matricea R1–R22: dublu click, două taburi, răspuns 202
pierdut, refresh în `working`, worker omorât după claim, worker vechi întors după lease, Redis mort,
eroare de DB la commit, sesiune expirată, rate limit, corpuri respinse, timeout de model, acțiuni
tamperate sau din alt tenant, retry de comerț.

Nu este, deliberat:

- **un al doilea stack.** Dacă harnessul ar reasambla routerele, ar putea trece cu o aplicație care
  nu există în producție. De aceea `build_stage1_app` REFUZĂ să pornească dacă `/web/v2/turns` nu e
  montat pe aplicația reală.
- **un flag de producție.** Nu există `FAKE_LLM=true`. Injecția e o rescriere de atribut de modul,
  făcută din `tests/e2e/stage1_app.py`, după ce poarta a trecut. Modulul nu există în imaginea de
  producție (`Dockerfile` copiază `src/`, `scripts/migrate.py`, `docs/*.sql`) — verificat mecanic de
  `test_production_image_does_not_ship_the_harness`.
- **un gate care inventează praguri.** Latența se RAPORTEAZĂ. Vezi §7.

---

## 2. Profilul de flag-uri certificat

Un singur loc declară pe ce configurație s-a măsurat: `tests/e2e/stage1_app.py::FLAG_PROFILES`.

| Profil | Ce aprinde | Stare |
|---|---|---|
| `v2_transport` (**certificat**) | ledger, executor, recovery, SSE, sesiuni v2 + legare de origin, context de pagină, acțiuni opace, coș, feedback, observabilitate, deadline | rulează în gate |
| `v2_single_brain` | tot ce e mai sus + `SINGLE_BRAIN_ENABLED` + `WEB_VIEW_V2_PROJECTOR_ENABLED` | rulabil, **necertificabil** |

De ce nu creierul unic: promovarea `search_entities` are verdict `NOT-READY`
([`NX-238-DECISION.md`](NX-238-DECISION.md)), iar `WEB_VIEW_V2_PROJECTOR_ENABLED` îl cere pe
`SINGLE_BRAIN_ENABLED`. NX-247 nu are voie să emită un GO pe care gate-ul de retrieval nu l-a dat —
ar fi exact „schimbare mare pe speranță" (D15). Profilul există ca gate-ul să fie pregătit în
momentul în care NX-203/NX-202 deblochează decizia, nu ca să pretindă că s-a măsurat.

`ADMISSION_ENABLED=false` în ambele profiluri: frâna de concurență e testată de NX-231, iar aici ar
introduce doar nedeterminism în programarea execuției.

---

## 3. Artefactele canonice (consumate de AMBELE repo-uri)

```
qa-suite/stage1/web-v2/
├── manifest.json          # hash-urile pachetului de contract. GENERAT, nu editat manual.
├── scenarios.json         # ID-uri de scenariu + invarianți + matricea R1–R22
└── gate-thresholds.json   # UN singur artefact de praguri, validat în ambele repo-uri
```

`manifest.json` se regenerează cu:

```powershell
python scripts/stage1_contract_manifest.py --write
python scripts/stage1_contract_manifest.py --check   # cod ≠0 la drift
```

Trei decizii de design merită explicate:

1. **Zero timestamp.** `generated_at` nu există. Un câmp de ceas ar face ca două generări ale
   aceluiași tree să difere, iar atunci „driftul rupe CI" n-ar mai putea distinge o schimbare de
   contract de trecerea timpului — și oamenii ar învăța să ignore semnalul.
2. **`backend_commit` e `null` în manifest.** Un fișier nu poate conține hash-ul commitului care îl
   conține. SHA-ul trăiește în CERTIFICATUL de rulare (`--certificate`), care nu se comite.
3. **Hash-uri pe bytes normalizați (CRLF→LF).** Același precedent ca `scripts/migrate.py`: altfel
   același fișier dă alt sha256 pe Windows (autocrlf) și pe Linux (CI), iar cross-repo-ul ar raporta
   drift fals. Rezultatul e egal cu sha256 al blobului git — exact ce hash-uiește frontendul.

`schema_sha256` NU e hash-ul unui fișier, ci `contracts_v2.schema_hash()` — moneda negocierii de
capability. Un test verifică că bytes-ii canonici pe care îi copiază frontendul hash-uiesc la exact
valoarea negociată; dacă cele două ar divergea, negocierea ar promite un contract și livra altul.

---

## 4. Rulare locală, copy/paste

### 4.1 Stackul efemer

```powershell
docker compose -f docker-compose.stage1-e2e.yml up -d postgres redis
docker compose -f docker-compose.stage1-e2e.yml run --rm bootstrap
```

`bootstrap` face patru lucruri pe care Supabase le are din oficiu și un Postgres simplu nu:

1. stubul `auth.uid()` — politicile RLS din `schema_v2_production.sql` îl REFERĂ, iar Postgres
   validează funcția la crearea politicii;
2. rolurile `anon` / `authenticated` / `service_role` (NOLOGIN) — migrările 003/009/011/039 le dau
   GRANT-uri;
3. schema de bază;
4. migrările **003** și **005**, prin `psql`. 005 folosește variabila psql `:'bot_password'` (parola
   nu se comite — pe DB-ul real a fost aplicată cu `apply_005.py`), deci `conn.execute` din runner ar
   da syntax error. 003 vine cu ea fiindcă 005 are nevoie de rolul pe care 003 îl creează.

### 4.2 Migrările — numai prin runnerul canonic

```powershell
$env:SUPABASE_DB_URL = "postgresql://postgres:stage1@127.0.0.1:55432/stage1"
$env:REDIS_URL = "redis://127.0.0.1:56379/0"
$env:ENV = "test"
python scripts/migrate.py --mark-applied 003,005
python scripts/migrate.py
python scripts/migrate.py --check
```

`--mark-applied` e adăugat de NX-247 și există exact pentru clasa de migrări psql-only: marchează
versiuni PUNCTUALE ca aplicate, cu checksumul REAL de pe disc — deci `--check` continuă să detecteze
o editare ulterioară a fișierului. Diferența față de `--baseline`: acela marchează TOT ca `legacy`,
ceea ce pe o DB goală ar sări peste toate migrările și ar declara completă o schemă incompletă.

Rezultatul așteptat: `aplicat: 36 migrări: 004, 006, …, 042`, apoi `migrări la zi (zero pending)`.

### 4.3 Contract + self-teste (nu ating DB-ul)

```powershell
python scripts/stage1_contract_manifest.py --check
python -m pytest -q tests/e2e/test_stage1_harness.py tests/e2e/test_stage1_contract_manifest.py
```

### 4.4 Matricea de defecte pe infrastructură reală

```powershell
python -m pytest -q -m "integration and stage1_web"
```

### 4.5 Harnessul servit (pentru Playwright, PR B)

```powershell
python scripts/stage1_e2e_server.py --port 8099 --origin http://localhost:4173 `
    --handshake .stage1-e2e/handshake.json
```

Scrie `.stage1-e2e/handshake.json` (gitignorat) cu `base_url`, secretul de control per proces și
tokenurile publice ale celor doi tenanți. Îl șterge la oprire. Refuză să pornească dacă hostul nu e
loopback sau dacă `ENV` nu e `test`.

Launcherul purjează la PORNIRE tenanții sintetici rămași dintr-o rulare anterioară, iar purja se
poate cere și separat:

```powershell
python scripts/stage1_e2e_server.py --purge-only
```

De ce există: un `kill -9` (pe Windows, `Stop-Process -Force`) sare peste `finally`, deci opriri
brutale repetate ar acumula tenanți și „stack efemer" ar deveni o afirmație falsă. Purja merge pe
prefix de slug (`e2e-`) și DOAR pe DB loopback — pe o bază partajată o ștergere pe prefix ar fi o
unealtă prea ascuțită pentru o problemă de igienă. Verificat: după un `Stop-Process -Force`, cei doi
tenanți rămân, iar `--purge-only` îi șterge (`purjat 2 tenanți sintetici`).

Frontendul se pornește separat, cu protocolul v2 la BUILD:

```powershell
# în repo-ul Sales MVP Frontend Final
$env:VITE_CHAT_PROTOCOL_V2 = "true"
npm run build
npm run preview -- --port 4173
```

### 4.6 Curățare completă

```powershell
docker compose -f docker-compose.stage1-e2e.yml down -v
```

Datele Postgres stau în `tmpfs`: stackul e efemer prin construcție, nu prin disciplină. Nu există
volum din care să reînvie starea unei rulări anterioare.

---

## 5. Cum e construit harnessul

### 5.1 Poarta de pornire (structurală, nu convenție)

`assert_harness_allowed` ridică `HarnessRefused` dacă: `settings.env != "test"`; `is_prod`; hostul de
bind nu e loopback; secretul de control are sub 32 de caractere; `WEB_ENABLED` sau
`WEB_TURN_V2_ENABLED` sunt stinse. Ultima condiție e cea care surprinde: **refuzăm mai degrabă decât
să trecem** un test care ar rula fără suprafața pe care pretinde că o exersează.

### 5.2 Zero rețea, dovedit pozitiv

`deny_outbound_network()` permite loopbackul + hosturile de DB/Redis din configurație. Orice altă
rezolvare DNS sau conexiune e refuzată **și numărată**. Contorul e dovada cerută de
`outbound_provider_network_attempt_count = 0`: absența unei erori nu dovedește absența unui apel.

`ModelCounters.calls_total == 0` e cealaltă jumătate — o folosim pe fiecare cale care NU trebuie să
ajungă la model: rate limit, plafon de cost, corp respins, acțiune tamperată, acțiune din alt tenant.

### 5.3 Rutele de control

Prefix `/__test__`, vocabular ÎNCHIS (7 rute, verificate de test): `health`, `reset`, `scenario`,
`fault`, `probe/counters`, `probe/turn`, `probe/conversation`. Fiecare cere **loopback ȘI** secretul
per proces (`compare_digest`), iar refuzul e **404, nu 403** — un 403 ar confirma că ruta există.

Nu există rută de ceas și nici de ID: cardul cere ca acelea să rămână seam de fixture. Un test
verifică asta pe numele rutelor.

`probe/turn` primește cheia sintetică a tenantului (`alpha`/`beta`), **niciodată `business_id`** —
altfel ruta de control ar fi chiar oracolul cross-tenant pe care R18 spune că nu are voie să existe.
Verificat pe semnătura handlerelor.

### 5.4 Modelul fals nu inventează fapte

`run_tool_loop` cheamă `execute` REAL — deci `search_products` lovește Postgres real cu filtrele lui
reale — și compune răspunsul EXCLUSIV din ce s-a întors. Consecința e că validatorul (stagiul 8) și
grounding guardul (NX-240) rulează pe fapte adevărate și **pot** să respingă. Un fake cu text fix ar
trece pe lângă exact stratul pe care gate-ul pretinde că îl verifică.

Proza nu conține cifre, cu o singură excepție motivată (întrebarea „cât costă acesta", unde cifra se
ia din rezultatul tool-ului). Asta nu e o scurtătură: e chiar regula NX-240 — faptele stau pe carduri
ca text localizat server-side, nu în proză.

### 5.5 Embedderul determinist

`embed_text` proiectează fiecare token pe o direcție unitară derivată din sha256 și adună. E un
spațiu vectorial REAL: „ser cu vitamina C" e efectiv mai aproape de un produs care conține acele
cuvinte decât de un șampon, deci `search_products_semantic` (pgvector, HNSW, JOIN real) rankează pe
semnal. Un stub `[0.0] * 1536` ar face orice produs egal de aproape: testul ar trece, iar retrievalul
n-ar fi fost exersat deloc.

Determinismul e bit-exact între procese, deci embeddingul seedat în DB și cel calculat la runtime
sunt identice. `model` la seedare = `settings.model_embed`, obligatoriu: read-path-ul filtrează
explicit pe model, iar un embedding scris cu alt nume ar fi invizibil și testul ar cădea tăcut pe
calea lexicală.

### 5.6 Doi tenanți cu ID-uri vecine

`sibling_business_ids()` produce două UUID-uri care diferă **doar în ultimul nibble**. Un bug de
izolare care compară prefixe, trunchiază, sau se sprijină pe „ID-uri evident diferite" trece
neobservat pe date de test comode. Cataloagele diferă și prin limbă (ro/en), ca o scurgere să se vadă
fără să compari ID-uri.

### 5.7 Probele

`tests/e2e/stage1_probes.py` — tot SQL-ul într-un registru (`PROBE_SQL`), ca testul de igienă să
poată itera peste el: fiecare statement începe cu `select`, conține `business_id = $1` și nu conține
niciun verb de scriere. Probele întorc NUMERE și statusuri închise; `safe_body`, `visitor_id`,
tokenurile și `response_json` nu ies de aici, ca un artefact de CI să nu conțină ce logurile n-au
voie să vadă.

---

## 6. Matricea R1–R22 — starea reală

Sursa de adevăr e `qa-suite/stage1/web-v2/scenarios.json`, iar un test verifică prin **parsare AST**
că fiecare `backend_test` referit EXISTĂ. O referință de test inexistentă e mai rea decât un test
lipsă, fiindcă arată ca acoperire.

| ID | Backend (acest PR) | Frontend (PR B) |
|---|---|---|
| R1 dublu submit | ✅ `test_r1_double_submit_executes_once` | ⏳ `concurrency.spec.js` |
| R2 două taburi | ✅ `test_r2_two_tabs_one_active_turn` | ⏳ |
| R3 202 pierdut | ✅ `test_r3_lost_accept_response_replays` | ⏳ `recovery.spec.js` |
| R4 refresh în working | ✅ `test_r4_status_during_working` | ⏳ |
| R5 worker omorât după claim | ✅ `test_r5_worker_killed_after_claim_is_reclaimed` | ⏳ |
| R6 worker vechi întors | ✅ `test_r6_stale_epoch_commit_is_fenced` | ⏳ |
| R7 SSE gap/duplicat | ✅ `test_r7_sse_resumes_without_duplicates` | ⏳ |
| R8 Redis mort | ✅ `test_r8_redis_dead_recovery_from_ledger` | ⏳ `failures.spec.js` |
| R9 DB tranzitoriu la commit | ✅ `test_r9_db_transient_at_commit_is_terminal_safe` | ⏳ |
| R10 sesiune expirată | ✅ `test_r10_session_renewal_keeps_binding` (+ origin) | ⏳ `security-boundary.spec.js` |
| R11 409 turn activ | ✅ `test_r11_conflict_references_active_turn` | ⏳ |
| R12 429 rate/cost | ✅ două teste (rate limit + plafon de cost) | ⏳ |
| R13 413/422 | ✅ `test_r13_rejected_bodies_write_nothing` (4 forme) | ⏳ |
| R14 timeout de model | ✅ `test_r14_model_timeout_persists_terminal` | ⏳ |
| R15 payload malformat | **N/A, justificat** (vezi mai jos) | ⏳ `failures.spec.js` |
| R16 drift de hash | ✅ `test_r16_any_artifact_edit_breaks_the_gate` (mutație) | ⏳ checker cross-repo |
| R17 acțiune tamperată | ✅ `test_r17_tampered_action_mutates_nothing` | ⏳ `actions.spec.js` |
| R18 acțiune din alt tenant | ✅ `test_r18_action_from_other_tenant_is_not_found` | ⏳ |
| R19 retry de comerț | ✅ `test_r19_commerce_retry_yields_one_receipt` | ⏳ |
| R20 „New chat" în working | ✅ `test_r20_reset_during_working_is_refused` | ⏳ |
| R21 offline apoi online | ✅ `test_r21_result_retained_across_offline_window` | ⏳ |
| R22 progres out-of-order | ✅ `test_r22_progress_never_regresses` | ⏳ |

**R15 nu are test backend, și trebuie să nu aibă.** Scenariul cere ca payloadul să fie STRICAT după
ce a părăsit serverul. Un test backend care construiește payloadul rupt ar dovedi doar că fixture-ul
e rupt, nu că decoderul se apără. Proprietatea backend echivalentă — serverul nu emite blocuri
necunoscute — e acoperită de invariantul `only_known_block_types`. Justificarea e în manifest, iar un
test cere ca ORICE rând fără `backend_test` să aibă una explicită (>80 de caractere) plus o țintă de
frontend.

R17/R18/R19 au un `pytest.skip` de gardă: depind de emiterea unui token de acțiune, iar emiterea
depinde de ce declară vandabil grounding guardul (NX-240). Pe catalogul sintetic `synced_at` e setat
exact ca să existe CTA-uri, și **măsurat: garda nu s-a declanșat niciodată** — rularea raportează
zero skip. Garda rămâne ca un scenariu care nu emite acțiuni să se vadă ca skip cu motiv, nu ca
trecere falsă.

Rezultatul măsurat pe stackul efemer (Postgres 16 + pgvector, Redis 7, două rulări consecutive):

```
35 passed, 2 xfailed, 0 skipped, 0 failed   (~18s)
```

Cele două `xfail` sunt **defecte reale ale sistemului**, nu ale harnessului — vezi §6.1. Ambele au
`strict=True`: în ziua în care cardul owner le repară, testul trece, `strict` transformă trecerea în
eroare (XPASS) și forțează ștergerea markerului. Un `xfail` care supraviețuiește fixului e un gate
care minte.

### 6.1 Ce a găsit gate-ul la prima rulare (ambele REPARATE în #295)

Ambele defecte trăiesc pe căi active doar cu flag-urile v2 aprinse, și de aceea nicio suită
existentă nu le atingea: producția rulează cu flag-urile stinse, iar testele de până acum foloseau
monkeypatch în loc de DB real.

**(1) Acțiunile opace nu pot fi acceptate — owner NX-236/237.**
`src/web/app.py:1220` și `src/web/action_service.py:204` scriu `messages.content_type = 'action'`.
CHECK-ul din `docs/schema_v2_production.sql:185` permite doar
`('text','image','audio','video','document','interactive','template','location','sticker')`.
Cu `WEB_ACTIONS_ENABLED=true`, acceptul ORICĂRUI turn pornit dintr-un buton crapă cu
`CheckViolationError` — adică butoanele nu funcționează deloc. Reprodus și în afara harnessului, cu
un `insert` direct. Fixul e o migrare care extinde CHECK-ul; aparține cardului owner.

**(2) Contextul de pagină și comanda de acțiune nu se rehidratează la execuție — owner NX-234/236.**
`load_execution_refs` (`src/db/queries/web_turns.py`) citește `payload` din `rec`, dar proiecția
EXTERIOARĂ a query-ului listează doar `m.id, m.body, m.content_type`. Coloana `payload` există în
subqueryul lateral și se pierde în select, deci `payload` nu e niciodată printre cheile Recordului
și `page_context` / `action` ies **mereu** `None`. Persistarea e corectă (verificat pe DB:
`messages.payload->'context'` conține ancora); numai citirea e ruptă. Consecințe: „recovery cu
aceeași ancoră" (NX-234) nu se întâmplă niciodată, iar un turn de acțiune reluat după restart își
pierde comanda (NX-236). Fixul e o coloană în selectul exterior.

Cardul interzice repararea în acest PR: *„repararea defectelor găsite în același PR dacă aparțin
cardurilor owner. Se deschide PR în repo/cardul corect, apoi gate-ul se rerulează."* Așa s-a și
făcut: fixurile au plecat în **#295** (migrarea 043 + `m.payload` în selectul exterior), gate-ul s-a
rerulat pe codul reparat și trece **37/37, zero xfail**. Cei doi markeri `xfail(strict=True)` au fost
ȘTERȘI — `strict` a făcut pasul obligatoriu, nu opțional.

**Ce NU au deblocat fixurile — măsurat, nu presupus.** Anticipasem că cele trei scenarii blocate de
aceste defecte urcă la `covered` (12/16). **Greșit**, și merită scris de ce, fiindcă e diferența
dintre un gate care raportează și unul care speră:

- `product_context` — contextul ajunge acum la execuție (defectul e reparat, verificat), dar
  invariantul `context_resolved_server_side` citește `state.references.resolved`, care există doar în
  forma **v2 persistată** a stării (`CONVERSATION_STATE_V2_WRITE_ENABLED`, nepromovat de NX-235).
  Măsurat pe profilul certificat: `schema_version: null`. N-am relaxat invariantul ca să treacă —
  ar fi fost mutarea țintei.
- `commerce_success` / `commerce_stale` — CTA-urile de coș se emit din `ctx.grounded`
  (`processor.py`: `commerce_product_refs` = produsele pe care guardul NX-240 le declară vandabile),
  deci **doar** pe profilul cu creier unic + projector v2, necertificabil cât timp NX-238 e
  `NOT-READY`. Măsurat: pe profilul certificat se emit doar kind-uri ne-mutante (`show_more`,
  `compare_selection`, `request_details`, `request_reviews`, `feedback_up/down`) — niciun `cart_*`.

Acoperirea rămâne deci **9/16 scenarii, 16/22 invarianți**; s-au schimbat CAUZELE, nu cifrele.

---

## 6.2 Acoperire reală: 9 din 16 scenarii, 16 din 22 de invarianți

Numerele sunt publicate în `gate-thresholds.json → gate.known_gaps` și legate de manifest prin test
(`test_known_gaps_in_thresholds_match_the_manifest`), fiindcă un gate care își ascunde golurile e mai
periculos decât unul care le arată.

Un audit al propriei implementări a găsit exact această problemă: prima versiune declara
`canonical_scenarios_covered_ratio: 1.0` în timp ce backendul rula 4 din 16 scenarii, iar 13 din 22
de checkere existau doar mutație-testate — declarate, implementate și INERTE pe calea reală. Trei
lucruri au reparat-o:

1. **`backend_coverage` per scenariu** (`covered` | `blocked` | `frontend_only`), cu motiv obligatoriu
   la `blocked` (test respinge motivele vagi);
2. **trei teste de acoperire** care refuză divergența: declarația vs execuția reală, orice invariant
   backend necerut de niciun scenariu, orice invariant neexecutat care nu ține de un scenariu blocat;
3. **acoperire în plus, obținută prin repararea harnessului**, nu prin relaxarea pragului — vezi
   §6.3.

| Blocat de | Scenarii | Invarianți inerți | Se închide când |
|---|---|---|---|
| Flag nepromovat (`CONVERSATION_STATE_V2_WRITE_ENABLED`, NX-235) | `memory_correction`, `product_context` | `revoked_need_absent`, `context_resolved_server_side` | NX-235 promovează scrierea stării v2 |
| Profil necertificabil (creier unic + projector v2) | `commerce_success`, `commerce_stale` | `one_receipt_per_action`, `cart_summary_server_owned`, `no_false_commerce_success` | NX-238 dă `GO` |
| Fără PRODUCĂTOR în `src/` | `routine` | `routine_steps_ordered` | cineva emite blocul `routine` |

Categoriile sunt cauze DISTINCTE, iar un test cere ca ele să însumeze exact numărul de scenarii
blocate (`test_blocked_subcategories_sum_to_the_blocked_total`) — altfel un gol ar putea rămâne
nenumărat chiar în artefactul al cărui rost e să numere golurile.

Al treilea rând merită subliniat: tipul `routine` există în contract (NX-228) și are componentă în FE
(NX-244), dar **nimic din `src/` nu emite un bloc `routine`**. Nu e un eșec de test — e o
funcționalitate declarată și neimplementată, pe care gate-ul a făcut-o vizibilă.

## 6.3 Trei bug-uri în harnessul însuși, găsite la auto-audit

Merită scrise, fiindcă toate trei aveau aceeași formă: **testul trecea pe o formă pe care producția
nu o emite niciodată.**

1. **Fake-ul presupunea JSON de la tool-uri.** Formatul real e `ToolResult.llm_view` — linii
   `[<id>] <nume> | <brand> | <preț> lei | …`. Consecința: pe calea reală nu extrăgea nimic, deci
   proza cădea pe mesajul de „n-am găsit" în timp ce vederea arăta trei carduri, iar
   `compare_products` primea o listă goală de id-uri. Parserul citește acum formatul real, iar
   testele unitare folosesc EXACT acea formă (nu un JSON inventat), ca driftul să nu se poată repeta.
2. **Checkerul de comparație cerea `columns`.** Contractul și proiecția folosesc `headers`. Nu ar fi
   trecut niciodată pe date reale — dar nu se executa, deci nimeni nu afla. Acum verifică și
   alinierea `cells` ↔ `headers`.
3. **Detecția promptului de feedback citea eticheta.** Pe date reale, `id`-urile sunt opace și `icon`
   e `None`, deci nu găsea nimic. Corectat la sursa de adevăr: se deschide plicul SIGILAT și se
   compară KIND-ul cu `FEEDBACK_KINDS`. Un test care ar deduce semantica din label ar fi exact
   frontendul-al-doilea-creier pe care boundary-ul interzice.

Un al patrulea, mai mic: o assertion vacuă în R7 (`all(... for ... in [])` e `True` pe listă goală),
înlocuită cu proprietatea explicită „evenimentul terminal nu se retrimite".

## 7. Praguri: ce judecă gate-ul și ce doar raportează

`qa-suite/stage1/web-v2/gate-thresholds.json`, un singur artefact, validat în ambele repo-uri.

**Judecă (zerouri ratificate):** execuții duplicate, receipts duplicate, vederi terminale goale,
erori de decode/hash de contract, încălcări de grounding și de hard constraints, scurgeri
cross-tenant, Axe serious/critical, încălcări ale boundary-ului de frontend, conexiuni DB ținute
peste așteptări externe, tentative de rețea spre provider, sentinel de secret în artefacte, PII în
traces. Astea nu sunt praguri de performanță — sunt invarianți. Nu au nevoie de baseline ca să fie
corecți: un singur receipt dublu e un defect indiferent de distribuție.

**Raportează, NU judecă:** latența. `src/observability/slo.py::RATIFIED` e `False` — pragurile NX-241
sunt PROPUSE, nu ratificate pe o fereastră reală de baseline. Un prag ales după ce s-au văzut datele
nu e un prag. Un test cere ca artefactul și codul să nu divergă: dacă `RATIFIED` se aprinde fără ca
pragurile de aici să se actualizeze, suita pică.

**Acoperire:** raporturile de acoperire (scenarii canonice, P0 din matrice, traces complete) sunt
`1.0`. Un gate care rulează pe jumătate din scenarii raportează verde pe ce n-a măsurat.

---

## 8. Privacy și artefacte

- probele nu întorc text liber, tokenuri sau PII (verificat pe registrul de SQL);
- `test_no_pii_or_raw_body_on_the_ledger_row` verifică pe rândul REAL că `web_turns` nu poartă
  corpul brut, `visitor_id` sau tokenul de canal — inputul safe trăiește în `messages`, PII-ul de
  canal doar în `channel_identities` (P12);
- handshake-ul (secret de control + tokenuri de tenant) e gitignorat, efemer și șters la oprire;
- certificatul de pereche conține hash-uri, versiuni și două SHA-uri — nimic altceva;
- politica `E2E_SECRET_SENTINEL` (sentinel injectat în token/text, jobul de upload pică dacă apare
  în artefacte) se aplică artefactelor de BROWSER și e a PR-ului B: aici nu există trace, video sau
  body de request de urcat.

---

## 9. Verdict și ce mai lipsește

**NO-GO pentru NX-249**, din trei motive rămase:

0. ~~Două defecte reale, găsite la prima rulare~~ — **REPARATE** în #295 (§6.1). Gate-ul rulat pe
   codul reparat trece 37/37, zero xfail. Nu au deblocat însă niciun scenariu suplimentar: cauzele
   erau cumulative, vezi §6.1.

1. **PR B nu există.** Jumătatea de browser (renderer pasiv dovedit comportamental, Axe, contract
   cross-repo, responsive, dedupe de SSE, zero mutație de `localStorage`) e ~jumătate din matrice.
   Fără ea nu se poate emite o pereche certificată.
2. **NX-238 e `NOT-READY`.** Profilul cu creier unic + projector v2 e rulabil, dar nu certificabil.
   Deblocarea e a NX-203 (corpus) + NX-202 (H3 sigilat).
3. **Pragurile de latență nu sunt ratificate.** Gate-ul le raportează. Ratificarea e o decizie pe o
   fereastră reală de baseline, nu o valoare aleasă ca să treacă.

Ce nu lipsește: infrastructura de măsurare. Harnessul rulează, matricea backend e completă și
verificabilă, driftul de contract rupe CI-ul pe fiecare PR, iar certificatul de pereche se emite
automat.

### Pași următori, în ordine

0. ~~Cele două defecte din §6.1~~ — **făcut** (#295, migrarea 043 + `m.payload`). Markerii
   `xfail(strict)` au fost șterși.
1. **PR B** (repo `Sales MVP Frontend Final`, branch `test/NX-247-stage1-web-e2e-frontend`):
   Playwright + Axe, `scripts/check-cross-repo-chat-contract.mjs` care checkout-uiește backendul la
   SHA-ul din certificat și recalculează `manifest.json`, cele 10 spec-uri din card.
2. **Eliminarea skip-urilor condiționate** de la R17/R18/R19, după ce emiterea CTA pe catalogul
   sintetic e deterministă.
3. **Ratificarea pragurilor NX-241/246** pe o fereastră de baseline reală → `RATIFIED = True` +
   valorile în `gate-thresholds.json`.
4. **NX-249** consumă `reports/stage1/certificate.json`: canary DOAR pe perechea de SHA-uri de
   acolo, niciodată pe „latest" independent al fiecărui repo — două „latest" verzi separat nu sunt o
   pereche testată.

---

## 10. Depanare

| Simptom | Cauză uzuală |
|---|---|
| `HarnessRefused: WEB_ENABLED=false` | profilul nu s-a aplicat înainte de primul `get_settings()`; `Settings` e `lru_cache`. Fixtura face `cache_clear()` + `importlib.reload(src.webhook.app)`. |
| `skip: migrarea 040_web_turns nu e aplicată` | `python scripts/migrate.py` nu a rulat pe DB-ul țintă |
| `OutboundNetworkDenied: dns:api.openai.com` | ceva a încercat un apel real — e exact ce trebuie să se întâmple; găsește apelantul, nu dezactiva garda |
| toate rutele de control dau 404 | `TestClient`/`ASGITransport` fără `client=("127.0.0.1", …)`: hostul implicit nu e loopback, iar poarta îl respinge corect |
| `DRIFT de contract` în CI | rulează `python scripts/stage1_contract_manifest.py --write` și comite artefactul; NU edita manifestul manual |
| teste de matrice care se sar | vezi §6: skip cu motiv ≠ trecere. Înainte de release, zero. |
