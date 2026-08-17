# Disaster recovery — NX-248

## 0. Ce e autoritativ după Stage 1

Întrebarea „ce pierdem dacă X dispare?" are un răspuns diferit pentru fiecare componentă. Fără
tabelul ăsta, recuperarea începe cu o dezbatere.

| Componentă | Autoritativă pentru | Dacă dispare |
|---|---|---|
| **Postgres** | `web_turns` (ledgerul), mesaje, stare, coș + receipts, feedback, config de tenant | backup + PITR; restore izolat, verificat (§3) |
| **Redis** | scheduling și coordonare (NX-233): wake-uri, lease-uri de admission, rate limit, backlog SSE | **niciun tur ACCEPTAT nu se pierde** — sweeperul îl reia din Postgres (§2.2) |
| **GHCR + manifest** | ce imagine/config sunt compatibile | redeploy după digest semnat, FĂRĂ rebuild |
| **OTel backend** | operațional, nu adevăr comercial | gap = alertă/`UNKNOWN`; nu blochează recuperarea |
| **Secret store** | credentialele curente + precedente | restore + rotație controlată ([SECRETS-ROTATION.md](SECRETS-ROTATION.md)) |

Rândul cu Redis e proprietatea centrală a NX-232/233 și e verificabil: acceptul e durabil în
Postgres ÎNAINTE ca requestul HTTP să se întoarcă, iar `executor`/`sweeper` revendică din DB.
Redis grăbește, nu decide.

---

## 1. Ținte — și de ce sunt `UNVERIFIED`

| Ce | Țintă | Stare | Se marchează `PASS` când |
|---|---|---|---|
| Postgres RPO | ≤ 5 min | **UNVERIFIED** | un drill PITR cu timestampuri reale o demonstrează |
| Postgres RTO | ≤ 60 min | **UNVERIFIED** | idem, cronometrat de la decizie la „ready" |
| Aplicație/config RPO | 0 pentru release comis | **parțial** | redeploy al digestului pe un host curat |
| Aplicație/config RTO | ≤ 15 min (DB+TLS sănătoase) | **UNVERIFIED** | cronometrat pe staging |
| Redis — rezultat client | RPO 0 după accept | **UNVERIFIED** | loss drill (§2.2) |
| Redis — reluare executor | ≤ 10 min | **UNVERIFIED** | idem |

`UNVERIFIED` **blochează NX-249**. Nu e birocrație: o țintă nedemonstrată e o presupunere pe care
o afli greșită exact în ziua în care o folosești. Aceeași disciplină ca `NOT-READY` (NX-238).

---

## 2. Runbook-uri per scenariu

Fiecare începe cu comenzi **read-only**. Sub presiune, prima acțiune nu trebuie să fie una care
schimbă ceva.

### 2.1 Postgres indisponibil / degradat

**Severitate: critică.** Marginea nu mai poate accepta ture.

```bash
# READ-ONLY, în ordinea asta
curl -s localhost:8000/health/live                     # aștept 200 — procesul e viu
curl -s localhost:8000/health/ready                    # aștept 503
curl -s -H "X-Ops-Token: $OPS_HEALTH_TOKEN" localhost:8000/health/detail   # CARE dependență
docker compose logs --tail=100 worker | grep -i "pool\|connect"
```

1. Confirmă la provider (status page, consolă) — o pană a providerului nu se repară din compose.
2. **Nu reporni containerele.** `live` e 200 tocmai ca să nu le repornească nici orchestratorul;
   repornirea nu repară Postgres și pierde starea caldă.
3. Când DB-ul revine: `ready` trece singur. Turele `accepted` sunt reluate de sweeper.
4. Escaladare: >15 min ⇒ anunț către client; >60 min ⇒ evaluezi restore (§2.3).

### 2.2 Volumul Redis pierdut

**Severitate: medie.** Prin construcție, NU pierde ture acceptate.

```bash
docker volume ls | grep nativx           # read-only
docker compose ps redis
docker compose logs --tail=50 worker | grep -i "sweeper\|reclaim\|fenced"
```

1. Repornește Redis (volum nou e acceptabil — nu conține adevăr comercial).
2. Urmărește sweeperul: turele `accepted`/`running` cu lease expirat sunt revendicate din Postgres.
3. **Verifică efectul dublu:** un tur reluat nu are voie să producă a doua oară un apel de model
   sau o mutație de coș. Fencing-ul (epoch CAS, NX-233) face un worker vechi să-și ARUNCE
   rezultatul; `web_turn_fenced_completion_total` crescând la restart e NORMAL.
4. Rate-limitul și bugetele reîncep de la zero — acceptabil și tranzitoriu.

### 2.3 Restore Postgres (PITR)

**Niciodată peste producție.** Restaurezi într-un proiect/DB IZOLAT și verifici acolo.

