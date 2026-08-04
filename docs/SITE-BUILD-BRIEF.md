# Nativx Assistant — Site Build Brief (prompt pentru Claude Code)

> **Cum folosești acest fișier:** deschide o sesiune Claude Code nouă (într-un folder de proiect nou, gol) și dă-i ca instrucțiune: *„Read `docs/SITE-BUILD-BRIEF.md` and build the premium presentation site exactly as specified. Start with the homepage MVP."* Acest brief e self-contained.
>
> **Stil:** explicațiile sunt în română; **tot copy-ul de pe site rămâne în ENGLEZĂ** (decizie de brand — pagina e în engleză). Dacă vrem versiune RO, e un fast-follow separat.
>
> **Regula de aur (non-negociabilă):** zero metrici inventate, zero testimoniale/ logo-uri/ clienți falși, zero timpi de setup contractuali. Fiecare afirmație = capabilitate reală sau mecanism. Orice cifră/produs/preț dintr-un mockup e **doar un stand-in etichetat** „Sample — your real data replaces this".

---

## 0. Ce construim și de ce

Un **site de prezentare premium** (nivel ~10.000 €) pentru **Nativx Assistant**, un asistent de vânzări AI pentru magazine online, livrat ca **serviciu managed** de Nativx Technology.

Site-ul trebuie să pară matur, clar, credibil, modern și orientat spre conversie — NU un template generic de SaaS. Obiectivul unic al fiecărei pagini: **du vizitatorul la „Book a demo".**

**Principiul central al acestui brief (citește-l de două ori):**
Site-ul vinde **IDEEA / MOTORUL**, nu un catalog anume. Produsul e un **motor generic de vânzări conversaționale** care rulează pe **datele fiecărui magazin** (produse, prețuri, promoții, politici) — date pe care Nativx le ingerează și le reglează la onboarding. **Fiecare client are alte date.** De aceea:

- Nu vindem „un asistent de beauty" (beauty e doar unul dintre verticale/demo-uri interne).
- Nu ne bazăm pe „datele/rezultatele noastre" — nu avem ce arăta din datele altcuiva; arătăm **capabilitatea + garanțiile**, care sunt adevărate indiferent de catalog.
- Orice produs/preț dintr-un vizual e un **stand-in explicit** pentru „aici vor fi datele tale".

---

## 1. CE ESTE PRODUSUL — ideea, nu datele (INIMA BRIEF-ULUI)

Aceasta e descrierea corectă a produsului. Fiecare capabilitate e o **proprietate a motorului**, adevărată pe orice catalog. Construiește tot copy-ul din ea.

### 1.1 Într-o frază
**The AI sales assistant for online stores — set up and run by Nativx. It sells across your website and WhatsApp, from your own catalog, and never quotes a price your catalog doesn't have.**

### 1.2 Ce este (framing corect)
Nu e „încă un chatbot". E un **vânzător digital** construit în jurul **datelor magazinului tău**: produse, prețuri, promoții, politici, comenzi. Un magazin mare și-l construiește intern cu o echipă AI — Nativx îl livrează la cheie pentru magazine care nu au echipă AI.

### 1.3 Principiul „datele sunt ale tale" (trebuie să transpară pe tot site-ul)
Motorul e **agnostic de date și de vertical**:
- Rulează pe catalogul TĂU, orice ai vinde.
- La setup, Nativx îl reglează pe categoriile, tonul, FAQ-ul și politicile tale.
- Ce vezi în orice demo/mockup e **catalog de exemplu** — produsele, prețurile și linkurile tale reale îl înlocuiesc la lansare.

Mesaj de folosit: *„It runs on your catalog, whatever you sell. What you see in the demo is a stand-in — your real products, prices and links replace it."*

### 1.4 Ce FACE (capabilități, descrise generic — nu legate de niciun catalog)

