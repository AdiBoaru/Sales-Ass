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
  "badge": "Top Favorit"     // opțional — etichetă scurtă (chip/tag colțul cardului).
                             //   Valori posibile azi: „Top Favorit", „Super Preț" (+ locale EN/HU).
}
```

### Cum se randează (recomandat, ca iZi)
- **`list_price`**: `price` mare/bold + `list_price` tăiat (`<s>79,99 lei</s>`), opțional „-25%" calculat
  în FE = `round((list_price - price) / list_price * 100)`. (Backendul NU trimite procentul — derivă-l tu.)
- **`badge`**: tag colorat în colțul cardului (ex. „Super Preț" verde, „Top Favorit" portocaliu).
- **`review_count`** + **`rating`**: `4.8 ★ (120)`.
- **`reason`**: un rând discret sub nume.

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
| **Tabel comparativ** | `comparison.{columns,rows}` | tabel; `null`→„—"; mobile→vertical |
| Chips | `suggestions[]` | tap → trimite textul ca mesaj |

Tot ce e mai sus e **aditiv** și opțional: un frontend care ignoră câmpurile noi continuă să
funcţioneze (degradare grațioasă). Implementarea lor = paritatea vizuală cu iZi.
