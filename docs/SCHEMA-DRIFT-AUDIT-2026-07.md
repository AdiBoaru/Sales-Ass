# Audit de drift schemă LIVE vs REPO (2026-07-23)

**Context:** follow-up NX-216. Bug-ul acela (cache înghețat) a avut cauza „un obiect de schemă
există LIVE dar nu în niciun fișier SQL din repo" (coloana `prompt_version` + indexul unic pe 4
coloane, create de migrarea 030 ajunsă în Supabase prin branch stacked, dar nu în `main`). Dacă
un astfel de drift a scăpat, pot exista altele care ar corupe tăcut alte căi — inclusiv
baseline-ul NX-201. Acest audit caută SISTEMATIC restul clasei.

**Metodă:** enumerăm obiectele de schemă din DB-ul live (coloane, indexuri unice, constrângeri)
și verificăm, pentru fiecare, dacă numele apare în fișierele SQL canonice ale repo-ului
(`schema_v2_production.sql` + migrările `003–034` de pe `origin/main` + `034` din acest PR).
Zero apariții = candidat de drift. Fuzzy prin construcție (grep pe nume, nu parser SQL) → listă
de triat, nu verdict automat. Script: rulat ad-hoc (read-only), nu se comite (folosește credențiale).

## Rezultat

### ✅ Clasa periculoasă (ON CONFLICT) — ZERO drift rămas
**Indexuri unice cu nume custom** (`CREATE UNIQUE INDEX idx_*` — cele care TREBUIE să fie într-o
migrare, exact tipul lui `idx_semcache_exact`): **0 candidați** după fixul 034. Cele 76 de
indexuri unice „flagate" de sweep-ul brut sunt toate `*_pkey` / `*_key` auto-generate de Postgres
din constrângeri inline (`primary key` / `unique`) din `schema_v2` — se recreează identic pe un DB
fresh, deci NU sunt drift. `prompt_version` era singurul de tipul periculos; e închis.

### ⚠️ Un singur drift de coloane — benign, cunoscut, non-hot-path
**Tabelul `source_products_raw`** (coloanele `source_site`, `source_url`, `scraped_at` — NOT NULL)
există live dar nu în migrările Python. Investigat:
- **Documentat** deja în `docs/DB_MIGRATION_NOTES.md`: „rămâne (sau muți în storage) | nu intră în
  hot path; ok ca audit".
- **Folosit doar de tooling-ul de seed TS** (`db/seed/seed.ts` scrie în el prin clientul Supabase
  JS) — NU de bot runtime-ul Python. Nu poate produce un bug de runtime ca `prompt_version`.
- **0 rânduri** în prezent.
- E drift doar în sensul îngust că DDL-ul lui trăiește în stratul TS, nu într-o migrare
  `docs/0NN_*.sql` → un DB migrat DOAR cu runner-ul Python nu l-ar avea (seed-ul TS îl creează
  sau eșuează grațios — `seed.ts:185` are deja `skipping source_products_raw` la fișier lipsă).

**Follow-up minor (nu blochează nimic):** formalizează DDL-ul `source_products_raw` într-o migrare
`docs/0NN_*.sql`, ca reproductibilitatea pe DB fresh să nu depindă de ordinea rulării TS-vs-Python.
Prioritate joasă (non-hot-path, 0 rânduri, degradare grațioasă existentă).

### Restul candidaților (zgomot)
Sweep-ul brut a raportat 222 candidați totali; după filtrarea claselor periculoase rămân cele
două de mai sus. Restul (constrângeri `*_check` / `*_fkey` auto-numite, coloane din `schema_v2`
referite doar implicit) sunt fals-pozitive ale metodei fuzzy, nu drift real.

## Concluzie
Fixul NX-216 (034) a închis **singurul** drift din clasa periculoasă (ON CONFLICT / index unic
desincronizat de cod). Nu mai există altă „bombă cu ceas" de același tip. Singurul rest e un tabel
de staging cunoscut, documentat, non-hot-path — de formalizat când convine, fără urgență.

**Implicație pentru NX-201:** baseline-ul de latență/cost NU e amenințat de alt drift de schemă
ascuns. Rămâne blocat doar pe warmup-ul cache-ului (ca măsurătoarea să fie cu cache viu), nu pe
integritatea schemei.
