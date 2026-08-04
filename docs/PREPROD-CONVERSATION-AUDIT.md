# Audit pre-producție — conversații reale pe pipeline (2026-06-19)

Testare „like real life": 15 persona-clienți români au purtat conversații lungi multi-tur
prin pipeline-ul REAL (`handle_turn`, DEFAULT_STAGES, LLM real, DB live demo `nativex-demo`),
via driverul cald `scripts/sim/server.py` (`/turn`, `/trace`, `/substrate`). Transcripturi
brute: `scripts/sim/runs/transcripts-20260619-124527.json`.

Acoperire: **15/15 conversații** (7 în prima rundă; 8 în a doua după hardening-ul serverului contra
capului de 15 sesiuni al pooler-ului Supabase). Transcripturile primei runde: `scripts/sim/runs/`.

**Backlog de reparații (gata de card-uri): [`tasks/NX-PREPROD-FIXES.md`](../tasks/NX-PREPROD-FIXES.md)** (NX-103…NX-111).

**Verdict: NOT READY for production.** Miezul e solid (recomandări grounded, zero halucinări de
preț, rutare de bază, moderare neutră), DAR calea principală de vânzare e ruptă pentru clienții
non-RO, linkul de checkout nu ajunge la client, iar handoff-ul lasă clientul în tăcere totală.

---

## 0. Blockere de infrastructură găsite ȘI rezolvate ÎNAINTE de simulare

Sub rolul REAL de runtime (`bot_runtime` + RLS), pipeline-ul nici nu funcționa. Trei bug-uri
critice, toate care ar fi lovit producția identic (prod folosește aceeași cale `set role
bot_runtime`, `DATABASE_URL_BOT` gol):

| # | Bug | Efect în producție | Fix |
|---|-----|--------------------|-----|
| I1 | `bot_runtime` nu avea `SELECT` pe `intent_aliases` (003 a dat doar INSERT/UPDATE). Agentul citește aliasele aprobate la build-ul de prompt (`agent.py` → `list_routing_aliases`). | `InsufficientPrivilegeError` → **orice mesaj sales/order degrada la fallback generic**. | `GRANT SELECT` — migrare `011` |
| I2 | Politicile de dashboard `admin write faqs` / `admin write aliases` erau `ALL TO public` și referă `business_users` → evaluate și pe SELECT-ul botului → `permission denied for business_users`. | `faq_stage` și `alias_stage` moarte (excepție prinsă → miss pe fiecare tur). | `ALTER POLICY ... TO authenticated` — migrare `011` |
| I3 | Migrarea `010` (indexul `uq_conversations_one_open`) **nu fusese aplicată pe DB-ul live**, dar codul (`get_or_create_conversation`, NX-87) face `ON CONFLICT` pe el. | `InvalidColumnReferenceError` → **primul mesaj al ORICĂRUI client NOU crăpa**. Clienții existenți scăpau (early-return pe `get_open_conversation`). | Aplicat migrarea `010` |
| I4 | Migrarea `012` (`inbound_dedupe.claimed_at`/`completed_at` + grant UPDATE, NX-86) **nu fusese aplicată pe live**, dar `claim_inbound` o cere. | `UndefinedColumnError` la `claim_inbound` (primul pas din `handle_turn`) → **orice mesaj crăpa**. | Aplicat migrarea `012` |

> Drift cod↔migrare (I3) e cel mai periculos: trece de teste (contacte existente) și pică abia la
> un client nou în prod. Recomand un gate CI care verifică toate migrările aplicate pe staging.

---

## 1. Ce MERGE (confirmat pe conversații reale)

- **Zero halucinări de preț/produs.** Toate prețurile/produsele din răspunsuri sunt grounded în
  catalog (validatorul ține). Nici sub abuz, nici la comandă-fantomă nu a inventat status/AWB.
- **Comandă inexistentă gestionată ONEST** (comanda-fantoma): la `AWB123456` fictiv a spus clar
  „nu găsesc nicio comandă", a cerut email/nr comandă — fără tracking inventat.
- **Moderare neutră pe insultă directă** (toxic T2): `message_moderated [harassment]` → „Hai să
  păstrăm conversația respectuoasă" — fără escaladare, fără să intre în joc.
