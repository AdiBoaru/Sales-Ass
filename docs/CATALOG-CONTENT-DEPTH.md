# Catalog v3.5 — Adâncimea de conținut (delta peste Catalog v3)

> **Nu e un contract nou.** Contractul de produs există și e livrat:
> [`docs/CATALOG-PRODUS-V3.md`](CATALOG-PRODUS-V3.md) + NX-168d…NX-172 (migrările 026–029).
> Acest document acoperă **singura zonă pe care v3 a lăsat-o intenționat neatinsă: conținutul.**
> v3 a construit scheletul (fapte canonice, relații, quality gate, embeddings versionate) și a
> populat carnea cu „safe autoring" — propoziții scurte, verificabile, ca să treacă audit-gate-ul
> fără halucinații. Decizia a fost corectă. Rezultatul e că produsul citește ca un tabel, nu ca
> o fișă de magazin.

## 1. Starea măsurată (live, tenant demo `nativex-demo`, 2026-07-22)

Catalogul **servit** = 150 produse (`schema_version=3`, `content_status='published'`,
`content_status_filter=true`). Cele 504 vechi sunt `archived` + `draft` → nu ajung la client. ✅

| Dimensiune | Măsurat pe cele 150 | Țintă |
|---|---|---|
| `description` | **mediană 163 car., max 233** | 1.500–4.000 |
| `short_description` | ~95 car. | 150–250 |
| `ai_summary` | uneori **o singură propoziție** copiată din `key_benefit` | 200–400, compus determinist |
| secțiune `benefits` | 150/150, dar **mediană 91 car.** | 4–6 bullets reale |
| secțiune `usage` | **65/150**, mediană **23 car.** | 150/150, 3–5 scenarii |
| secțiune `ingredients` | 83/150, mediană 32 car. | pe categoriile relevante |
| **FAQ per produs** | **inexistent** (n-are tabel) | 3–6 per produs |
| imagini | 150/150, dar **max 1 poză/produs** | 4–6, cu tip (textură/aplicare/…) |
| variante | 46/150 | toate produsele cu ml/nuanțe |
| `concerns` | 74/150 | ≥95% pe categoriile relevante |
| `not_recommended_for` | **0/150** | contractul v3 îl cere; gate-ul NX-170 n-are pe ce lucra |
| `differentiators` | **4/150** | 150/150 (e baza pentru `compare_products`) |
| `spf` | 7 | toate produsele cu SPF |
| relații (`product_relations`) | **957 rânduri** ✅ | ok |

**Exemplu real, integral, al unui produs „published":**

```
name:              NudeLab Ink Tuș de ochi lichid
short_description: Tuș lichid cu vârf de pâslă flexibil; linie intensă dintr-o singură trecere.
description:       NudeLab Ink Tuș de ochi lichid. Linie precisă și intens pigmentată, nu se
                   decojește pe parcursul zilei. Recomandat pentru conturarea privirii.
ai_summary:        Recomandat pentru conturarea privirii.
attributes:        best_for, key_benefit, routine_step, fragrance_free   (4 chei)
```

`ai_summary` — **textul pe care se face embedding-ul și ranking-ul** — are șapte cuvinte, copiate
din `key_benefit`. Un client care întreabă „ceva pentru ochi care rezistă la transpirație" nu are
cum să nimerească produsul ăsta: nu există text pe care să se potrivească.

**Diagnostic:** nu e o problemă de schemă și nici de motor. E **lipsă de conținut**. Un produs de
al nostru are ~10–15% din substanța unei fișe reale de magazin.

---

## 2. Ce lipsește ca infrastructură (puțin — restul există)

