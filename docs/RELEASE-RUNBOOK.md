# Release runbook — NX-248

Comenzi exacte, în ordine, cu ce se verifică între ele. Scris pentru cineva care execută sub
presiune, nu pentru cineva care citește pe îndelete.

**Regula care ține totul:** se construiește O SINGURĂ dată, se promovează un DIGEST. Dacă la orice
pas apare un tag (`latest`, `sha-…`) în locul unui `sha256:…`, oprește-te — nu mai știi ce
promovezi.

---

## 0. Ce trebuie să existe înainte de primul release

- [ ] GitHub Environments `staging` și `production`, cu approvals + secrete SEPARATE.
- [ ] `VPS_HOST_KEY` / `STAGING_HOST_KEY` = cheia publică a hostului, obținută **out-of-band**
      (de pe consola providerului, nu prin `ssh-keyscan` de pe runner) și aprobată de un om.
- [ ] `PROD_DATABASE_URL_MIGRATION` / `STAGING_DATABASE_URL_MIGRATION` — credentialul de DDL,
      existent DOAR în environmentul respectiv.
- [ ] Pe VPS: `.env.migrate` (mod 0400, owner-ul de deploy) cu `DATABASE_URL_MIGRATION`.
      **Nu** în `.env` — vezi §Verificare rapidă.
- [ ] Contul de deploy nu e root și nu poate face decât ce-i trebuie în `/opt/nativextech/nativx`.

---

## 1. Release normal (staging → producție)

### 1.1 Build (automat, la push pe `main`)

`release.yml` → jobul `build`:

1. build o singură dată, cu SBOM + provenance;
2. `cosign sign` (keyless, identitatea = workflow-ul);
3. Trivy CRITICAL/HIGH **cu fix disponibil**, fail-closed (vezi §1.5);
4. `image_contract.py` (non-root, conținut, zero secrete, canary injectat de CI);
5. `build_manifest.py` → `manifest.json` (digest, release, config, interval de schemă, digestul
   PRECEDENT).

Ieșirea care contează: **digestul**. Îl iei din rezumatul rulării.

### 1.2 Staging (automat, același digest) — DEZACTIVAT până există un mediu

Jobul rulează doar cu variabila de repo **`STAGING_ENABLED=true`**; altfel se sare (`skipped`).
Azi nu există un mediu de staging: zero GitHub Environments, zero secrete `STAGING_*`, un singur
VPS — cel de producție. Un job care nu poate reuși prin nicio acțiune a noastră ar înroșji fiecare
release, exact ca poarta de scan de la §1.5, și cu același efect: nu te mai uiți la pipeline.

Ca să-l pornești: adaugă cele 7 secrete `STAGING_*` în environmentul `staging`, apoi setează
variabila. Comutatorul e separat de secrete deliberat — pornirea staging-ului e o decizie, nu un
efect secundar al adăugării unui secret.

Cât timp e stins, **nimic nu verifică digestul înainte de producție**. Promovarea rămâne posibilă,
dar smoke-ul rulează abia pe producție, adică prea târziu ca să te mai protejeze.



Ordinea nu e negociabilă — semnătura se verifică ÎNAINTE de a atinge hostul:

```
cosign verify → preflight → migrare (job separat) → deploy prin digest → smoke
```

Smoke-ul verifică lanțul real, nu un „200 OK". **Ce lanț anume depinde de ce servește instanța**,
iar profilul detectat ajunge în raport (`"profile"`) și în linia de rezumat a rulării:

| profil | când | ce verifică |
|---|---|---|
| `v2` | `WEB_TURN_V2_ENABLED=true` | `bootstrap → accept (202) → terminal → replay byte-identic → idempotență` |
| `v1` | flagul stins (starea de AZI în producție) | `bootstrap → POST /web/chat → răspuns nevid` |

Detectarea nu e o presupunere: `_v2_gate` verifică flagul ÎNAINTEA sesiunii, deci un 404 pe o
cerere cu sesiune validă înseamnă „feature stins" (ruta lipsă ar da 404 și fără parametri, unde
FastAPI răspunde 422). Nu se trimite o cerere separată de probing — acceptul E sonda.

