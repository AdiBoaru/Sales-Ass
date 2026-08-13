# Boundary WebWidget v2 — cine deține ce

> **Statut:** normativ pentru `web-view.v2` (NX-228). Inert în producție: contractul există, dar
> nicio rută nu îl servește încă (NX-232/233). V1 rămâne neatins până la cutoverul NX-249.
>
> Sursa contractului: [`src/web/contracts_v2.py`](../src/web/contracts_v2.py) ·
> Forma pentru FE: [`FRONTEND-CONTRACT-IZI-V2.md`](FRONTEND-CONTRACT-IZI-V2.md) ·
> Cardul: [`tasks/stage1/NX-228.md`](../tasks/stage1/NX-228.md)

## 1. Enunțul

Frontendul WebWidget **nu este un al doilea motor conversațional**. Primește un ViewModel
display-ready și îl randează cu `switch(block.type)`. Nu decide, nu deduce, nu calculează și nu
repară semantic răspunsul backendului.

Asta **nu** înseamnă remote-code UI. Contractul permite exclusiv un union discriminat finit de
blocuri și tokenuri semantice allowlisted. Backendul deține semantica și ordinea; **frontendul
deține layoutul, tema, accesibilitatea și siguranța randării.**

## 2. De ce, concret

Nu e o preferință de arhitectură. În v1, aceste reguli comerciale trăiesc în browser, fără
validator și fără test:

| Ce face frontendul azi | Unde | De ce e o problemă |
|---|---|---|
| Calculează procentul de reducere | [`ChatProductCard.jsx:266`](../../Sales%20MVP%20Frontend%20Final/src/components/store/ChatProductCard.jsx) | Regula de discount are două implementări. Backendul nu o poate valida și nu o poate schimba singur. |
| Ghicește tonul unui badge dintr-un regex peste cuvinte românești | `inferBadgeTone`, `:64` | Sensul se deduce din ETICHETĂ. Un badge nou în altă limbă cade tăcut pe „neutral". |
| Parsează prețuri din string cu euristici de virgulă | `chatClient.js:103` | „1.234,56" vs „1,234.56" — o interpretare greșită schimbă prețul cu trei ordine de mărime. |
| Alege comportamentul CTA după `offer.kind` | `ChatOffer.jsx:29`, `chatClient.js:273` | Semantica acțiunii se reconstruiește în client dintr-un enum. |
| Compune mesaje din numele produsului | `ChatProductCard.jsx:304` | „Spune-mi mai multe despre {nume}" e voce de client fabricată de UI. |
| Acumulează criterii de conversație | `ChatWidget.jsx:404` | A doua memorie, care diverge de a serverului la primul refresh. |
| Mută coșul/wishlist-ul în `localStorage` | `ChatProductCard.jsx:253`, `ChatWidget.jsx:240` | Asistentul pretinde o mutație pe care serverul nu a văzut-o. |
| Interpretează `stock: 0` | contract v1 §2 | Disponibilitatea e un fapt live; interpretarea lui e o decizie comercială. |
| Mapează `RON → „Lei"` | `ChatProductCard.jsx:77` | Localizarea monedei e conținut, nu stil. |

Plus microcopy hardcodat în componente: „Spune-mi mai multe", „De ce ți-l recomand",
„Funcționalități principale", „De luat în calcul", „Ideală pentru", „Evită dacă", „POTRIVIRE".
Toate sunt text comercial, în română, într-un renderer care se pretinde agnostic.

Și invers: frontendul consumă câmpuri pe care backendul **nu le emite niciodată** — `brand`,
`score`, `why`, `best`, `avoid`, `pros`, `cons`, `changes`, `highlights`, `meta`. Sunt
fixture-only. Un contract în care jumătate din câmpuri există doar în mock-uri nu e un contract.

## 3. Matricea de ownership

`source of truth` = de unde vine valoarea. `validator` = ce o oprește dacă e greșită.
`renderer` = ce face FE cu ea. Fiecare câmp are **exact un** proprietar.

### 3.1 Conținut și comerț — backend, fără excepție

