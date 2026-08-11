# Contract Frontend v2 — `web-turn.v2` / `web-view.v2`

> **Pentru echipa de frontend.** Ce trimiți, ce primești, ce ai voie să faci cu ce primești.
>
> **Statut: inert.** Contractul există în backend, dar nicio rută nu îl servește încă (vine în
> NX-232/233). V1 ([`FRONTEND-CONTRACT-IZI.md`](FRONTEND-CONTRACT-IZI.md)) rămâne **activ și
> neschimbat** până la cutoverul NX-249. Nu migra nimic pe baza acestui document încă —
> citește-l ca să știi spre ce mergem.
>
> Sursa de adevăr: [`src/web/contracts_v2.py`](../src/web/contracts_v2.py).
> Motivația fiecărei decizii: [`WEB-WIDGET-BOUNDARY-V2.md`](WEB-WIDGET-BOUNDARY-V2.md).

---

## 0. Diferența într-o propoziție

**În v1 primeai date și calculai afișarea. În v2 primești afișarea.**

```jsonc
// v1 — tu calculai
{ "price": 89.0, "list_price": 109.0, "currency": "RON" }
// → tu: Math.round(((109-89)/109)*100) = -18%, tu: RON → "Lei", tu: Intl.NumberFormat

// v2 — vine gata
{ "price": { "current": "89,00 lei", "previous": "109,00 lei", "discount": "-18%" } }
// → tu: afișezi trei string-uri
```

Nu mai există niciun număr pe care să-l poți calcula greșit, pentru că nu mai există niciun număr.

---

## 1. Request — `web-turn.v2`

```jsonc
{
  "schema_version": "web-turn.v2",
  "client_turn_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",  // UUID, OBLIGATORIU
  "input": { "type": "text", "text": "Compară-l cu ceva mai ieftin" },
  "context": {                       // opțional; DOAR ID-uri opace
    "surface": "product",            // home|category|product|cart|checkout|order|other
    "product_id": "opq_p_8812",
    "variant_id": null,
    "category_id": null,
    "cart_ref": "opq_cart_v7",
    "locale": "ro-RO"
  },
  "id_token": "host.signed.jwt"      // opțional, forward opac (NX-129)
}
```

`input` e union **exclusiv**. Varianta acțiune conține exact:

```jsonc
{ "type": "action", "action_token": "opaque.signed.token" }
```

**`client_turn_id` e obligatoriu.** E cheia de idempotency: același `client_turn_id` întoarce
exact același rezultat, fără al doilea apel LLM. Fără el nu există replay, deci nu există
recovery la refresh. Generează-l tu, o dată per tur, și păstrează-l până la status terminal.

**Ce NU trimiți, niciodată:** `business_id`, preț, stoc, nume de produs, badge, total,
constraints, stare de conversație. Contextul cară ID-uri opace, nu fapte — serverul rehidratează
tot (NX-234). Un câmp în plus respinge requestul.

---

## 2. Response — `web-view.v2`

```jsonc
{
  "schema_version": "web-view.v2",
  "conversation": { "id": "opq_conv_1", "revision": 12 },
  "turn": {
    "id": "opq_turn_2",
    "client_turn_id": "3f2504e0-…",
    "status": "completed"           // accepted|working|validating|completed|failed|cancelled
  },
  "messages": [ { "id": "opq_m_2", "role": "assistant", "blocks": [ /* … */ ] } ],
  "progress": { "label": "Caut în catalog", "detail": "Verific stocul" },  // doar pe neterminal
  "composer": { "enabled": true, "label": "Mesaj", "placeholder": "Scrie un mesaj…", "send_label": "Trimite" },
  "chrome":   { "launcher_label": "…", "dialog_title": "…", "dialog_description": "…",
                "close_label": "…", "new_chat_label": "…" },
  "a11y":     { "announcements": { "accepted": "…", "working": "…", "validating": "…",
                                   "completed": "…", "failed": "…", "cancelled": "…" } },
  "error":    { "code": "agent_deadline_exceeded", "message": "…", "retryable": true,
                "retry_action": { /* ActionView */ } }   // doar cu status "failed"
}
```