- **Recomandări de produs solide** pe RO: căutare semantică (500 embeddings) → 3 produse cu preț +
  rating + recomandare, follow-up „mai ieftin sub 50 lei" înțeles din context.
- **Gate-ul de handoff nu vorbește peste om** (tehnic corect): după escaladare, `gate_halt` ține
  botul tăcut (nu dublează omul).
- **Rutare de bază OK** pe RO: sales→sales, salut→simple, „vreau operator"→handoff.

---

## 2. Probleme (rankuite după impact pe client real)

### 🔴 F1 — Limba se rupe pe ruta de SALES (HU/EN) — CRITIC (P11)
Pe `route=simple` botul răspunde corect în EN/HU, dar **pe `route=sales` (agentul) revine la
română**, chiar după cereri explicite repetate („MAGYARUL", „english please" ×3).
- Englez T3: „half of it is still in romanian lol"; T5: „now its ALL in romanian again".
- Maghiar: a scris TOT în maghiară de la T1; botul a răspuns aproape mereu în română; la T7 l-a
  trecut la om (în română) probabil fiindcă a insistat pe limbă.
- **Cauză probabilă:** `ctx.language` ajunge la triaj (răspunsul simple îl respectă) dar **agentul
  / prompt builder / compozitorul rich NU îl onorează** — system prompt-ul de agent + scaffolding-ul
  par să fie pe RO implicit.
- **Impact:** orice client non-RO are o experiență ruptă pe exact fluxul de bani. Blocant pentru
  orice piață HU/EN.

### 🟠 F2 — Limbi amestecate în ACELAȘI mesaj — HIGH
Chiar când încearcă EN/HU: numele produselor, linia de sugestii „Poți cere și…" și **disclaimer-ul
AI rămân în română**. Scaffolding-ul rich e hardcodat RO. (Englez T3, Maghiar T3.)

