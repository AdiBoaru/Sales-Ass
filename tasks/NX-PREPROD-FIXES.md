# Backlog reparații pre-producție (din auditul de conversații 2026-06-19)

Sursă: 15 conversații persona reale prin pipeline-ul live ([sim harness](../scripts/sim/) +
[raport](../docs/PREPROD-CONVERSATION-AUDIT.md)). Fiecare intrare e gata de transformat în card
complet (`/gen-task NX-1XX ...`). Ordonate după impact pe client.

> **Infra deja FIXAT** (migrări aplicate live în această sesiune): `010` (index one-open conv —
> primul mesaj al clientului nou crăpa), `011` (SELECT intent_aliases + policy faqs/aliases — sales
> degrada la fallback), `012` (inbound_dedupe claimed_at/completed_at + UPDATE grant — claim_inbound
> crăpa). Toate erau drift cod↔migrare pe branch-ul `feat/NX-85-conv-lock`. **De aplicat și pe prod
> + gate CI care verifică migrările aplicate pe staging.**

---

## NX-103 — 🔴 CRITIC — Localizare end-to-end a stagiului Agent (limba pe ruta sales)
**Dovadă:** persona `maghiar` + `englez` — pe `route=simple` botul răspunde corect HU/EN, dar pe
`route=sales` (agentul) revine la **română**, chiar după „MAGYARUL" / „english please" ×3. Răspunsuri
mixte: nume produse + linia „Poți cere și…" + disclaimer rămân RO chiar când restul e EN/HU.
**Cauză probabilă:** `ctx.language` ajunge la triaj dar NU e propagat în `prompt_builder.build_agent_system`
/ `build_rich_system` / `build_reco_system`, iar scaffolding-ul rich (chips, disclaimer) e hardcodat RO.
**Fix:** propagă `ctx.language` în toate prompturile agentului + localizează chips + disclaimer; test HU/EN end-to-end pe sales.
**Files:** `src/worker/stages/agent.py`, `src/agent/prompt_builder.py`, `src/worker/compose.py`, scaffolding chips.
**Efort:** M-L. **DoD:** client HU/EN primește TOT răspunsul (inclusiv produse/chips/disclaimer) în limba lui pe sales.

## NX-104 — 🟠 HIGH — Linkul de checkout ajunge efectiv la client
**Dovadă:** `cadou`, `ten-gras`, `englez` (×4 conversații) — event `checkout_link_created` emis (de 2×)
dar **niciun URL în textul răspunsului**; botul se contrazice „nu am încă un link generat aici". Client gata
să cumpere → nu poate. `checkout_base_url` gol.
**Cauză:** (a) config checkout lipsă → tool-ul nu produce URL; (b) compozitorul rich/reco nu pune URL-ul în reply chiar când există; (c) agentul pretinde că ajută fără să livreze.
**Fix:** setează checkout config (date) + asigură că URL-ul tool-ului apare în reply; fără URL → mesaj onest „checkout indisponibil aici, uite cum comanzi". Nu pretinde un link nelivrat.
**Files:** `src/tools/commerce_tools.py`, `src/worker/stages/agent.py`, `scripts/set_store_config.py` (date), config.
**Efort:** M. **DoD:** la intenție de cumpărare, clientul primește un URL clickabil SAU un mesaj onest; zero „link fantomă".