De ce contează: până la NX-249, cerând `accept (202)` smoke-ul cerea un contract **oprit prin
decizie ratificată**. Deployul reușea, smoke-ul pica, iar releaseul raporta „picat" pentru o
promovare care mersese — cu imaginea deja schimbată. Un gate pe care nimeni nu-l poate trece nu
filtrează nimic (aceeași greșeală ca la poarta de scan, §Excepții).

**La cutover:** adaugă `SMOKE_EXPECT_PROFILE: v2` în pasul de smoke din `release.yml`. Din acel
moment o cădere tăcută înapoi pe v1 devine eșec, în loc de smoke verde pe alt contract.

> `SMOKE_BASE_URL` e API-ul (`WEBHOOK_HOST`, ex. `https://bot.…`), **nu** vitrina care încarcă
> widgetul (`https://demo.…`). Vitrina e un SPA cu catch-all: întoarce 200 + HTML la orice cale,
> deci smoke-ul ar muri cu `JSONDecodeError` fără să spună de ce.

### 1.3 Promovare în producție (MANUAL)

```
Actions → Release → Run workflow
  digest:        sha256:…   ← din rularea de build, copiat, nu retastat
  build_run_id:  32254699091   ← ID-ul ACELEIAȘI rulări (e în URL). De acolo vine manifestul.
  rollback:      false
  first_release: false   ← true DOAR la prima promovare (vezi mai jos)
```

`build_run_id` nu e redundant: manifestul e artefact al rulării care a CONSTRUIT digestul, iar
promovarea nu reconstruiește nimic. Cele două se verifică reciproc — dacă manifestul din rularea
indicată are alt digest decât cel cerut, preflight refuză. Un ID greșit e respins, nu folosit tăcut.

`first_release` există pentru un impas de bootstrap: `--require-rollback-possible` cere o țintă de
rollback, dar la primul release nu există niciuna, deci poarta nu putea fi trecută niciodată.
Acceptarea e DECLARATĂ, nu dedusă — din manifest, „primul release" și „manifestul precedent n-a
putut fi citit" arată identic (`previous_digest` gol în ambele), iar a doua e exact situația în care
vrei să te oprești. Steagul acoperă DOAR absența unei ținte: o schemă care depășește ce tolerează
imaginea precedentă blochează în continuare.

Environmentul `production` cere aprobarea umană. Pașii rulează în ordinea:

```
verify semnătură → preflight --require-rollback-possible → migrare → deploy → smoke
```

`--require-rollback-possible` e poarta care oprește releaseul **înainte** de deploy dacă imaginea
precedentă nu mai tolerează schema curentă. Vezi §4.

### 1.4 După deploy

```bash
# pe VPS, în /opt/nativextech/nativx
python scripts/release/verify_manifest.py --manifest manifest.json --base-url http://localhost:8000
```

Verifică trei lucruri: manifestul e neatins (amprentă), fiecare container rulează exact digestul,
iar `/health/ready` raportează ACELAȘI `release` + `config`. Ultimul prinde **deployul parțial**:
imagine nouă cu configurație veche arată perfect la `docker compose ps`.

### 1.5 Politica de scan (ce blochează și ce nu)

Poarta blochează pe **CRITICAL/HIGH care au fix publicat**. Atât.

Nu e o relaxare, e condiția ca poarta să fie o poartă. Măsurat pe imaginea reală (2026-08-19,
digestul din producție): din 26 de finding-uri HIGH/CRITICAL, **17 nu aveau niciun patch în lume**
— `affected` sau `fix_deferred`, toate în pachete de bază Debian pe care aplicația nu le folosește
(cele 4 CRITICAL, toate în `perl-base`, într-un serviciu Python care nu invocă perl). O poartă care
cere zero din ele nu poate fi trecută prin nicio acțiune a noastră. Consecința nu e „suntem mai în
siguranță", ci: **85 de rulări roșii la rând, zero deployuri promovate, și o producție actualizată
manual prin SSH — fără scan, fără staging, fără smoke.** Un control pe care nimeni nu-l poate
satisface e ocolit, iar ocolirea ia cu ea și controalele care funcționau.

