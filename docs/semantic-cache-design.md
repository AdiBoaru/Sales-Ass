# Semantic Cache — design pentru Nativx Assistant (G5b)

_Traducerea reviewului de arhitectură (`docs/semantic-cache-architecture-review.md.pdf`)
în deciziile concrete ale proiectului NOSTRU. Ancora pentru cardurile G5b-1 + G5b-2._

## 0. Obiectiv + reframing

**Obiectiv:** tăiem apeluri LLM (stagiul 4, „straturi gratuite") fără să degradăm
calitatea și fără contaminare cross-tenant sau prețuri învechite.

**Reframing față de review (CRUCIAL):** reviewul e despre un „business assistant"
generic. Noi suntem bot de **VÂNZĂRI**, unde:
1. Query-urile de top conțin **numere** (buget) și **entități** (brand/concern) —
   exact tokenii pe care embeddings îi sub-ponderează. „cremă sub **80 lei**" ≈
   „cremă sub **200 lei**" la cosine ~0.97, dar răspuns diferit (capcana §0 din review).
2. Tierul scump (recomandări de produs, model mini) e **`dynamic`** — depinde de
   **prețuri**. Invariantul nostru e „ZERO prețuri inventate/învechite" (validatorul).
   Un preț cache-uit învechit ÎNCALCĂ regula de aur.

**Decizie (cu Adi):** cache-uim **AMBELE** tiere — static (FAQ) **și** recomandări de
produs — DAR caching-ul dinamicului e SIGUR doar cu **invalidare la schimbarea
datelor** (vezi §4). Fără seed de FAQ acum (Adi populează `faqs` ulterior); cache-ul
se încălzește din răspunsuri + (când există) FAQ.

## 1. Ce avem deja (partea grea e făcută)

- **Izolarea cross-tenant (riscul #1 din review) e REZOLVATĂ structural:** RLS +
  `where business_id = $1` + NX-50 (login role) + NX-04 (assert la checkout). Reviewul
  cere „izolare structurală, pre-filter în ANN, scope din identitate, nu din cod" —
  exact modelul nostru. Namespace-ul lor = `business_id` filtrat sub RLS.
- **pgvector HNSW cosine** + tabelul `semantic_cache` (business_id, locale, query_norm,
  embedding 1536, answer, hit_count, expires_at) + index HNSW + purjă pg_cron.
- **Embedding model** = `text-embedding-3-small` (1536). Identitatea spațiului de
  embedding e parte din entry (re-embed la schimbarea modelului — §7 review).

## 2. Arhitectura (vedere pe straturi)

```
mesaj (după Gates) →
  L1 CANONICALIZE + ROUTE VOLATILITY (determinist, fără LLM)
    • canonical_str + canonical_hash (lowercase, fără diacritice, colaps spații/filler)
    • clasă de volatilitate: static (FAQ) | dynamic (produs/preț) | realtime (comandă, "a mea")
    • realtime/personalizat → BYPASS cache (regenerează)
  │
  ├─ L2 EXACT (KV): GET (business_id, locale, canonical_hash[, data_version])
  │     HIT ⇒ servește (O(1), zero false-positive) ── iese aici
  │     MISS ⇒
  ├─ L3 SEMANTIC (pgvector): embed(canonical) → HNSW cosine,
  │     filtru business_id + locale + neexpirat [+ data_version pt dynamic],
  │     auto-accept DOAR la cosine ≥ τ_high (conservator)
  │     • dynamic: + retrieval-signature price-check (vezi §4) înainte de a servi
  │     HIT ⇒ servește ── iese aici
  │     MISS ⇒
  └─ L4 PIPELINE normal (triaj → agent → validator) → răspuns
        │
        └─ L5 WRITE-BACK gated, ASYNC (post-tur, nu blochează răspunsul):
             dacă (volatilitate cacheabilă ∧ răspuns sigur ∧ nu clarify/refuz ∧
             nu personalizat) → scrie L2+L3 cu provenance (embedding_model,
             expires_at pe volatilitate, retrieval_signature, quality_score)
```

Note de latență (din review): verificare DOAR în gray-zone (nu o avem în v1 —
auto-accept conservator); lookup-ul rulează ÎN PARALEL cu începutul pipeline-ului
ideal; write-back async.

## 3. Ce e v1 vs faza 2 (mapare pe carduri)

| Component | Card | Note |
|---|---|---|
| Canonicalize + L1 exact + L2 semantic (τ_high) | **G5b-1** | doar **tier static** servit; dynamic bypass până la G5b-2 |
| Router de volatilitate (static/dynamic/realtime) | **G5b-1** | determinist; produs/buget/comandă → bypass |
| Write-back gated async + provenance + migrare schema | **G5b-1** | scrie static; pregătește câmpurile pt dynamic |
| Instrumentare (`cache_lookup`/`cache_write`, hit-rate pe intent) | **G5b-1** | „instrument first" (review §10) |
| **Invalidare** (retrieval-signature price-check + data_version + purjă) | **G5b-2** | DEBLOCHEAZĂ caching-ul de produs (dynamic) SIGUR |
| Caching recomandări de produs (dynamic) | **G5b-2** | activat DOAR după ce invalidarea există |
| Gray-zone verify (cross-encoder / cheap-LLM) | faza 2+ | cârlig; v1 = auto-accept conservator |
| kb_version invalidation, context-fuser, per-user micro-TTL | faza 2+ | cârlige documentate |

**Regula de siguranță:** NU servim conținut `dynamic` din cache până nu există
mecanismul de invalidare (G5b-2). G5b-1 livrabil singur = cache pe tierul static.

## 4. Invalidarea (cerința lui Adi: „update DB → reset cache")

Patru mecanisme, în ordinea importanței pentru noi:

1. **Retrieval-signature cu snapshot de preț (PRIMARĂ, self-healing, automată).**
   La write-back, un entry `dynamic` stochează `retrieval_signature` = lista
   `{product_id, price}` (din `ctx.reply.products` — au deja id+preț din R2) care a
   fundamentat răspunsul. La fiecare candidat de HIT dynamic, re-citim prețurile
   curente ale acelor produse; dacă **oricare diferă** → tratăm ca MISS (învechit) →
   regenerăm (+ ștergem entry-ul). **Efect: scade un preț mâine → următorul lookup
   detectează discrepanța → regenerează cu preț proaspăt. Automat, fără purjă
   manuală, ZERO servire de preț învechit.** Cost: o citire mică (≤6 id-uri, indexat)
   pe calea de hit pt entry-uri dynamic; static sare peste.

2. **`data_version` per business (bulk, ieftin).** Coloană nouă `businesses.data_version`
   (int), incrementată de jobul de sync de catalog (`catalog_sync_runs`) la fiecare
   rulare. Entry-urile dynamic stochează `data_version` la scriere; filtrul cere
   `data_version = curent`. Bump → toate entry-urile dynamic vechi devin **instant
   inaccesibile** (purjate lazy de TTL/cron). Acoperă schimbările în masă (re-sync).

3. **TTL pe volatilitate (backstop).** `expires_at` (există). static: zile;
   dynamic: minute-ore; realtime: necacheat. Cron-ul de purjă există.

4. **Purjă țintită (manual / event).** Funcție de purjă pe business (offboarding) sau
   pe `product_id` (din `retrieval_signature`) la o intervenție manuală.

Pentru „scade un preț → reset", **#1 e steaua** (automat, fine-grained). #2 e plasa
ieftină pentru re-sync în masă. Împreună: zero preț învechit servit vreodată.

## 5. Schema (adăugiri peste `semantic_cache`)

```sql
-- G5b-1 (migrare 006):
alter table semantic_cache
  add column canonical_hash   text,          -- L1 exact (hash al canonical_str)
  add column volatility_class text not null default 'static',  -- static|dynamic|realtime
  add column embedding_model  text not null default 'text-embedding-3-small',
  add column quality_score    real,          -- scorul gate-ului la scriere
  add column is_curated       boolean not null default false;  -- golden, TTL-exempt
create index idx_semcache_exact on semantic_cache (business_id, locale, canonical_hash);

-- G5b-2 (migrare 007):
alter table semantic_cache
  add column retrieval_signature jsonb,       -- [{product_id, price}] grounding
  add column data_version        integer;     -- versiunea de date la scriere
alter table businesses add column data_version integer not null default 1;
```
`query_norm` existent = `canonical_str`. Vectorul rămâne în coloana `embedding`
(HNSW). RLS pe `semantic_cache` (din 003) ne dă pre-filtrarea pe tenant gratis.

## 6. Multi-tenant (deja conform reviewului)

- Pre-filtrare în ANN = `where business_id = $1 and locale = $2` + HNSW (NU
  post-filtrare). RLS pe `bot_runtime` e plasa (NX-50/04) — un filtru uitat = „zero
  rânduri", nu datele altui tenant.
- Fără fallback global de căutare: miss pe tenant → regenerăm, NU lărgim căutarea.
- Fără embeddings/„popular answers" partajate între tenanți.
- Offboarding = `delete from semantic_cache where business_id = $1`.

## 7. Cost (onest pentru un bot de vânzări)

- Cei **40-60% mai puține apeluri de generare** din review sunt un număr de FAQ-bot.
  La noi, tierul scump (mini, produse) e dynamic → economia reală vine din:
  (a) **L1 exact** pe query-uri statice repetate (capul Zipfian) — ieftin, sigur;
  (b) **G5b-2**: recomandări de produs repetate, în fereastra de preț stabil, cu
  invalidare → aici se taie apeluri **mini** (scumpe).
- `cost_per_cache_serve` = embedding (~$0.00001) + query pgvector + (price-check mic).
  Neglijabil față de un tur de agent.
- **Instrument first** (review §10): logăm `from_cache`, `similarity`, exact-vs-semantic,
  hit-rate pe intent ÎNAINTE să calibrăm `τ_high`. Nu promitem un procent; măsurăm.

## 8. Riscuri & mitigări (din review §11, mapate la noi)

| Risc | Mitigarea noastră |
|---|---|
| Contaminare cross-tenant | RLS + business_id + NX-50/04 (structural); fără fallback global |
| Match semantic greșit (numere/entități) | τ_high conservator + buget/numere → bypass semantic (L1 exact only) |
| Preț/răspuns învechit | retrieval-signature price-check (primar) + data_version + TTL + never-cache realtime |
| Hit halucinant | cache DOAR răspunsuri grounded + gate de calitate la write-back |
| Poisoning | write-back async în spatele gate-ului (confident, grounded, non-refuz, non-PII) |
| PII în cache partajat | never-cache personalizat/realtime; corpul nu se loghează (P12) |
| Schimbare model embedding | `embedding_model` în entry; re-embed/invalidare la schimbare |

## 9. Cârlige lăsate pentru faza 2

Gray-zone verify (cross-encoder/cheap-LLM judge) · `kb_version` (FAQ/KB versionat când
`faqs` se populează) · context-enabled fuser (cache „core generic" + injectează prețul
live) pentru recomandări semi-dynamic · per-user micro-TTL pentru personalizat ·
event-driven purge din webhook de sync.