| Câmp v2 | Owner | Source of truth | Validator | Renderer FE |
|---|---|---|---|---|
| `blocks[].text` | agent + projector | AnswerPlan (NX-211/240) | validator determinist + critic | escapează și afișează |
| ordinea blocurilor | projector | AnswerPlan | contract (listă ordonată) | randează în ordinea primită |
| `price.current` | projector | `products.price` / variantă | `validate_web_view_v2` vs sursă | afișează textul |
| `price.previous` | projector | `products.list_price` | `previous > current`, altfel respins | afișează tăiat |
| `price.discount` | **projector** | calculat din pereche | recalculat și comparat (±1pp) | afișează eticheta |
| moneda | projector | DomainPack | inclusă în textul prețului | — (nu mai mapează nimic) |
| `availability` | projector | `products.availability` / `stock` | grounding vs sursă | afișează textul |
| `rating` | projector | `products.rating` + `review_count` | grounding vs sursă | afișează textul |
| `badges[].label` | projector | reguli de badge server-side | vocabular + limită | afișează |
| `badges[].tone` | **projector** | regula care a produs badge-ul | `Literal` închis | mapează ton → culoare |
| `reason` | agent | evidence per produs | grounding | afișează |
| `comparison.headers/rows/cells` | projector | Facts/Evidence (NX-205) | aliniere 1:1 impusă de model | randează tabelul |
| `memory.criteria` | ConversationStateV2 (NX-235) | state server-side | contract | afișează snapshotul |
| `cart_summary.*` | commerce adapter (NX-237) | coș canonic + receipt | receipt obligatoriu | afișează |
| `notice.*` | projector | fallback/no-result/recovery | P6 | afișează |
| `status_list[].freshness` | projector | momentul citirii | text, nu timestamp | afișează |

### 3.2 Acțiuni — semantica e a serverului, gestul e al clientului

| Câmp v2 | Owner | Source of truth | Validator | Renderer FE |
|---|---|---|---|---|
| `action.label` | projector | copy server-owned | lungime de chip (40) | afișează |
| `action.appearance` | projector | rolul acțiunii | `Literal` închis | mapează pe stil |
| `activation.token` | NX-236 | token semnat, legat de tur | semnătură + one-shot | **retransmite NESCHIMBAT** |
| `activation.href` | projector | `products.product_url` | allowlist de scheme + catalog | navighează |
| `action.enabled` | projector | stare server-side | contract | dezactivează controlul |

Frontendul **nu** citește tokenul, **nu** îl compune, **nu** îl completează și **nu** deduce ce
face un buton din eticheta lui. În v1 `Chip.payload` se pierdea la
[`render.py:178`](../src/channels/web/render.py) și rămânea doar textul — adică semantica se
recupera ghicind. În v2 eticheta e pentru ochi, tokenul e pentru mașină.

### 3.3 Chrome, composer, a11y — server-owned, obligatoriu

| Câmp v2 | Owner | Validator | Renderer FE |
|---|---|---|---|
| `chrome.launcher_label` … `new_chat_label` | projector | non-blank, obligatoriu | accessible name |
| `composer.label` / `placeholder` / `send_label` | projector | non-blank, obligatoriu | label + placeholder |
| `composer.enabled` | executor (NX-233/243) | contract | single-flight: input inactiv |
| `a11y.announcements.*` (toate 6) | projector | toate 6 obligatorii, non-blank | live region |
| `progress.label` / `detail` | executor | interzis pe terminal | stare reală, fără CoT |

Un label lipsă sau gol **nu** e „FE pune ceva implicit" — e contract invalid. Altfel microcopy-ul
comercial se întoarce în browser pe ușa din dos.

### 3.4 Ce rămâne al frontendului

Layout, grilă, spațiere, tipografie, temă, paletă, breakpointuri, animații; `role`/`focus`/
`aria-live politeness`; deschis/închis, scroll, draft, expanded/collapsed; lifecycle de
transport (bootstrap, `client_turn_id`, `turn_id`, pending, reconnect, recovery); validarea
structurală a JSON-ului și escaparea la randare; maparea `tone`/`appearance`/`icon` pe
componente și CSS.

Astea sunt decizii de prezentare. Nu sunt decizii despre ce e adevărat.

