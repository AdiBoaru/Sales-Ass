# Nativx Assistant — Landing Premium Pack

> Document de lucru pentru redesign-ul paginii (claude.ai/design). Copy-ul rămâne în **engleză** (pagina e în engleză); explicațiile sunt în română.
> **Regulă de aur:** zero metrici inventate, zero testimoniale false. Unde acum scrie `+X%`, `<Ns`, `[sample]`, `[Sample Name]` → afirmații concrete de capabilitate, NU cifre inventate.
> Fiecare prompt de mai jos se lipește **în chat-ul claude.ai/design**, o secțiune pe rând. Sunt copy-only (păstrează structura/animatiile DC).
> Acesta e un fișier scratch — îl poți șterge după ce termini. Nu face parte din backend.
> **Update (decizie 2026-06-18):** referința **eMAG/iZi/SOLE/Aura e scoasă complet** din site. Peste tot unde apărea analogia, e înlocuită cu framing generic, fără nume: *„the in-house sales assistant the biggest online stores build for themselves — delivered as a managed service"*. Prompturile de mai jos sunt deja actualizate.

---

## 1. Verdict onest

Pagina e **completă structural, dar citește ca un produs mid-tier / early-stage, nu ca un serviciu premium managed.** Scor mediu: **claritate ~6.5/10, premium ~5.5/10.**

Trei lucruri strică încrederea din prima privire:
1. **Placeholdere vizibile** (`+X%`, `<Ns`, `[sample]`, `[Sample Name] [Role] [Company]`) → pagina arată ca un wireframe neterminat.
2. **Subhead de 45 de cuvinte** în hero, care îngroapă diferențiatorii reali.
3. **Lipsește ancora de audiență + model de business** — un străin nu poate spune dacă e SaaS self-serve, o aplicație, sau o agenție managed pentru magazine din RO.

