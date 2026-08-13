# Acțiuni opace v2 (NX-236) — contract, threat model, rotație de chei, runbook

**Status:** implementat, în spatele lui `WEB_ACTIONS_ENABLED` (default OFF).
**Cod:** [`src/web/action_models.py`](../src/web/action_models.py) ·
[`src/web/action_crypto.py`](../src/web/action_crypto.py) ·
[`src/web/action_service.py`](../src/web/action_service.py) ·
[`src/agent/action_kernel.py`](../src/agent/action_kernel.py).
**Contractul de sârmă:** [`FRONTEND-CONTRACT-IZI-V2.md`](FRONTEND-CONTRACT-IZI-V2.md) §3
(`ActionView`). **Ownership:** [`WEB-WIDGET-BOUNDARY-V2.md`](WEB-WIDGET-BOUNDARY-V2.md) §3.2.

---

## 1. Problema, într-o propoziție

Un buton de chat avea până acum o singură reprezentare: **eticheta**. Frontendul o afișa, iar la
apăsare o retrimitea ca mesaj — deci cine putea scrie eticheta putea scrie intenția, iar backendul
reconstruia sensul ghicind din text. NX-236 desparte gestul de semnificație: eticheta rămâne pentru
ochi, iar ce înseamnă butonul călătorește sigilat, într-un token pe care frontendul nu-l poate
citi, compune sau completa.

---

## 2. Cele două fețe ale unui control

| | `ActionView` (public) | `ActionEnvelope` (server-only) |
|---|---|---|
| Ce conține | `id`, `label`, `appearance`, `icon`, `enabled`, `activation` | kind + argumente canonice + legături + expirare |
| Cine îl vede | frontendul | nimeni în afara backendului (sigilat AEAD) |
| Ce face FE cu el | îl randează | îl retransmite NESCHIMBAT |

`activation` are exact două forme:

- `navigate` — un `href` deja validat (https absolut sau rută relativă). **Nu** devine token: un
  link nu poartă o comandă, deci n-are ce semnătură să ducă.
- `submit` — `token`, opac. Frontendul îl trimite înapoi ca
  `{"type": "action", "action_token": "<token>"}` pe `POST /web/v2/turns`.

---

## 3. Registry — vocabular ÎNCHIS

`KIND_REGISTRY` ([`action_models.py`](../src/web/action_models.py)) e finit. Fiecare intrare declară
dacă e mutantă, politica de consum, argumentele permise și dacă poate fi **emisă** în Stage 1.

| kind | mutant | emis Stage 1 | argumente | handler |
|---|---|---|---|---|
| `select_product` | nu | **da** | `product_ref` | fișa produsului (determinist) |
| `request_details` | nu | **da** | `product_ref` | `serve_details` (NX-220) |
| `request_reviews` | nu | **da** | `product_ref` | `serve_reviews` (NX-219) |
| `compare_selection` | nu | **da** | `product_refs` (2–3) | `serve_comparison` |
| `show_more` | nu | **da** | `session_ref` | paginare deterministă (NX-119b) |
| `answer_clarification` | nu | **da** | `question_id`, `option_ref` | umple slotul + reia ruta |
| `refine_search` | nu | **nu** | `filter` (enum) | REZERVAT — vezi mai jos |
| `cart_add_line`, `cart_set_quantity`, `cart_remove`, `cart_clear`, `checkout` | **da** | **nu** | — | NX-237 (receipt) |

**Două nume diferă de card, deliberat:** `compare_products` → `compare_selection` și `cart_add` →
`cart_add_line`, fiindcă primele erau deja **nume de tool-uri ale modelului**. Registrele trebuie să
fie disjuncte ca invariantul „un token nu poate numi un tool" să fie **mecanic**
(`assert_registry_disjoint`, verificat la import și în teste), nu promis într-un comentariu. Numele
nu ajunge niciodată la client (e sub sigiliu), deci costul redenumirii e zero.

**De ce `refine_search` e rezervat și neemis:** nu există încă o rafinare deterministă server-side
care să nu fie, de fapt, un prompt — „mai ieftin" trăiește azi în planner, legat de textul turului.
Cardul cere explicit „elimină/omite orice acțiune fără handler sigur". Numele rămâne în registry ca
metrica să poată număra o încercare; nimic nu îl emite, deci nimeni nu poate purta un token cu el.

**De ce comerțul e refuzat:** un buton care spune „am adăugat în coș" fără un receipt e o minciună
cu UI frumos. Rămâne `action_unavailable`, cu copy onest, până la NX-237.

---

## 4. Ciclul de viață