## 4. Invarianții impuși de tip

1. **`extra="forbid"` + union finit.** Un `block.type` necunoscut sau un câmp în plus respinge
   **întreg** payloadul. Fără skip/omit best-effort: un renderer care sare peste ce nu înțelege
   afișează un răspuns pe jumătate și îl numește succes.
2. **Terminalele nu pot fi goale.** `completed|failed|cancelled` cer minimum un bloc cu conținut,
   impus de `model_validator`. Un `divider` nu contează — nu e un răspuns. (P6)
3. **Zero numere afișabile.** Prețul e `"89,00 lei"`, reducerea e `"-18%"`, stocul e
   `"Ultimele 3 bucăți"`. Un `float` în contract e o invitație la aritmetică în browser, deci nu
   există niciunul.
4. **Copy obligatoriu.** Chrome, composer și toate cele șase anunțuri sunt non-blank. Whitespace
   se strip-uiește înainte de verificare: „gol deghizat" e tot gol.
5. **Untrusted by default.** `PageContextClaim` se numește *claim* pentru că e ce **afirmă**
   browserul. Adevăr abia după rehidratarea NX-234. `business_id` nu apare deloc în request —
   e server-owned (P7).

## 4bis. Marginea: tenant, sesiune, identitate (NX-229)

Ownership-ul de mai sus presupune că serverul știe *al cui* e requestul. Cum se stabilește asta e
tot o graniță, cu aceeași regulă: frontendul **forwardează** credențiale, nu le interpretează.

| Credențial | Cine îl emite | Ce face FE cu el | Ce dovedește |
|---|---|---|---|
| `widget_public_token` | noi, per canal | îl trimite | care tenant — **nu** că apelantul e autorizat (e public) |
| `visitor_id` + `sig` | backend, la bootstrap | le păstrează și le retrimite opac | sesiune emisă de noi, neexpirată, legată de token și (opțional) de origin |
| `id_token` | site-ul gazdă | îl forwardează **neschimbat**, în body | identitatea shopperului |
| `Authorization: Bearer` | site-ul demo | îl trimite dacă gazda i-l dă | drept de acces la site — **niciodată** cine e clientul |

`business_id` nu apare în niciunul. E derivat exclusiv server-side din tokenul public.

Trei consecințe care schimbă ce trebuie să facă frontendul:

- **Sesiunile expiră.** În v1 nu expirau niciodată. Un `403` pe o sesiune veche nu e un bug — e
  contractul; FE cere bootstrap nou și continuă. Restaurarea transcriptului rămâne server-side.
- **`Origin` se verifică pe toate endpointurile**, nu doar la bootstrap, și trebuie să fie exact
  cel allowlistat (subdomeniu ≠ același origin, port ≠ același origin).
- **`Authorization` nu produce identitate.** Dacă gazda vrea un client identificat, emite
  `id_token`; headerul nu e o scurtătură.

Detaliile — scenarii de atac, procedura de rotație a cheii și ce **nu** apără marginea — sunt în
[`WEB-EDGE-THREAT-MODEL.md`](WEB-EDGE-THREAT-MODEL.md).

## 4ter. Ciclul de viață al unui turn v2 (NX-233) — accept durabil, executor, recovery

Requestul HTTP **nu mai rulează pipeline-ul**. `POST /web/v2/turns` doar acceptă durabil
(ledgerul NX-232 + inputul SAFE, într-un singur checkout) și răspunde `202`; un **executor
separat** (în procesul worker) revendică turnul cu lease + epoch fencing, rulează pipeline-ul și
comite terminalul atomic (rezultat + mesaj + stare). `GET /web/v2/turns/{id}` întoarce statusul
sau **exact** ViewModelul terminal (proiecția deterministă a payload-ului persistat); SSE
(`…/events`) e opțional și transportă doar schimbări reale de status + rezultatul deja comis —
niciodată tokeni LLM, draft sau chain-of-thought. Pollingul rămâne fallback-ul autoritativ.