```bash
# 1. restore la provider, într-un proiect nou (operație manuală, aprobată)
# 2. verificare READ-ONLY — refuză orice țintă care nu arată a drill
export DR_VERIFY_DSN="postgresql://…@…/nativx_restore_2026_08_17?sslmode=disable"
python scripts/dr/restore_verify.py \
  --business-id 6098812a-50fc-44bd-a1ba-bc77e6399158 \
  --backup-timestamp 2026-08-17T02:00:00Z \
  --out reports/nx248/evidence/dr.json
```

Verifică, în ordine: migrări la zi → grant-uri (`bot_runtime` poate scrie ledgerul și **nu** e
supra-privilegiat) → RLS activ pe tabelele tenant-scoped → **izolare cross-tenant reală** (rulată
ca `bot_runtime`, nu ca superuser — altfel RLS e ocolit și testul ar fi verde degeaba) → ledger
recuperabil (niciun `completed` fără rezultat persistat).

RPO/RTO se CALCULEAZĂ din `--backup-timestamp`. Fără el, verdictul e `UNVERIFIED` — și
`UNVERIFIED` iese cu cod ≠ 0, deci nu poate trece drept succes într-un pipeline.

**Ștergerea mediului de drill e o operație umană separată.** Scriptul nu șterge nimic: unul care
poate distruge un mediu poate distruge mediul greșit.

### 2.4 Host pierdut

RTO-ul depinde doar de cât de repede se ridică un host nou, fiindcă artefactul e deja construit:

```bash
# pe hostul nou
git clone <repo> /opt/nativextech/nativx && cd /opt/nativextech/nativx
cp .env.exemplu .env                      # secrete din secret store, NU din backup
echo "IMAGE_DIGEST=sha256:…" >> .env      # digestul champion din manifest
docker compose pull && docker compose up -d
python scripts/release/verify_manifest.py --manifest manifest.json --base-url http://localhost:8000
```

Zero build, zero migrare (schema trăiește în Supabase, neatinsă de pierderea hostului).

### 2.5 Registry (GHCR) indisponibil

Instanța curentă continuă — nimeni nu trage imagini în starea stabilă. **Nu se face build pe VPS**
(1 vCPU, și ar produce alt artefact decât cel testat). Deployurile se amână.

### 2.6 Secret compromis

Vezi [SECRETS-ROTATION.md](SECRETS-ROTATION.md). Ordinea: revocă → rotește → verifică → abia apoi
postmortem. Pentru cheile de acțiuni (NX-236), scoaterea cheii din inel e instantanee și
deliberată: butoanele deja afișate primesc `action_invalid`, cu copy server-owned.

### 2.7 OTLP jos

`ready` NU se schimbă (telemetria e `optional` peste tot). Drop-urile sunt numărate în
`web_observability_dropped_total`. Kill-switch: `OBSERVABILITY_EXPORTER=` (gol) — păstrează
măsurarea locală, oprește exportul.

### 2.8 Deploy întrerupt la jumătate

```bash
python scripts/release/verify_manifest.py --manifest manifest.json --base-url http://localhost:8000
```

Dacă serviciile rulează digeste diferite: `docker compose up -d` reconciliază din `.env`
(idempotent). Dacă `config raportat ≠ manifest`, ai imagine nouă cu configurație veche — repari
`.env` și repornești, sau faci rollback (§3 din runbook).

---

## 3. Dependențe de platformă (contează la restore în altă parte)

Schema noastră ia de la Supabase lucruri pe care un Postgres gol nu le are. Descoperit rulând
drill-ul de migrare, nu citind cod:

| Ce | Unde se folosește | Fără el |
|---|---|---|
| schema `auth` + `auth.users` | politici RLS de dashboard | `schema "auth" does not exist` |
| funcția `auth.uid()` | aceleași politici | politicile nu se creează |
| rolurile `anon`, `authenticated`, `service_role` | `GRANT`-uri din schema de bază | `role "authenticated" does not exist` |
| extensiile `vector`, `pg_trgm` | embeddings + căutare lexicală | `extension "vector" is not available` |

Inventarul executabil: `scripts/release/migration_drill.py::_PLATFORM_SHIM`. E ȘI lista de care ai
nevoie dacă vreodată restaurezi în afara Supabase (incident de furnizor). **Nu e o migrare** și nu
ajunge în `docs/0NN_*.sql`: în producție `auth` e al platformei, iar o migrare care l-ar crea ar
intra în conflict cu el.

---

## 4. Cadență

| Când | Ce | Artefact |
|---|---|---|
| trimestrial | restore izolat + `restore_verify.py` | `reports/nx248/evidence/dr.json` |
| înainte de launch | idem + rollback drill cronometrat | `dr.json`, `rollback.json` |
| la fiecare release | drill-uri de migrare (CI) | `migrations.json` |
| lunar | verificare că backupurile chiar există la provider | notă în runbook |

Un backup necontrolat nu e un backup, e o speranță cu cost de stocare.

---

## 5. Ce NU face acest runbook

- nu autorizează ștergerea automată a niciunui mediu (nici de drill);
- nu autorizează restore peste producție;
- nu prevede down-migration distructiv în incident (vezi RELEASE-RUNBOOK §4);
- nu tratează multi-region sau failover automat (out of scope, cere ADR).
