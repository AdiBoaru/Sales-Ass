# iZi (eMAG) vs. Nativx „Aria" — diferența de calitate pe un tur real

**Data:** 2026-06-29
**Autor:** analiză de arhitect (Claude Code), la cererea lui Adi
**Metodă:** același input pe ambele boturi, comparație de comportament observat (nu speculație).
**Trigger:** „de ce iZi răspunde așa de bine față de noi?"

> **Status: document de analiză (referință), NU implementare.** Recomandările (A/B/C)
> intră în backlog ca taskuri separate. Complementar cu
> [`PRODUCT-RANKING-ANALYSIS-2026.md`](PRODUCT-RANKING-ANALYSIS-2026.md) (ranking — P0 livrat
> în PR #140).

---

## Inputul comun

> **„Mai aveti pe stoc crema X? O iau acum."**

Mesajul are **3 intenții împachetate**: (1) un produs *numit* („crema X"), (2) verificare de
*stoc*, (3) *cumpărare imediată* („o iau acum"). Calitatea răspunsului = cât de bine sunt onorate
toate trei + cât de onest e botul când produsul numit nu există.

---

## Ce a făcut iZi (eMAG)

1. **Onestitate pe numele cerut:** *„Am găsit mai multe creme … similare cu «crema X», toate
   disponibile acum, dar **nu există un produs cu numele exact «crema X»**."* — recunoaște explicit
   că entitatea numită nu există, apoi pivotează la alternative.
2. **Onorează „o iau acum":** fiecare card arată *„Comandă până la 18:00: **livrare mâine**"* +
   *„-100 Lei în Coș"* + chip *„adaug-o în coș"* — adresează stoc + imediatețe + drum spre coș.
3. **Coaching înainte de pick:** paragraf despre cum alegi (tip de ten, zonă, ingrediente cheie,
   cantitate/preț).
4. **Recomandare grounded + ofertă de rafinare:** *„recomand [CeraVe 340 g] **pentru că** oferă
   mult produs, e formulat pt piele uscată și are recenzii foarte bune. **Dacă nu aceasta era «crema
   X», spune-mi ce brand/ambalaj are și ajustez.**"*
5. **Motive de card concrete:** *„cremă mare față+corp, foarte hidratantă, cu **ceramide și acid
   hialuronic**"* — fapt real, nu reformulare.
6. **Semnale de comerț bogate:** badge-uri variate, **-19% / -23% / -27%**, rate 0%, vouchere.

## Ce a făcut Aria (noi)

1. **Intro generic, fără disclosure:** *„Mai avem opțiuni pentru cremă, și o poți lua acum."* — nu
   recunoaște că „crema X" nu există ca atare.
2. **„o poți lua acum"** — vag; fără ETA de livrare, fără push concret spre coș.
3. **Coaching prezent** (bun, paritate cu iZi): *„Alege crema după nevoia principală a tenului…"*.
4. **Pick cu rationale subțire / ungrounded:** *„Velvet Root … **pentru ten sensibil**"* — dar
   clientul **nu** a spus *ten sensibil* (nevoie inventată); fără ofertă de rafinare pe identitatea
   lipsă.
5. **Motive tautologice:** *„pentru ten uniform — **uniformizează** aspectul tenului"* (gol).
6. **Date uniforme:** **toate cardurile 4.8★** și **«Top Favorit»** pe 4/5 → *badge blindness*;
   nume cu ID rezidual *„…328 / …003 / …029"*.

---

## Tabel sintetic

| Dimensiune | iZi | Aria | Tip gap |
|---|---|---|---|
| Produs numit inexistent | disclosure explicit + pivot | gloss generic | **A (cod)** |
| „cumpăr acum" (stoc/ETA/coș) | livrare mâine + coș + chip add | „o poți lua acum" vag | **A (cod)** |
| Pick | grounded pe atribute + ofertă rafinare | nevoie inventată, fără rafinare | **A (cod)** |
| Motive de card | concrete (ingrediente/uz) | tautologice | **A (cod)** |
| Nume produs | reale | cu ID rezidual „…328" | **B (date)** |
| Rating-uri | variate (4.66–4.89) | toate 4.8 → badge pe tot | **B (date)** |
| Livrare/ETA | reală (logistică eMAG) | inexistentă în seed | **B (date)** |
| Rate 0% / vouchere / Smart Deals | da (platformă eMAG) | n/a | **C (platformă)** |

---

## Cele 3 categorii de cauze (cheia analizei)

### A. Gap de COMPORTAMENT (cod — astea sunt „creierul")
1. **Disclosure pe produs numit inexistent.** Azi avem disclosure DOAR pe *brand* inexistent
   (`src/tools/catalog_tools.py:426` → „Nu am găsit niciun produs de la brandul «X»"; prompt
   `src/agent/prompt_builder.py:74`). NU avem echivalent pentru un *named-entity* generic („crema
   X"): triajul rutează SALES → search semantic → afișează similare FĂRĂ să spună că numele exact
   lipsește. iZi face exact acest disclosure. → extindem `had_any_match`/semnalul de tool la
   „named-product-not-found" + instrucțiune de prompt.
2. **Onorarea intenției „cumpăr acum".** Semnal de purchase-intent → arată stoc/ETA + împinge spre
   coș. Avem deja toolurile (`delivery_eta`, `cart_add`, `checkout_link`) dar nu le legăm de acest
   semnal.
3. **Pick grounded + ofertă de rafinare.** Pick-ul justificat din atribute REALE (nu o nevoie
   inventată ca „ten sensibil") + „dacă nu asta era, spune-mi brandul/ambalajul". PR #140 a făcut
   pick-ul **determinist** (top-ul clasat); rămâne să întărim *justificarea* să fie grounded +
   hedge-ul de rafinare.
4. **Motive de card ne-tautologice.** „uniform — uniformizează" e gol; ancora trebuie să fie un
   fapt real (ingredient/uz) — disciplină de prompt + scrub (avem deja ancora pe `top_pros`; aici
   datele lipsesc, vezi B).

### B. Gap de DATE (seed/sync — NU cod) — *partea mare din „magia" iZi*
- **Nume reale** (ale noastre au ID rezidual „…328").
- **Rating-uri variate** (toate 4.8 → badge-ul «Top Favorit», prag ≥4.7 & ≥50 recenzii, se aprinde
  pe aproape tot → *badge blindness*; analiza de ranking cere 15–25% din catalog cu badge).
- **Livrare/ETA reală**, **promoții reale**.
- **Consecință critică:** PR #140 (ranking blended) **nu se vede** când toate rating-urile sunt
  4.8 — departajarea pe nr. recenzii (176 > 141 > 123) ar reordona, dar uniformitatea ascunde
  câștigul. **Ca să se vadă motorul, trebuie date variate.**

### Anatomia unui card iZi: ce e DATE de card, nu AI

Insight (Adi, 2026-06-29): semnalele bogate de pe cardul iZi **apar deja pe cardul/PDP-ul
produsului** — iZi doar le randează, nu le „gândește". Maparea unui card real
(„La Roche-Posay Cicaplast B5+ 100 ml") pe contractul nostru de randare
([`FRONTEND-CONTRACT-IZI.md`](FRONTEND-CONTRACT-IZI.md)):

| Linie pe card iZi | Câmp | La noi |
|---|---|---|
| nume (Cicaplast B5+ 100 ml) | `name` | ✓ (sintetic, cu ID rezidual) |
| `4.75` | `rating` + `review_count` | ✓ |
| `7449 → 5399 Lei` | `list_price` → `price` | ✓ (strikethrough în contract + floor) |
| „Comanda până la 18:00: **livrare mâine**" | delivery cutoff + ETA | ✗ **câmp nou** |
| „**-100 Lei în Coș**" | promo extra la coș (voucher) | ✗ **câmp nou** |

**Concluzie:** din 5 semnale, **3 le avem deja**; lipsesc **2 câmpuri de card** — *delivery
cutoff/ETA* și *voucher la coș* — **ambele DATE/integrare, nu AI**:
- **delivery „livrare mâine"** = cutoff orar + locație stoc + curier (tool-ul `delivery_eta` există
  dar cere adresă; pe card e o regulă „comandă până la X → mâine" din logistică reală).
- **„-100 Lei în Coș"** = model de promoție la coș (extra-discount aplicat în coș) — n-avem câmpul.
  Adăugare = câmp în product data model + sync + **update spec render + FE** (contractul iZi).

→ Întărește B: paritatea de card vizuală = **2 câmpuri noi de date + render**, nu logică de „creier".

### C. Specific eMAG (NU copiem orbește)
Rate 0% dobândă, vouchere eMAG, taxonomia „Smart/Multi Deals" — depind de platforma de comerț a
eMAG. Noi putem face discount% și promoțiile retailerului; finanțarea/voucherele cer integrarea lui.

---

## Citirea de arhitect (onest)

Motorul nostru e **comparabil arhitectural** — avem carduri, badge derivat, coaching, chips,
comparație, și ranking blended + pick determinist (PR #140). Diferența reală de *comportament* e
**îngustă**: iZi **parsează intenția completă și e onest când nu găsește**; noi aplatizăm și trecem
peste. Cea mai mare parte din „iZi arată mai bine" e **DATE**, nu creier.

**Concluzie de prioritizare:**
- **B (date) are cel mai mare raport impact/efort vizual** — fără el, nici munca de ranking deja
  livrată nu se vede. Nu e cod, e seed/sync.
- **A (comportament)** e gap-ul real de „creier", îngust și bine definit (4 itemi), aliniat cu P1
  din analiza de ranking (onestitate + relaxare).
- **C** rămâne opțional, dependent de integrarea retailerului.

Recomandare: **B + A1 (disclosure named-entity) întâi** — împreună închid ~80% din diferența
percepută pe acest tip de tur.

---

# RUNDA 2 (2026-07-02) — conversație MULTI-TUR pe web widget

**Metodă:** același scenariu de 4 tururi pe ambele boturi (web widget Aria vs iZi):
(1) „ser cu vitamina C sub 150 lei" → (2) „am tenul mixt, gras în zona T" →
(3) „spune-mi mai multe despre primul" → (4) „adaugă-l în coș și dă-mi link de plată".
Runda 1 a testat UN tur; runda 2 testează **memoria, adâncimea și bucla de bani** — și
scoate findings NOI, invizibile pe un singur tur. Build-ul testat include #168/#169
(formulare consultativă vizibilă, fără „👉 Recomandarea mea").

## Tur cu tur

| Tur | iZi | Aria | Gap |
|---|---|---|---|
| 1. vit. C <150 | 7 seruri, TOATE cu vitamina C (10–15%), coaching pe concentrație, pick grounded (CeraVe „pentru că are 10%, bine tolerat") | 3 seruri generice, **niciunul cu vitamina C**, dar intro-ul PRETINDE „seruri potrivite pentru un ser cu vitamina C" | **A+B**: gate-ul de onestitate NU s-a aprins pe feature lipsă; catalogul n-are vitamina C |
| 2. ten mixt/gras | păstrează stiva (vit. C + buget + ten mixt), **re-rankează ACELAȘI set**, explică per produs (niacinamidă pt sebum) | **pierde constrângerea vitamina C**, re-search de la zero, întoarce un **TONER** ca top card; gate-ul „nu am exact ce cauți" s-a aprins AICI (cerere satisfiabilă) — invers decât la turul 1 | **A**: fără constraint stacking; gate pe turul greșit |
| 3. detalii primul | detaliu real: textură, când se folosește (dimineața sub SPF), **sinteză de recenzii** („Din ce spun utilizatorii…"), chips de utilizare | meta-narare („Am ales să-ți spun mai multe…"), repetă cardul + ACELAȘI șablon de coaching, ZERO fapte noi, referință suspendată („un alt produs… poate fi mai potrivit" — nenumit) | **A+B**: turul de detaliu nu comută pe template de detaliu; ingrediente/review summaries goale pe demo |
| 4. coș + link plată | **adaugă în coș**, explică flow-ul de plată, apoi **cross-sell de rutină** (retinol seara + SPF) | refuză („nu pot adăuga în coș sau genera link de plată"), apoi chip **„Adaugă-l în coș"** — contradicție directă | **A+config**: `checkout_link` → `no_checkout_url` (`commerce_tools._checkout_base`; demo n-are `checkout_url`); `cart_add` nu cere URL dar agentul a generalizat refuzul |

## Findings NOI (neacoperite de runda 1)

1. **P0 — gate-ul de relevanță e INVERSAT pe acest scenariu.** Tace pe turul 1 (feature
   „vitamina C" inexistentă în rezultate → pretinde potrivire) și se aprinde pe turul 2
   (cerere satisfiabilă). Extensia lui A1: disclosure trebuie să prindă și *feature/atribut
   cerut explicit*, nu doar named-entity/brand.
2. **P0 — bucla de bani moartă pe web.** ~~Root cause CONFIG~~ **[CORECTAT la implementare
   NX-137, diagnostic live pe sim]:** configul ERA seedat; cauzele reale: (a) linkul de
   checkout creat cu succes **nu ajungea niciodată în reply pe calea rich** (regulile rich
   interzic linkuri în proza modelului, nimeni nu-l atașa) → fix `Offer(open_url)`; (b)
   modelul uneori NU chema `checkout_link` deși clientul îl cerea explicit (cross-sell
   deturna turul) → fix fallback determinist pe `purchase_intent`; (c) BONUS descoperit:
   FAQ + semantic_cache erau moarte TĂCUT la runtime (DataError de codec pgvector — dublă
   encodare `_vec()` vs codec NX-113c) → fix codec tolerant. Agravantele de copy/chips
   rămân reale și fixate (llm_view instructiv + notă anti-contradicție în compunere).
3. **P1 — fără stivă de constrângeri multi-tur.** Fiecare tur re-caută de la zero; rafinarea
   „ten mixt" a șters „vitamina C" și a adus alt TIP de produs (toner în conversație de ser).
   iZi acumulează constrângeri și re-rankează setul deja arătat.
4. **P1 — șablonul consultativ (#169) e repetat VERBATIM 4/4 tururi** („La un X, uită-te
   la… Ți-aș recomanda în primul rând Y, pentru că… Dacă vrei Z, W e o alegere bună") —
   vizibil ca template de la al 2-lea mesaj. iZi variază structura pe stadiu (rafinare →
   comparație → detaliu → rutină). Turul de detaliu NU trebuie să repete coaching-ul.
5. **P2 — proza și cardurile diverg.** La turul 2 proza recomandă „Fresh Serum" #1, dar
   cardul top (Top Favorit) e Tonerul. Pick-ul determinist și ordinea cardurilor trebuie să
   fie același obiect.
6. **P2 — chips generice vs ancorate.** iZi: „Compară serul CeraVe cu ACM Duolys CE",
   „Adaugă Serum CeraVe în coș" (nume + stadiu). Aria: „Compară primele două", „Ai ceva și
   mai ieftin?" — și uneori contradictorii cu mesajul (vezi 2b).
7. **A nou — cross-sell post-add-to-cart.** După coș, iZi construiește rutina (retinol +
   SPF, „dacă vrei o rutină completă"). Noi n-avem acest comportament deloc.

## Ce rămâne DATE (B), confirmat și pe runda 2

- Fără atribute de ingredient (vitamina C/niacinamidă/retinol) → nici căutarea, nici
  coaching-ul nu pot fi concrete. `ai_summary` templat → „textură ușoară și plăcută" pe
  aproape fiecare card, 4 tururi la rând.
- Fără ETA livrare și voucher-la-coș (cele 2 câmpuri de card din runda 1 — neschimbat).
- `product_review_summaries` goale pe demo → turul de detaliu n-are ce sinteză de recenzii
  să țeasă (comportamentul din get_product_details există, materia primă nu).
- 3 carduri vs 7 la iZi (lățime de sortiment; minor, configurabil).

**Citire de arhitect:** runda 1 a arătat că pe UN tur diferența e mai ales DATE. Runda 2
arată că pe MULTI-TUR apar 3 gap-uri reale de creier — onestitate pe feature (1),
memorie de constrângeri (3), comutare de template pe stadiu (4) — plus un fix de config
de 5 minute (2) care ține închisă toată bucla de bani a demo-ului.

---

# RUNDA 3 (2026-07-02) — 7 scenarii iZi, extragerea GRAMATICII

**Metodă:** 7 conversații iZi pe scenarii diverse (fond de ten cu subton — rulat de 2 ori,
comparație de CONCEPTE „Natural Glow vs Velvet Matte" — de 2 ori, verificare nuanță în stoc,
șampon anti-mătreață 3 tururi, cremă de ochi + livrare, ser vitamina C + buget, cremă ten
gras + închidere „mulțumesc"). Scop: patternurile REPETABILE, nu răspunsuri izolate.

## Insight-ul central: iZi e la fel de TEMPLAT ca noi

Rulările duplicate produc răspunsuri structural identice (aceeași schemă, sortiment ușor
diferit) → orchestrarea e fixă, LLM-ul doar umple sloturi. **Exact filosofia noastră de
arhitectură.** Diferența de calitate NU e „mai multă libertate pentru model", e **calitatea
umplerii sloturilor + 6 comportamente de context** pe care nu le avem. Ne validează
pipeline-ul și ne acuză promptul + datele.

## Gramatica iZi (schema fixă, toate cele 7 scenarii)

1. **Lead** — cererea reformulată ca REZOLVATĂ, cu constrângerile ecou („Ți-am ales creme
   de ochi pentru cearcăne, **toate sub 120 lei**").
2. **Framing** — ce varietate urmează („variante naturale, dermatocosmetice și cu camuflare").
3. **5–7 carduri**, fiecare cu un motiv de UN rând care SEGMENTEAZĂ („Bun dacă ai ten
   sensibil…" / „Variantă economică…" / „Tratament concentrat pentru mătreață severă…").
4. **Coaching** — 3–4 criterii de decizie SPECIFICE categoriei (la șampon: tip scalp,
   intensitate, frecvență; la cremă ochi: doar cearcăne vs și pungi, textură, camuflare).
5. **Pick + alternativă pe nevoie diferită** — „în primul rând X pentru că [fapte]; dacă
   [altă nevoie], Y".
6. **Exact 5 chips**: rafinare-pe-atribut · rafinare-pe-buget · comparație CU NUME ·
   detaliu/recenzii CU NUME · add-to-cart CU NUME.

## Cele 12 patternuri

**Compunerea răspunsului**
- **P1 Motivele de card = partajarea spațiului de decizie.** Fiecare card răspunde „pentru
  CINE/CÂND e alegerea corectă", pe axe DIFERITE (tip ten, buget, severitate, format,
  clasă de brand). Împreună formează un arbore de decizie. La noi: aceleași adjective pe
  toate cardurile.
- **P2 Sortiment construit DIVERS, nu top-N pe scor.** Scară de preț (25→92 lei la șampon)
  + clase de brand (dermato / natural / mass-market) + mărimi. Cardurile NU sunt clone.
- **P3 Coaching per categorie = playbook curat**, nu generic. (La noi: DomainPack e casa
  naturală pentru `decision_criteria` per categorie.)
- **P4 Ecou personal al constrângerilor** în pick: „rămâne în bugetul tău", „în cazul tău",
  „pentru locația ta".

**Memoria și operațiile pe set**
- **P5 Stivă de constrângeri** (re-confirmat): „fără parfum" se ADAUGĂ la anti-mătreață +
  scalp sensibil; „până în 150" se adaugă la vitamina C + ten tern; setul anterior se
  păstrează parțial (continuitate).
- **P6 Anaforă pe setul afișat**: „Da, cât costă?" → rezolvă la pick-ul din turul trecut;
  „Cofeina sună bine" → rezolvă INGREDIENT menționat → produsul potrivit din set;
  „primul" → ordinal corect.
- **P7 Întrebări TRANSVERSALE pe set**: „care dintre ele e cea mai ușoară ca textură?" →
  re-rankează subsetul pe UN atribut, cu dovadă din recenzii („cele mai multe recenzii
  laudă…"). Asta e o operație pe `displayed_products`, nu un search nou.
- **P8 Multi-intent onorat integral** („Cofeina sună bine. Aveți livrare? În cât timp
  ajunge?" = preferință + 2 întrebări operaționale, toate 3 adresate).

**Onestitate și fallback**
- **P9 Fallback GRADAT pe disponibilitate** (nuanța Warm Beige): (1) disclosure explicit
  „nu există exact nuanța X în gamă", (2) cele mai apropiate DIN ACEEAȘI gamă, ghidate pe
  deschis/închis, (3) alternative cross-brand care CHIAR poartă numele cerut, cu trade-off
  numit („finish mai natural, nu la fel de mat"). Consistent pe ambele rulări.
- **P10 Rating-uri mici afișate fără cosmetizare** (nuanțele Seventeen au rating 3 și
  rămân pe carduri — sunt ce a cerut userul).

**Comerț și închidere**
- **P11 Întrebările operaționale primesc răspuns operațional REAL, personalizat**: cutoff
  18:00, curier mâine 09–18 (~19 lei) vs easybox de la 16:00 (0 lei), pe locația salvată a
  userului. Repetarea întrebării → re-răspuns răbdător, restructurat. (Platformă eMAG —
  parțial C, dar PATTERNUL e replicabil: operațional ≠ deflectare.)
- **P12 Închidere conversațională**: la „Mulțumesc!" → mesaj scurt cald, FĂRĂ carduri,
  chips pivotate pe categorii ADIACENTE (gel curățare, toner, mască, rutină, SPF — toate
  pentru ten gras) = construcție de coș prin rutină.

**Plus, pe comparație:** „Natural Glow vs Velvet Matte" sunt CONCEPTE, nu product_ids —
iZi răspunde cu tabel pe 7–8 dimensiuni decizionale împerecheate + regulă de decizie pe
tipul de ten + nuanța „poți avea ambele". `compare_products` al nostru nu are cale pentru
comparație de concepte (educație de categorie).

## Maparea pe pipeline-ul nostru: ce e COD, ce e PROMPT, ce e DATE

| Pattern | La noi azi | Cauza gap-ului |
|---|---|---|
| P1 motive segmentante | adjective interșanjabile | PROMPT (disciplină anti-tautologie) + DATE (atribute/ingrediente) |
| P2 sortiment divers | top-3 clone pe scor | COD (diversificare MMR pe preț/brand/clasă în retrieval) |
| P3 coaching per categorie | șablon unic generic | DATE+COD (criterii per categorie în DomainPack/categories) |
| P5 stivă constrângeri | re-search de la zero, pierde filtre | COD (stack în `state`, aplicat la re-search) |
| P6–P7 set-ops (anaforă, rank transversal) | `displayed_products` există ca ref-uri, dar comportamentul nu | PROMPT+COD (instrucțiuni + eventual tool `rank_displayed`) |
| P8 multi-intent | parțial (runda 1: aplatizare) | COD/PROMPT (triaj + agent onorează toate intențiile) |
| P9 fallback gradat pe variantă/nuanță | disclosure doar pe brand | COD (extensie NX-118 variant + nearest-in-line + cross-line) |
| P11 operațional grounded | `delivery_eta` există, nelegat | COD (leagă întrebarea de livrare de tool) + DATE (reguli cutoff) |
| P12 închidere + chips adiacente | probabil search forțat | COD (detecție closure în triaj → template scurt + chips pe categorii vecine) |
| comparație de concepte | doar product_ids | COD/PROMPT (ramură de educație în compare / faq) |

**Citire de arhitect (runda 3):** gramatica iZi e reproductibilă 1:1 pe arhitectura
noastră — nimic de acolo nu cere alt pipeline. Cele mai mari 3 pârghii pe calitatea
PERCEPUTĂ: (1) motive de card segmentante + sortiment divers (P1+P2 — primul lucru pe
care îl vede userul), (2) stiva de constrângeri + set-ops (P5–P7 — face conversația să
pară „cu memorie"), (3) fallback-ul gradat pe disponibilitate (P9 — momentul de maximă
încredere sau pierdere a ei).

---

# RUNDA 4 (2026-07-02) — auditul PROMPTURILOR: cât din gap e în instrucțiuni

**Întrebarea lui Adi:** „poate ar trebui să facem și modificări la prompturi — și alea
s-ar putea să producă diferența?" **Răspuns scurt: DA, parțial** — auditul liniei
`_SYSTEM` (triaj) + `_TOOLS_BLOCK`/`_RICH_RULES` (agent) + `_rich_bundle` (materia primă)
arată 3 categorii:

## A. Reparabil DOAR din prompt (ieftin, imediat)
1. **Chips-urile generice sunt PREDATE de prompt.** Exemplele din regula `suggestions`
   (prompt_builder.py:155-158) sunt chiar chips-urile generice observate („Compară primele
   două"). iZi are 5 roluri fixe ancorate pe nume. → NX-132.
2. **„PÂNĂ LA 4 produse (ideal 4)"** vs iZi 5-7 (capul de 6 din compose permite). → NX-132.
3. **Nicio regulă de SET pe motive** — anti-tautologia există per-card, dar nimic nu cere
   axe DIFERITE între carduri (segmentarea P1). → NX-132.
4. **Multi-intent, comparație de concepte, ecoul constrângerilor, interdicția scheletului
   în MOD DETALIU** — absente din instrucțiuni. → NX-132.

## B. Prompt-ul e DEJA corect, dar e înfometat de DATE (promptul nu poate repara)
- `fit_clause`/education cer atribute REALE din „fațete"/„descriere" — dar pe demo
  `top_pros` = gol, `ai_summary` = templat (160 chars), fațetele fără atribute în
  `attributes`. Cere specificitate pe sloturi goale → filler sau risc de halucinație.
  **Datele rămân precondiția #1** (seed ingrediente/atribute — backlog existent).
- MOD DETALIU există în prompt (deep-dive pe ingrediente) — n-are ingrediente de defalcat.

## C. Gaură STRUCTURALĂ în lanțul de prompturi (prompt + cod împreună)
- **Stiva de constrângeri nu există nicăieri în lanț**: triajul extrage sloturi DOAR din
  mesajul curent (triage.py:123-125), `_filters_hint` seedează doar din turul curent
  (agent.py:697-714), iar `_TOOLS_BLOCK` nu spune „păstrează constrângerile anterioare".
  Istoricul E în context, dar nimeni nu-i cere modelului să-l folosească la args. → NX-133
  (stiva în state = mecanismul; regula de prompt = întărirea).
- **Sortimentul de clone nu e reparabil din prompt**: modelul alege din ce-i dă retrieval-ul;
  6 candidați aproape identici → niciun prompt nu-i face diverși. → NX-134 (cod).
- **`delivery_eta` NU e în toolset** (`_SALES_TOOLS`, base.py:64-73) — întrebările de livrare
  n-au cale grounded decât FAQ (goale pe demo). → NX-137 (FAQ seed; ETA real = alt task).
- **Turul de închidere**: suggestions permise DOAR pe clarify (triage.py:130-132) → „mulțumesc"
  n-are chips adiacente. → NX-136.

**Concluzie runda 4:** ~o treime din gap-ul de calitate percepută e prompt-only (NX-132),
dar cele mai vizibile două simptome (motive generice, coaching generic) au promptul DEJA
scris corect și pică pe date. Ordinea corectă: NX-132 (prompt) + seed de date ÎMPREUNĂ,
apoi NX-133/134 (memorie + sortiment), NX-135/136/137 în coadă.

**Carduri generate:** NX-132 (gramatica iZi în prompturi) · NX-133 (stiva de constrângeri)
· NX-134 (diversificare sortiment) · NX-135 (fallback gradat variantă) · NX-136 (închidere
+ chips adiacente) · NX-137 (checkout_url demo + livrare din FAQ).