| Aspect | Regula |
|---|---|
| stări DB (NX-232, neschimbate) | `accepted → running → completed\|failed\|cancelled` |
| stări de sârmă | `accepted → working → validating → terminal` (`running` nu iese; `working/validating` = proiecții din `running`) |
| single-flight | un turn activ per conversație (partial unique); al doilea ID → `409 conversation_turn_in_progress` cu referința turnului activ |
| lease / heartbeat | `WEB_TURN_LEASE_TTL_S=300` (aliniat CLAIM_TTL_S) / `WEB_TURN_HEARTBEAT_S=60`; renew CAS pe owner+epoch |
| deadline | `deadline_at` fixat la accept (`WEB_TURN_DEADLINE_S`); **nu** se prelungește la reclaim |
| attempts | `WEB_TURN_MAX_ATTEMPTS=3`; peste → terminal onest `attempts_exhausted` |
| wake | listă Redis best-effort, DUPĂ commit; pierderea lui e acoperită de scanul executorului + sweeper (**DB = autoritatea**) |
| recovery | sweeper bounded (advisory lock): re-wake `accepted`, reclaim lease expirat, terminalizează peste deadline/attempts cu error-view randabil (P6) |
| autorizare GET/SSE | tenant din sesiune + `session_ref_hash(token, visitor)` — exact sesiunea care a creat turnul; altcineva → 404 indistinct |
| composer | `enabled=false` pe orice status ne-terminal e UX (NX-243), nu mutex: single-flight-ul real e în DB |

Frontendul (NX-243) generează `client_turn_id`, trimite `WebTurnRequestV2`, păstrează `turn_id`
și afișează ce primește. Nu alege workerul, nu calculează retry semantic, nu schimbă ID-ul la
refresh, nu inventează progres și nu decide dacă un error e sigur de retrimis.

## 4quater. Contextul de pagină (NX-234) — ID-uri de la browser, fapte de la server

Frontendul REAL are deja starea paginii și un coș local. Are, adică, tot ce-i trebuie ca să
trimită „produsul curent" cu nume, preț și stoc — și fix aici moare boundary-ul, fiindcă în clipa
în care backendul crede acele câmpuri, browserul devine al doilea motor comercial: fără validator,
fără test, fără tenant. `PageContextClaim` se numește *claim* din motivul ăsta.

Ce trece frontiera: **tipul suprafeței și identificatori opaci**. Atât.

| Câmp | Cine îl afirmă | Ce face serverul | Ce devine |
|---|---|---|---|
| `surface` | browser | enum finit; suprafață necunoscută → `other` | decide CE ID-uri au sens |
| `product_id` | browser | lookup tenant-scoped pe `products.id` (UUID) **sau** `products.external_id` (cheia platformei) | `ProductEvidence` cu `source` + `freshness`, sau nimic |
| `variant_id` | browser | trebuie să fie varianta ACESTUI produs | `VariantEvidence`, sau context `invalid` |
| `category_id` | browser | `categories.id` sau `slug`; compatibilă cu produsul (materialized path) | `CategoryEvidence`, sau ignorată |
| `cart_ref` | browser | **nicio sursă canonică până la NX-237** | `unavailable` cu motiv (niciodată `ConversationState.cart`) |
| `locale` | browser | intersecție cu `businesses.supported_locales` | limba negociată SERVER-side, cu reason code |
| `business_id` | — | **nu apare în request și nu se adaugă niciodată** (P7) | derivat din sesiune |

Reguli care nu depind de disciplină:

- **Un câmp comercial în `context` = `422 context_commercial_field`**, înainte de accept, deci
  înainte de fingerprint și de orice muncă. `extra="forbid"` e mecanismul; codul dedicat e ca
  „browserul încearcă să dicteze prețul" să se vadă în metrici, nu ca `schema_invalid` generic.
- **Suprafața decide ce ID are sens.** Un `product_id` afirmat pe `home` se ignoră cu reason code:
  o ancoră pe care nimeni n-a navigat e exact mecanismul prin care s-ar putea ancora conversația
  pe orice produs.
- **Un ID de la alt tenant e indistinct de unul inexistent** (și de unul nepublicat). Aceeași
  semantică externă, deliberat — altfel statusul devine un oracol de existență.