⚠️ Statusul e **`turn.status`**, nu `status` la top level. În v1 `status` înseamnă stocul unui
produs; două înțelesuri pe același nume ar fi o capcană.

### Reguli de lifecycle pe care te poți baza

- `accepted|working|validating` — `messages` poate fi gol, `progress` poate exista.
- `completed|failed|cancelled` — **întotdeauna** cel puțin un bloc randabil. Nu vei primi
  niciodată un terminal gol; dacă primești, e un bug de server, nu un caz de tratat cu fallback
  local.
- `progress` nu apare pe terminal. `error` apare **doar** cu `failed`, și `failed` îl are mereu.
- Nu simula etape cu timere locale și nu afișa chain-of-thought. Dacă serverul nu spune că
  lucrează, nu inventa că lucrează.

---

## 3. Blocuri — unionul complet

Discriminat pe `type`. **Finit și strict:** un `type` necunoscut sau un câmp în plus înseamnă
payload invalid — respinge tot, nu doar blocul. Nu face skip best-effort.

| `type` | Câmpuri | Ce randezi |
|---|---|---|
| `text` | `variant` (`lead\|body\|caption\|disclosure`), `text` | paragraf, deja segmentat |
| `product_list` | `items[]` (1..6) | carduri de produs |
| `comparison` | `headers[]` (2..3), `rows[].cells[]` | tabel; `cells` aliniate 1:1 cu `headers` |
| `key_value` | `title?`, `rows[]{label,value}` | listă de perechi |
| `status_list` | `items[]{label,detail?,tone,icon?,freshness?}` | rânduri de stare |
| `routine` | `title?`, `steps[]{title,detail?}` | pași ordonați |
| `notice` | `level` (`info\|success\|warning\|error`), `title?`, `text`, `actions[]` | bandă de mesaj |
| `memory` | `title?`, `criteria[]` | snapshot al criteriilor active |
| `cart_summary` | `title?`, `lines[]`, `total?`, `actions[]` | coș (**abia după NX-237**) |
| `action_row` | `actions[]` (1..4) | rând de butoane/chips |
| `divider` | — | separator |

### `product_list[].items[]`

```jsonc
{
  "view_id": "pv_1",                     // identitate în VIEW (folosește-o ca `key`)
  "title": "Petala Rich Cremă hidratantă",
  "subtitle": "Petala",                  // opțional
  "image": { "src": "https://…", "alt": "…" },   // `alt` e OBLIGATORIU când există imagine
  "price": { "current": "89,00 lei", "previous": "109,00 lei", "discount": "-18%" },
  "rating": "4,8 din 5 (120 recenzii)",  // string gata formatat
  "availability": "În stoc",             // string; NU primești un număr de interpretat
  "reason": "Cea mai bogată din listă…",
  "badges": [ { "label": "Super preț", "tone": "danger" } ],
  "actions": [ /* ActionView, max 3 */ ]
}
```

Nu vei mai primi `product_id`. Nu-ți trebuie: singurul lucru pe care îl făceai cu el era să-l
trimiți înapoi, iar pentru asta există tokenul acțiunii.

### `ActionView`

```jsonc
{
  "id": "a1",
  "label": "Vezi produsul",              // max 40 caractere — e o etichetă, nu o propoziție
  "appearance": "primary",               // primary|secondary|chip|link|danger
  "icon": "tag",                         // opțional, din allowlist
  "enabled": true,
  "activation": { "type": "navigate", "href": "https://…", "target": "_blank" }
  // SAU:      { "type": "submit", "token": "opaque.signed.token" }
}
```