Cele fără fix nu dispar din vedere:

| Unde | Ce vezi | Blochează? |
|---|---|---|
| artefactul `trivy-scan-<run_id>` | raportul SARIF complet, **și când poarta e roșie** | nu |
| `security-rescan.yml` (săptămânal) | inventar complet + verdict pe ce a devenit reparabil | da, când apare un patch |
| `.trivyignore.yaml` | excepții pentru fix-uri disponibile pe care le amânăm conștient | expiră |

Trei detalii care par mărunte și nu sunt:

- **`limit-severities-for-sarif: true`** e obligatoriu. Fără el, `entrypoint.sh` al acțiunii face
  `unset TRIVY_SEVERITY` când formatul e `sarif` — deci `severity: CRITICAL,HIGH` devine
  decorativ, iar poarta impune în realitate „zero vulnerabilități de orice severitate, inclusiv
  LOW". Dacă vezi „Building SARIF report with all severities" în log, opțiunea lipsește.
- **Artefactul de scan se încarcă cu `if: always()`**, fiindcă pasul iese cu 1 și sare tot ce
  urmează. Fără asta, raportul se pierde exact când e necesar.
- **`.trivyignore.yaml`, nu `.trivyignore`.** Doar formatul YAML are `expired_at`. În formatul
  simplu, o „dată de expirare" e un comentariu, iar regula nu se aplică.

Patch-urile de OS intră la build: stagiul `runtime` din `Dockerfile` rulează `apt-get upgrade`.
Baza pinuită pe digest fixează punctul de plecare, dar `python:3.12-slim` se republică rar, iar
Debian publică în `trixie-security` între timp — fără upgrade, imaginea rămâne în urmă cu
vulnerabilități care AU fix.

---

## 2. Migrarea

Rulează ca **job separat**, înainte de servicii, cu singurul credential de DDL din sistem:

```bash
docker compose --profile migrate run --rm migrate
```

| Cod | Înseamnă | Ce faci |
|---|---|---|
| 0 | aplicat (sau nimic de aplicat) | continui |
| 1 | `--check`: pending / drift de checksum | rulezi migrarea; drift ⇒ investighezi |
| **3** | alt migrator ține lock-ul | **aștepți**. Nu e eroare — e concurență rezolvată |
| 4 | sesiune prin pooler tranzacțional | reconectezi DIRECT (port 5432). Nu forța |

Codul 4 merită explicat: prin pgbouncer în mod tranzacție, un advisory lock de SESIUNE e o iluzie
(fiecare statement poate ajunge pe alt backend), deci două joburi ar crede amândouă că-l dețin.
Runner-ul verifică lock-ul în `pg_locks` după acquire și refuză dacă nu-l vede.

**DDL-ul e expand/contract.** `drop`/`rename` apar într-un release ULTERIOR, după ce fereastra de
rollback s-a închis. În incident nu se face down-migration — vezi §4.

---

## 3. Rollback

```bash
python scripts/release/rollback.py --manifest manifest.json          # DRY-RUN (implicit)
python scripts/release/rollback.py --manifest manifest.json --apply  # execută
```

Ținta vine din `previous_digest`-ul manifestului, nu de la tastatură. Scriptul:

1. verifică fezabilitatea contra schemei APLICATE;
2. rescrie DOAR linia `IMAGE_DIGEST` din `.env` (restul configurației rămâne — un rollback nu are
   voie să distrugă secretele hostului în timp ce repară codul);
3. `pull` + `up -d`, apoi aștepți `ready` și rulezi smoke.

**Nu șterge nimic**: nici volume, nici rânduri de ledger, nici rezultate de tur. Rollbackul
schimbă ce COD rulează, nu ce s-a întâmplat.

