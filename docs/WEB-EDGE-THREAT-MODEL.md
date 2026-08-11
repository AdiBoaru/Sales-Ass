# Threat model — marginea web (NX-229)

> Ce apără marginea `/web/*`, împotriva cui, și ce **nu** apără. Scris ca să fie contrazis: dacă
> un scenariu de aici nu mai e adevărat în cod, documentul e greșit, nu codul.
>
> Cod: [`src/web/security.py`](../src/web/security.py) · [`src/web/session.py`](../src/web/session.py) ·
> [`src/web/identity.py`](../src/web/identity.py) · [`src/web/app.py`](../src/web/app.py)
> Card: [`tasks/stage1/NX-229.md`](../tasks/stage1/NX-229.md)

## 1. Modelul de încredere — cinci credențiale, cinci semantici

Confuzia dintre ele e clasa de bug pe care o previne acest card. Fiecare rând are un rol; niciunul
nu se poate substitui altuia.

| # | Credențial | Ce dovedește | Ce **nu** dovedește |
|---|---|---|---|
| 1 | `widget_public_token` | care canal/tenant e vizat | că apelantul e autorizat — e **public**, trăiește în bundle-ul site-ului |
| 2 | `visitor_id` + `sig` | că sesiunea a fost emisă de noi, nu a expirat, e legată de acest token și (opțional) de acest origin | cine e persoana |
| 3 | `id_token` (body) | identitatea shopperului (`sub` = `customer_ref`) | nimic despre tenant — acela e derivat server-side |
| 4 | `Authorization: Bearer` | drept de **acces la site-ul demo** | **niciodată** cine e clientul — vezi §4 |
| 5 | `Origin` | policy de admitere | nimic: un bot îl poate scrie orice |

`business_id` nu apare în niciunul dintre transporturile de mai sus. E derivat exclusiv
server-side din (1), prin control plane (`admin_conn`), și e singurul mod în care poate fi derivat.

## 2. Ce era deschis înainte de NX-229

Nu ipoteze — lucruri citite în cod la baseline `origin/main@2ab53ff`:

| # | Gaură | Unde | Ce însemna practic |
|---|---|---|---|
| G1 | Sesiunea nu expira **niciodată** | `_compute_sig(token, visitor_id, secret)` — nicio noțiune de timp | o pereche `visitor_id`+`sig` scursă azi era validă și peste un an |
| G2 | Cheia de sesiune nu se putea roti | un singur `session_secret`, fără overlap | schimbarea lui invalida toate sesiunile simultan → în practică nu se schimba niciodată |
| G3 | `Origin` verificat **doar** la `/bootstrap` | `app.py` | `/chat` (calea care cheltuie LLM), `/messages` și `/stream` erau descoperite |
| G4 | `/bootstrap` fără rate limit | `app.py` | se puteau coase oricâte `visitor_id`-uri proaspete → limita per-visitor de pe `/chat` devenea ocolibilă |
| G5 | `Authorization` ignorat tăcut | frontendul îl trimitea, backendul nu-l citea | un credential care traversează rețeaua fără să fie nici validat, nici respins |
| G6 | Allowlist de origini global, nu per tenant | `WEB_CORS_ORIGINS` | originul unui client se aplica tuturor |

## 3. Scenarii de atac și ce le oprește

| Atac | Apărare | Verificat de |
|---|---|---|
| Token public furat din bundle | e public prin design; nu autorizează date private. Cost limitat de rate limit + cap per vizitator | `test_bootstrap_rate_limited_after_burst` |
| Sesiune scursă, refolosită luni mai târziu | `exp` în claims (12h) | `test_v2_expires` |
| Prelungirea expirării prin editarea claims | MAC peste claims; orice modificare invalidează | `test_v2_tampered_claims_rejected` |
| **Sesiune validă prezentată cu tokenul altui tenant** | amprenta tokenului e în claims | `test_v2_cross_tenant_token_swap_rejected` |
| Furt de sesiune folosit de pe altă pagină | origin binding | `test_v2_origin_binding_rejects_other_page`, `test_session_bound_to_origin_cannot_be_used_elsewhere` |
| Visitor swap (aceeași sig, alt `visitor_id`) | `vis` în claims | `test_v2_visitor_swap_rejected` |
| Origin spoof (`null`, subdomeniu, port, schemă) | normalizare + potrivire exactă | 8 teste în `test_web_security.py` |
| Bot care ignoră CORS | `Origin` verificat **server-side**, pe toate endpointurile | `test_chat_rejects_disallowed_origin_before_spending` |
| `alg=none` / confuzie de algoritm pe JWT | HS256 pinuit dur, o singură primitivă | `test_demo_access_alg_none_rejected` |
| Token de identitate fără `exp` | `exp` obligatoriu | `test_demo_access_without_exp_rejected` |
| Rotație de cheie în mijlocul unei sesiuni | dual-key: noua semnează, ambele verifică | `test_v2_previous_key_still_verifies_during_overlap` |
| Enumerare de sesiuni prin mesaje de eroare | 403 nediferențiat pentru token necunoscut vs semnătură invalidă; motivul rămâne doar în log | `_verify` |
| Secrete scurse în loguri | amprentă SHA-256, nu valoarea | `test_session_rejection_never_logs_the_token` |
| Explozie de cardinalitate în metrici din originuri ostile | bucket hash-uit | `test_origin_bucket_is_low_cardinality_and_opaque` |
| Redis căzut folosit ca bypass | fail-closed pe bootstrap și `/chat` | `test_bootstrap_fails_closed_when_redis_is_down` |