- **Contextul intră în fingerprint-ul de idempotency.** Aceeași întrebare de pe două pagini nu e
  același turn: refolosirea `client_turn_id` după navigare e `409`, nu un turn vechi cu context nou.
- **Nimic din context nu e „adevăr" în request.** Faptele apar abia din catalog, o singură dată
  per turn, într-un `TurnSnapshot` imuabil — promptul, tool-urile și projectorul citesc din el, nu
  fac fiecare lookupul lui (altfel două prețuri diferite în același răspuns).
- **Frontendul nu repară un context stale** și nu face fallback la propriile date comerciale.
  Un context invalid se rezolvă server-side sau nu se rezolvă deloc.

Acoperirea și prospețimea faptelor (ce e `UNKNOWN` și de ce):
[`WEB-CONTEXT-DATA-READINESS.md`](WEB-CONTEXT-DATA-READINESS.md).

## 5. URL-uri

Permis: `https://` absolut, sau rută relativă care începe cu `/`.
Interzis: `javascript:`, `data:`, `file:`, `vbscript:`, `blob:`, `about:`, protocol-relativ
(`//host`), `http://` în clar, orice URL cu whitespace intern sau backslash.

Verificarea e o **allowlist pe valoarea parsată**, nu un regex de blocare: o listă de lucruri
interzise se ocolește, o listă de lucruri permise nu.

## 6. Ce NU garantează contractul — citește asta

**JSON Schema publicat e necesar, dar nu suficient.** Allowlistul de URL trăiește într-un
`model_validator`, deci nu apare în JSON Schema. Un client care validează **doar** cu JSON Schema
ar accepta `javascript:`. De aceea serverul validează întotdeauna prin Pydantic înainte de
livrare, iar schema publicată e contract de **formă**, nu poartă de securitate.
Fixat de `test_json_schema_is_necessary_but_not_sufficient`.

**Contractul nu sanitizează proza și nu pretinde că o face.** Nu există niciun câmp care să
însemne „randează ca HTML" — `text` e text, iar escaparea e treaba rendererului. Unghiularele
dintr-un mesaj ajung la FE ca șir literal.

**`cart_summary` e contract-ready, dar nu se emite până la NX-237.** Până există coș canonic
server-side cu receipt, asistentul poate doar `navigate` către o pagină validată sau poate omite
CTA-ul. Nu are voie să pretindă că a adăugat ceva printr-un bridge ascuns către `localStorage`.

## 7. Versionare și rollout

- Major-ul e în **nume** (`web-view.v2`), nu într-un câmp numeric. Major necunoscut ⇒ clientul
  refuză contractul și afișează doar eroarea tehnică locală minimă.
- Backendul publică JSON Schema cu **hash stabil**, fixat ca snapshot în
  `tests/test_web_contract_v2.py`. Dacă testul pică, contractul s-a schimbat — actualizarea
  constantei e o decizie conștientă, nu un fix de copiat orbește.
- Un rollout minor/aditiv cere **capability/schema-hash negotiation** înainte de trafic:
  `negotiate_schema()` refuză să livreze un schema pe care clientul nu l-a acceptat.
- Fallbackul terminal e întotdeauna un bloc **cunoscut** din schema negociată.
- V1 și v2 au endpointuri și randori separați. Fără aliasuri care ghicesc câmpuri.

## 8. Compatibilitate și deprecare

| Etapă | V1 | V2 |
|---|---|---|
| NX-228 | activ, neatins | contract inert, fără rută |
| NX-232 → NX-241 (acum: NX-233 livrat) | activ | rute + executor + SSE în spatele flagurilor (`WEB_TURN_V2_ENABLED` etc., toate OFF) |
| NX-249 canary | reactivabil fără pierderea turelor acceptate | trafic canary |
| După cutover NX-249 | eliminat | singurul contract |

**Niciun card nu are voie să modifice v1 in-place.** `docs/FRONTEND-CONTRACT-IZI.md`,
`src/channels/web/render.py`, `tests/fixtures/web_response/payloads.json` și
`validate_web_payload` rămân exact cum sunt. Eliminarea v1 se face doar prin NX-249.
