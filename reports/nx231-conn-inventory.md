# NX-231 — inventarul conexiunilor DB în runtime (before / after)

_Artefactul cerut de Definition of Ready („toate utilizările `deps.conn` și context managers tenant
sunt inventariate într-un raport"). Regenerabil oricând:_

```bash
python scripts/check_no_raw_conn.py --report                 # inventar citibil
python scripts/check_no_raw_conn.py --out reports/conn.json  # aceleași date, JSON
```

Baseline: `origin/main@61ec53e` (înainte de NX-231).

## R1 — `deps.conn` (conexiunea vie, ținută cât ține turul)

**Înainte: 48 de utilizări în 15 fișiere. După: 0.**

| # | Fișier | Ce ținea conexiunea peste |
|---|---|---|
| 8 | `src/tools/catalog_tools.py` | `embed()` (vectorul de query) + scara de relaxare |
| 7 | `src/tools/commerce_tools.py` | validare catalog → scriere `checkout_links` / abonare restock |
| 6 | `src/worker/stages/cache.py` | `embed()` ÎNTRE lookup-ul exact și cel semantic — pe TOT traficul |
| 5 | `src/agent/deterministic.py` | intenții pre-loop (review/detail/link/compare) |
| 4 | `src/agent/planner.py` | cross-sell, superlativ, „mai ieftin", rehidratare |
| 3 | `src/worker/stages/agent.py` | prompt inputs + prune de siguranță, în jurul buclei de tool-calling |
| 3 | `src/worker/stages/faq.py` | `embed()` înaintea lookup-ului |
| 2 | `src/tools/faq_tools.py` · `stages/alias.py` · `stages/gates.py` · `stages/triage.py` | — |
| 1 | `src/tools/handoff_tools.py` · `orders_tools.py` · `stages/handoff.py` · `stages/language.py` | — |

## R2 — conexiune ținută peste un await EXTERN

Ăsta e bug-ul propriu-zis; R1 era doar mecanismul prin care se producea. Locuri unde o conexiune
din `bot_pool` (max 10) exista în timp ce se aștepta rețeaua altcuiva:

| Locul | Ce se aștepta cu conexiunea în mână |
|---|---|
| `worker/processor.handle_turn` | TOT turul: triaj (nano), agent (mini), bucla de tool-calling, embed-uri |
| `web/app.web_chat` | idem, plus `tenant_conn` deschis explicit în jurul lui `handle_turn` |
| `worker/dispatcher._dispatch_claimed_row` | `send_text`/`send_rich`/`send_template` — HTTP-ul Meta/Telegram/web |
| `stages/gates` (moderare) | apelul de moderation API, apoi `block_contact` |
| `tools/handoff_tools` | POST-ul către webhook-ul operatorului |

**După: 0** (verificat mecanic, regula R2 din `scripts/check_no_raw_conn.py`).

## R3 — punct de intrare (stagiu / tool) care primește `conn`

Înainte: 0 (contractul `(ctx, deps)` era deja respectat — `conn` venea prin `deps`).
După: 0. Regula există ca plasă: un stagiu nou care ar cere `conn` ar readuce proprietatea
conexiunii la apelant.

## Ce a rămas legitim

- `src/db/queries/**` — stratul de repository. Acolo `conn` E materialul de lucru.
- `admin_conn` (control plane): `resolve_channel`, due-tenants, joburi admin. Nu ține nimic peste
  apeluri externe pe calea unui tur.
- Joburi offline (`src/jobs/*`) — rulează pe `admin_pool`, în afara căii de request, și nu
  concurează pentru `bot_pool`.
- `PipelineDeps(conn=...)` în `tests/` — puntea de compat le mapează la un provider static.
  Guard-ul scanează doar `src/`.

## Starea guard-ului

`scripts/conn_allowlist.json` e **gol**. Fiecare excepție viitoare cere un motiv scris; checkerul
refuză o intrare fără el. Dacă lista începe să crească, e semnalul că invariantul se erodează.