- `navigate` → deschide `href`. E deja validat de backend.
- `submit` → trimite `{"type":"action","action_token": <token>}` ca tur nou, cu tokenul
  **exact cum l-ai primit**. Nu-l decoda, nu-l completa, nu-l compune.

**Nu deduce ce face un buton din eticheta lui.** Eticheta e pentru ochi, tokenul e pentru mașină.

---

## 4. Tokenuri semantice

| Token | Valori | Ce faci |
|---|---|---|
| `tone` | `neutral` `info` `success` `warning` `danger` | mapezi pe culoare |
| `appearance` | `primary` `secondary` `chip` `link` `danger` | mapezi pe stil de control |
| `icon` | `truck` `tag` `percent` `shield` `clock` `gift` `info` `check` `alert` | mapezi pe componenta de icon |
| `variant` (text) | `lead` `body` `caption` `disclosure` | mapezi pe stil tipografic |
| `level` (notice) | `info` `success` `warning` `error` | mapezi pe bandă |

Vocabulare **închise**. Nu vei primi o valoare din afara listei — dacă se întâmplă, e payload
invalid, nu un caz de fallback pe „neutral". `promo` a existat în FE-ul canonic; v2 nu îl emite,
și se adaugă doar printr-un minor cu schema hash negociat.

**URL-uri:** primești doar `https://` absolut sau rută relativă `/…`. Niciodată `javascript:`,
`data:`, `file:`, protocol-relativ sau `http://` în clar.

---

## 5. Ce dispare din codul tău

Când migrezi, **șterge** — nu adapta:

- calculul procentului de reducere (`hasDiscount` / `discountPct`);
- `inferBadgeTone()` — tonul vine din backend;
- `parsePrice()` și euristicile de virgulă/punct;
- `Intl.NumberFormat` pe prețuri și `CURRENCY_LABEL` (`RON → "Lei"`);
- `switch (offer.kind)` — comportamentul vine ca `activation.type`;
- compunerea de mesaje din numele produsului (`Spune-mi mai multe despre ${name}`);
- acumularea criteriilor din `m.criteria` — vine ca bloc `memory`;
- `addToCart` / `toggleWish` din interiorul asistentului — mutațiile trec prin `submit` (NX-237);
- microcopy hardcodat: „Spune-mi mai multe", „De ce ți-l recomand", „Funcționalități
  principale", „De luat în calcul", „Ideală pentru", „Evită dacă", „POTRIVIRE" — tot copy-ul vine
  în blocuri, `chrome`, `composer` și `a11y`.

Și **câmpurile fixture-only** (`brand`, `score`, `why`, `best`, `avoid`, `pros`, `cons`,
`changes`, `highlights`, `meta`) dispar din contract: backendul nu le-a emis niciodată. Ce era
util din ele se exprimă prin blocuri (`key_value`, `routine`, `status_list`, `text`).

## 6. Ce rămâne al tău

Layout, grilă, spațiere, tipografie, temă, paletă, breakpointuri, animații. `role`, `focus`,
`aria-live politeness`. Deschis/închis, scroll, draft, expanded/collapsed. Bootstrap,
`client_turn_id`, pending, reconnect, recovery. Validarea structurală a JSON-ului și
**escaparea la randare** — contractul nu sanitizează proza și nu are niciun câmp „randează ca
HTML"; ce vine în `text` e text literal.

---

## 7. Fixturi

[`tests/fixtures/web_v2/`](../tests/fixtures/web_v2/) — scrise de mână, nu generate din modele:

- `valid_views.json` — 12 envelope-uri care acoperă toate cele 11 blocuri și toate cele 6
  statusuri (greeting, recomandare, comparație, no-result, rutină, stare comandă, coș, cele trei
  stări neterminale, `failed`, `cancelled`);
- `invalid_views.json` — 28 de cazuri care **trebuie** respinse, fiecare cu `reason`;
- `requests.json` — requesturi valide și invalide.

Gate: `python -m pytest tests/test_web_contract_v2.py -q`.