| # | Lipsă | Propunere |
|---|---|---|
| 1 | **FAQ per produs** — `faqs` e la nivel de business, fără `product_id`. „Se poate folosi cu retinol?" n-are casă | tabel nou `product_faqs` (migrarea 032) |
| 2 | **Proveniența textului** — nu se poate distinge textul producătorului de ce poate afirma botul | `product_sections.voice ∈ {brand, assistant}` |
| 3 | `product_sections` **n-are `business_id`** → fără tenant-scope, fără RLS, fără `locale` | coloane + backfill (032) |
| 4 | `product_images` n-are **tip** → nu putem cere „arată-mi textura", iar cardul ia orb prima poză | `kind ∈ {main, texture, application, before_after, ingredient, packaging}` |
| 5 | `restock_date` — la stoc epuizat botul nu poate spune când revine | coloană pe `products` |

Restul (relații, `net_content`, `price_per_unit`, `content_status`, embeddings versionate) **există deja.**

---

## 3. Regula care face conținutul bogat SIGUR: „voice"

Motivul pentru care v3 a scris texte de 163 de caractere e real: un text de producător de tip
*„repară bariera cutanată în 3 zile, recomandat de dermatologi, hipoalergenic"* este:

- **legitim de afișat**, atribuit brandului — asta face orice magazin;
- **interzis de afirmat de bot** — `has_medical_claim` (stagiul 8) îl taie, pe drept: e răspundere juridică.

Soluția nu e text sărac, ci **proveniență**:

```
voice='brand'      → text al producătorului. Afișabil pe card. Citabil DOAR atribuit
                     („producătorul îl descrie ca…"). NU intră în embedding.
voice='assistant'  → botul îl poate afirma direct. Trece prin has_medical_claim la INGESTION;
                     un bloc care pică verificarea NU se scrie în DB.
```

Verificarea se mută de la runtime (unde produce răspunsuri ciuntite și fallback-uri) la ingestion
(unde e ieftină, vizibilă și reparabilă de om). Precedent existent: `attributes.claim_provenance`
(83 produse).

Așa putem avea descrieri de 3.000 de caractere **fără** să crească riscul de claim.

---

## 4. `ai_summary` se compune determinist, nu se copiază

`ai_summary` nu e text de marketing — e textul pe care se face potrivirea. Se generează din fapte:

```
{name} — {categorie}. {short_description}
Potrivit pentru: {suitable_for} · {concerns}.
Ingrediente-cheie: {key_ingredients}. {texture}, {net_content}. {finish}/{coverage}/{spf}
```

Blocurile `voice='brand'` nu intră în embedding (altfel ranking-ul învață marketing, nu potrivire).
`content_hash` pe textul compus → re-embed doar la schimbare reală. NX-170 a livrat deja
`_embed_text` determinist — aici doar îl alimentăm cu conținut care există.

---

## 5. Feliile propuse

| # | Felie | Conținut | DDL |
|---|---|---|---|
| A | **DDL conținut** | `product_faqs`, `sections.business_id/locale/voice`, `images.kind`, `products.restock_date` | **032** |
| B | **Umplerea faptelor** | `concerns` 74→150, `differentiators` 4→150, `spf`, `coverage`, `not_recommended_for` (cu motiv+sursă, conform contractului v3) | nu |
| C | **Autoring de conținut** | descrieri 1.500–4.000 car. (`voice='brand'`), `benefits`/`usage`/`scenarios` reale, 3–6 FAQ per produs, 4–6 poze cu `kind` | nu |
| D | **Read-path** | `get_product_details` citește secțiuni + ingrediente + **FAQ** + badges; `search_products` întoarce facetele-cheie; card web primește pozele tipizate | nu |
| E | **Gate** | audit v3 extins: lungime minimă text, ≥3 FAQ, zero claim medical în `voice='assistant'`, zero „produs fictiv"; `ai_summary` recompus + re-embed | nu |

**Ordinea contează: A → B → D → C → E.** Conținutul (C) se scrie ultimul dintre cele grele,
altfel îl scriem a doua oară. D înaintea lui C ca să vedem imediat efectul pe conversație.

**Efortul real e la C** — 150 de produse × conținut de fișă adevărată. Ăsta nu e „safe autoring"
din atribute, e scris de conținut. Fie cu un pipeline LLM dedicat cu verificare la ingestion,
fie hand-authored pe top 30 și pipeline pe restul.