**Cel mai mare lucru care te ține pe loc:** cel mai puternic și mai de neimitat atu — *validatorul determinist de catalog* (de asta „never invents a price") — e îngropat ca microcopy peste tot, în loc să fie o promisiune de nivel-hero. Liderii (Sierra, Decagon, Gorgias, Tidio) câștigă prin **restrângere, proof atribuit și o singură idee clară pe ecran.** Ai materia primă să câștigi la toate trei odată ce mor placeholderele și validatorul e promovat.

---

## 2. Benchmark vs. piață premium

| Dimensiune | Nativx azi | Standard premium | Fix scurt |
|---|---|---|---|
| **Hero clarity (5s)** | H1 prinde ideea, dar subhead de 45 cuvinte + fără audiență/model | 1 H1 declarativ + subhead „rule-of-three" (CE / UNDE / PENTRU CINE) | Taie subhead-ul; pune audiența în eyebrow |
| **Poziționare** | Defensiv („Not just a chatbot") + generic „omnichannel" | Afirmă un substantiv de rol/categorie, nu te aperi | Scoate „Not just a chatbot"; condu cu „AI sales assistant" |
| **Proof / trust** | Carduri testimonial `[Sample]` goale | Citate atribuite SAU capabilități oneste — niciodată carduri goale | Șterge testimonialele false; promovează validatorul |
| **Specificitate** | „upsells naturally", „smooth handoff", „feeds your CRM", „flywheel" | Verbe concrete + mecanism. Specificul = premium, adjectivul = ieftin | Înlocuiește fiecare adjectiv moale cu mecanismul real |
| **Restrângere vizuală** | 11 carduri „It [verb]" aproape identice; dashboard supraîncărcat; trust dublu (carduri + badge-uri) | O idee pe secțiune, puține-dar-ascuțite | Variază deschiderea cardurilor; spune trust-ul o singură dată |
| **Proof omnichannel** | Pornește secțiunea cu un snippet de cod (dev-facing) | Arată, nu afirma — un thread care continuă din web în WhatsApp | „We install it for you — one line, zero dev work" + mockup continuitate |
| **Conversie / CTA** | Banda „FOR PARTNERS & INVESTORS" e înfiptă între proof și pricing | O audiență, un motion deasupra fold-ului | Mută banda investitori în footer/pagină separată |
| **Pricing** | „Transparent" dar zero semnal de preț; carduri-slogan goale | Ori un range onest, ori o frază clară despre ce e inclus | „setup fee + monthly retainer, scoped to your store" |
| **Integritate metrici** | Chips placeholder `+X%`, `<Ns`, `[sample]` | Un singur număr onest de scală SAU „spec lines" oneste | Scoate orice cifră inventată; pune capabilități |
| **Provenance / credibilitate** | Se sprijinea pe nume RO (eMAG/SOLE) — **eliminat prin decizie** | Un cadru de credibilitate care merge și internațional | „The in-house sales assistant the biggest online stores build for themselves" — fără nume, merge global |

---

## 3. Strategia de HERO (cea mai importantă schimbare)

**Ce e greșit acum:** chips placeholder + subhead supraîncărcat + fără audiență/model. Diferențiatorul cel mai tare („never invents a price") e îngropat la mijloc de subhead.

**Variante de H1 (testate la 5 secunde), în ordinea recomandării:**
1. `The AI sales assistant for your online store.` *(cel mai clar — categorie + audiență)*
2. `Your best salesperson, on every channel.`
3. `Turn every chat into a sale — on your site, WhatsApp, and Telegram.`
4. `Conversations that sell, not just answer.`

**Recomandarea mea concretă (folosită și în promptul Hero de mai jos):**
- **Eyebrow:** `AI sales assistant for online stores — set up and run by Nativx`
- **H1:** `Your best salesperson — on your website, WhatsApp and Telegram, 24/7.`
- **Subhead:** `Nativx recommends the right product, guides the shopper to checkout, and never quotes a price your catalog doesn't have — across your website, WhatsApp and Telegram, in Romanian, Hungarian and English, with one shared customer history.`

**Testul de 5 secunde (ce ar spune un străin):** „E un AI sales assistant care gestionează chat-urile și vinde produse pentru un magazin online, pe site + WhatsApp + Telegram, în 3 limbi — și cineva ți-l setează și ți-l rulează." ✅ CE / UNDE / PENTRU CINE, fără scroll.

---

## 4. Top 6 priorități (ordine de execuție)

1. **Scoate TOATE placeholderele** (chips hero + testimoniale goale). Efort: mic. → prompturile **Hero** + **ROI/Trust**.
2. **Rescrie hero-ul** (taie eyebrow redundant + „Not just a chatbot", subhead de o linie, adaugă audiența + modelul managed). Efort: mic. → **Hero**.
3. **Promovează validatorul** la promisiune hero-adjacent + îl faci eroul tabelului Compare și al secțiunii Trust. Efort: mic. → **Features / Compare / ROI-Trust**.
4. **Înlocuiește snippet-ul de cod** din Channels cu copy managed + dovadă vizuală de continuitate. Efort: mediu. → **Omnichannel**.
5. **Mută banda „FOR PARTNERS & INVESTORS"** afară din fluxul de cumpărător. Efort: mic. → **Partners/Pricing**.
6. **Curăță pentru restrângere** (11→~6 carduri features, dashboard 4 tile-uri, trust o singură dată, pricing onest). Efort: mediu. → **Features / Insights / ROI-Trust / Pricing**.

---

## 5. De verificat ÎNAINTE de publicare (capcane de onestitate)

- **Telegram** e arhitectural canal de **TEST** (long polling), nu producție hardened. Prompturile spun „Telegram available for testing" — confirmă poziționarea înainte să-l prezinți ca egal cu WhatsApp.
- **Conectori nativi Shopify/Woo/Magento/PrestaShop NU există** în cod (CLAUDE.md). Secțiunea Integrations + FAQ trebuie să spună onest „ingerăm catalogul tău, indiferent de platformă" — nu „integrare nativă". Decide dacă păstrezi logo-urile ca „compatibil" sau le reformulezi.
- **Data residency:** nu afirma „date în România/EU" decât dacă regiunea proiectului Supabase chiar e aia.
- **Cifre/timp de setup:** nu pune „live în 48h" etc. dacă nu te angajezi contractual.
- **Dashboard „SAMPLE DATA":** păstrează eticheta (semnal de integritate), doar reformulat-o.

---

## 6. Cele 12 prompturi gata de lipit (claude.ai/design)

> Lipește-le pe rând. Fiecare e copy-only și păstrează structura/animatiile.

---

### 6.1 — Nav + Hero + Trust bar
*Diagnostic: H1 prinde, dar chips placeholder + subhead de 45 cuvinte + lipsă audiență/model rup testul de 5 secunde.*

```
Edit ONLY the copy in the top "Nav + Hero + Trust bar" section of the Nativx Assistant landing page (the navbar, the hero with the H1/eyebrow/subhead/CTAs/stat chips/mockup caption, and the trust-bar line below the hero). Do NOT change layout, structure, components, styles, animations, the website/WhatsApp mockup toggle, or any DC/runtime behavior. This is a copy-only pass — replace the text strings exactly as listed below, keeping every element in its existing slot.

EYEBROW (small label above the H1):
Replace with: "AI sales assistant for online stores — set up and run by Nativx"

H1 (main headline):
Replace with: "Your best salesperson — on your website, WhatsApp and Telegram, 24/7."
(Use a real em dash "—", not a hyphen.)

SUBHEAD (paragraph under the H1):
Replace with: "Nativx recommends the right product, guides the shopper to checkout, and never quotes a price your catalog doesn't have — across your website, WhatsApp and Telegram, in Romanian, Hungarian and English, with one shared customer history."
(Use a real em dash. Do NOT re-add the old "Not just a chatbot" opener or the "exactly what your customers want" closer.)

CTAs:
Primary button text: "Book a demo" (unchanged — keep it visually primary).
Secondary button/link text: change from "See it in action" to "See a live conversation".

STAT CHIPS — there are three chips currently showing placeholder/fabricated text ("24/7 - Always-on selling [sample]", "+X% - Recovered carts [sample]", "<Ns - Average reply [sample]"). REMOVE every "[sample]" token and DELETE the fabricated metrics "+X%" and "<Ns" entirely. Do NOT invent any number, percentage, or response time. Replace the three chips, in order, with these honest capability statements:
- Chip 1: "Always on — replies day and night, in Romanian, Hungarian and English"
- Chip 2: "Never invents a price — every figure is validated against your live catalog"
- Chip 3: "One customer history — same shopper across website, WhatsApp and Telegram"
(If a chip has a short "headline" part and a "label" part, fold each statement into the existing visual format; the key requirement is that NO placeholder text, no "[sample]", and no invented metric remains.)

HERO VISUAL CAPTION (the small caption under/near the website/WhatsApp mockup):
Replace with: "One assistant, one history — so a WhatsApp chat picks up right where the website left off."

TRUST BAR LINE (the sentence below the hero, above or beside the vertical chips):
Replace with: "The kind of in-house AI sales assistant the biggest online stores build for themselves — now available for yours, fully managed."
Keep the vertical chips exactly as they are: "Beauty / HVAC / Auto / Salons".

NAV LINKS: the top nav currently has 8 items (Channels, Features, Insights, How it works, Compare, Partners, FAQ + Book a demo). Trim it to these items in this order, keeping the "Book a demo" button at the end: "How it works", "Features", "Channels", "Compare", "FAQ", then the "Book a demo" button. Remove the "Insights" and "Partners" links from the top nav (if there is a footer, you may move them there; otherwise just remove them from the top nav). Keep the existing nav styling and behavior.

Do not add any new metrics, testimonials, logos, or claims beyond the exact copy above.
```

---

### 6.2 — Problem section
*Diagnostic: H2 e tare, dar cardurile sunt inegale (2 au body, 2 sunt doar titlu); card 4 inventează „Thousands" + buzzword „demand signal".*

```
In the Nativx Assistant landing page, update ONLY the copy in the "Problem section" (the section with eyebrow "The problem" and H2 "Every day your store loses sales it never even sees." containing four pain cards). Keep all layout, structure, styling, grid, spacing, animations, and DC/support.js behavior exactly as they are. This is a copy-only edit, except for adding ONE new closing line described below.

Do not invent any numbers, percentages, or metrics. Specifically, REMOVE the fabricated word "Thousands" and the buzzword phrase "The demand signal is invisible" from card 4 — replace them with the honest copy below. Normalize every spaced hyphen ( - ) used as punctuation to a real em-dash (—).

Make all four cards equal in depth: each card gets ONE concrete, scenario-anchored body line (mirroring the existing "11pm" card's specific style). Use this EXACT copy:

EYEBROW (unchanged): The problem
H2 (unchanged): Every day your store loses sales it never even sees.

Card 1 — Title (unchanged): Sales lost after hours
Card 1 — Body: A customer asks at 11pm. No one replies until morning — and by then they've bought elsewhere.

Card 2 — Title (unchanged): Carts abandoned, never recovered
Card 2 — Body: A shopper fills a cart on Friday night, gets distracted, and never comes back — no nudge, no recovery.

Card 3 — Title (unchanged): Staff buried in the same questions
Card 3 — Body: Your team answers the same delivery, stock, and how-to questions all day — on your site, WhatsApp, and Telegram.

Card 4 — Title (unchanged): You don't know what they want
Card 4 — Body: Every question, search, and out-of-stock request goes unrecorded — so you're left guessing what shoppers actually want.

ADD a single closing pivot line directly beneath the four cards (centered, as a quiet lead-in to the next/solution section; match the section's existing type styling, no new component needed). EXACT copy:
None of this is a staffing problem. It's a coverage problem.

Do not change any other section. Keep the tone premium and plain-spoken — confident, specific, no hype.
```

---

### 6.3 — Omnichannel / Channels
*Diagnostic: citește ca „chatbot pe 3 canale"; cardul-lider e un snippet de cod (DIY) care strică poziționarea managed. Îngroapă proof-ul real: „își amintește fiecare client".*

```
Edit the landing page section labeled "Omnichannel" / "Channels" (the one whose headline is currently "One assistant. Every channel." with the three chips Website widget / WhatsApp / Telegram and three cards, where the first card shows a code snippet for installing the website widget).

Make ONLY the copy and small structural changes below. Keep ALL existing layout, styling, spacing, grid, animations, scroll/reveal behavior, and DC/support.js runtime behavior exactly as they are. Do not add new sections, images, metrics, percentages, response times, or testimonials. Do not invent any numbers.

1) EYEBROW / SECTION LABEL
Change from: "Omnichannel"
To: "Every channel, one assistant"

2) H2 / SECTION HEADLINE
Change from: "One assistant. Every channel."
To: "One salesperson. Every channel. One memory of every customer."

3) SUBHEAD (the paragraph under the H2)
Replace with exactly:
"The same assistant recommends products, recovers carts, and tracks orders on your website, WhatsApp, and Telegram — and remembers each customer across all three. It never quotes a price or product that isn't in your catalog."

4) CHIPS
Keep the three existing chips: "Website widget", "WhatsApp", "Telegram".
ADD a fourth chip with the same styling as the others: "Romanian, Hungarian & English".

5) CARDS — reorder so continuity leads. The card that currently shows the code/install snippet must NO LONGER be first and must NO LONGER contain a code snippet. Set the three cards, in this order, to:

CARD 1
Title: "It remembers each customer"
Body: "Site, WhatsApp, or Telegram — it's the same conversation. Profile, past questions, and order history follow the customer wherever they message you, so they never have to repeat themselves."

CARD 2
Title: "It sells, it doesn't just answer"
Body: "Recommends the right product, recovers an abandoned cart, and tracks an order — in the customer's own language. The selling job your best in-store assistant would do, on every channel."

CARD 3
Title: "Answers in context, on the spot"
Body: "On a product page it already knows the product, so recommendations and answers are specific, not generic. And every price, product, and link comes straight from your catalog — never invented."

6) REMOVE THE CODE SNIPPET
Delete the install code snippet / <code> block that was inside the old first card. Do not show any code on screen in this section. Replace that done-for-you idea with a single small reassurance line placed under the cards (use the existing small/footnote text style if one exists, otherwise a quiet muted line):
"We add it to your site for you — one line of code, zero dev work on your side."

7) HARD RULES — apply throughout this section:
- Output stays in ENGLISH.
- Do NOT add or invent any metrics, percentages, response times, ROI, or testimonials. There should be zero placeholder text like "+X%", "<Ns", "[sample]", "[Sample Name]", or "[Company]" — if any exist in this section, delete them.
- Do NOT use the word "chatbot" anywhere. This is an AI sales assistant.
- Keep the tone premium and plain-spoken: confident, specific, no hype/buzzword soup.

Make no other changes outside this section.
```

---

### 6.4 — Features (inclusiv cardul „never invents a price")
*Diagnostic: 11 carduri deschid aproape toate cu „It [verb]" (monoton); validatorul e afirmație seacă; cuvinte moi („naturally", „smooth", „feeds", „tells the truth").*

```
In the Nativx Assistant landing page Design Component, update ONLY the text copy of the "Features" section (the one with the H2 "It sells, it doesn't just chat." and the prominent "never invents a price" hero card followed by a grid of capability cards). Keep all layout, structure, styling, the hero/validator visual, the card grid, icons, and all animation/DC behavior exactly as they are. This is a copy-only change. Do NOT invent any numbers, percentages, response times, or testimonials, and remove any placeholder or fabricated-metric text if present.

Make these exact text replacements:

1) Eyebrow (currently "Features"): change to:
Built to sell, not to chat

2) Keep the H2 as is:
It sells, it doesn't just chat.

3) Sub-headline: replace with:
Your assistant is a trained salesperson that knows your catalog, promotions and policies, and never quotes a price that isn't in them.

4) Hero card title (currently "The core guarantee - It never invents a price."): change to:
The core guarantee: it never invents a price.

5) Hero card body (currently a bare claim like "It never invents a price."): replace with:
A deterministic validator checks every price, product and link against your live catalog before a message is sent. Wrong number, wrong link, it doesn't go out. This is the kind of in-house sales assistant the biggest online stores build for themselves, delivered as a managed service for your store.

6) Replace the capability cards (vary the openers so no two start with "It"; keep the same number of card slots and visual treatment). Use these exact titles and bodies:

- Title: Recommends, doesn't just answer
  Body: Reads what the customer is shopping for and suggests the right product from your live catalog, the way your best floor salesperson would.

- Title: Knows today's promotions
  Body: Applies your current prices, sale prices and offers, so customers always hear the deal that's actually live right now.

- Title: Recovers abandoned carts
  Body: Follows up on carts customers left behind and brings them back to finish checkout, on the channel they were already using.

- Title: Suggests add-ons that fit the cart
  Body: Recommends complementary products based on what's in the cart and what's actually in stock, never a random upsell.

- Title: Answers shipping, returns and policy questions
  Body: Handles the repeat questions about delivery, returns and store policy from your own answers, so your team doesn't have to.

- Title: Same assistant on web, WhatsApp and Telegram, in RO, HU and EN
  Body: One assistant across your website widget, WhatsApp and Telegram, replying in Romanian, Hungarian or English to match the customer.

- Title: Reads voice notes and photos
  Body: Understands voice messages and photos: a customer can send a picture and it finds the matching product in your catalog.

- Title: Tracks orders and delivery
  Body: Pulls live order and shipping status so customers get the answer in the chat instead of opening a ticket.

- Title: Books appointments
  Body: Schedules appointments straight from the conversation and syncs them to your calendar, ideal for salons and services.

- Title: Turns conversations into demand analytics
  Body: Writes every lead, intent and order signal back to you, so each chat becomes demand data you can act on.

- Title: Hands off cleanly to your team
  Body: Knows when to bring in a human and hands over the full conversation, so your customer never has to repeat themselves.

Important: Remove the old duplicate card "It sells not just chats" (it repeated the H2) and replace it with the "Recommends, doesn't just answer" card above. Drop the soft words "naturally", "smooth", "tells the truth", and the vague phrase "feeds your CRM" entirely. Do not add any statistics, percentages, customer names, logos, or testimonials. Keep the copy in English, premium and plain-spoken. Change text only; preserve all existing components, classes, layout, and animations.
```

---

### 6.5 — See it in action (demo auto-play)
*Diagnostic: H2 are „it" fără antecedent și reduce produsul la „recommends"; ascunde proof-ul de preț; microcopy mașinărie („Processing…", „Ask anything…"); badge voucher inventat.*

```
Edit ONLY the section of my Nativx Assistant landing page called "See it in action" - the auto-playing demo with the 5 shopper scenarios (gift, skincare, vacuum, mountain gear, weeknight dinners) shown in a phone mockup that animates ask -> think -> answer. This is the InAction component (InAction.dc.html). DO NOT touch any other section.

CHANGE COPY ONLY. Keep every structural, layout, and animation behavior exactly as-is: keep all 5 auto-playing scenarios, the sidebar scenario picker, the phone mockup, the ask -> think -> answer -> next animation, the typing/think timing, and all DC bindings, {{ }} interpolation, sc-if/sc-for, and event handlers. Only swap the text strings below. The one allowed additive element is a small one-line caption strip under the phone (described below) - add it as plain text only, matching the existing type styles; do not restructure anything.

Replace the exact text strings as follows:

1) Eyebrow / kicker: keep as "See it in action".

2) H2 headline - replace:
OLD: "Whatever your customer needs, it recommends."
NEW: "Your customer describes what they need. It finds it in your catalog."

3) Sub-headline - replace:
OLD: "Pick a scenario - or watch it play. Real shopping conversations, the way your customers actually ask."
NEW: "Pick a scenario, or just watch. These are the messy, real-world ways customers ask - answered from your live catalog, on your website, WhatsApp, and Telegram."

4) The "think" step bubble between the question and the answer - replace:
OLD: "Processing your question…"
NEW: "Checking your catalog…"

5) The chat input placeholder text in the phone mockup - replace:
OLD: "Ask anything…"
NEW: "Describe what you're shopping for…"

6) Any promo/discount badge shown on a product card - replace:
OLD: "Voucher −10% extra"
NEW: "Promo from your store"
(This matters: a product that "never invents prices" must not appear to invent a discount. The badge must read as coming from the merchant's own catalog, not made up by the assistant.)

7) Add a single caption strip directly under the phone mockup (centered, small/muted text matching the existing note styling). Text:
"Every price, stock status, and link comes straight from your real catalog - it never makes one up."

8) The honesty note line currently below the demo - replace:
OLD: "Illustrative conversations - your real catalog replaces these."
NEW: "Sample catalog shown - your real products, prices, and links replace these on launch."

REMOVAL REQUIREMENT: Remove the invented "−10% voucher" discount wording, the machine-voiced "Processing your question…", and the generic "Ask anything…" placeholder. Do NOT introduce any fabricated metrics, percentages, response times, customer names, or testimonials anywhere. The only numbers/prices/ratings allowed are the existing sample catalog data, which stays honestly labelled by the note in step 8.

Keep the tone premium, plain-spoken, and confident - no hype words, no buzzword soup. Output all copy in English. After editing, leave all other sections of the page untouched.
```

---

### 6.6 — Customer insights / Dashboard
*Diagnostic: citește ca un tool BI separat, nu ca byproduct gratuit al asistentului; supra-promite („ad attribution", „sentiment" pe care produsul nu le are); jargon („w/o", „hot leads").*

```
Edit ONLY the "Customer insights" / dashboard section of the Nativx Assistant landing page (the section with the eyebrow "Customer insights", the H2 "Finally know what your customers actually want.", and the analytics dashboard mock labeled "SAMPLE DATA"). Change COPY ONLY. Keep the existing layout, grid, dashboard mock, chart, tiles, animations, and all Design Component (support.js) behavior exactly as they are. Do not add, remove, or restructure components except where I explicitly say to relabel or hide a tile below. Do not invent any numbers, percentages, response times, or testimonials.

Replace the copy as follows:

1) Eyebrow / kicker: change "Customer insights" to:
"Demand intelligence, for free"

2) H2 headline: replace "Finally know what your customers actually want." with:
"Every conversation tells you what to stock, price, and promote next."

3) Sub-headline: replace the current sub text (which reads "every conversation becomes product/pricing/stocking intelligence" and "This is the differentiator - not a footnote.") with this exact two-sentence copy, and DELETE the sentence "This is the differentiator - not a footnote." entirely:
"The assistant is already talking to every customer, so it already knows what they want. Stop guessing what to stock, what to price, and which conversations made you money - it's all here, no extra setup."

4) Add a short trust line directly above or beside the dashboard mock (plain text, smaller than the H2, no new metrics):
"Honest by construction: the assistant only quotes prices and products from your real catalog, so the revenue it reports is money it actually helped close - tracked by checkout link, not guessed."

5) Add a small muted caption near the dashboard mock (e.g. just under it):
"Pulled automatically from your catalog, orders, and conversations - no spreadsheets, no manual entry."

6) Relabel the dashboard tiles (text only; keep the tiles' position/style, keep their sample values visibly muted/greyed):
- KPI labeled "Attributed revenue" -> "Revenue the assistant helped close"
- KPI labeled "Resolved w/o human" -> "Handled without a human"
- Keep "Carts recovered" as-is.
- Tile labeled "Hot leads" -> "High-intent contacts (asked for price or stock)"
- Tile labeled "Ad attribution" -> "Which conversations led to orders"  (this product does NOT do cross-channel ad/ROAS attribution, so do not imply it does)
- Tile labeled "Unmet demand" -> "Unmet demand - what customers ask for that you don't stock"  (make this the most prominent insight tile if the layout allows promoting one)
- Tile labeled "Language + sentiment" -> "Demand by language (RO / HU / EN)"  (REMOVE the word "sentiment" and any sentiment visual - the product has no per-conversation sentiment)

7) Sample-data badge on the mock: change "SAMPLE DATA" to:
"Sample data - your live numbers once connected"

8) Footnote: replace "All figures shown are clearly-labeled sample data - replaced with your live numbers once connected." with:
"Sample figures - replaced with your live data once your store is connected."

Hard requirements: Remove the meta-line "This is the differentiator - not a footnote." Remove the "sentiment" claim. Do not present any sample figure as a real result - keep every hardcoded number/percentage/name in the mock visibly muted/greyed under the sample-data badge. If the dashboard currently shows more than ~5-6 tiles, you may keep the layout, but ensure "Unmet demand", "Revenue the assistant helped close", "Handled without a human", and "Carts recovered" read as the primary tiles. English copy only. Tone: premium, plain-spoken, specific, zero hype.
```

---

### 6.7 — How it works + Integrations
*Diagnostic: pașii pică testul de 5s („It learns your store", „You watch it grow" = antropomorfism vag); „self-improving flywheel" = buzzword.*

```
In the Nativx Assistant landing page, update only the text copy in the "How it works" section (the section with eyebrow "How it works", id="how") and the "Works with your store" Integrations block directly below it. Change copy ONLY — keep all layout, grid, numbered step cards (1-4), colors, animations, data-reveal behavior, and the platform logo chips exactly as they are. Do not add or remove any cards or elements.

Apply these EXACT copy replacements:

HOW IT WORKS — sub-headline (under the H2 "Live in days. Zero technical work for you."):
Replace: "A fully managed service — we connect the channels, sync your catalog, and keep tuning it. A self-improving flywheel."
With: "Fully managed — we connect your channels, sync your catalog, and keep tuning it on your real conversations. You touch nothing technical."

STEP 1 (keep heading "Connect & import") — body:
Replace: "Add the widget, connect WhatsApp & Telegram, import your catalog. We do the setup."
With: "We add the website widget and connect WhatsApp and Telegram. The whole setup is on us — you touch nothing technical."

STEP 2:
Replace heading "It learns your store" with: "We sync your catalog"
Replace body "Your products, prices, promotions, policies and tone of voice — tuned per vertical."
With: "Products, prices, stock and policies — pulled from your store and kept in sync. We tune its tone of voice to your brand and vertical."

STEP 3:
Replace heading "It sells & supports 24/7" with: "It sells and supports 24/7"
Replace body "On every channel, in every language — recommending, recovering carts, answering, closing."
With: "Recommends products, recovers carts, tracks orders and answers in RO, HU and EN — on every channel. It only quotes prices from your live catalog, never invents one."

STEP 4 (the dark card):
Replace heading "You watch it grow" with: "You see what customers want"
Replace body "Revenue and insights roll in — and we keep tuning it month after month."
With: "A clear dashboard of what your customers ask for and buy — plus the conversations driving sales. We keep refining it month after month."

INTEGRATIONS BLOCK ("Works with your store") — sub-line:
Replace: "Connects to the platforms you already use — or any store via API/CSV catalog sync and an orders webhook."
With: "Connects to Shopify, WooCommerce, Magento, PrestaShop — or any store via API or CSV. Your catalog, prices and stock stay in sync, and your orders sync automatically."

IMPORTANT: Remove the buzzword phrase "self-improving flywheel" entirely (done in the sub-headline replacement above) and remove the developer jargon "orders webhook" (replaced with "your orders sync automatically"). Do NOT introduce any numbers, percentages, response times, or testimonials — keep all copy as honest capability statements only. Leave the platform name chips (Shopify, WooCommerce, Magento, PrestaShop, "Any store · API / CSV") unchanged.
```

> ⚠️ Verifică claim-ul de integrare: dacă nu ai conectori nativi, formularea „Connects to Shopify..." poate fi citită ca integrare nativă. Variantă onestă: „Works with Shopify, WooCommerce, Magento, PrestaShop — and any store. We ingest your catalog whatever platform you run."

---

### 6.8 — Compare table + Mid CTA
*Diagnostic: rândurile citesc ca „feature bingo" generic; cei 2 diferențiatori reali (never invents prices, attributed revenue) sunt îngropați; H2 e tot negativ.*

```
Edit ONLY the "Compare" section (the section with eyebrow "Compare" and the comparison table) and the "Mid CTA band" immediately below it on the Nativx Assistant landing page. Change COPY ONLY. Do not alter any layout, styles, grid, table structure, cell markers (✅, ✕, 🧑, —, Partial, Limited, etc.), colors, animations, data-reveal behavior, or the DC/support.js runtime. Keep the exact same number of table rows and the exact same per-cell ✅/✕/🧑/—/Partial/Limited markers each row already has — only the left-hand ROW LABEL text changes, plus the headline, column headers, and the mid-CTA. Do NOT invent any numbers, percentages, response times, or testimonials.

1) COMPARE SECTION HEADLINE (H2) — replace:
OLD: "Not a basic chatbot. Not more manual work."
NEW: "Closer to a trained sales rep than a chatbot — without the manual work."

2) COLUMN HEADERS — make them parallel noun phrases. Replace:
"Nativx" -> "Nativx Assistant"
"Basic chatbot" -> "A basic chatbot"
"Doing it manually" -> "Answering by hand"

3) TABLE ROW LABELS — rewrite all row labels as parallel, verb-led capability phrases AND REORDER so the two true differentiators come first. Keep each row's existing center-cell markers exactly as they are; only move/relabel the left text. Final row order and labels, top to bottom:
Row 1: "Never invents a price or product"   (this was the "Never invents prices" row — keep ITS markers: ✅ / ✕ / 🧑)
Row 2: "Shows you the revenue it drives"      (this was the "Attributed revenue" row — keep ITS markers: ✅ / ✕ / ✕)
Row 3: "Sells on website, WhatsApp & Telegram"  (was "Website + WhatsApp + Telegram" — keep markers: ✅ / Partial / —)
Row 4: "Recommends products and guides to checkout"  (was "Recommends & sells" — keep markers: ✅ / — / 🧑)
Row 5: "Recovers abandoned carts"             (was "Recovers carts" — keep markers: ✅ / — / —)
Row 6: "Shows what customers keep asking for"  (was "Customer-demand analytics" — keep markers: ✅ / ✕ / ✕)
Row 7: "Tracks orders and shipping"           (was "Order tracking" — keep markers: ✅ / Partial / 🧑)
Row 8: "Understands voice notes and photos"   (was "Voice & photos" — keep markers: ✅ / ✕ / 🧑)
Row 9: "Replies in Romanian, Hungarian & English"  (was "Multilingual (RO/HU/EN)" — keep markers: ✅ / Partial / Limited)
Row 10: "Hands off to your team when needed"  (was "Human handoff" — keep markers: ✅ / Partial / 🧑)
Row 11: "Answers every customer, 24/7"        (was "24/7" — keep markers: ✅ / ✅ / ✕)

4) ADD one small proof line directly under Row 1's label (a muted sub-line inside the same first cell, smaller and lighter than the label — e.g. font-size ~11.5px, color #94a3b8 — matching the existing muted-caption style; do not add a new row or change markers):
"Every price, product and link is checked against your live catalog before it sends."

5) MID CTA BAND — replace the headline so the ownable half leads, and add a small reassurance line beneath it (smaller, semi-transparent white, e.g. font-size ~14px, color rgba(255,255,255,.85); do not change the gradient band, button, or layout). Keep the "Book a demo" button text unchanged.
OLD headline: "Sell more on every channel — and finally see what your customers want."
NEW headline: "Finally see what your customers actually want — and sell more on every channel."
NEW sub-line (new element under the headline): "No setup work on your side — we tune it on your catalog and run it for you."
Button stays: "Book a demo"

Apply these as copy-only edits; leave everything else byte-identical.
```

---

### 6.9 — ROI/Attribution + Testimoniale + Why Nativx
*Diagnostic: cele 3 carduri `[Sample]` sub „Proof, not promises" se auto-contrazic; „Built to be dependable and safe" e generic; trust dublu (carduri + badge-uri).*

```
Edit the Nativx Assistant landing page Design Component. Change ONLY the copy in the section that contains the ROI/Attribution block (eyebrow "Proof, not promises", H2 "See exactly what the assistant earned."), the placeholder testimonial cards, and the "Why Nativx" grid ("Built to be dependable and safe." with 6 cards) plus the closing trust badge row. Keep all layout, components, styling, animations, and DC/support.js runtime behavior exactly as-is. Do NOT add, invent, or imply any metric, percentage, response time, ROI figure, customer name, logo, or testimonial quote. Make only the text replacements below, plus the two clearly-marked structural removals.

1) ROI / ATTRIBUTION BLOCK — replace the text:
- Eyebrow: "Proof, not promises" -> "Results you can see"
- H2: "See exactly what the assistant earned." -> "See which orders the assistant helped close."
- Sub: -> "Tracked checkout links tie each order back to the conversation that drove it — split into assisted (the assistant helped) and bot-led (the assistant closed it). You see attributed revenue, not a vague ROI claim."
- The 3 bullets become exactly these three (no numbers invented):
  • "Attributed revenue per period — every order the assistant touched, tied to a real checkout link."
  • "Assisted vs. bot-led split, so you can trust the number instead of taking it on faith."
  • "Which products and conversations converted — demand insight you can act on, not just a dashboard."

2) TESTIMONIALS — DELETE every placeholder. Remove all "[Sample]", "[Sample - replace with a real client quote]", "[Sample Name]", "[Role]", "[Company]" text. Do NOT fabricate any quote, name, role, or company. Keep the same card layout/components but replace their contents with these honest proof statements:
- Section heading/eyebrow for this block: "Honest about being early"
- Large/featured card text: "We're a new managed service, so we won't show you testimonials we don't have yet. Instead: we onboard a limited number of stores each month and tune the assistant to your exact catalog before it ever talks to a customer. You watch it answer in private first — then decide when it goes live."
- Second card text: "Catalog-accurate by design. Every price and product link is checked against your live catalog before a message sends — so the assistant never quotes a price you don't have."
- Third card text: "See it on your store. We run it on your real catalog in a demo and on the live website widget — sample data is labelled as sample data, so what you see is exactly what you'd get."
(These are capability statements, not quotes — remove any quotation marks, avatar names, or person/role/company attribution fields from these cards.)

3) WHY NATIVX — replace the heading and regroup the 6 cards under three small theme labels, each card getting a plain-language payoff:
- Eyebrow: keep "Why Nativx"
- H2: "Built to be dependable and safe." -> "It never invents a price or a product."
- Add a one-line subhead under the H2: "Catalog-accurate, GDPR-safe, and yours to supervise — three things a generic chatbot can't promise."
- Group the cards under three short labels (add these as small group headings if the layout allows; if not, just reorder the cards in this order and apply the new card copy):
  SAFE OUTPUT
   • "Catalog-checked replies — every price and link is pulled from your live catalog before sending, so it can't make one up."  (replaces "Price & link validator")
   • "Always identifies as AI — it never pretends to be a human agent, in any conversation."  (this replaces the old "AI disclosure always on" badge, now promoted to a card)
  SAFE DATA
   • "Hosted in the EU — your customer data stays on EU infrastructure."  (replaces "EU data residency")
   • "GDPR-built — phone numbers and personal data are isolated and erasable on request, by design."  (replaces "GDPR-compliant")
  SAFE TO ADOPT
   • "Watch it first — see it answer in private, on your own catalog, before it talks to a single customer."  (replaces "Try it in shadow mode")
   • "No bill surprises — a hard daily cap means your costs can never run away."  (replaces "Rate-limited & cost-capped")
   • "Junk stays out — spam and abuse are blocked before they ever reach a reply."  (replaces "Spam & abuse filtered")
   • "Fully managed — we set it up, tune it, and keep it running. You don't touch the tech."  (promoted from the "Fully managed" badge)
   • "Tuned to your vertical — beauty, HVAC, auto, or salon, the assistant is built around your catalog."  (promoted from the "Tuned per vertical" badge)

4) CLOSING BADGE ROW — REMOVE it entirely. Delete the badges "EU-hosted", "GDPR-ready", "Fully managed", "Tuned per vertical", and "AI disclosure always on". Every one of these points now appears exactly once inside the grouped "Why Nativx" cards above, so the duplicate badge row should be deleted to avoid saying GDPR/EU/managed/vertical twice.

Final check before saving: there must be ZERO "[Sample]"/"[Company]"/"[Role]"/"[Sample Name]" placeholders, ZERO invented numbers or percentages, ZERO fabricated quotes or person names, and GDPR/EU/managed/vertical must each appear only once. All structure, styling, and animations stay intact.
```

> ⚠️ Verifică „Hosted in the EU" cu regiunea reală Supabase înainte să publici.

---

### 6.10 — Partners/Opportunity + Pricing
*Diagnostic: banda „FOR PARTNERS & INVESTORS" dă whiplash de audiență chiar înainte de pricing; pricing zice „Transparent" dar nu arată niciun preț.*

```
Edit the Nativx Assistant landing page. Make ONLY copy changes to TWO adjacent sections — the "Opportunity / Partners" band (currently badged "FOR PARTNERS & INVESTORS" with the H2 "The opportunity." and a "Partner with us" button) and the "Pricing" section directly below it (H2 "Transparent and scoped to your store.").

Goal: make the whole primary scroll 100% merchant-facing. Today the Opportunity band is written for partners/investors, wedged between the product proof and the pricing, which gives a store owner whiplash. Convert it into a merchant benefits band. Keep ALL structure, layout, grid, styling, animations, and DC/support.js behavior exactly as-is. Do not add or remove sections, cards, or buttons (except the two explicit removals noted). Only swap the text.

OPPORTUNITY / PARTNERS BAND — replace text as follows:
- Badge/eyebrow: change "FOR PARTNERS & INVESTORS" to "WHY STORES CHOOSE NATIVX"
- H2: change "The opportunity." to "The in-house assistant the big players built — for your store."
- Subhead: change "Every store wants what eMAG and SOLE built in-house - but can't build it themselves." to "The biggest online stores build their own AI sales assistants in-house. Nativx gives your store the same — as a managed service across your website, WhatsApp, and Telegram. No AI team required."
- Card 1 title: change "A SaaS-agency model" to "Prices it can't get wrong"
- Card 1 body: change "One-time setup fee + a monthly retainer. Predictable, recurring revenue that compounds with every client." to "Every product, price, and link is checked against your live catalog before it sends. It never invents a price — something a generic chatbot can't guarantee."
- Card 2 title: change "Scalable platform" to "Tuned to your vertical"
- Card 2 body: change "Multi-tenant from day one. One platform, every client isolated, onboard a new store in days." to "Set up and trained for how your customers actually shop — whether you sell beauty, HVAC, auto, or run a salon. Live in days, not months."
- Card 3 title: change "Defensible" to "One assistant, every channel"
- Card 3 body: change "A deterministic, validated pipeline. Hard to copy, easy to trust - prices it can't invent, products it can't fabricate." to "The same assistant answers on your website, WhatsApp, and Telegram — in Romanian, Hungarian, and English. It recommends products, recovers carts, and tracks orders, and hands off to your team when needed."
- CTA button: change "Partner with us" to "Book a demo" (keep its existing link target #demo and its existing styling).

PRICING SECTION — replace text as follows:
- Eyebrow: "PRICING" (unchanged wording, just keep it)
- H2: change "Transparent and scoped to your store." to "Simple, and scoped to your store." (We are NOT showing a price, so do not claim "transparent".)
- Card 1 title: change "One-time setup fee" to "One-time setup"
- Card 1 tagline (the line currently styled like a price, "Connect & tune"): change to "We connect your channels, sync and embed your catalog, tune it to your vertical, and take it live for you."
- Card 2 title: keep "Monthly retainer"
- Card 2 tagline (currently "Run & improve"): change to "Always-on selling on web, WhatsApp, and Telegram — cart recovery, order tracking, demand analytics, and continuous tuning."
- Closing line: change "No fixed list price - it's scoped to your store. Book a demo for a tailored quote." to "Scoped to your catalog size and channels — book a demo and we'll put together a quote for your store."

REQUIRED REMOVALS (do not leave any trace of these in the primary scroll):
- Remove all partner/investor framing from this band: the "FOR PARTNERS & INVESTORS" badge, the word "opportunity", "Predictable, recurring revenue", "compounds with every client", and the "Partner with us" CTA are all replaced per above.
- If you want to preserve a partner/reseller path, add ONLY a single quiet text link reading "Partner with us" in the page footer pointing to a contact/partner anchor — never inline in this band. This is optional; if it adds any visual weight, omit it.
- Do NOT introduce any numbers, percentages, prices, response times, ROI figures, or testimonials anywhere. If any "from €X", "+X%", "<Ns", "[sample]", "[Sample Name]", or "[Company]" placeholder text exists in or near these sections, delete it — do not replace it with a guessed figure.

Tone: premium but plain-spoken, confident, specific, zero hype. Output stays in English. Change copy only — leave every structural, style, and animation detail untouched.
```

---

### 6.11 — FAQ
*Diagnostic: H2 e truism; cel mai tare claim (validatorul) e afirmat seac; mai multe răspunsuri sunt mine de fabricație (conectori inexistenți, timp de setup inventat, data residency neverificat).*

```
Edit ONLY the FAQ section of the Nativx Assistant landing page (the section with the eyebrow "FAQ" and the H2 "Questions, answered."). Change copy only — keep all existing layout, accordion/expand behavior, animations, styling, and Design Component (DC) runtime behavior exactly as they are. Do not touch any other section.

This is an AI sales assistant for online stores — on the website widget + WhatsApp + Telegram, in Romanian/Hungarian/English — sold as a managed service (setup fee + monthly retainer). Keep the tone premium but plain-spoken: confident, specific, no hype.

1) Keep the eyebrow as: FAQ

2) Replace the H2 "Questions, answered." with:
What merchants ask before they switch it on.

3) Directly under the H2, add one short framing line (subhead style, same as other section subheads):
Nativx Assistant is an AI sales assistant for online stores — on your website, WhatsApp, and Telegram. Here's what stores ask before going live.

4) Replace the entire Q&A list with EXACTLY these nine items, in this order (ordered by buyer anxiety, accuracy guarantee first). Use the existing question/answer accordion components — just swap the text:

Q1. Does it ever quote a wrong price or invent a product?
A1. No. It can't quote a price or link a product that isn't in your live catalog. Every reply is checked against your real product data before it sends — if a detail can't be verified, the assistant asks a clarifying question or hands off instead of guessing. No invented prices, no made-up links, structurally.

Q2. What does it actually do, and where?
A2. It recommends products, recovers abandoned carts, and tracks orders for your customers — across your website widget and WhatsApp, with Telegram available for testing. One assistant, the same answers on every channel.

Q3. How do we get started — do I have to integrate anything?
A3. You don't integrate anything yourself. This is a managed service: during setup we ingest your product catalog and connect your channels for you — whatever platform your store runs on. You review the assistant in a private test chat and approve it before it ever talks to a customer.

Q4. Is it GDPR-compliant, and how is customer data handled?
A4. Yes. Customer contact details are isolated to a single secured store and phone numbers never appear in our logs. Any customer can be fully erased on request: a one-action data-erase anonymizes their record and removes their phone number while preserving your aggregate analytics.

Q5. Which languages does it speak?
A5. Romanian, Hungarian, and English. It detects the customer's language per message and replies in it — including matching FAQs, cached answers, and templates to that same language.

Q6. What happens when a conversation needs a person?
A6. It hands the conversation to your team and goes quiet, so your staff can take over with no awkward double-replies. You decide when the assistant resumes.

Q7. What do I learn from it as a merchant?
A7. You get demand analytics from real conversations: what customers ask for, what they can't find, and how cart-recovery plays out — so your team sees what to stock and where you're losing sales.

Q8. How am I billed?
A8. It's a managed service: a one-time setup fee plus a monthly retainer, scoped to your catalog and conversation volume. We set it up, tune it on your products, and run it — you don't staff an AI team. Book a demo and we'll scope it to your store.

Q9. Who is this built for?
A9. Online stores — built first for beauty, HVAC, auto, and salon retailers. It's the kind of in-house sales assistant the biggest online stores build for themselves, delivered as a managed service so you don't have to build one yourself.

REMOVAL RULES (apply while editing — these must NOT appear anywhere in the new FAQ):
- Remove any setup-duration number or estimate (e.g. "[X days]", "live in 48 hours", "ready in a few days", "within a week"). We make no such time commitment.
- Remove any named ecommerce-platform integration claimed as built-in (Shopify, WooCommerce, Magento, PrestaShop, etc.). We do not have native connectors — say only that we ingest your catalog whatever platform your store runs on.
- Remove any specific data-residency claim ("stored in the EU", "in Romania", any named datacenter or city). Use only the verifiable data points in A4.
- Remove any coverage metric like "100% of conversations" or "X reports". Use only the honest capabilities in A7.
- Remove any hype adjectives ("seamless", "effortless", "powerful AI", "cutting-edge", vague "multilingual"). Replace with the concrete wording above.
- Do not add any percentages, response times, ROI figures, customer counts, logos, or testimonials. None are provided and none may be invented.

If the current FAQ has more or fewer than nine items, end with exactly these nine. Keep everything else (icons, dividers, motion, spacing) unchanged.
```

---

### 6.12 — Final CTA + Form + Footer
*Diagnostic: închiderea re-descrie produsul în loc să scadă fricțiunea; contrazice „omnichannel" (zice doar website + WhatsApp); „shadow mode" e jargon; an hardcodat 2026.*

```
Edit ONLY the copy in the final "Book a demo" section of the landing page - that is the closing call-to-action band, the contact form, and the footer at the very bottom of the page. Keep ALL structure, layout, components, styling, animations, and DC/support.js runtime behavior exactly as they are. Change text only (the two small additions noted below are single lines that fit the existing layout; do not redesign anything).

CONTEXT (do not print this, just use it): The product is Nativx Assistant - an omnichannel AI sales assistant for online stores, on website widget + WhatsApp + Telegram, multilingual Romanian/Hungarian/English, that recommends products, recovers carts, tracks orders, and only ever quotes prices and links that exist in the store's real catalog (it never invents a price). It is sold as a managed service (we set it up and run it; monthly retainer, cancellable). Keep the tone premium but plain-spoken: confident, specific, no hype, no buzzwords. Do NOT add any metric, percentage, response time, ROI figure, customer logo, or testimonial.

Make these EXACT copy replacements:

1) Final CTA heading (currently "Book a demo."):
   -> "See it run on your own catalog."

2) Final CTA sub-headline (currently "See your own catalog in the assistant - selling on your website and on WhatsApp - in a 20-minute call."):
   -> "In a 20-minute call, watch the assistant answer questions and recommend from your own catalog - on your website, WhatsApp, and Telegram, in Romanian, Hungarian, and English."

3) The three reassurance checks/bullets in this section. Replace the three items so each shows the mechanism instead of an adjective:
   - "No technical work" -> "We set it up on your catalog - no work on your side."
   - "Try risk-free in shadow mode" -> "It drafts real replies for you to approve before any reach a customer."
   - "Live in days" -> "Managed monthly - cancel anytime."
   IMPORTANT: remove the words "shadow mode," "risk-free," and "Live in days" entirely - they are jargon/vague claims.

4) Add ONE short trust line directly above the form (single line, same visual weight as supporting text, no new section):
   -> "It recommends from your real catalog and never invents a price or a link."

5) Form field label currently "WhatsApp or email":
   -> "WhatsApp number or email" (the input must accept either a phone number or an email - do not split it into two fields).

6) Form submit button (currently "Book a demo"):
   -> "Book my 20-minute demo"

7) Add ONE line of microcopy directly under the submit button (small/muted text, fits existing layout):
   -> "We only use this to set up your demo - no spam, ever. We'll reach out within one business day to book your 20 minutes."

8) Footer brand tagline (currently "An omnichannel AI sales assistant for online stores. By Nativx Technology."):
   -> "The AI sales assistant for online stores - on your website, WhatsApp, and Telegram. The kind of in-house sales assistant the biggest online stores build for themselves, delivered as a managed service for your store. By Nativx Technology."

9) Footer WhatsApp call-to-action (currently a plain "WhatsApp" / "WhatsApp CTA" label). Reword it as the casual, low-commitment path and keep it visually secondary to the form button (it must still deep-link to the real WhatsApp number / wa.me link already wired up - do not change the link target, only the label):
   -> "Prefer to chat first? Message us on WhatsApp."

10) Footer copyright line: remove the hardcoded year "2026". Replace with "(c) Nativx Technology" with NO year (or, if a dynamic current-year token is available in the runtime, use that token instead of a literal year so it never goes stale).

CLEANUP / NON-NEGOTIABLE: Do not introduce any fabricated number, percentage, response time, or testimonial anywhere in this section. The only number that may remain is the real "20-minute" demo length. Ensure every footer nav link (Product/Company columns, nativxtech.com, Privacy, Terms, the WhatsApp link) points to a real destination - if any is a "#" stub or "Coming soon," remove that link rather than ship a dead one. Leave the "EU - GDPR" line as-is. Output the updated section with all original structure, classes, and animations intact - copy changes only.
```

---

## 6.13 — Logo unic + Hero mockup refăcut (Website + WhatsApp), EN + EUR

**Logo — direcție:** evită clișeul „AI sparkle" cu 4 colțuri (ce folosește iZi). Marca fuzionează **conversație (chat bubble) + vânzare/creștere (săgeată ascendentă)** — fiindcă e un *sales* assistant, nu un chatbot generic. Păstrează squircle-ul gradient indigo→violet, marca albă în interior.

Starter SVG (bază de la care pornește claude.ai/design):

```svg
<svg width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="nvx" x1="4" y1="4" x2="44" y2="44" gradientUnits="userSpaceOnUse">
      <stop stop-color="#6366F1"/><stop offset="1" stop-color="#9333EA"/>
    </linearGradient>
  </defs>
  <rect width="48" height="48" rx="13" fill="url(#nvx)"/>
  <path d="M14 15h20a4 4 0 0 1 4 4v8a4 4 0 0 1-4 4H23l-7 6 1-6h-3a4 4 0 0 1-4-4v-8a4 4 0 0 1 4-4z" fill="#fff"/>
  <path d="M17 27l4-4 3 3 6-7" stroke="#4F46E5" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M27 18h5v5" stroke="#4F46E5" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
```

### Prompt LOGO (claude.ai/design)

```
Design and apply a new, original brand mark (app icon / avatar) for "Nativx Assistant", an AI sales assistant for online stores. Replace the current "M"-chevron mark EVERYWHERE it appears: the top-nav logo, the hero chat-header avatar (in BOTH the Website and WhatsApp mockups), the favicon, and the footer logo.

Design constraints:
- Keep the brand container: a rounded square ("squircle") with the indigo→violet gradient (from #6366F1 / #4F46E5 to #9333EA / #7C3AED), white mark inside.
- The mark must fuse two ideas: a conversation (chat / speech bubble) and selling/growth (a rising arrow or uptrend) — this is a sales assistant, not a generic chatbot.
- IT MUST BE ORIGINAL. Do NOT use the generic 4-point "AI sparkle / star / twinkle" shape (that's the cliché competitors like iZi use), no magic-wand, no stars. Make it a clean, geometric, ownable mark that still reads at 16px (favicon) and inside a small chat-header circle.
- Keep the wordmark "Nativx Assistant" with "Nativx" in near-black and "Assistant" in indigo (#4F46E5), in Space Grotesk.

Use this SVG as the starting point (a white speech bubble with an indigo uptrend arrow inside, on the gradient squircle), and refine it to be crisper and better balanced:

<svg width="48" height="48" viewBox="0 0 48 48" fill="none"><defs><linearGradient id="nvx" x1="4" y1="4" x2="44" y2="44" gradientUnits="userSpaceOnUse"><stop stop-color="#6366F1"/><stop offset="1" stop-color="#9333EA"/></linearGradient></defs><rect width="48" height="48" rx="13" fill="url(#nvx)"/><path d="M14 15h20a4 4 0 0 1 4 4v8a4 4 0 0 1-4 4H23l-7 6 1-6h-3a4 4 0 0 1-4-4v-8a4 4 0 0 1 4-4z" fill="#fff"/><path d="M17 27l4-4 3 3 6-7" stroke="#4F46E5" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M27 18h5v5" stroke="#4F46E5" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>

Give me 2-3 refined variations of the mark to choose from, then apply the chosen one consistently across nav, both chat-header avatars, favicon, and footer. Change the mark only — don't change any other copy or layout.
```

### Prompt WEBSITE MOCKUP (flux pe 2 ture, EN, EUR)

```
In the hero section of the Nativx Assistant landing page, redesign ONLY the "Website" tab chat mockup (the website-widget preview with the chat thread). Keep the outer frame, the faint mock store page behind it, the "Website / WhatsApp" toggle, the chat header, the input bar, the "Ask the assistant" launcher bubble, and all styling/animations. Replace ONLY the CONTENT of the chat thread with a two-turn conversation that shows the assistant understanding context. Everything in ENGLISH. All prices in EUR (€), not lei — keep the existing price typographic style, just change the currency. Keep the thread scrollable (it's taller now).

Do not hardcode a budget in the user's messages. Do not show invented discounts as if the assistant made them up. Keep one small honest caption: "Sample catalog — your real products and prices replace these."

The thread, top to bottom:

USER (outgoing bubble): "I'm looking for a moisturizer for dry skin."

ASSISTANT (one message, two stacked lines):
  Bold line: "Here are a few rich moisturizers for dry skin — gentle, fragrance-free formulas that work for sensitive skin too."
  Lighter line below: "Some are face-only, one works for face and body, so you can pick what fits."

PRODUCT CARDS (horizontal carousel, 3 cards, same card design as now, prices in €):
  Card 1 — badge "Top pick" · brand "CeraVe" · "PM Facial Moisturising Lotion — ceramides + niacinamide" · ★ 4.8 (312) · "Delivery: Tomorrow" · €15.49
  Card 2 — badge "Bestseller" · brand "La Roche-Posay" · "Toleriane Sensitive rich cream, 40ml" · ★ 4.7 (208) · "Delivery: Tomorrow" · €17.90
  Card 3 — badge "Best value" · brand "CeraVe" · "Moisturising Cream tub, 340ml — face & body" · ★ 4.9 (540) · "Delivery: Tomorrow" · €13.20

FOLLOW-UP PILLS (quick replies): "Something cheaper"  ·  "Fragrance-free only"  ·  "Add a gentle cleanser"  ·  "Build a full routine"

USER (outgoing bubble): "Something cheaper."

ASSISTANT (one message, two stacked lines):
  Bold line: "Found gentler picks at a lower price — good hydration, simple formulas, still fine for sensitive skin."
  Lighter line below: "Two are face-only and one works for face and body."

PRODUCT CARDS (carousel, 2 cards, €):
  Card 1 — badge "Top pick" · brand "Mixa" · "Hyalurogel Rich 24h, 50ml — hyaluronic acid" · ★ 4.6 (165) · "Delivery: Tomorrow" · €7.99
  Card 2 — badge "Bestseller" · brand "Garnier" · "Hyaluronic Aloe day cream — dry & sensitive, 50ml" · ★ 4.76 (203) · "Delivery: Tomorrow" · €6.99

FOLLOW-UP PILLS: "Only fragrance-free"  ·  "Add SPF"  ·  "Show day creams"

Keep the chat header as "Nativx Assistant · Online" and use the new brand avatar. Change copy + currency only; preserve all components, classes, layout, scroll, and animations.
```

### Prompt WHATSAPP MOCKUP (același flux, stil WhatsApp)

```
In the hero section, redesign ONLY the "WhatsApp" tab mockup (the phone showing a WhatsApp conversation) to mirror the SAME two-turn flow as the website mockup, but styled exactly like real WhatsApp. Keep the phone frame, the "Website / WhatsApp" toggle, and all animations. Everything in ENGLISH, all prices in EUR (€).

WhatsApp styling rules:
- Header bar: WhatsApp green (#075E54 / #128C7E), the new Nativx brand avatar in a white circle, name "Nativx Assistant", status line "online". Keep the call/menu icons if present.
- Chat background: WhatsApp wallpaper beige (#EFEAE2).
- USER (outgoing) bubbles: light green (#D9FDD3), right-aligned, rounded with the small tail, each with a timestamp and double blue read-ticks (✓✓ in #53BDEB).
- ASSISTANT (incoming) bubbles: white (#FFFFFF), left-aligned, with a timestamp.
- Product cards: show as a WhatsApp rich/link attachment inside a white bubble — small product image on top, then title, brand, ★ rating, price in €, and a green "View product" button at the bottom. Show ONE product card per turn (the top pick) — WhatsApp doesn't do horizontal carousels.
- Quick replies: render the follow-up options as WhatsApp interactive reply buttons — full-width white buttons with a green label, stacked under the assistant's message.
- Input bar at the bottom: WhatsApp style (rounded "Message" input + a green round send button).

The conversation:

USER: "I'm looking for a moisturizer for dry skin."  (timestamp + ✓✓)
ASSISTANT: "Here are a few rich moisturizers for dry skin — gentle, fragrance-free, and fine for sensitive skin too. My top pick:"
PRODUCT CARD (rich attachment): "CeraVe — PM Facial Moisturising Lotion, ceramides + niacinamide" · ★ 4.8 · "Delivery: Tomorrow" · €15.49 · button "View product"
QUICK-REPLY BUTTONS: [ Something cheaper ] [ Fragrance-free only ] [ Build a full routine ]

USER: "Something cheaper."  (timestamp + ✓✓)
ASSISTANT: "Found a gentler pick at a lower price — good hydration, simple formula, still fine for sensitive skin:"
PRODUCT CARD (rich attachment): "Mixa — Hyalurogel Rich 24h, 50ml, hyaluronic acid" · ★ 4.6 · "Delivery: Tomorrow" · €7.99 · button "View product"
QUICK-REPLY BUTTONS: [ Only fragrance-free ] [ Add SPF ] [ Show day creams ]

Change copy + currency + WhatsApp styling only; keep the phone frame, the Website/WhatsApp toggle, and all animations intact.
```

> **Notă EUR:** dacă treci pe euro, propagă `lei → €` și în secțiunea **See it in action** (6.5) și în **dashboard** (6.6), altfel rămâne inconsistent (unele „lei", altele „€").

---

## 7. Cum folosești pachetul

1. Deschide **claude.ai/design**, proiectul Nativx Assistant.
2. Lipește **6.1 (Hero)** primul — e cea mai mare schimbare de claritate. Verifică preview-ul.
3. Apoi **6.9 (ROI/Trust)** — omoară testimonialele false (cel mai mare killer de încredere).
4. Continuă în ordinea priorităților (§4): Features → Omnichannel → Partners/Pricing → restul.
5. Pentru `InAction.dc.html` (6.5), e fișier separat — lipești promptul acolo.
6. Bifează „De verificat" (§5) înainte de publicare.
