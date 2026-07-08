# Contributing — Nativx Assistant

## Branches

Fiecare task are branch-ul său specificat în card. Format:
```
feat/TXXX-nume-scurt    # funcționalitate nouă (la fel: feat/NX-XX-nume)
fix/TXXX-nume-scurt     # bug fix
chore/TXXX-nume-scurt   # infra, config, migrări
test/TXXX-nume-scurt    # teste standalone
docs/TXXX-nume-scurt    # documentație
```
Pentru lucru grupat fără card (ex. „G1 queries"), nume descriptiv: `feat/db-runtime-queries`.

Niciodată nu lucrezi direct pe `main` (excepție: T001 — repo gol).

Creare branch:
```bash
git checkout main && git pull
git checkout -b feat/TXXX-nume-scurt
```

## Commit messages

Format conventional commits cu ID-ul taskului:
```
feat(T042): add semantic cache lookup with language filter
fix(T031): correct business_id isolation in faq query
chore(NX-51): add inbound_dedupe DDL to 004_inbound_dedupe.sql
```

Reguli:
- Prima linie ≤ 72 caractere
- Fără punct la final
- Timp prezent ("add", nu "added")
- ID-ul taskului în paranteză — obligatoriu

## Linting și teste

Înainte de orice PR:
```bash
ruff check .
ruff format .
pytest -x -q
```

Toate trei trebuie să treacă cu zero erori. `ruff format` modifică fișierele — comite după.

### Golden regression (NX-145)

Pentru schimbări de prompt, model, pipeline, tool executor sau planner, rulează gate-ul golden:
```bash
pytest tests/test_golden.py tests/test_eval_regression.py -q
```

Pentru snapshot manual/nocturn:
```bash
python scripts/eval_regression.py --out reports/golden_snapshot.json
python scripts/eval_regression.py --baseline reports/golden_snapshot.json
```

`eval_regression.py` folosește LLM scriptat și stub-uri DB: zero OpenAI și zero DB real.
Un diff pe rută, tool-uri, product IDs, cacheable sau pass/fail este regresie de investigat.

## Pull Requests

- Titlu: același format ca commit-ul principal
- Branch: `feat/TXXX-...` → `main`
- Reviewer: celălalt membru (S revieuiește J, J revieuiește S)
- Merge: Squash and merge, după aprobare + CI verde
- Nu forța merge fără review, indiferent de urgență

### ⚠️ Branch-ul cu PR deschis e ÎNGHEȚAT

PR-urile se merge-uiesc repede — uneori la primul commit. Un push pe branch
DUPĂ merge rămâne ORFAN (squash merge → commit-ul nu ajunge în main și nimeni
nu observă). S-a întâmplat de 3 ori (#15, #17, #23). Reguli:

1. După `gh pr create`, NU mai împingi scope nou pe acel branch — chiar dacă
   e înrudit. Branch NOU din main + PR separat.
2. Excepție (fix cerut la review): verifică ÎNTÂI `gh pr view N --json state`
   — doar dacă e `OPEN` ai voie să împingi.
3. Recuperare orfan: `git checkout -b fix/recover-... origin/main &&
   git cherry-pick <sha>` + PR nou.
4. La început de sesiune, un scan rapid prinde orfanii devreme — fișierele
   din commit lipsesc din main, nu doar SHA-ul (squash schimbă SHA-urile mereu).

## Reguli permanente (din CLAUDE.md)

1. Orice query SQL are `WHERE business_id = $1` — fără excepție
2. Lookup-urile în `faqs` / `semantic_cache` / `wa_templates` includ și `locale` (limba e parte din cheie)
3. PII (telefoane / id-uri de canal) DOAR în `channel_identities` — `orders` NU are customer_phone; niciodată în loguri
4. LLM se apelează DOAR din stagiile triaj și agent
5. Un singur scriitor per câmp din `TurnContext` — respectă docstring-ul câmpului
6. `outbox` e singurul punct de ieșire — nicio trimitere directă la Meta din stagii

## Structura unui task

1. Citești cardul complet (`tasks/TXXX.md`)
2. Verifici că dependențele sunt în `main`
3. Ești pe branch-ul corect
4. Implementezi STRICT ce cere **Technical Description** + **Implementation Steps**
5. Respecți **Out of Scope** — ce e acolo nu se atinge
6. Scrii testele din **Test Cases** (minimum)
7. `ruff check . && ruff format . && pytest -x -q` — verde
8. Bifezi **Definition of Done** punct cu punct
9. Deschizi PR după confirmare
