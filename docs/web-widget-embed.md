# Web widget — contractul de integrare (gateway NX-20 + margine NX-25/NX-229)

> **Widgetul `web/widget.js` (NX-21) a fost ȘTERS.** Era un front-end vanilla JS cu shadow DOM,
> scris ca schelet de demo și **niciodată servit**: Dockerfile-ul nu copia `web/` în imagine, nicio
> rută `StaticFiles` nu-l monta, iar `GET https://bot.nativextech.com/web/widget.js` răspundea
> `404`. Widgetul REAL trăiește în repo-ul frontend separat (`Sales MVP Frontend Final`) — vezi
> [`FRONTEND-CONTRACT-IZI.md`](FRONTEND-CONTRACT-IZI.md) (v1, activ) și
> [`FRONTEND-CONTRACT-IZI-V2.md`](FRONTEND-CONTRACT-IZI-V2.md) (v2, până la cutoverul NX-249).
> Istoricul fișierului: `git log --diff-filter=D -- web/widget.js`.
>
> **Ce rămâne valabil aici:** contractul pe care ORICE widget îl consumă — endpointurile
> gateway-ului, login passthrough-ul, marginea de securitate și provisioning-ul canalului. Ce
> ținea de UI-ul șters (embed de o linie, atribute `data-*`, temă din snippet, pagina de test) a
> fost scos: acele decizii aparțin acum repo-ului FE.

## Tokenul public

Tokenul public al tenantului identifică magazinul și se pasează pe fiecare apel.

> Tokenul e **public** prin design (apare în HTML-ul site-ului). Nu autentifică — doar
> identifică tenantul. Gateway-ul îl rate-limitează agresiv (IP + visitor) și respinge originile
> neautorizate (CORS allowlist per tenant — NX-25). Niciun secret în front-end.

## Sesiune, trimitere, primire (contractul NX-20)

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

## Login passthrough — verificare comandă/retur (NX-129)

Web-ul e anonim by design, deci un vizitator NU poate verifica o comandă sau cere un retur (nu există
cont). Ca un client **logat pe site** să-și vadă comenzile în chat, site-ul gazdă îi pasează o
identitate semnată — pattern-ul de producție pentru widget-uri (Intercom „Identity Verification" /
Zendesk JWT). Fără asta, intenția de comandă/retur primește un mesaj „loghează-te pe site" (NX-128).

**Fluxul:**
1. Backend-ul gazdei (NU browserul), pentru un client autentificat, emite un **JWT HS256** semnat cu
   `identity_secret`-ul per-tenant: `{"sub": "<customer_ref>", "exp": <now+5min>}`. `customer_ref` =
   id-ul STABIL de client din eshop (id opac, **nu** email/telefon — vezi P12).
2. Widgetul pasează `id_token` (JWT-ul) la `GET /web/bootstrap` și pe fiecare mesaj
   (`POST /web/messages` / `POST /web/chat`), pe lângă `token`+`visitor_id`+`sig`.
3. Gateway-ul verifică server-side semnătura + `exp` + `alg=HS256` (respinge `alg=none`) → `sub` =
   `customer_ref` → leagă o identitate **verificată** stabilă (`channel_identities`, `verified=true`)
   → `check_order` poate găsi comenzile clientului (NX-130).

```python
# backend gazdă (Python) — la randarea paginii pentru un client logat
import base64, hashlib, hmac, json, time
def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def mint_id_token(customer_ref: str, identity_secret: str, ttl_s: int = 300) -> str:
    h = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = _b64(json.dumps({"sub": customer_ref, "exp": int(time.time()) + ttl_s}).encode())
    s = hmac.new(identity_secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64(s)}"
```

- **Token scurt** (`exp` ~5 min); widgetul îl reîmprospătează din pagina gazdă. `id_token` invalid/
  expirat/absent → NU blochează chat-ul, doar rămâne anonim (feature-ul de comandă cere login).
- **Secretul** (`identity_secret`) stă DOAR pe backend-ul gazdei + control plane (`channels.settings`),
  niciodată în browser/HTML/loguri. Separat de `session_secret` (semnătura de vizitator anonim).
- **Contractul cu ingestia de comenzi (NX-130):** `customer_ref` din `sub` trebuie să fie ACEEAȘI
  cheie pe care webhook-ul de comenzi o trimite în `orders.external_customer_ref`. Altfel identitatea
  e verificată, dar nu mapează la nicio comandă.
