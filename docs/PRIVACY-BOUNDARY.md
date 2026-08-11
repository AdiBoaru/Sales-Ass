# Frontiera de privacy — flux de date și inventar de sink-uri (NX-230)

> Unde intră textul clientului, unde ajunge, și ce formă are în fiecare loc.
>
> Cod: [`src/privacy/`](../src/privacy/) · Card: [`tasks/stage1/NX-230.md`](../tasks/stage1/NX-230.md)
> Teste: [`tests/test_privacy_boundary.py`](../tests/test_privacy_boundary.py)

## 1. Bucla pe care o repară cardul

Masca de PII exista deja (NX-121, `gates.mask_pii`) și funcționa. Acoperea însă doar promptul
turului **curent**. Comentariul care o însoțea spunea singur ce rămâne descoperit:

> „`messages.body` stocat rămâne RAW (PII legitim în storage — explicit out-of-scope) → istoricul
> îl poate reintroduce în prompt la turul următor"

În cod, bucla era închisă:

```
processor.py:359   insert_message(body=event["body"])     ← BRUT, pe disc
        ↓ ~90 de linii mai jos
runner → gates      mask_pii(ctx.message.body)            ← masca, doar pentru promptul curent
        ↓ turul următor
context.py:32      body = (m.body or "").strip()          ← citește înapoi BRUTUL din DB
        ↓
                    prompt                                 ← PII-ul se întoarce
```

Turul 1 masca telefonul pentru model și îl scria brut în DB. Turul 2 îl citea din DB și îl punea în
prompt. Masca era o perdea în fața unei uși deschise.

## 2. Fluxul de acum

```
ingress (web / worker)
   │
   ▼
apply_boundary(body)                    ← O SINGURĂ DATĂ, înainte de prima scriere durabilă
   ├── RawInbound  (RawText)  ─────────► memoria turului: agentul principal (D6)
   └── SafeInbound (str redactat) ─────► messages.body, analytics, cache, logs, ledger
                                          │
                                          ▼
                             context.conversation_transcript
                             (redactare ȘI la citire — rândurile vechi sunt brute)
```

Două forme, două tipuri. Nu se pot încurca: `RawText` nu se serializează și nu se afișează.

## 3. Inventarul de sink-uri

Condiția de STOP a cardului cere ca fiecare sink să fie inventariat. Ăsta e inventarul.

| # | Sink | Formă acum | Cum |
|---|---|---|---|
| 1 | `messages.body` (inbound) | **safe** | `processor.handle_turn` → `SafeInbound.text` |
| 2 | `messages.body` (outbound) | safe prin construcție | textul botului, nu al clientului |
| 3 | prompt — tur curent | **raw permis (D6)** | `ctx.message.body`, memoria turului |
| 4 | prompt — istoric | **safe** | `context.conversation_transcript` redactează la citire |
| 5 | loguri | **safe** | `RawText.__str__` → placeholder; `safe_for_telemetry` pentru `str` |
| 6 | `analytics_events` | **safe** | doar contoare + bucketuri, niciodată text |
| 7 | Redis `inbound` envelope | ⚠️ **raw** | vezi §5 — felia 2 |
| 8 | `semantic_cache.query_norm` | ⚠️ **raw** | vezi §5 — felia 2 |
| 9 | `conversation_summaries` | ⚠️ derivat din istoric | citește rânduri deja safe de acum încolo |
| 10 | `contacts.profile` | are mască proprie | `profile._PHONE_RE` — de consolidat, felia 2 |
| 11 | `conversation_facts` | are gardă proprie | `memory_safety` — de consolidat, felia 2 |
| 12 | `outbox.payload` | textul botului | nu conține input de client |
| 13 | `inbound_dedupe` | doar `provider_msg_id` | fără conținut |
| 14 | traces / OTel | **safe** | aceleași reguli ca logurile |

## 4. Politica per destinație

Nu toate sink-urile au aceleași nevoi, iar a pretinde că au duce fie la scurgeri, fie la stricarea
funcționalității ([`policy.py`](../src/privacy/policy.py)):

| Profil | Categorii | Justificare |
|---|---|---|
| `PERSIST` | telefon, email, IBAN, card, CNP, adresă, secret | tot PII-ul propriu-zis. **Numărul de comandă rămâne** — `check_order` are nevoie de el, iar o comandă fără cont are valoare mică pentru cine ar citi rândul |
| `TELEMETRY` | tot, inclusiv referințe de comandă | o metrică n-are nevoie de niciun identificator, iar cheile de cache sunt cel mai lung-trăitor sink |
| `PROMPT` | telefon, email, IBAN, card | comportamentul NX-121, neschimbat, sub același kill-switch |

Un nume de profil greșit întoarce `TELEMETRY` — cel mai strict. O eroare de tipografie nu are voie
să deschidă o scurgere.

## 5. Ce NU acoperă încă — felia 2

Scris explicit ca să nu fie confundat cu acoperire:

- **Envelope-ul Redis** (`inbound` stream) încă poartă body brut între webhook și worker. E
  efemer (consumat și șters) și intra-VPC, dar nu e redactat. Frontiera se aplică în worker, la
  intrarea în `handle_turn`.
- **`semantic_cache.query_norm`** stochează interogarea normalizată brută. E cel mai
  lung-trăitor sink dintre toate și merită felia lui, cu invalidare.
- **Cele cinci detectoare** nu sunt încă toate consolidate. `privacy/detectors.py` e acum sursa
  canonică, dar `safety/external_data.py`, `worker/memory_safety.py`, `worker/profile.py` și
  `worker/summarizer.py` își păstrează încă regexurile proprii. Consolidarea lor schimbă
  comportamentul unor gărzi de siguranță, deci cere măsurare separată — nu un rider pe cardul ăsta.
- **Backfill pentru datele demo existente.** Rândurile vechi sunt redactate **la citire**, deci nu
  mai ajung în prompt. Nu sunt însă rescrise pe disc. Cardul cere script dry-run + aprobare
  explicită înainte de orice atingere a datelor live; nu am rulat nimic.

## 6. GDPR și retenție

- `gdpr_erase_contact` rămâne sursa de adevăr pentru ștergere: anonimizează `contacts`, șterge
  `channel_identities`, golește `messages.body`/`payload`/`media_ref`. Frontiera de privacy **reduce
  ce era acolo de la bun început** — nu înlocuiește ștergerea.
- Retenția pe `messages` / `analytics_events` rămâne prin drop de partiții lunare.
- `SensitiveTokenMap` e request-scoped și refuză serializarea (`__reduce__` ridică). Nu există
  vault raw, iar default-ul Stage 1 e că nu va exista: dacă businessul îl cere, e card separat cu
  KMS, TTL, audit și DPA.

## 7. Ce garantează tipurile, nu disciplina

`RawText` refuză să se afișeze: `repr`, `str`, f-string, `%`-logging și `json.dumps` produc un
placeholder sau eșuează. Ca să obții valoarea trebuie să scrii `.value` — explicit, greppable,
vizibil în review.

Asta e diferența dintre „am uitat să maschez aici" și „am scris deliberat `.value` aici". Prima e
invizibilă; a doua e o decizie pe care cineva o poate contesta.