Alternativ, prin CI: `Run workflow` cu `digest: <precedent>` + `rollback: true` (sare peste
migrare).

---

## 4. Când rollbackul NU e o opțiune

Dacă `rollback_possible()` spune nu, ai una dintre trei situații:

| Motiv | Ce înseamnă | Ce faci |
|---|---|---|
| fără digest precedent | primul release | nu există țintă; repari înainte |
| interval necunoscut | manifestul precedent lipsește/e ilizibil | nu promitem ce nu putem verifica |
| **schema a depășit intervalul** | contractul expand/contract a fost rupt | **release-fix**, nu rollback |

În ultimul caz, revenirea ar rula cod orb peste coloane pe care nu le știe. Calea corectă e un
release nou, mic, care repară — nu un `drop column` sub incident. Un down-migration distructiv sub
presiune e cum se pierd date; poarta există ca să nu ajungi acolo.

---

## 5. Verificare rapidă (2 minute, oricând)

```bash
# 1. Producția nu consumă taguri mutabile
grep -c ":latest" docker-compose.prod.yml            # aștept: 0

# 2. Credentialul de DDL nu e în env_file-ul serviciilor de runtime
grep -n "DATABASE_URL_MIGRATION" .env                # aștept: NIMIC (e în .env.migrate)

# 3. Ce rulează chiar acum
curl -s localhost:8000/health/ready | python -m json.tool
curl -s -H "X-Ops-Token: $OPS_HEALTH_TOKEN" localhost:8000/health/detail | python -m json.tool

# 4. Procesele fără HTTP
docker compose exec worker python -m src.ops.worker_health --role worker
```

Punctul 2 e verificat și mecanic, în CI:
`tests/test_ops_release.py::test_credentialul_de_migrare_nu_ajunge_in_runtime`.

---

## 6. Incidente frecvente

| Simptom | Cauză probabilă | Verifică |
|---|---|---|
| `ready` 503 după deploy | schemă în afara intervalului | `/health/detail` → `schema.applied` vs `requires..tolerates` |
| container `unhealthy`, log tăcut | startup blocat (registru/chei) | `/health/detail` → `startup.probes` |
| worker `unhealthy`, procesul pare viu | bucla de lease moartă | `worker_health --role worker` → `lease_loop_dead` |
| deploy „reușit", comportament vechi | deploy parțial (config veche) | `verify_manifest.py --base-url …` → `config raportat ≠ manifest` |
| `docker compose config` eșuează | `IMAGE_DIGEST` lipsă | intenționat: fără digest, nu pornim nimic |
| migrarea iese cu 3 | alt job migrează | aștepți; NU rulezi cu `--force` (nu există) |

---

## 7. Excepții (calea deprecată) — RETRASĂ

`deploy.yml` **nu mai există**. A fost șters pe 2026-08-23, după ce condiția de retragere s-a
împlinit: primul deploy de producție promovat prin digest a reușit (rularea `32277763628`,
2026-08-19, jobul „Producție (promovare prin digest)"). Tabelul de excepții a rămas gol — calea
deprecată n-a fost folosită niciodată după ce i s-a scos triggerul automat.

Nu mai există cale de ocolire a `release.yml`. Un deploy fără digest verificat, semnătură și smoke
nu mai e o opțiune pe care o poate alege cineva din reflex — dacă `release.yml` e blocat, se repară
`release.yml`.

### Ce a mai rămas din retragere

Heartbeat-ul vechi `/tmp/scheduler_alive` (`src/jobs/scheduler.py:32`) **încă e folosit**, deci nu
s-a șters odată cu workflow-ul: `docker-compose.prod.yml` a trecut pe
`python -m src.ops.worker_health` (NX-248), dar `docker-compose.yml` (dev local) îl citește în
continuare la `healthcheck`. Ștergerea liniei fără să muți și healthcheck-ul local pe `worker_health`
lasă devul cu un container `unhealthy`. E o schimbare de dev loop, nu de producție — se face
separat, cu compose-ul local actualizat în același commit.