## NX-105 — 🟠 HIGH — Handoff: mesaj de holding + triaj anti-escaladare prematură
**Dovadă:** `comanda-fantoma`/`retur-suparat`/`vreau-om`/`haggler`/`troll`/`back-in-stock` — după handoff,
**tăcere totală** pe 4-7 mesaje (client scrie „alo? e cineva?" → zero). Și: `handoff source=triage` pe
haggling, plângere de retur, opinie politică, presiune da/nu — triajul escaladează prea larg, nu doar pe
cerere explicită de om. Întrebări legitime ulterioare sunt înghițite de gate_halt.
**Fix:** (a) mesaj de holding throttled în handoff activ (1× la primul follow-up / la N min) în loc de tăcere absolută; (b) tunează promptul de triaj să rezerve `handoff` pt cerere explicită/caz sensibil, încercând întâi să asiste (order/sales/clarify).
**Files:** `src/worker/stages/gates.py`, `src/worker/stages/triage.py`, `src/worker/stages/handoff.py`.
**Efort:** M (spart: 105a holding-message · 105b triaj-tuning). **DoD:** după handoff clientul primește confirmare/ETA, nu tăcere; haggling/plângere ușoară nu mai escaladează automat.

## NX-106 — 🟠 HIGH — compare_products: diferențiere reală
**Dovadă:** `comparatie`, `ten-gras`, `englez`, `ingrediente` — la „compară X și Y" repetă fraze identice
pentru ambele, **colapsează la 1 produs**, nu folosește rating/preț, ignoră de 3× „de ce 58 vs 88 lei".
**Fix:** când userul numește 2 produse → `compare_products` obligatoriu, output structurat pe preț/rating/atribute(/ingrediente); interzice colapsul la 1; răspunde explicit la diferența de preț.
**Files:** `src/tools/` (compare_products), `src/worker/stages/agent.py`, `prompt_builder`.
**Efort:** M. **DoD:** comparație cap-la-cap cu ≥2 diferențieri concrete; răspunde la „de ce diferă prețul".

## NX-107 — 🟠 HIGH — search_products: buget = filtru DUR + fix category drift la „mai ieftin"
**Dovadă:** `indecis-lung` (cerut <50 lei, primit 60-98, nu a admis că n-are); `mesaje-fragmentate`
(„mai ieftin" → **deodorante** + o mască de 207 lei, a pierdut categoria „cremă").
**Cauză:** (a) `budget_max` nu e aplicat ca `WHERE price<=`; (b) la refinare pe preț se pierde categoria activă din context → search fără filtru de categorie (context poisoning).
**Fix:** budget ca filtru SQL dur; păstrează categoria activă pe follow-up de preț; 0 rezultate în buget → mesaj onest, nu produse peste buget / altă categorie.
**Files:** `src/tools/` (search_products), `src/worker/context.py`/state (categoria activă), agent.
**Efort:** M. **DoD:** „sub 50 lei" → doar produse ≤50 SAU „n-am nimic sub 50"; „mai ieftin" păstrează categoria.

## NX-108 — 🟡 MEDIUM-HIGH — Moderare: fals-pozitive pe colocvial RO benign
**Dovadă:** `troll` („prostiile") + `ten-gras` („lipește-l aici") → `message_moderated` a blocat întrebări
legitime de produs/checkout din ACELAȘI mesaj. Reformulat fără cuvânt → mergea.
**Fix:** ridică pragul / nu bloca pe categorii slabe; dacă mesajul conține o cerere legitimă, tratează neutru fără a tăia. Corpus de test RO colocvial benign.
**Files:** `src/worker/stages/gates.py` (`_moderation_blocked`), config.
**Efort:** S-M. **DoD:** limbaj colocvial RO inofensiv nu mai blochează cereri legitime.

## NX-109 — 🟡 MEDIUM — back_in_stock invocat + comunicarea stocului
**Dovadă:** `back-in-stock` — bot a promis „te anunț când revine" dar **nu a apelat `subscribe_back_in_stock`**,
a inventat un „cont/mesaj de abonare" inexistent (halucinare ușoară de proces), a evitat de 3× întrebarea de stoc.
**Fix:** la „anunță-mă când revine" → `subscribe_back_in_stock` pe canalul curent + confirmare concretă; surface `availability`/`stock_total` în reco; nu inventa proces.
**Files:** `src/tools/commerce_tools.py`, agent prompt, `get_product_details`/search (availability).
**Efort:** M. **DoD:** abonare reală creată + confirmată; stocul comunicat onest.

## NX-110 — 🟡 MEDIUM — Summarizer păstrează referințele de produs (retenție lungă)
**Dovadă:** `indecis-lung` (13 ture) — după `summarizer_run` (turn 10) botul nu mai știe prima cremă
(31 lei, turn 1) nici bugetul final (200 lei, turn 6). Retenție OK ~6-8 ture, cedează după sumarizare.
**Fix:** include în context, pe lângă rezumat, referințele cheie din state (`displayed_products` + ultimul buget/constrângere); promptul de summarizer să păstreze produse+prețuri+buget menționate.
**Files:** `src/worker/summarizer.py`, `src/worker/context.py`, state.
**Efort:** M. **DoD:** după sumarizare, botul reține produsele afișate + bugetul exprimat.

## NX-111 — 🟢 QUICK WINS
- **Disclaimer 1×/conversație** (primul mesaj bot), nu pe fiecare tur — semnalat de aproape toate personajele ca repetitiv/eroziv. `src/worker/compose.py` / processor. **Efort S.**
- **Surface ingrediente:** `product_ingredients` (12.368 rânduri) + `ingredients` (2.246) EXISTĂ în DB dar agentul nu le vede → expune în `get_product_details` (persona `ingrediente` n-a putut afla compoziția — e gaură de TOOLING, nu de date). **Efort S-M.**
- **Order vs intenție comercială:** „cum fac comanda?" / „cât costă livrarea?" rutate la `order` (lookup comandă inexistentă) în loc de sales/FAQ. Tunează triajul. **Efort S.**

---

## Găuri de DATE (nu cod — de seedat înainte de prod)
- `faqs=0` → livrare/retur/garanție fără răspuns curat (afectează `cadou`, `ten-gras`, `englez`).
- `orders=0` + fără config checkout → fluxul de comandă/checkout nu e real.
- `intent_aliases` aprobate=0, `wa_templates` aprobate=0 → stratul gratuit + proactivul inactive.
- **Nume produse templated** („Mira Atelier Glow Accesoriu beauty pentru calmare 437") + tagline-uri
  generice identice → par date de test, sperie clientul, fac comparația fără sens. Catalog real necesar.
- `vertical='ecommerce'` în loc de `'beauty'` → prompt mai puțin specific.

## Ce MERGE (de păstrat — nu regresa)
Zero halucinări de preț/produs; grounding puternic pe ingrediente (refuză să inventeze compoziții/
concentrații, inclusiv un claim fabricat „10% niacinamidă"); comandă inexistentă onestă; moderare
neutră pe insultă directă; off-topic fără halucinare (oră/scor/politică); retenție context pe 6-8 ture.