```
TUR SURSĂ (executor v2)                          TUR CONSUMATOR (accept v2)
  render_web(reply) ─┐                             POST /web/v2/turns
  plan_actions(view) │ ACEEAȘI tranzacție           input.type = action
  merge_actions ─────┤ (fencing pe lease_epoch)     │
  complete_turn ─────┘ → response_json.actions      ├─ crypto: open (AES-SIV)
                                                    ├─ audiență + expirare (+skew)
GET /web/v2/turns/{id}  (proiecție, la fiecare      ├─ tenant + sesiune (pseudonime)
citire)                                             ├─ turul-sursă (rând de ledger)
  issue_actions(row) → tokenuri DETERMINISTE        ├─ DOVADA DE EMITERE (re-derivare)
  ActionView(label, submit token)                   ├─ kind disponibil?
                                                    └─ consum one-shot (fingerprint)
                                                          ↓
                                              accept_web_turn(action_payload=…)
                                                          ↓
                                              executor → action_kernel_stage (înainte de triaj)
```

### 4.1 Dovada de emitere — fără tabel nou

`action_id` e **derivat**: `HMAC(key_id_sub, {turul-sursă, kind, argumente canonice})`. Planul
(`{kind, args}`, fără criptografie) se persistă în `web_turns.response_json["actions"]`, adică **în
tranzacția terminală** a turului-sursă. La consum re-derivăm id-urile din planul persistat: un token
al cărui id nu apare acolo **nu a fost emis de noi**, oricât de valid ar fi sigiliul.

Consecințe practice: zero migrare, zero registru paralel în Redis, retenție moștenită de la ledger
(de aceea `WEB_ACTION_TTL_S` trebuie să fie **sub** `WEB_TURNS_RETENTION_HOURS` — validat la boot),
iar un GDPR erase omoară automat butoanele conversației.

### 4.2 Consumul one-shot — tot în ledger

Cheia de consum e `request_fingerprint`-ul turului care folosește acțiunea: pentru un input de tip
acțiune, fingerprint-ul e HMAC peste `action_id` — **fără text și fără contextul de pagină**, altfel
același buton apăsat de pe două pagini ar produce două chei și s-ar putea consuma de două ori.

| Situație | Ce decide | Rezultat |
|---|---|---|
| același token + același `client_turn_id` | `unique (business, conversație, client_turn_id)` | replay exact, zero a doua execuție |
| același token + ALT `client_turn_id` | căutare pe fingerprint | `action_already_consumed` (409) |
| două consumări CONCURENTE | indexul parțial „un singur turn activ per conversație" | una inserează; cealaltă recitește și primește `already_consumed` |

### 4.3 Tokenurile nu se persistă

Se re-derivă determinist la fiecare proiecție (AES-SIV, `issued_at = completed_at` al rândului).
Două consecințe: `GET` repetat și SSE reconectat livrează **aceiași bytes**, iar un DB scurs nu
conține butoane valabile.

---

## 5. Threat model

| Atac | Ce îl oprește | Rezultat |
|---|---|---|
| Editez un byte din token | AEAD (AES-SIV) | `action_invalid`, generic |
| Rescriu prefixul (`versiune`/`key_id`) | prefixul e AAD, legat criptografic | `action_invalid` |
| Fabric un token | nu am cheia | `action_invalid` |
| Citesc tokenul ca să învăț suprafața | e sigilat, nu semnat-și-lizibil | zero informație |
| Mut tokenul pe alt tenant | pseudonim de tenant recalculat din sesiunea verificată | `action_not_found` (404) |
| Mut tokenul pe alt vizitator | pseudonim de sesiune + `session_ref_hash` pe rândul-sursă | `action_not_found` |
| Refolosesc un buton | consum one-shot în ledger | `action_already_consumed` |
| Reordonez produsele și apăs | acțiunea NUMEȘTE produsul (`product_ref`) | același produs, mereu |
| Paginez o căutare veche | `session_ref` = `active_search.fp` | `action_stale`, cu copy onest |
| Răspund la o clarificare veche | `question_id` = `q:<slot>:<încercare>` | `action_stale`, starea NU se modifică |
| Îmi fac singur un token de `cart_add_line` | kind mutant, `available=False` | `action_unavailable`, zero mutație |
| Pun un `kind` de tool în token | registre disjuncte + dispatch typed | respins înainte de dispatch |
| Schimb eticheta butonului în DevTools | eticheta nu e input | semantica nu se schimbă |
| Trimit un token de 1 MB | cap de contract (4096) + cap de claims (1024) | `action_invalid`, fără parsare |

**Ce NU acoperă cardul:** un browser compromis care are DEJA sesiunea validă poate apăsa butoanele
pe care le-ar fi putut apăsa oricum utilizatorul. Tokenul protejează *integritatea semanticii*, nu
compromiterea sesiunii — aia e treaba lui NX-229 (origin binding, expirare de sesiune).

---

## 6. Coduri de eroare (vocabular ÎNCHIS)

| cod | HTTP | retryable | Când |
|---|---|---|---|
| `action_not_supported` | 422 | nu | kill-switch stins |
| `action_invalid` | 400 | nu | crypto/format/kind/args — GENERIC, fără detalii |
| `action_expired` | 410 | **da** | TTL depășit; clientul cere o acțiune nouă |
| `action_not_found` | 404 | nu | sursă lipsă / alt tenant / altă sesiune / neemisă |
| `action_already_consumed` | 409 | nu | one-shot folosit de alt turn |
| `action_stale` | 409 | **da** | sesiune/întrebare schimbată între emitere și click |
| `action_unavailable` | 409 | nu | kind cunoscut, fără handler sigur (comerț → NX-237) |