- **Activare:** `WEB_IDENTITY_ENABLED=true` + `identity_secret` seedat pe canal (vezi provisioning).

## Izolare & conformitate

- **Izolarea CSS față de site-ul gazdă** e a repo-ului FE (shadow DOM sau echivalent), nu a
  backendului. Backendul nu emite markup — vezi [`WEB-WIDGET-BOUNDARY-V2.md`](WEB-WIDGET-BOUNDARY-V2.md).
- **Disclosure AI permanent** (art. 50 AI Act) — rând fix în header („Asistent AI · răspunsuri
  automate"), vizibil în orice stare, NU un mesaj care se pierde la scroll.
- **PII** — telefonul/numele, dacă vizitatorul le scrie, merg prin mesaj → pipeline →
  `channel_identities` (server-side). Niciodată salvate în browser.

## Marginea de securitate (NX-229)

Threat model complet: [`WEB-EDGE-THREAT-MODEL.md`](WEB-EDGE-THREAT-MODEL.md). Pentru integratorul
care pune widgetul pe site, contează patru lucruri:

**1. Originul trebuie declarat exact.** `Origin` se verifică **server-side pe toate** endpointurile
`/web/*` (nu doar la bootstrap): CORS-ul browserului oprește doar citirea răspunsului de către JS,
nu procesarea pe server — un bot îl ignoră complet. Potrivirea e exactă după normalizare:
`https://demo.exemplu.ro` ≠ `https://www.demo.exemplu.ro` ≠ `http://demo.exemplu.ro` ≠
`https://demo.exemplu.ro:8443`. Portul implicit și slash-ul final sunt normalizate, restul nu.
Allowlist gol ⇒ niciun origin de browser acceptat (secure-by-default).

Originile se pot declara **per canal** în `channels.settings.allowed_origins` (CSV) — mai strâns
decât `WEB_CORS_ORIGINS`, care rămâne allowlistul procesului.

**2. Sesiunile expiră.** Sesiunea v2 poartă `exp` (implicit 12h), `key_id` și, opțional, originul
care a cerut-o. Un `403` pe o sesiune veche e comportament normal: cere `GET /web/bootstrap` din
nou. Widgetul nu interpretează `sig` în nicio versiune — pentru el e opac.

**3. `id_token` are un singur transport: body-ul.** Nu se citește din headere. Site-ul gazdă îl
semnează cu `identity_secret`-ul tenantului; widgetul îl forwardează neschimbat. Opțional se pot
configura `iss`/`aud` — verificate doar dacă sunt setate.

**4. `Authorization: Bearer` nu identifică clientul.** Dacă e activat
(`WEB_DEMO_ACCESS_ENABLED`), e o **poartă de acces la site**: verificată explicit, dar nu produce
niciodată un `customer_ref`. Un client care vrea shopper identificat emite `id_token`. Cât timp
poarta e oprită, headerul nici nu e acceptat de CORS — nu invităm un credential pe care nu-l
citește nimeni.

### Rotația cheii de sesiune

Schimbare de date, nu de configurare: mută `session_secret` în `session_secret_prev`, scrie noul
secret în `session_secret`, așteaptă 12h, șterge `session_secret_prev`. Ambele chei verifică în
overlap, deci nimeni nu e deconectat la mijlocul unei conversații.

## Temă per client

Tema e a repo-ului FE. Personalizarea server-side din `businesses.settings.web_widget` (citită de
un `/web/config` viitor) rămâne o extensie neimplementată — vezi NX-20 (gateway).

## Test local

Contractul se exersează pe calea reală, fără UI: `python scripts/sim/web_audit.py` (audit
conversațional pe `/web/chat`). Testarea vizuală a widgetului se face în repo-ul FE.

## Provisioning canal (server-side)

Tokenul public + secretul de sesiune se seedează pe canalul `webchat` al tenantului
(`channels.kind='webchat'`, `provider_account_id=<public_token>`, `settings.session_secret`) —
vezi `scripts/seed_web_channel.py` (NX-20a) și `docs` gateway.

Pentru login passthrough (NX-129), adaugă pe ACELAȘI canal `settings.identity_secret` (o cheie
aleatoare per-tenant, SEPARATĂ de `session_secret`) și partajeaz-o cu backend-ul gazdei (care semnează
JWT-urile). Absența ei = login passthrough inactiv pe tenant (sesiunea anonimă rămâne neatinsă).
