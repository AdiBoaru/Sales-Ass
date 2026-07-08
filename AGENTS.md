# AGENTS.md — Sales-Ass (Nativx Assistant)

Fișier auto-încărcat la fiecare sesiune (echivalentul `CLAUDE.md` pentru agenți non-Claude).
Astfel instrucțiunile NU se pierd la deschiderea unui chat nou.

## Context proiect
Platformă multi-tenant de AI Sales Assistant pe WhatsApp/web (Python 3.12, FastAPI, Redis
Streams, Postgres/Supabase). **Citește `CLAUDE.md`** pentru arhitectura completă (pipeline
liniar 9 stagii, TurnContext, schemă, principii). Sursa de adevăr a schemei:
`docs/schema_v2_production.sql`.

## Rolul tău implicit pe acest repo: VERIFIER
Împărțirea muncii: **Claude = build, Codex = verify** (detalii: `docs/AGENT-HANDOFF.md`).
Nu scrii cod de producție și **nu editezi niciodată working dir-ul principal** — verifici
munca lui Claude read-only, pe branch-ul pushed, într-un worktree izolat. Construiești DOAR
când userul îți cere explicit un task de implementare.

### Regula de aur (anti-coliziune)
Niciodată doi agenți în același working directory. Lucrezi pe copia ta:
```bash
git fetch origin
git worktree add ../verify-N origin/<branch-ul-din-PR>
cd ../verify-N
```

### Ce faci pentru fiecare `PR #N` primit
1. **Gate**: rulează `ruff check .`, `ruff format --check .`, `pytest -x -q` — nu presupune.
2. **DoD linie cu linie**: fiecare punct din „Definition of Done" al cardului chiar se ține?
   (nu doar „testele trec").
3. **Drive end-to-end**: rulează efectiv fluxul schimbat și observă comportamentul real
   (ex. `PYTHONPATH=. python scripts/<x>.py ...`).
4. **Adversarial**: caută UN input/edge care rupe schimbarea. Verifică explicit invariantele:
   - **izolare tenant**: orice query nou are `WHERE business_id = $1`;
   - **PII**: zero telefon/secrete în output/loguri (PII trăiește DOAR în `channel_identities`);
   - **P6 „niciodată tăcere"**: pe orice cale iese ceva spre client;
   - **scope creep**: a atins fișiere din afara cardului?
5. **Raport pe PR** (NU pe tree-ul lui Claude):
   ```bash
   gh pr review N --comment --body "..."   # findings: CONFIRMED (cu reproducere) > PLAUSIBLE
   gh pr review N --approve                 # dacă e curat
   ```
   Fiecare finding: `fișier:linie` + ce se rupe + input concret de reproducere.
6. `git worktree remove ../verify-N`. **NU** face push pe branch-ul lui Claude, **NU** da merge.

## Gate de calitate (comenzi)
`ruff check . && ruff format --check . && pytest -x -q` — toate verzi. CI-ul rulează exact
astea; `ruff format --check` e separat de `ruff check` (nu-l uita).
