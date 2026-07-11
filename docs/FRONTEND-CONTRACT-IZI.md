# Contract Frontend — paritate iZi (widget web)

> Sursa de adevăr a **payload-ului** pe care backendul îl emite spre widget. Backendul (repo
> „Sales Ass") produce aceste câmpuri; **randarea vizuală e în frontend**. Acest document descrie
> EXACT ce primește frontendul ca să randeze tabelul comparativ, prețul tăiat, badge-urile și
> snippet-ul de recenzii (gap-urile de paritate cu iZi/eMAG).

Randorul backend unic e [`render_web`](../src/channels/web/render.py) (NX-127): **aceeași formă**
pe ruta sincronă (`POST /web/chat` → JSON HTTP) și pe cea async (SSE, eveniment `type:"rich"`).

---

## 1. Forma răspunsului (sync + SSE)

```jsonc
{
  "content": "string",            // textul de lead (framing + coaching), deja cu disclaimer
  "products": [ /* Card */ ],     // cardurile de produs (0..6)
  "suggestions": ["string"],      // chips de follow-up (text trimis ca mesaj nou la tap)
  "offer":       { /* Offer */ }, // opțional — buton CTA (checkout/link)
  "comparison":  { /* Comparison */ } // opțional — tabel comparativ (NOU)
}
```

Pe SSE evenimentul are în plus `"id"` și `"type": "rich"`; restul cheilor sunt identice.

**Regulă generală:** cheile opționale **lipsesc** când nu există date (NU vin ca `null`). Frontendul
trebuie să citească defensiv (`card.badge && ...`), nu să presupună prezența lor.

> **Off-category (redirect onest) — fără câmp nou:** când retrievalul e o potrivire din categoria
> GREȘITĂ (ex. clientul cere „fond de ten" pe un catalog fără fonduri), backendul NU mai emite
> „👉 Recomandarea mea"; `content` devine un mesaj onest de redirect („nu am exact ce cauți, dar
> astea sunt cele mai apropiate…"), iar `products[]` rămân prezente ca **alternative apropiate**,
> nu ca match exact. Contract identic (doar text de `content`) — FE nu are nimic de schimbat; e util
> de știut că uneori cardurile sunt „cele mai apropiate", nu „exact ce ai cerut".

### Input — login passthrough (NX-129)

Pe lângă `token`/`visitor_id`/`sig`, requesturile (`/web/bootstrap`, `/web/messages`, `/web/chat`)
acceptă un câmp opțional **`id_token`** (JWT host-signed). Când pagina gazdă are un client LOGAT,
ea îl emite (semnat cu `identity_secret`-ul tenantului) și widgetul îl **forwardează** ca atare —
frontendul NU îl generează și NU îl interpretează, doar îl trece mai departe. Backendul îl verifică
și deblochează verificarea de comandă/retur. Fluxul complet + exemplul de semnare: vezi
[`web-widget-embed.md`](web-widget-embed.md) § „Login passthrough".

---

## 2. Card de produs (`products[]`) — câmpuri NOI evidențiate

```jsonc
{
  "product_id": "uuid",      // obligatoriu
  "name": "string",          // obligatoriu
  "price": 58.99,            // obligatoriu — prețul CURENT (ce plătește clientul)

  "image_url": "https://…",  // opțional
  "url": "https://…",        // opțional — link produs
  "rating": 4.8,             // opțional — 0..5

  // ——— NOI (paritate iZi) ———
  "list_price": 79.99,       // opțional — preț ORIGINAL, randează-l TĂIAT lângă `price`.
                             //   Prezent DOAR la reducere reală (list_price > price).
  "review_count": 120,       // opțional — nr. recenzii. Prezent doar > 0. Randează „(120 recenzii)".
  "reason": "string"         // opțional — de ce se potrivește (1 rând sub card, deja prezent)
                             //   ⚠ în transcript apărea gol pe carduri — acum vine populat per card.
  "badge": "Top Favorit",    // opțional — etichetă scurtă (chip/tag colțul cardului).
                             //   Valori posibile azi: „Top Favorit", „Super Preț" (+ locale EN/HU).

  // ——— Full-eMAG (contract EXTINS, aditiv — se aprind pe măsură ce backendul le emite) ———
  "badges": [{ "label": "Super Preț", "tone": "danger" }], // opțional — badge-uri CU ton semantic
                             //   `tone` ∈ info|danger (azi): deal→danger, top→info. `badge` (string)
                             //   rămâne emis în paralel pt FE-ul de bază. Randează `badges` dacă există.
  "currency": "RON",         // opțional — moneda cardului (din DomainPack); FE mapează RON→„Lei".
  "details": "string",       // opțional — descriere EXTINSĂ („Spune-mi mai multe"), din ai_summary
                             //   (catalog, medical-guarded). Randează colapsat/expandabil, NU în card.

  // ——— Variante / nuanțe (NX-166, aditiv) ———
  "variants": [              // opțional — selector de variantă/nuanță/mărime, max ~16/card
    {
      "variant_id": "uuid",  // obligatoriu în fiecare variantă
      "label": "Medium Warm 07",
      "price": 89.00,        // opțional — preț curent al variantei
      "list_price": 109.00,  // opțional — preț original tăiat, doar dacă list_price > price
      "stock": 8,            // opțional — stoc per variantă; 0 = out-of-stock, randează disabled
      "color_hex": "#C89463",// opțional — swatch de culoare
      "attributes": {        // opțional — chei compacte, neutre pe vertical
        "shade": "07",
        "undertone": "warm",
        "depth": "medium"
      }
    }
  ]
}
```

> **Neemis încă (blocat pe DATE):** `highlights:[{text,tone,icon}]` (livrare urgentă, „-100 Lei în coș")
> și `meta:[{label,value}]` („Livrare: Marți, 7 Iul.") — cer ETA livrare (curier/ERP) + promo engine.
> `offer.kind` = doar `open_url` azi (checkout/book/quick_reply = contract-ready, neemise).

### Cum se randează (recomandat, ca iZi)
- **`list_price`**: `price` mare/bold + `list_price` tăiat (`<s>79,99 lei</s>`), opțional „-25%" calculat
  în FE = `round((list_price - price) / list_price * 100)`. (Backendul NU trimite procentul — derivă-l tu.)
- **`badge`** / **`badges`**: tag colorat în colțul cardului. Preferă `badges[]` (are `tone` → culoare);
  fallback pe `badge` (string). Ex. „Super Preț" (danger/roșu), „Top Favorit" (info/albastru).
- **`review_count`** + **`rating`**: `4.8 ★ (120)`.
- **`reason`**: un rând discret sub nume. **`details`**: expandabil „Spune-mi mai multe".
- **`variants`**: randează selector de swatch/nuanță când există. `stock: 0` înseamnă variantă
  vizibilă dar dezactivată/OOS; nu o ascunde, fiindcă botul poate vorbi despre disponibilitate.

---

## 3. Tabel comparativ (`comparison`) — NOU (P0)

Apare când userul cere o comparație („compară primele două"). Înlocuiește re-listarea de carduri
cu un **tabel structurat** (ca iZi). `content` = doar lead-ul; tabelul îl randezi din `comparison`.
`products[]` conține și cardurile-header (poză + nume + preț) ale produselor comparate.

```jsonc
"comparison": {
  "columns": [                    // 2..3 — un produs / coloană (ordinea cerută de user, păstrată)
    {
      "product_id": "uuid",
      "name": "Crema A",
      "price": 58.99,             // CURENT
      "list_price": 79.99,        // opțional — original tăiat (la reducere)
      "image_url": "https://…",   // opțional
      "url": "https://…",         // opțional
      "rating": 4.8               // opțional
    }
    // … încă 1-2 coloane
  ],
  "rows": [                       // o dimensiune / rând; `values` aliniat 1:1 cu `columns`
    { "label": "Preț",            "values": ["58.99 lei", "88.99 lei"] },
    { "label": "Rating",          "values": ["4.8★", "4.6★"] },
    { "label": "Disponibilitate", "values": ["În stoc", "Stoc limitat"] },
    { "label": "Avantaje",        "values": ["hidratează intens; fără parfum", "bogată"] },
    { "label": "De luat în calcul","values": ["tub mic", null] },  // null ⇒ randează „—"
    { "label": "Brand",           "values": ["BrandX", "BrandY"] }
  ]
}
```

### Reguli de randare
- **Header tabel** = `columns` (poză + nume + preț, exact ca un card mic). Aplică și aici `list_price`.
- **Corp tabel** = `rows`; fiecare `values[i]` aparține `columns[i]`. **`null` ⇒ „—"** (celulă lipsă,
  NU „0"/gol).
- Etichetele (`label`) și textul celulelor vin **deja localizate** (ro/en/hu) — afișează-le ca atare.
- Numărul de rânduri e variabil (un rând complet gol e omis de backend — ex. niciun produs cu minusuri).
- Mobile: dacă tabelul nu încape, comută pe layout vertical (per produs), nu trunchia.

### Fallback (canale fără tabel)
Pe WhatsApp/Telegram backendul trimite acelaşi conținut ca **text aplatizat** în `content`/mesaj
(nu primesc `comparison`). Frontendul web primește MEREU `comparison` când e o comparație.

---

## 4. Offer (existent, neschimbat)

```jsonc
"offer": { "kind": "open_url"|"checkout"|"quick_reply"|"book", "label": "string",
           "url": "https://…" /*opt*/, "payload": "string" /*opt*/ }
```
- `open_url`/`checkout` → buton care deschide `url`.
- `quick_reply` → trimite `payload` ca mesaj nou (ca un chip).
- `book` → declanșează fluxul de programare cu `payload`.

---

## 5. Chips (`suggestions[]`)

Listă de string-uri. La tap, **trimite textul ca mesaj nou** (intră în pipeline ca tur nou — e
voce de client). Ex. la o comparație: `["Adaugă Crema A", "Adaugă Crema B", "Ceva mai ieftin"]`.

---

## 6. Rezumat: ce trebuie să implementeze frontendul

| Element | Câmp(uri) | Acțiune FE |
|---|---|---|
| Preț tăiat (reducere) | `list_price` pe card/coloană | `<s>list_price</s>` + `price`; „-X%" derivat în FE |
| Badge | `badge` | tag/chip în colțul cardului |
| Nr. recenzii | `review_count` (+`rating`) | `4.8 ★ (120)` |
| Motiv per card | `reason` | rând discret sub nume |
| Selector nuanțe/variante | `variants[]` | swatches/opțiuni; `stock:0` disabled/OOS |
| **Tabel comparativ** | `comparison.{columns,rows}` | tabel; `null`→„—"; mobile→vertical |
| Chips | `suggestions[]` | tap → trimite textul ca mesaj |

Tot ce e mai sus e **aditiv** și opțional: un frontend care ignoră câmpurile noi continuă să
funcţioneze (degradare grațioasă). Implementarea lor = paritatea vizuală cu iZi.

---

## 7. Production Gate / Fixtures

Contractul este acoperit de fixtures versionate în
[`tests/fixtures/web_response/payloads.json`](../tests/fixtures/web_response/payloads.json):

- `text_only`
- `products`
- `offer`
- `comparison`
- `no_match`
- `fallback_error`
- `rate_limit`

Backendul validează payloadurile cu checkerul pur
[`src/evals/web_response.py`](../src/evals/web_response.py). Gate-ul prinde explicit:

- `content` gol sau `products`/`suggestions` cu formă greșită;
- `product_id` emis dar absent din sursa de produse;
- preț din card diferit de sursa DB;
- preț menționat în `content` care nu apare în payload/sursă;
- URL gol, invalid sau URL în text care nu apare în payload;
- produs menționat în `content` dar absent din `products`;
- claim de stoc/livrare fără sursă explicită;
- tabel `comparison` rupt (coloane/rânduri nealiniate).

Acest gate nu înlocuiește frontendul și nu decide designul. Doar garantează că frontendul primește
un payload stabil, randabil și grounded.
