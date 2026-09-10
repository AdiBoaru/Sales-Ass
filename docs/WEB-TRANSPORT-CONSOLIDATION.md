# NX-290 — un canal, trei transporturi: care rămâne și cum se retrag celelalte

Status: **FAZA 1 livrată** (anunț + contor + kill-switch). Fazele 2 și 3 au porți, mai jos.
Nu acoperă `/web/chat` — retragerea rutei sincrone v1 e a NX-249 și are propriul criteriu
([`STAGE1-CUTOVER.md`](STAGE1-CUTOVER.md)). Documentul ăsta nu-l dublează.

---

## 1. Problema

Webchat e singurul canal viu (NX-179), dar are trei drumuri prin care poate trece un tur:

| # | Drum | Cum livrează | Cine îl cheamă |
|---|---|---|---|
| A | `POST /web/chat` | sincron: reply-ul E răspunsul HTTP | widgetul v1 |
| B | `POST /web/messages` + `GET /web/stream` | Redis stream → worker → outbox → dispatcher → `WebSender` publish pe pub/sub → SSE | **nimeni** |
| C | `POST /web/v2/turns` (+ `/events`) | ledger `web_turns` → executor → proiecție reluabilă după cursor | widgetul v2 |

Trei drumuri înseamnă trei contracte, trei seturi de teste și trei feluri în care același bug
trebuie reparat. Gate-ul E2E (NX-247) acoperă 9 din 16 scenarii tocmai fiindcă suprafața e triplă.

## 2. Inventarul, măsurat (2026-09-10)

Nu „credem că B nu se folosește". Comenzile care au produs afirmația, reproductibile:

```bash
# în repo-ul FE (Sales MVP Frontend Final, branch main)
grep -rnoE "/web/(chat|messages|stream|bootstrap|v2/turns)" . \
  --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=.git
grep -rn "EventSource" src/
```

Rezultatul: `/web/v2/turns` 15 apariții, `/web/chat` 14, `/web/bootstrap` 10, `/web/stream` **2**,
`/web/messages` **0**. Cele două apariții ale lui `/web/stream` sunt valori de fixture în teste
(`sse_url: '/web/stream'`), nu apeluri. Singurul `EventSource` din tot repo-ul e în
`src/chat/transport/webTurnTransport.js`, adică pe transportul **v2**.

Pe backend, `web:out:` pub/sub e atins doar de `WebSender` (publish) și de `/web/stream`
(subscribe). SSE-ul v2 nu-l folosește: e o proiecție a ledgerului, citită din DB. Deci seam-ul
dintre B și C e curat — retragerea lui B nu poate atinge C.

Trafic real în fereastra observată: **zero** (tenantul `sole-ro` are 0 conversații și 0 mesaje;
producția nu răspunde din 2026-09-03). Consecința e importantă și e tratată la §5.

## 3. Decizia

**Suprafața țintă: A + C.** `POST /web/messages` și `GET /web/stream` se retrag împreună.

Motivul principal NU e „nu-l cheamă nimeni" — asta doar face retragerea ieftină. Motivul e
**semantica livrării**. B publică pe un canal pub/sub și păstrează un backlog cu TTL de 300s: cine
e conectat acum primește, cine nu, pierde. Rândul din `outbox` se marchează totuși `sent`. Adică
sistemul înregistrează o livrare care nu s-a întâmplat.

Standardul actual pentru un widget de chat e invers: **jurnalul e autoritatea, streamul e o
optimizare**. Serverul scrie ce trebuie văzut într-un log ordonat per conversație; clientul se
sincronizează după cursor la fiecare (re)conectare; streamul se reia cu `Last-Event-ID` și nu e
niciodată singura cale prin care ajunge un mesaj. C e deja construit exact așa (ledger `web_turns`
+ `GET` autoritativ + SSE ca proiecție). B e generația dinainte, păstrată din inerție.

## 4. Mecanismul de retragere (ce s-a implementat în faza 1)

Retragerea urmează procedura standard, nu una inventată aici:

- **`Deprecation`** (RFC 9745) — sf-date `@<epoch>`: când a fost declarată depășită.
- **`Sunset`** (RFC 8594) — HTTP-date IMF-fixdate: de când serverul își rezervă dreptul să refuze.
- **`Link; rel="deprecation"`** → documentul ăsta; **`rel="successor-version"`** → succesorul.
- **`deprecated: true` în OpenAPI**, ca generatoarele de client s-o vadă fără să citească headere.
- **`410 Gone`** cu corp citibil de mașină (`error`, `route`, `reason`, `successor`, `sunset`,
  `doc`) — motivul pentru care `410` bate `404` pe o rută retrasă.
- **Contor de utilizare** (`legacy_route_called` în `analytics_events`, plus o linie de log), ca
  „nu-l cheamă nimeni" să fie o măsurătoare, nu o convingere.