## 4. De ce `Authorization` nu poate deveni identitate

Frontendul trimitea un Supabase JWT într-un header pe care backendul îl ignora. Cardul interzice
ambiguitatea: ori e validat explicit, ori e consumat la margine. Owner-ul a confirmat că e
**poarta de acces la site-ul demo**.

Riscul real nu e că headerul e greșit — e că e *aproape* corect. Un JWT valid a sosit; distanța
până la „deci userul logat e `sub`" e o singură linie de cod scrisă pe grabă, iar rezultatul ar fi
identitate de cumpărător derivată dintr-un control de acces la site.

De aceea `verify_demo_access` întoarce `tuple[bool, str | None]`, nu claims. **Semnătura tipului e
apărarea**, nu disciplina: linia aceea nu se poate scrie, pentru că subiectul nu iese din funcție.
Identitatea shopperului rămâne `id_token` din body — un singur transport, o singură semantică.

Consecință de configurare: `Authorization` intră în `allow_headers` CORS **doar** când poarta e
pornită. Cât timp nu e validat, nici nu-l invităm.

## 5. Ce NU apără marginea

Scris explicit, ca să nu fie confundat cu acoperire:

- **CORS nu e autentificare.** Oprește JS-ul cross-origin să *citească* răspunsul; nu oprește un
  bot să facă requestul. Apărarea reală e verificarea server-side a originului plus semnătura.
- **Tokenul public nu e secret.** Trăiește în bundle-ul site-ului. Tot ce poate face e să
  identifice canalul; costul e limitat de rate limit și de capul zilnic per vizitator.
- **Origin binding nu apără împotriva unei pagini compromise.** Dacă atacatorul rulează cod pe
  originul permis, e în interiorul perimetrului.
- **Autorizarea rezultatelor și semantica tokenurilor de acțiune** sunt NX-232/NX-236, nu aici.
  Marginea leagă sesiunea de tenant; legarea unui `turn_id` de sesiunea care l-a creat vine cu
  ledgerul.
- **Doar HS256.** Un proiect Supabase migrat pe chei asimetrice (RS256/ES256 cu JWKS) cade pe
  `bad_alg` — fail-closed și vizibil, nu acceptat tăcut.
- **Secretele stau în `channels.settings`**, nu într-un secret manager. Era deja o datorie
  cunoscută în v1; NX-229 nu o plătește, dar nici nu o adâncește.

## 6. Rotația cheii de sesiune — procedură

E o schimbare de **date**, nu de configurare. `key_id` se derivă din secret
(`sha256(secret)[:8]`), deci nu există un al treilea câmp care să se desincronizeze.

1. mută valoarea curentă din `channels.settings.session_secret` în `session_secret_prev`;
2. scrie noul secret în `session_secret`;
3. așteaptă `WEB_SESSION_TTL_S` (12h) — sesiunile vechi expiră natural;
4. șterge `session_secret_prev`.

În overlap ambele verifică, iar `SessionClaims.key_age` (`current` | `previous`) face vizibil câți
utilizatori mai sunt pe cheia veche. Fără pasul de overlap, rotația deconectează pe toată lumea
deodată — motivul pentru care în v1 nu se rota nimic.

## 7. Rollout — trei pași, fiecare reversibil singur

Verificarea acceptă **mereu** ambele versiuni de sesiune; flagurile controlează doar ce se emite.
Un rollback pe pasul 1 nu invalidează sesiunile v2 deja emise.

| Pas | Flag | Precondiție |
|---|---|---|
| 1 | `WEB_SESSION_V2_ENABLED=true` | — |
| 2 | `WEB_SESSION_ORIGIN_BINDING=true` | allowlistul confirmat în producție |
| 3 | `WEB_SESSION_V2_REQUIRED=true` | au trecut ≥ `WEB_SESSION_TTL_S` de la pasul 1 |

Poarta demo (`WEB_DEMO_ACCESS_ENABLED`) e independentă de cele trei.

## 8. Originile — starea reală, august 2026

Confirmate de owner: **`https://demo.nativextech.com`**. Nici `shop.`, nici `localhost`.

Două lucruri de reparat în afara acestui card:

1. `.env.prod.example` de pe `main` încă spune `shop.nativextech.com`. Corecția e în PR #276.
2. Repo-ul frontend (`vite.config.js`) spoof-uiește `Origin: https://shop.nativextech.com` în
   proxy-ul de dev, cu un comentariu care afirmă că botul acceptă doar acel origin. **După
   NX-229 acel origin e respins** — proxy-ul trebuie să spoof-uiască `https://demo.nativextech.com`.
   E o schimbare în repo-ul FE, nu aici.