### 🟠 F3 — Nicio comparație reală între produse — HIGH
Cerut explicit să compare 2 produse (comparatie; și englez T4):
- repetă fraze generice identice pentru ambele („oferă confort și hidratare");
- **colapsează la UN singur produs** (462) deși clientul cere comparație cap-la-cap;
- **ignoră de 3× întrebarea „de ce una 58 și alta 88 lei?"**;
- **nu folosește niciodată rating-ul 4.8 vs 4.6** ca argument obiectiv;
- sugerează follow-up („compară ingredientele") pe care apoi nu-l poate face → loop frustrant.
- Parțial gaură de date (cele două „Hydra" sunt aproape duplicate în seed), dar gestionarea
  (repetiție, ignorarea întrebării de preț, colaps la un produs) e logică/prompt. `compare_products`
  pare neapelat sau fără diferențiere reală.

### 🟠 F4 — Linkul de checkout nu ajunge NICIODATĂ la client — HIGH (pierdere directă de vânzare)
Englez era gata să cumpere („ill take the 093, how do i order"). Event-urile arată
`cart_updated` + `checkout_link_created` (de 2×) — dar **niciun link/URL în textul răspunsului**.
Botul a zis generic „urmează pașii din checkout".
- **Cauză probabilă:** `checkout_base_url` gol (fără config comerț) → `checkout_link` întoarce fără
  URL → agentul n-are ce afișa, dar tot pretinde că ajută. Plus compozitorul rich pare să nu emită
  link-ul. **Botul nu trebuie să pretindă un link pe care nu-l poate livra.**

### 🟠 F5 — Tăcere TOTALĂ după handoff — HIGH (UX)
„Tehnic by design" (`gate_halt`), dar dezastruos ca experiență. După escaladare, clientul trimite
4–7 mesaje („alo? e cineva?", „VREAU BANII ÎNAPOI", amenințare ANPC) și primește **ZERO** răspuns —
nici „un coleg îți răspunde în ~X min, te rog așteaptă". Toate personajele escaladate
(comanda-fantoma, retur-suparat, vreau-om, toxic) au trăit „robot mort".
- **Recomandare:** mesaj de holding throttled în handoff activ (confirmare + ETA, o dată la N min
  sau pe primele 1–2 follow-up-uri), și setarea așteptării în mesajul de escaladare.

### 🟠 F6 — Triajul escaladează prea ușor la OM — MEDIUM/HIGH
Ground truth (`/trace`): în ambele cazuri `handoff_requested source='triage'`.
- toxic T4: tirada de preț „31 lei pt o cremă de cacat, mă furați…" → triaj `route=handoff` →
  escaladare la om. Moderarea NU a flagged-o (nu e „harassment" pur), a trecut la triaj care a
  pus-o pe handoff. **Abuzul ar trebui să rămână neutru, nu să consume un operator.**
- retur-suparat T1: „am primit comanda azi și e gresit" → triaj `route=handoff` imediat, **fără să
  ceară nr comandă / ce e greșit / să explice procedura de retur** → apoi tăcere.
- **Recomandare:** tunează triajul să prefere asistarea (order/sales/clarify) și să rezerve
  `handoff` pentru cerere explicită de om / caz cu adevărat sensibil.

### 🟡 F7 — Randare rich ruptă — MEDIUM
- Header comparație rupt: **„Între Hydra și Hydra"** (nume duplicat/trunchiat).
- Chip-uri de sugestii **trunchiate vizibil**: „să le compar dire…", „efectul pe pi…". Cap de
  lungime care taie la mijloc de cuvânt. Arată neprofesionist. (comparatie T2, T5.)

### 🟡 F8 — Latență 9–18s pe ture sales — MEDIUM (UX)
Fiecare tur sales = triaj + tool loop agent + compose rich, secvențial. Pe WhatsApp pare blocat.
- **Recomandare:** paralelizează triaj+retrieval unde se poate, streaming/răspuns parțial, model
  mai rapid pe pasul de compose.

### 🟡 F9 — Disclaimer AI la FIECARE mesaj — MEDIUM (UX/încredere)
„Funcționez cu inteligență artificială…" pe fiecare răspuns sales. Mai multe persona l-au semnalat
ca repetitiv și eroziv pentru încredere.
- **Recomandare:** o dată per conversație (primul mesaj), nu pe fiecare tur. (Art. 50 — de confirmat
  cu legal că o dată/conversație e suficient.)

### 🟡 F10 — Întrebare generală rutată ca lookup de comandă — MEDIUM
Englez „is shipping free or do i pay?" → `route=order` → a căutat o comandă inexistentă pe cont →
„I couldn't verify a recent order". Trebuia răspuns ca FAQ/simple. Agravat de `faqs=0`.

---

### Runda 2 (8 persona) — confirmări + descoperiri noi
- **F4 confirmat ×4** (cadou, ten-gras, englez): `checkout_link_created` fără URL în reply; ten-gras: botul se contrazice „nu am link generat aici". Bucla de bani e ruptă structural.
- **F5/F6 confirmate** (haggler, troll, back-in-stock): tăcere după handoff + escaladare prematură (haggling, opinie politică, presiune da/nu → `handoff source=triage`).
- 🟡 **NOU — Moderare fals-pozitivă pe colocvial RO** (troll „prostiile", ten-gras „lipește-l aici") → `message_moderated` a blocat cereri legitime din același mesaj. → **NX-108**.
- 🟠 **NOU — Buget = filtru moale + drift de categorie la „mai ieftin"** (indecis-lung: cerut <50, primit 60-98 fără să admită; mesaje-fragmentate: „mai ieftin" → **deodorante** + mască 207 lei, a pierdut categoria). → **NX-107**.
- 🟡 **NOU — Summarizer pierde referințele de produs** (indecis-lung, 13 ture): după `summarizer_run` botul nu mai știe prima cremă nici bugetul; retenție OK ~6-8 ture. → **NX-110**.
- 🟡 **NOU — `subscribe_back_in_stock` neinvocat** (back-in-stock): promite urmărire dar nu creează abonare, inventează un „cont/mesaj de abonare", evită întrebarea de stoc. → **NX-109**.
- 🟢 **NOU — ingrediente există în DB dar agentul nu le vede** (ingrediente persona): `product_ingredients`=12.368 rânduri, dar tool-ul nu le expune → gaură de TOOLING, nu de date. → **NX-111**.
- ✅ **Grounding excelent pe ingrediente:** a refuzat de 4× să inventeze compoziții/concentrații (inclusiv un claim fabricat „10% niacinamidă"). Off-topic fără halucinare (oră/scor/politică). De păstrat.

## 3. Găuri de DATE (blochează producția, nu sunt bug-uri de cod)

- **`faqs=0`** → fără răspunsuri curate la livrare/retur/garanție → întrebările cad pe agent/order
  și primesc răspuns vag/greșit (F10). Promisiunea „strat gratuit 40–60%" nerealizată.
- **`orders=0` + fără `checkout_base_url`/config comerț** → fluxurile order-status și checkout nu pot
  fi testate/folosite real; `checkout_link` nu poate produce URL (F4).
- **`intent_aliases` aprobate=0** → stratul alias nu deflectează niciodată (fiecare query plătește
  triaj+agent). Shadow mode n-a populat aliase.
- **`wa_templates` aprobate=0** → proactivul (AWB / back-in-stock / coș abandonat) nu poate trimite.
- **Calitate catalog:** nume templated, aproape-duplicate („Nera Rituals Balance Crema 019", două
  „Hydra" quasi-identice) → fac comparația fără sens și recomandările par false.
- **`vertical='ecommerce'` nu `'beauty'`** → promptul generat e mai puțin specific (concerns/ingrediente).

---

## 4. Recomandări de ARHITECTURĂ / design

1. **Localizare end-to-end a stagiului Agent.** `ctx.language` trebuie să curgă în system prompt-ul
   agentului, compozitorul rich/reco, chip-urile de sugestii ȘI disclaimer. Acum doar triajul îl
   respectă. Cel mai mare impact pentru multi-piață. (F1, F2, F9)
2. **UX de handoff:** politică de mesaj de holding în handoff activ (confirmare + ETA, throttled) în
   loc de tăcere; setarea așteptării în mesajul de escaladare. (F5)
3. **Capabilitate de comparație:** `compare_products` să diferențieze real (preț, rating, atribute/
   ingrediente cheie) și să NU colapseze la un produs când userul a numit două. (F3)
4. **Bucla de comerț:** când `checkout_base_url` lipsește, botul să NU pretindă că a creat un link;
   ori livrează link real, ori „checkout nu e disponibil aici, uite cum comanzi". Repară compozitorul
   care pierde link-ul. (F4)
5. **Triaj vs escaladare:** abuzul rămâne în banda moderare-neutru; tirada/plângerea ≠ cerere de om.
   Tunează triajul + ordinea moderare/risc. (F6)
6. **Latență:** paralelizare triaj+retrieval, streaming, model rapid pe compose. (F8)
7. **Render rich:** repară trunchierea chip-urilor și header-ul duplicat. (F7)
8. **Capacitate DB:** pool-urile prod (bot 10 + admin 10 = 20) DEPĂȘESC capul pooler-ului Supabase
   în session mode (15 clienți) → risc `EMAXCONNSESSION` sub sarcină. Aliniază mărimile sau treci pe
   transaction-mode pooling. (observat ca artefact de harness sub 15 persona concurente)

---

## 5. Quick wins (ieftine, impact mare)

- Disclaimer o dată per conversație, nu pe fiecare mesaj. (F9)
- Localizează disclaimer + chip-uri + scaffolding reco după `ctx.language`. (F2)
- Nu pretinde link de checkout când nu se produce niciunul. (F4)
- Fix la cap-ul de lungime care trunchiază chip-urile de sugestii. (F7)
- Mesaj de holding la primul follow-up după escaladare. (F5)
- Seedează `faqs` (livrare/retur/garanție) + câteva aliase aprobate → activează stratul gratuit. (F10)

---

## Note despre harness (NU sunt bug-uri de producție)

- **HTTP 500 intermitent în test** = `EMAXCONNSESSION (pool_size: 15)` — pooler-ul Supabase în
  session mode, lovit de 15 persona concurente × 2 conexiuni/request (admin + tenant) + pool-urile
  calde. Secvențial nu apare. Personajul „toxic" a corelat 500 cu cuvinte vulgare — coincidență
  (ferestre de concurență mare). Vezi totuși recomandarea de capacitate (§4.8).
- Personajul descrie uneori răspunsul botului în loc de citat verbatim; `/trace` rămâne sursa
  autoritară (mesaje + timeline de evenimente).