Mesajul e **server-owned și localizat**; motivul fin (`tenant_mismatch`, `not_emitted`,
`session_mismatch`, …) rămâne în log — pe sârmă ar fi un oracol.

---

## 7. Chei: format, rotație, incident

### Format

```
WEB_ACTION_KEYS="k2:<base64 ≥32B>,k1:<base64 ≥32B>"
```

Prima cheie **emite**, toate **verifică**. Din fiecare master se derivă (HKDF-SHA256, context
separat per rol) trei subchei: sigiliu AES-256-SIV, HMAC pentru `action_id`, HMAC pentru pseudonime.
Materialul master nu se păstrează după derivare.

Generare:

```bash
python -c "import base64,os;print('k1:'+base64.b64encode(os.urandom(32)).decode())"
```

### Rotație (fără butoane moarte)

1. Pune cheia NOUĂ în **față**, păstrând-o pe cea veche: `WEB_ACTION_KEYS=k2:…,k1:…`.
2. Deployează. Din acest moment se emite cu `k2`; tokenurile `k1` rămân valide.
3. Așteaptă cel puțin `WEB_ACTION_TTL_S` (default 30 min) — fereastra de overlap.
4. Scoate `k1`.

Scoaterea cheii vechi **înainte** de expirare invalidează butoanele deja afișate: clientul primește
`action_invalid` cu copy server-owned (nu tăcere, dar nici comportamentul dorit). Metrica de
control: `web_action_key_age{slot=previous}` trebuie să ajungă la zero înainte de pasul 4.

### Incident — cheie compromisă

1. **Scoate cheia din inel imediat.** Invalidarea e instantanee și deliberată: nu există „revocare
   per token"; TTL-ul scurt + consumul one-shot sunt mecanismul.
2. Rotește secretul (pas 1–2 de mai sus, cu o cheie nouă generată).
3. Urmărește `web_action_verified{reason=unknown_key}` — vârful arată cât trafic purta tokenuri
   emise cu ea. Un vârf de `reason=bad_seal` fără rotație recentă = **tentativă de tamper**.
4. `WEB_TURN_FINGERPRINT_SECRET` NU se rotește în același timp: ar schimba cheile de consum și ar
   permite re-consumarea butoanelor deja folosite. Dacă trebuie rotit, fă-o după ce toate acțiunile
   emise anterior au expirat.

### Rollback

`WEB_ACTIONS_ENABLED=false`. Butoanele deja afișate primesc `action_not_supported` (422, cu copy);
rezultatele turelor rămân intacte — planul persistat e inert fără flag, iar proiecția revine la
ViewModel-ul fără butoane semantice, byte-identic cu cel de dinainte de card.

---

## 8. Observabilitate (low-cardinality)

| Eveniment | Etichete | Ce urmărești |
|---|---|---|
| `web_action_verified` | `outcome`, `reason`, `kind` | tamper spike, unknown key, cross-tenant |
| `web_action_key_age` | `bucket`, `slot` | dacă overlapul de rotație e destul de lung |
| `web_action_consumed` | `kind`, `outcome` | câte butoane chiar se folosesc, pe kind |
| `web_action_handler_ms` | `kind`, `outcome`, `elapsed_ms` | costul drumului determinist |
| `web_action_replay` | `mode` (`same_turn`) | retry-uri de client |
| `web_action_stale` | `reason` | cât de des se schimbă lumea sub buton |

**Niciodată în etichete:** token (nici hash, nici prefix), `action_id`, id de produs, id de sesiune
sau de conversație, argumente brute. `redact_token()` raportează doar lungimea — un prefix de hash
ar fi destul cât să coreleze două loguri, iar corelația dintre un buton apăsat și o conversație e
exact ce n-are voie să existe.

Alarme: vârf de `bad_seal`, orice `unknown_key` în afara ferestrei de rotație, `action_stale` peste
prag (semn că TTL-ul e prea lung față de ritmul conversației), orice mutație fără receipt (după
NX-237).

---

## 9. Proba reproductibilă

```bash
python scripts/action_drive.py
```

Emite un ViewModel cu recenzii, comparație, clarificare, paginare și un cart action dezactivat, apoi
trece FIECARE token prin toate scenariile din §5: valid, byte schimbat, altă sesiune, alt tenant,
expirat, sursă ștearsă, acțiune neemisă, cheie rotită (cu și fără overlap), retry pe același turn,
consum din alt turn. Rulează **fără DB, fără Redis, fără OpenAI** (cost zero), iar ieșirea e deja
redactată: coduri + contoare, niciun token complet.

Rezultatul așteptat (și cel obținut la implementare): **88 de scenarii, 0 neașteptate**, cu
`{"handler": 0, "model": 0, "tool": 0, "mutation": 0}` pe toate drumurile refuzate.

Concurența reală (unicii de DB, 20 de consumări simultane) e în
[`tests/test_web_action_replay_db.py`](../tests/test_web_action_replay_db.py) (`-m integration`).
