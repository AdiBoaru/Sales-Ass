Generează un card de task NOU pentru proiectul Nativx Assistant: $ARGUMENTS

(Argumentele conțin ID-ul taskului + rândul din planul master sau descrierea taskului nou.)

You are a Senior Software Architect and Technical Lead working on the **Nativx Assistant**
project. Before anything else, read `CLAUDE.md` (architecture, schema, principles 1-12)
and `CONTRIBUTING.md`. The existing task cards in `tasks/` are the gold standard —
match their structure, depth and tone exactly. Citește 2-3 carduri existente
(ex. tasks/T021.md, tasks/T030.md, tasks/T013.md) înainte să scrii.

Scrie cardul în `tasks/TXXX.md` cu EXACT acest format, în română:

# TXXX — Nume task
**Owner:** S | J · **Faza:** MVP | P1 | P2 · **Zi/Ord:** … · **Branch:** `tip/TXXX-nume` · **Complexitate:** XS/S/M/L · **Estimare:** Xh

## Goal
Ce trebuie să realizeze taskul și DE CE există în arhitectură (1 paragraf).

## Business Context
Cum contribuie componenta la sistem și la banii clientului (1-3 fraze).

## Technical Description
Logica cerută, fluxul de date, integrarea cu pipeline-ul, error handling, validări,
securitate, performanță. Include cod/SQL concret unde clarifică. Aici e 60% din valoare.

## Principii CLAUDE.md aplicabile
Numerele principiilor (1-12) relevante + CE înseamnă concret pentru implementare.

## Implementation Steps
Pași numerotați în ordinea reală, inclusiv pasul de test.

## Files To Create / Files To Modify
Căi exacte din structura proiectului.

## Database Changes
DDL/indexuri/migrări sau „None". Orice query NOU include `business_id = $1` și,
unde e cazul, `language`.

## API Changes
Endpoint/request/response/erori sau „None".

## Events de emis (analytics)
Tip + properties pentru ctx.events / analytics.*. Dacă „None" — justifică.

## Dependencies
ID-urile taskurilor care trebuie să fie în main înainte.

## Out of Scope
Minim 2 puncte — ce NU face taskul (anti scope-creep).

## Definition of Done
Checklist bifabil, fiecare punct verificabil prin comandă sau test.

## Test Cases
**Happy Path** / **Edge Cases** / **Failure Cases** — minim 2 fiecare.
Testele cu LLM folosesc replay/mock (T140) — ZERO apeluri reale în CI.

Reguli suplimentare:
1. Română, ton direct, fără umplutură corporatistă.
2. Granularitate 0.5-4h; >4h → propune spargerea, marcat [PROPUNERE].
3. Nu inventa componente — marchează [PROPUNERE — lipsește din plan] dace e cazul.
4. Multi-tenant paranoid: business_id în orice exemplu de query; language pe faq/cache/templates.
5. PII doar în conv.channel_identities și conv.orders.customer_phone; spune ce se redactează în loguri.
6. LLM doar în triaj și agent — dacă taskul cheamă LLM din altă parte, semnalează, nu scrie.
7. Numește câmpurile TurnContext pe care taskul are voie să le scrie.
8. Dacă face apeluri LLM/embeddings: estimare de cost per 1000 mesaje + ce limitează costul.

Când generezi mai multe carduri odată, adaugă la final: Task Order (lanțul de dependențe),
Parallel Tasks (ce pot lucra S și J simultan fără conflicte de fișiere) și Architecture
Review DOAR dacă ai găsit ceva nou față de plan.
