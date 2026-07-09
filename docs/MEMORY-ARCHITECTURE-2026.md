# Arhitectura memoriei clientului — generic pe orice business (2026-07)

Status: **PROPUNERE de design, pre-implementare.** Cere părere Codex (secțiunea finală).
Autor: Claude. Context: [CLAUDE.md](../CLAUDE.md), [AGENT-ARCHITECTURE.md](AGENT-ARCHITECTURE.md),
audit memorie 4-straturi (evaluare externă 6.8/10).

---

## 0. De ce documentul ăsta

O simulare pe cod real (apel nano real + whitelist real + DB live) a scos două lucruri:

1. **Feature mort în live:** migrările `022`+`023` NU sunt aplicate pe Supabase (aplicate doar
   003→021). `conversation_facts` nu există → `upsert_facts` crapă cu `UndefinedTableError`,
   înghițit tăcut de savepoint-ul best-effort din
   [processor.py:307](../src/worker/processor.py#L307). Niciun fact nu se persistă vreodată.
   → *fix trivial de deploy: `scripts/migrate.py`. Nu e subiectul acestui doc.*

2. **Design care nu scalează la „orice business":** extractorul nano a emis `budget_max_lei`,
   `preferred_brand`, `fragrance_free_preference`; whitelist-ul beauty voia `budget_band`,
   `fav_brands`, `restriction`. `select_whitelisted_facts` e **fail-closed** →
   **4 din 5 facts aruncate**. → *ăsta e subiectul: arhitectura.*

Documentul propune o memorie **generică prin construcție** — business nou = memorie funcțională
din ziua 1, fără curatare manuală — păstrând garanțiile de PII / anti-halucinație.

---

## 1. Problema, formulată general

`fact_type_whitelist` ([beauty_salon.json:32](../src/domain/defaults/beauty_salon.json#L32)) e o
listă **pozitivă, per-vertical, scrisă de mână**. Cu `select_whitelisted_facts` fail-closed
([facts.py:48](../src/db/queries/facts.py#L48)), sistemul reține **doar ce a prezis un om în
avans**. Trei defecte structurale pentru o agenție multi-tenant:

- **Cold-start per client** — brutărie, sală de fitness, cabinet avocatură: fiecare vertical nou
  cere cuiva să scrie cheile. Zero config = zero memorie.
- **Predicție a viitorului** — reții doar ce ai anticipat; orice relevant neanticipat = aruncat.
- **Matching fragil** — modelul trebuie să nimerească cheia exactă snake_case (l-am văzut ratând).

---

## 2. Intuiția centrală: memoria are DOI consumatori cu nevoi OPUSE

Designul actual îi confundă într-un singur tabel gated de un singur whitelist:

| Consumator | Ce citește | Ce vrea de la vocabular |
|---|---|---|
| **MODELUL** (recall injectat în prompt, `facts_block`) | limbaj natural | **DESCHIS** — generalitate: să prindă orice a spus clientul. Whitelist-ul aici e *dăunător*. |
| **CODUL** (filtre search, dimensiuni analytics, export CRM, `contacts.profile`) | chei structurate | **ÎNCHIS/canonic** — chei stabile pe care codul determinist se poate baza. |

Whitelist-ul unic servește al doilea consumator și sabotează primul. „Orice business" pune
greutatea pe primul. **Soluția = separă cei doi consumatori în două straturi.**

---

## 3. Design: două straturi

### Strat A — Memorie deschisă a clientului (pentru MODEL)
- **Scop:** recall injectat în promptul agentului. Modelul consumă limbaj natural → cheile pot fi
  libere.
- **Siguranța prin INVERSIUNE** (blocklist + igienă, nu allowlist):
  - **filtru negativ pe CATEGORII periculoase**: contact-PII (telefon, email, nume, adresă),
    financiar (IBAN, card, CNP), și **condiții medicale/sănătate** (leg P0-safety —
    `safety_medical_guardrail`). Detectat pe *forma valorii* (regex, avem deja `_PHONE_RE`) +
    *numele fact_type* (blocklist) + instrucțiunea anti-PII din promptul extractorului.
  - **igienă generică:** `fact_type` snake_case scurt; `fact_value` scalar/listă scurtă ≤120ch;
    `confidence`∈[0,1]; **cap N** per contact cu evicție (vezi §6).
  - **redactare recursivă a valorii** rămâne ([facts.py:36](../src/db/queries/facts.py#L36)).
- **Garanția anti-halucinație nu se pierde — se mută:** de la „doar chei prezise" la „doar ce a
  spus clientul explicit, scalar scurt, cu confidence". Promptul deja cere asta.
- **Blast radius mic:** Stratul A e citit DOAR de model ca context, niciodată de cod pentru
  decizii → o intrare proastă are impact limitat.

→ Ăsta e stratul care face sistemul generic. Brutăria reține `gluten_free`, avocatul reține
`case_type`, fără ca nimeni să scrie o listă.

### Strat B — Atribute canonice (pentru COD)
- **Scop:** consumatorii determiniști — filtre search, analytics, CRM, `contacts.profile`.
- **Vocabular DERIVAT, nu scris de mână** (Principiul 9 — „promptul se generează din DB"):
  - din **axele filtrabile ale catalogului** businessului: `searchable_facets`
    ([beauty_salon.json:94](../src/domain/defaults/beauty_salon.json#L94)) + atributele pe care
    `search_products` le poate filtra + axele de categorie. **Nuanță importantă:** NU tot
    `products.attributes` — alea sunt atribute de *produs*, nu de *client*. Un fapt de client se
    leagă de catalog ca „vreau produse CU atributul X" → deci vocabularul canonic de client =
    exact **atributele filtrabile** (cele pe care o preferință se poate agăța).
  - plus un **nucleu universal** mic, valabil pe orice comerț: `budget_band`, `fav_brands`,
    `restriction`, `size`.
- **Canonicalizare:** un map (seed + învățat) mapează cheile libere din Stratul A → chei canonice
  când se aliniază (`preferred_brand`→`fav_brands`, `budget_max_lei`→`budget_band`). Exact
  mecanismul `concern_map` ([beauty_salon.json:4](../src/domain/defaults/beauty_salon.json#L4)),
  generalizat.

---

## 4. Model de date

Refolosim `conversation_facts` (023), cu **o coloană nouă** în loc de un al doilea tabel:

```
conversation_facts
  ... (existent: business_id, contact_id, conversation_id, fact_type, fact_value,
       confidence, source_message_id, first_seen_at, last_seen_at, expires_at)
  + canonical_key text NULL   -- rezolvat de canonicalizare; NULL = fapt liber (doar Strat A)
```

- `fact_type` = ce a emis modelul (Strat A, brut).
- `canonical_key` = slotul canonic rezolvat (Strat B), sau NULL.
- **`contacts.profile`** devine *view materializat* al Stratului B: subsetul cu `canonical_key IS
  NOT NULL`, ultima valoare high-confidence per `canonical_key`. Rezolvă suprapunerea profile↔facts
  pe care am semnalat-o (azi sunt două sisteme paralele fără politică de reconciliere).

---

## 5. Fluxul post-tur

1. **Extracție nano** (deja există, [profile.py:185](../src/worker/profile.py#L185)) — dar promptul
   se schimbă: în loc de whitelist fail-closed, dăm modelului **ghidaj**: „preferă aceste chei
   canonice dacă se potrivesc: {canonical_keys}; altfel emite cheie liberă descriptivă". Modelul
   aterizează natural pe canonic când poate (reduce fragmentarea), dar nu pierdem ce nu se
   potrivește.
2. **Filtru siguranță/igienă** (negativ) → Stratul A persistat.
3. **Canonicalizare** → setează `canonical_key` unde se aliniază → update `contacts.profile`.
4. **Retrieval** (turul următor): pentru prompt, fetch Strat A pe relevanță (§6); pentru
   search/filtre, citește Strat B canonic.

---

## 6. Retrieval / relevanță (gap-ul „recall inteligent")

- **Azi:** `order by confidence desc, last_seen desc, cap`
  ([facts.py:128](../src/db/queries/facts.py#L128)). Vertical-agnostic, ok ca baseline.
- **Îmbunătățire fără vector:** relevanță la turul CURENT = boost pe facts al căror `fact_type`/
  valoare se suprapun cu route/category/query-ul curent (determinist, ieftin, cod nu LLM). „cremă
  de noapte" → boost `skin_type`/`restriction` peste un fapt irelevant.
- **Evicție la cap:** low-confidence + oldest `last_seen` (blend). De decis (§ Codex).
- **Vector memory = Strat 3, DEFERRED** — auditul însuși recomanda să nu sărim la el. Rămânem pe
  facts structurate + retrieval determinist; semantic recall doar dacă volumul per contact o cere.

---

## 7. Migrare / compatibilitate

- Aplică `022`+`023` (tabela există). Fără asta nimic nu contează.
- Adaugă `canonical_key` (migrare nouă, aditivă).
- Whitelist-ul existent NU dispare — devine **seed-ul vocabularului canonic + țintele de
  canonicalizare**, nu o poartă fail-closed. Zero breaking change pe verticalele existente.
- `select_whitelisted_facts` fail-closed → înlocuit de `safety_filter` (negativ) + `canonicalize`.

---

## 8. Riscuri / puncte slabe (onest)

- **Inversarea allowlist→blocklist mărește suprafața.** Miza P0 (PII + medical, răspundere
  juridică) e reală. Blocklist-ul trebuie airtight, iar „open capture" ar putea stoca facts
  sensibile-dar-ne-PII („însărcinată", „diabet") = date de sănătate. Trebuie decis dacă le blocăm
  total sau le stocăm-dar-nu-le-expunem.
- **Fragmentarea** (`fav_brand` vs `preferred_brand` la doi tenanți) rămâne pentru **analytics
  agregat**, nu pentru recall per-client. E izolată în Stratul B; canonicalizarea o rezolvă, dar
  poate cere un job offline de merge.
- **Catalog ≠ preferințe client** — atributele de produs nu-s 1:1 cu faptele de client; de-aia
  Stratul B derivă din *facets filtrabile* + nucleu universal, nu din tot `attributes`. Rămâne de
  validat că e suficient.

---

## 9. Pentru Codex — întrebări de arhitectură pe care vrem părerea ta

Nu e un PR de verificat; e un **design review**. Te rugăm o părere critică pe:

1. **Siguranță: inversarea allowlist→blocklist e acceptabilă** dat fiind P0 medical/PII
   (răspundere juridică)? Unde tragi linia? Facts de condiție medicală: blocate total, sau
   stocate-dar-niciodată-expuse modelului?
2. **Un tabel + `canonical_key`** vs. două tabele (memorie brută vs. profil canonic)? Care e mai
   curat pe termen lung pentru RLS + analytics?
3. **Controlul fragmentării:** canonicalizarea ghidată de prompt e suficientă, sau trebuie un job
   offline periodic de merge/reconciliere a cheilor?
4. **Evicția la cap** (N facts/contact): lowest-confidence, oldest last_seen, sau blend? Există
   risc să evicționăm un fapt stabil valoros pentru unul recent zgomotos?
5. **Derivarea Stratului B din catalog:** „facets filtrabile + nucleu universal" e sursa corectă,
   sau ne mințim că product-attrs ≈ customer-facts? Alternativă: config explicit per business?
6. **Relevanța la recall:** boost determinist pe turul curent merită complexitatea la MVP, sau
   confidence/recency e destul până apare volum real?
7. **Ceva ce am ratat** în tensiunea generic-vs-safe? Modul în care alte sisteme de memorie de
   agent (2026) rezolvă cold-start-ul fără să sacrifice PII?

Formatul preferat: verdict pe fiecare punct + orice risc pe care designul nu-l acoperă.
```
git fetch origin && git worktree add ../verify-mem origin/<branch> && cd ../verify-mem
# doc: docs/MEMORY-ARCHITECTURE-2026.md
```