1. **Vinde, nu doar răspunde.** Citește ce caută clientul și recomandă produsul potrivit din **catalogul tău** — ca cel mai bun vânzător din magazin.
2. **Nu inventează niciodată un preț.** Un validator determinist verifică fiecare preț, produs și link contra catalogului tău real **înainte** de trimitere. Preț greșit / link greșit → mesajul nu pleacă. E o garanție **structurală**, adevărată pe orice date — nu o promisiune de prompt. (Acesta e diferențiatorul principal.)
3. **Un singur asistent pe toate canalele.** Același „creier" răspunde pe **website (widget)** și **WhatsApp** (Telegram = canal de test). Nu trei boți, unul singur.
4. **O singură memorie a clientului.** Același client pe două canale = un singur istoric; nu repetă ce a spus deja.
5. **Vorbește limba clientului.** Detectează limba per mesaj și răspunde în **română, maghiară sau engleză**.
6. **Recuperează coșuri abandonate.** Reia conversația și readuce clientul la checkout, pe canalul pe care era deja (cu respectarea consimțământului).
7. **Urmărește comenzi și livrare.** Răspunde în chat despre statusul comenzii, în loc de un tichet.
8. **Predă curat către om.** Când e nevoie de o persoană, se oprește și transferă conversația cu tot contextul — fără dublu-răspuns.
9. **Nu tace niciodată.** Dacă modelul se blochează, degradează controlat spre un răspuns sigur — pe orice cale iese ceva spre client.
10. **Transformă conversațiile în date de cerere.** Ce cer clienții, ce **nu găsesc** (cerere neîmplinită), ce conversații au dus la comenzi. *(A se prezenta onest ca „in the making / early" — vezi §3.)*
11. **Complet gestionat.** Nativx face setup-ul, ingerează catalogul, reglează tonul, testează privat și operează lunar. Clientul nu angajează o echipă AI.
12. **Sigur și izolat prin construcție.** Multi-tenant (fiecare magazin izolat), PII (telefon) izolat, ștergere GDPR la cerere, plafon de cost zilnic, filtrare de abuz, se identifică mereu ca AI.

### 1.5 Ce îl face diferit (moatul)
Nu e „mai creativ" decât alte AI-uri — e **mai sigur, mai util și mai vandabil**:
- **Catalog-accurate by design** (validatorul determinist) — nu inventează prețuri/produse.
- **Un creier, toate canalele** — nu integrări separate.
- **Managed** — rezultat + liniște, nu un proiect software.

### 1.6 Cum se cumpără (model)
Serviciu **managed**: **setup fee unic + retainer lunar**, scalat după mărimea catalogului și volumul de conversații. Fără preț de listă public.

### 1.7 Ce NU este (evită în copy)
Nu e: chatbot generic, magician AI, jucărie tech, platformă abstractă de automation, tool de o singură categorie.

---

## 2. Poziționare & Hero

**Promisiunea principală (una singură):**
> Every conversation can become a sale — but every answer stays grounded in your real catalog.

**Poziționare recomandată (hibrid):** ancorată pe *catalog-accurate* (moatul), ambalată în *managed sales assistant* (modelul), cu *demand intelligence* ca al doilea câștig onest.

**Hero (folosește exact):**
- **Eyebrow:** `AI sales assistant for online stores — set up and run by Nativx`
- **H1:** `Your best salesperson — on your website and WhatsApp, 24/7.`
- **Subhead:** `It recommends products, recovers carts and answers your customers in Romanian, Hungarian and English — from your own catalog, and it never quotes a price your catalog doesn't have. Fully managed by Nativx.`
- **CTA primar:** `Book a demo` · **CTA secundar:** `See a live conversation`
- **Trust line:** `The kind of in-house AI sales assistant the biggest online stores build for themselves — now available for yours, fully managed.`

**Unghi de vânzare din „datele sunt ale tale" (folosește-l ca reasigurare recurentă):**
> We don't show you a canned demo with someone else's numbers. In 20 minutes, we run it on **your** catalog and you watch it answer **your** customers.

---

## 3. Garanții de onestitate (allowed / forbidden)

**PERMIS (dovedit / adevărat pe orice date):**
- „recommends products from your real catalog"
- „checks every price, product and link against your catalog before sending"
- „never invents a price / never gives a medical claim"
- „website + WhatsApp; Telegram available for testing"
- „Romanian, Hungarian and English"
- „one customer history across channels (where identity is linked)"
- „hands off to a human", „always identifies as AI"
- „GDPR: phone numbers isolated, erasable on request"
- „multi-tenant, each store isolated", „daily cost cap", „abuse filtered"
- „managed service: setup fee + monthly retainer", „test it privately before go-live"

**INTERZIS până există dovadă contractuală/de cod:**
- ❌ orice procent / ROI / „+X% sales" / timp de răspuns („<Ns")
- ❌ testimoniale, nume de clienți, logo-uri, case studies inventate
- ❌ „native integration Shopify/Woo/Magento/PrestaShop" (conectori nativi NU există → spune „we ingest your catalog, whatever platform you run")
- ❌ „Telegram production" (e canal de TEST)
- ❌ „EU-hosted / data in EU" (neverificat)
- ❌ „sentiment analysis", „ad/ROAS attribution" (nu există)
- ❌ „live in 24/48h" ca angajament
- ❌ memorie/profilare pe termen lung ca feature matur (e early)
- ❌ orice text de tip placeholder: `[sample]`, `+X%`, `<Ns`, `[Company]`, `[Name]`

