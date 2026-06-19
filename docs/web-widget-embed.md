# Web widget — instrucțiuni de embed (NX-21, Epic E26)

Widget de chat embeddabil pe site-ul clientului. Front-end pur (vanilla JS, shadow DOM),
servit din infra Nativx; clientul lipește **o singură linie**. Consumă gateway-ul SSE NX-20.

## Embed de o linie

```html
<script
  src="https://api.nativx.tech/web/widget.js"
  data-token="pub_8f3a...e21"
  async
></script>
```

Atât. Apare un buton flotant de chat; la deschidere widgetul își creează sesiunea anonimă
și se conectează la gateway.

## Atribute `data-*`

| Atribut | Obligatoriu | Default | Ce face |
|---|---|---|---|
| `data-token` | **da** | — | Public token al tenantului (identifică magazinul; NU e secret) |
| `data-api` | nu | originea scriptului | Base URL gateway (ex. `https://api.nativx.tech`) |
| `data-locale` | nu | `ro` | `ro` \| `hu` \| `en` — limba *chrome*-ului UI |
| `data-title` | nu | „Asistent" | Titlul din header |
| `data-primary` | nu | `#1f6feb` | Culoarea primară (hex) |
| `data-position` | nu | `right` | `right` \| `left` — colțul butonului |

> Tokenul e **public** prin design (apare în HTML-ul site-ului). Nu autentifică — doar
> identifică tenantul. Gateway-ul îl rate-limitează agresiv (IP + visitor) și respinge originile
> neautorizate (CORS allowlist per tenant — NX-25). Niciun secret în front-end.

## Ce face widgetul (contractul NX-20 consumat)

1. **Sesiune** — `GET /web/bootstrap?token=…` → `{visitor_id, sig}` (vizitator anonim, semnat
   HMAC de gateway). Persistat în `localStorage` (`nx_session_<token>`, doar id opac — **zero PII**).
2. **Trimitere** — `POST /web/messages {token, visitor_id, sig, text}` → mesajul intră în pipeline
   ca orice canal (`channel_kind='webchat'`).
3. **Primire** — `GET /web/stream?token=…&visitor_id=…&sig=…` (EventSource SSE). Răspunsurile bot
   vin pe acest stream; reconectare nativă la drop de rețea (Last-Event-ID).

## Variantă sincronă — `POST /web/chat` (NX-25b)

Pentru widget-uri care **randează carduri de produs** și vor răspunsul în același request (nu prin
SSE), gateway-ul expune o variantă **request/response**:

```
POST /web/chat
{ "token": "pub_…", "visitor_id": "web_…", "sig": "…", "message": "ce ai pentru ten gras?" }
→ { "content": "…", "products": [ { "name", "price", "image_url", "url", "rating", "reason" } ],
    "suggestions": ["Mai ieftin", "Compară cu X"] }
```

- **Aceeași autentificare** ca `/web/messages`: `visitor_id` + `sig` din `GET /web/bootstrap`
  (un singur apel la montare). `history`-ul trimis de frontend e **ignorat** — serverul ține
  istoricul în DB pe `visitor_id` (memorie, state, validator).
- **Același pipeline** ca toate canalele: gates, triaj, agent, **validator de prețuri** (zero preț
  inventat), căutare reală în catalog, analytics. NU e un endpoint paralel — rulează `handle_turn`
  in-process cu `deliver=False` (răspunsul HTTP e transportul, fără outbox/dispatcher).
- **CORS** — browserul shop-ului apelează cross-origin → setează originile permise în
  `WEB_CORS_ORIGINS` (CSV). Preflight-ul (înainte de body) se gate-uiește pe această listă;
  gardele server-side rămân token + sig + rate-limit. Binding fin origin↔token per canal = NX-25.
- **Operațional**: `/web/chat` rulează pipeline-ul (DB + LLM) **în procesul API**, nu în worker →
  containerul care servește FastAPI are nevoie de `OPENAI_API_KEY` + credențialele `bot_runtime`
  (`DATABASE_URL_BOT`), exact ca worker-ul. (Calea SSE păstrează API-ul subțire; sincronul nu.)

`content` are deja disclaimer-ul AI; `products`/`suggestions` sunt goale când turul nu produce
recomandare (frontendul afișează doar textul). Carduri ⇐ `reply.rich.items` (au `rating`/`reason`)
sau `reply.products`; `suggestions` ⇐ chip-urile de follow-up.

## Izolare & conformitate

- **Shadow DOM** — tot UI-ul trăiește într-un `shadowRoot` (mode open); zero conflict CSS cu
  site-ul gazdă (verificat pe `web/test-host.html`, cu CSS global ostil).
- **Disclosure AI permanent** (art. 50 AI Act) — rând fix în header („Asistent AI · răspunsuri
  automate"), vizibil în orice stare, NU un mesaj care se pierde la scroll.
- **PII** — telefonul/numele, dacă vizitatorul le scrie, merg prin mesaj → pipeline →
  `channel_identities` (server-side). Niciodată salvate în browser.

## Temă per client

V1: tema vine din atributele `data-*` ale snippet-ului (agenția le completează la onboarding).
Personalizare server-side din `businesses.settings.web_widget` (citită de un `/web/config` viitor)
e o extensie ulterioară — vezi NX-20 (gateway).

## Test local

Deschide `web/test-host.html` direct în browser: are CSS ostil + un gateway **mock** (fetch +
EventSource interceptate) → demonstrează widgetul și izolarea fără backend real.

## Provisioning canal (server-side)

Tokenul public + secretul de sesiune se seedează pe canalul `webchat` al tenantului
(`channels.kind='webchat'`, `provider_account_id=<public_token>`, `settings.session_secret`) —
vezi `scripts/seed_web_channel.py` (NX-20a) și `docs` gateway.