Sursa unică a acestor afirmații: [`src/web/deprecation.py`](../src/web/deprecation.py). Headerul,
corpul lui `410`, ce anunță bootstrapul și ce intră în contor se derivă din ACELAȘI tabel — ținute
separat, ar diverge exact când contează.

Trei decizii care merită numite:

1. **Ceasul nu schimbă comportamentul.** Ruta nu începe să dea `410` fiindcă a trecut data.
   Refuzul e explicit (`WEB_LEGACY_ASYNC_ENABLED=false`) și reversibil dintr-o variabilă de mediu.
   Data de sunset e o promisiune publicată; raportul spune dacă a trecut, codul n-o execută singur.
2. **Poarta stă după autentificare.** Fără sesiune validă răspunsul rămâne `403`: retragerea se
   anunță clienților legitimi, nu suprafeței de scanare.
3. **`sse_url` dispare din bootstrap** când transportul e stins — cheia lipsește, nu devine `null`.
   A continua să anunțăm o rută stinsă ar trimite clienți fix într-un `410`.

Kill-switch: `WEB_LEGACY_ASYNC_ENABLED` (implicit `true` = comportamentul de azi, plus anunț).

## 5. Fazele și porțile lor

| Fază | Ce se întâmplă | Poarta de intrare |
|---|---|---|
| 1 · anunț | headere, OpenAPI, contor, kill-switch, docs | livrată |
| 2 · refuz | `WEB_LEGACY_ASYNC_ENABLED=false` → `410` | producție care răspunde (altfel flip-ul nu e observabil) |
| 3 · ștergere | cod, teste, diagrame, `claim:routes` | ambele porți de mai jos |

Ștergerea are **două** porți, și niciuna nu e suficientă singură:

- **Telemetrie** — `python scripts/legacy_route_report.py --business-id <uuid>`. Verdicte:
  `BLOCKED` (există apeluri) · `UNKNOWN` (zero ture servite în fereastră) · `INSUFFICIENT`
  (eșantion mic sau sunset netrecut) · `SAFE_TO_REMOVE`.
- **Inventar de clienți** — reluarea comenzilor din §2 pe `main` și pe `dist/`, consemnată în PR-ul
  de ștergere. Telemetria nu poate vedea un client care există dar n-a rulat.

Verdictul de AZI e `UNKNOWN`, și e verdictul corect: cu zero ture servite, absența apelurilor nu
dovedește nimic — ar fi absența dovezii citită drept dovada absenței. Aceeași capcană numită la
NX-238 și NX-246. Fereastra utilă începe când producția răspunde din nou.

De ce sunset la 28 de zile și nu 90: fereastra lungă protejează integratori terți, pe care nu-i
avem. Singurul integrator e un repo pe care îl deținem și îl putem citi. Mecanismul rămâne cel
standard; doar durata e calibrată pe realitate.

## 6. Ce cade odată cu transportul (faza 3)

Retragerea lui B lasă fără consumator:

- `src/channels/web/sender.py` (`WebSender`) și cheile `web:out:*` / `web:backlog:*`;
- ramura `webchat` din dispatcher (calea sincronă scrie `deliver=False`, deci nu trece prin outbox);
- **proactivul pe web** — și aici e decizia reală, nu o curățenie.

Azi, un job proactiv pentru webchat (back-in-stock, coș abandonat) produce un rând de outbox,
dispatcherul îl publică, nimeni nu e abonat, mesajul moare după 300s, iar rândul rămâne `sent`.
Poarta proactivă e construită pe fereastra de 24h Meta, semantică pe care webchat n-o are.

Standardul pentru „ajunge la un client care nu are tabul deschis" e **Web Push** (RFC 8030 +
VAPID RFC 8292): service worker, permisiune explicită, subscripție stocată server-side. Nu îl
avem, și nu se poate improviza dintr-un pub/sub.

Deci, la faza 3: `webchat` declară că **nu are transport de push**, iar joburile proactive pentru
el se marchează `skipped_no_transport` în loc de `sent`. Nu e o pierdere de funcționalitate — e
încetarea unei minciuni din ledger. Recâștigarea proactivului pe web e un card propriu, cu două
variante oneste: Web Push/VAPID, sau livrare durabilă la reconectare (mesajele rămân în jurnal și
se iau la următorul `bootstrap`, adică exact modelul de la §3, extins de la un tur la conversație).

## 7. Rollback

Faza 2: `WEB_LEGACY_ASYNC_ENABLED=true` + restart. `410`-urile poartă `Cache-Control: no-store`
tocmai ca un proxy să nu păstreze refuzul peste rollback.

Faza 3: revert de commit. De aceea ștergerea e un commit separat, nu amestecat cu altceva.