**Demand intelligence (§1.4 pct.10):** prezintă-l ca **early/onest**, nu ca dashboard finit. Formulare OK: *„Turns conversations into demand data — what customers ask for and can't find."* Fără KPI-uri inventate; dacă arăți un dashboard, etichetează-l „Sample data — your live numbers once connected".

---

## 4. Structura site-ului

**Nav (top):** How it works · Features · Channels · Compare · FAQ · **[Book a demo]**
(Fără Partners/Investors în top nav — eventual link discret în footer.)

**V1 (MVP) = o homepage completă (single page)** cu secțiunile din §5. Sub-paginile (Security, Use Cases per vertical, About) le stubuiești ca rute goale pentru v1.5.

---

## 5. Homepage — secțiuni în ordine (cu scop psihologic)

Pentru fiecare: **titlu + scop + vizual + CTA**. Copy-ul detaliat în §6.

1. **Hero** — claritate în 5s (CE/UNDE/PENTRU CINE) + moatul. Vizual: mockup dual **Website ⇄ WhatsApp** care rulează o conversație scurtă (vezi §7). CTA: Book a demo / See a live conversation.
2. **Problem** — `Every day your store loses sales it never even sees.` 4 carduri egale (after-hours · coș abandonat · echipă sufocată · cerere invizibilă) + linia-pivot `None of this is a staffing problem. It's a coverage problem.` Scop: recunoaștere + reframing.
3. **Product explanation** — `It sells, it doesn't just chat.` Card-hero cu **validatorul** (mecanism). Scop: din „ce chatbot?" în „ce vânzător".
4. **Feature highlights** — `Built to sell, not to chat.` Grid de 6-8 carduri (din §6, mecanism concret fiecare, openere variate). Scop: profunzime prin specificitate.
5. **Pipeline visual (diferențiator premium)** — `From your customer's message to a safe answer — in one pass.` Diagramă animată pe scroll: `message → triage → agent → catalog validator → reply`, evidențiind că LLM-ul e „încadrat" de cod determinist. Scop: face ingineria vizibilă = premium/credibil (nimeni din piață nu arată asta). Vezi §7.
6. **Channels** — `One salesperson. Every channel. One memory of every customer.` Website widget · WhatsApp · Telegram (test) + „one customer history" + RO/HU/EN. Scop: un creier, nu 3 boți.
7. **Proof / Trust** — `Honest by construction.` În loc de testimoniale false → capabilități-dovadă (validator, GDPR erase, „test it privately first"). Scop: încredere fără fabricație.
8. **Compare / Objections** — `Closer to a trained sales rep than a chatbot — without the manual work.` Tabel Nativx vs basic chatbot vs by-hand, cu cei 2 diferențiatori reali sus (never invents a price, attributed revenue). Scop: dezarmează „doar un chatbot".
9. **How it works** — `Live in days. Zero technical work for you.` 4 pași: ingest catalog → connect channels → private test → go live & tune. Scop: reduce frica de implementare.
10. **FAQ** — `What merchants ask before they switch it on.` 9 Q&A ordonate pe anxietate (accuracy first) — vezi §6.6.
11. **Final CTA + form** — `See it run on your own catalog.` Sub-hero + form 2 câmpuri + WhatsApp secundar. Scop: conversie cu fricțiune minimă.
12. **Footer** — brand tagline, nav real (fără linkuri moarte), „AI sales assistant … by Nativx Technology", link discret „Partner with us", copyright fără an hardcodat.

---

## 6. Copy Bank (EN — folosește direct)

### 6.1 Headline-uri (hero + secțiuni)
1. Your best salesperson — on your website and WhatsApp, 24/7.
2. The AI sales assistant that never invents a price.
3. It sells. It doesn't just chat.
4. Every answer, checked against your real catalog.
5. One salesperson. Every channel. One memory of every customer.
6. Closer to a trained sales rep than a chatbot — without the manual work.
7. It runs on your catalog, whatever you sell.

### 6.2 Subheadline-uri
- Recommends products, recovers carts and answers customers — every price and link checked against your live catalog before it sends.
- We set it up on your catalog, tune it to your store, and run it for you. No AI team required.
- The same assistant on your website and WhatsApp — one customer history, no repeating.
- It only quotes prices and links that exist in your store. Structurally, not by prompt.

### 6.3 CTA-uri
Primary: `Book a demo` · `Book my 20-minute demo` · `See it on your own catalog`
Secondary: `See a live conversation` · `Message us on WhatsApp` · `See how it works`

### 6.4 Feature cards (titlu + body)
1. **Never invents a price** — every price, product and link is checked against your live catalog before sending.
2. **Recommends, doesn't just answer** — reads what the shopper needs and suggests the right product from your catalog.
3. **Recovers abandoned carts** — follows up on the channel the customer was already using.
4. **One customer, one history** — website, WhatsApp or Telegram, it's the same conversation.
5. **Speaks RO, HU & English** — detects the customer's language per message and replies in it.
6. **Hands off cleanly** — brings in your team with the full context, no double replies.
7. **Tracks orders and delivery** — the answer arrives in the chat, not in a ticket.
8. **Never goes silent** — if the AI stalls, it degrades to a safe answer, never nothing.
9. **Tuned to your store** — set up around your catalog, categories and policies at onboarding.
10. **Turns conversations into demand data** — what customers ask for, and what they can't find. *(early)*

### 6.5 Obiecții + răspunsuri
- *„It's just a chatbot."* → It's built around your catalog, prices and policies — it recommends, validates and sells, and never quotes a price you don't have.
- *„AI will invent prices."* → It can't. A deterministic validator checks every price and link against your live catalog before sending. Wrong number → it doesn't go out.
- *„No time/team to integrate."* → You integrate nothing. We ingest your catalog and connect your channels for you.
- *„It'll annoy customers."* → When it can't help, it hands off to your team with full context — no dead ends.
- *„GDPR?"* → Phone numbers are isolated and never appear in logs; any customer can be fully erased on request.
- *„My catalog is complex."* → It's tuned to your store and answers only from your real data — specific, not generic.
- *„How do I know it makes money?"* → Tracked checkout links tie orders back to the conversation — assisted vs. bot-led.
- *„What platform do you support?"* → We ingest your catalog whatever platform you run — no native connector required.
- *„How much?"* → A one-time setup plus a monthly retainer, scoped to your catalog and volume. Book a demo and we'll scope it.

### 6.6 FAQ (9 items, accuracy first)
1. **Does it ever quote a wrong price or invent a product?** No. It can't quote a price or link a product that isn't in your live catalog. Every reply is checked against your real product data before it sends — if a detail can't be verified, it asks a clarifying question or hands off instead of guessing.
2. **What does it actually do, and where?** It recommends products, recovers abandoned carts and tracks orders — on your website widget and WhatsApp, with Telegram available for testing. One assistant, the same answers on every channel.
3. **How do we get started — do I integrate anything?** You integrate nothing. It's a managed service: we ingest your catalog and connect your channels for you, whatever platform your store runs on. You review it in a private test before it talks to a customer.
4. **Is it GDPR-compliant?** Yes. Contact details are isolated to a single secured store and phone numbers never appear in our logs. Any customer can be fully erased on request.
5. **Which languages does it speak?** Romanian, Hungarian and English. It detects the customer's language per message and replies in it.
6. **What happens when a conversation needs a person?** It hands over to your team with full context and goes quiet, so there are no double replies. You decide when it resumes.
7. **What do I learn as a merchant?** Demand data from real conversations: what customers ask for, what they can't find, and how cart recovery plays out.
8. **How am I billed?** A one-time setup fee plus a monthly retainer, scoped to your catalog and conversation volume. We set it up, tune it and run it.
9. **Who is this built for?** Online stores — the kind of in-house sales assistant the biggest stores build for themselves, delivered as a managed service so you don't have to build one.

### 6.7 Final CTA + form
- Heading: `See it run on your own catalog.`
- Sub: `In a 20-minute call, watch the assistant answer questions and recommend from your own catalog — on your website and WhatsApp, in Romanian, Hungarian and English.`
- 3 reasigurări: `We set it up on your catalog — no work on your side.` · `It drafts real replies for you to approve before any reach a customer.` · `Managed monthly — cancel anytime.`
- Form: Name · `WhatsApp number or email` (un câmp, acceptă ambele) · (opțional) Store URL. Buton: `Book my 20-minute demo`. Microcopy: `We only use this to set up your demo — no spam. We'll reach out within one business day.`
- WhatsApp secundar: `Prefer to chat first? Message us on WhatsApp.`

---

## 7. Vizualuri & mockup-uri — regula „datele sunt ale tale"

**Regula:** orice produs/preț/rating/număr dintr-un vizual e **stand-in**, etichetat clar. Nu prezenta nicio cifră ca rezultat real.

- **Hero mockup dual (Website ⇄ WhatsApp):** aceeași conversație scurtă (2 ture: întrebare → recomandare → „something cheaper" → recomandare revizuită), în ambele stiluri de canal, cu toggle. Product cards cu preț/rating/delivery. Caption: `Sample catalog — your real products and prices replace this.` Prețuri în **€**.
- **Recomandare puternică pentru „idee, nu date":** pune un **vertical switcher** în zona de demo (Beauty · HVAC · Auto · Salon). Când comuți, se schimbă catalogul de exemplu, dar **asistentul e același** — comunică vizual „**același motor, catalogul tău, orice ai vinde**". Acesta e cel mai clar mod de a arăta ideea peste date.
- **Pipeline visual:** 4-5 noduri animate pe scroll (`message → triage → agent → catalog validator → safe reply`), cu validatorul evidențiat. Sursă de inspirație pentru fluxul real: descrierea pipeline-ului din brief-ul de brand. Fără cod pe ecran în hero.
- **Dashboard (dacă apare):** etichetă vizibilă `Sample data — your live numbers once connected`, cifre estompate/muted.
- **Interzis vizual:** stock photos cu oameni la laptop, 3D blobs, sparkle/stele AI, gradient purple pe tot site-ul, grafice cu cifre neverificate, carduri pline de metrici.

---

## 8. Design direction (nivel 10k €)

- **Stil:** editorial-tech sobru, „dependable operator", nu „AI toy". Mult whitespace, o idee pe ecran, restrângere (max ~6 carduri/secțiune), trust spus o singură dată.
- **Paletă:** Indigo `#4F46E5` / `#6366F1` → Violet `#7C3AED` / `#9333EA` ca **accent punctual** (nu gradient purple omniprezent). Neutre dominante: near-black `#111827`, slate `#374151`, surface `#FFFFFF` / `#F9FAFB`, border `#E5E7EB`. Funcțional: success `#16A34A`. Secțiuni „trust" pe fundal închis pentru gravitate.
- **WhatsApp mockup only:** verde `#075E54`/`#128C7E`, bubble outgoing `#D9FDD3`, fundal `#EFEAE2`.
- **Tipografie:** Headings **Space Grotesk**, body **Inter**, mono accente **JetBrains/IBM Plex Mono**. Fără letter-spacing negativ agresiv. Fonturi self-hosted.
- **Layout:** grilă 12 coloane, secțiuni full-bleed alternând light/dark, carduri cu border subtil + shadow discret. **Mobile-first**.
- **Animații:** funcționale, care **demonstrează** (pipeline aprins pe scroll, mockup care rulează conversația ask→„Checking your catalog…"→answer, reveal-uri discrete). Respectă `prefers-reduced-motion`. Zero animație gratuită.
- **Iconografie:** line icons 1.5–2px, geometrice, un singur set consistent.
- **Logo:** squircle gradient indigo→violet, marca albă = **chat bubble + săgeată ascendentă** („conversation that sells"). **Evită** sparkle-ul cu 4 colțuri (clișeu competitori). Lizibil la 16px favicon. (Starter SVG în `LANDING-PREMIUM-PACK.md §6.13`.)
- **Wordmark:** `Nativx` near-black + `Assistant` indigo, Space Grotesk.

---

## 9. Stack tehnic & structură

- **Stack:** **Next.js (App Router) + TypeScript + Tailwind CSS + Framer Motion**, deploy pe **Vercel**. (Alt.: Astro dacă vrei și mai lightweight — dar Next.js e ok pentru animațiile interactive.)
- **Fără backend greu:** formularul → o Route Handler simplă care trimite email/notificare (stub pentru v1: log + TODO integrare). Analytics privacy-friendly (Plausible/Umami) — opțional.
- **Copy centralizat:** pune tot textul în `src/content/*.ts` (copy.ts, features.ts, faq.ts) ca să fie ușor de editat și, mai târziu, de tradus.
- **Structură:**
```
site/
├── src/app/            page.tsx (homepage) + /security /use-cases/[vertical] /about (stub v1)
│                       + /api/demo (form handler stub)
├── src/components/     Hero · ChatMockup(Web|WhatsApp+toggle+vertical switcher) · ProblemGrid
│                       · FeatureCard · PipelineViz · ChannelsSection · TrustProof
│                       · CompareTable · HowItWorks · FaqAccordion · DemoForm · Footer · Nav
├── src/content/        copy.ts · features.ts · faq.ts · compare.ts · verticals.ts
├── src/styles/         tokens (paletă/fonturi) · globals.css
└── public/             logo (SVG/favicon) · fonts · (mockup assets)
```
- **Nu depinde de repo-ul backend.** Site-ul e decuplat; mockup-urile sunt statice/animclient-side (nu widget-ul live).

---

## 10. Prioritizare & criterii de acceptare

**MVP (v1):** Nav · Hero dual-mockup · Problem · Features grid · Compare · How it works · FAQ · Final CTA + form · Footer + logo/favicon. Copy final din §6. Onestitate 100% (§3).

**Premium (v1.5):** PipelineViz animat · ChatMockup care rulează conversația + vertical switcher · Security page · Proof/Trust section · video demo (când există).

**V2 (după proof):** pagini SEO per vertical · About/Vision · demand-intelligence dashboard real · pilot case study.

**Definition of Done „premium 10k €":**
- ✅ Testul de 5 secunde trecut (CE/UNDE/PENTRU CINE fără scroll).
- ✅ Zero placeholder, zero metrică/testimonial inventat (grep pentru `[sample]`, `+X%`, `<Ns`, `[Company]` → 0).
- ✅ Diferențiatorul (validator catalog) e **promisiune hero**, nu microcopy.
- ✅ „Datele sunt ale tale" transpare clar (caption stand-in + vertical switcher + „on your catalog").
- ✅ O idee pe secțiune; restrângere vizuală; un singur CTA dominant.
- ✅ Cel puțin un element care **demonstrează** (pipeline sau conversație animată).
- ✅ Cuvântul „chatbot" NU apare descriind produsul (doar în compare/contrast).
- ✅ Responsive impecabil, dark/light coerent, `prefers-reduced-motion` respectat.
- ✅ Lighthouse ≥ 90 (Performance / SEO / Accessibility / Best Practices).
- ✅ Fiecare footer link e real (fără `#`/„coming soon" moarte).

**SEO homepage:**
- Title: `Nativx Assistant — AI Sales Assistant for Online Stores`
- Description: `Recommends products, recovers carts and answers customers on your website and WhatsApp — every price checked against your real catalog. Managed by Nativx.`

---

## 11. Ordine de execuție sugerată pentru Claude Code

1. Scaffold Next.js + Tailwind + Framer Motion + fonturi + tokens de culoare (§8).
2. Nav + Footer + layout global.
3. Hero cu dual-mockup (static întâi, animație după).
4. Copy centralizat în `src/content` (§6).
5. Problem → Features → Compare → How it works → FAQ → Final CTA + form.
6. Pipeline visual + animații (premium pass).
7. Vertical switcher în demo (mesajul „idee peste date").
8. Audit de onestitate (§3 + DoD §10) → build → Lighthouse.

> **Reminder final:** vindem **motorul și garanțiile**, care sunt adevărate pe orice catalog. Datele din vizualuri sunt stand-in etichetat. Dacă o secțiune nu întărește una dintre ideile — *vinde din catalogul tău · nu inventează prețuri · un asistent pe toate canalele · managed* — simplific-o sau scoate-o.
