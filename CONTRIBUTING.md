# Contributing — Nativx Assistant

## Branches

Fiecare task are branch-ul său specificat în card. Format:
```
feat/TXXX-nume-scurt    # funcționalitate nouă
fix/TXXX-nume-scurt     # bug fix
chore/TXXX-nume-scurt   # infra, config, migrări
test/TXXX-nume-scurt    # teste standalone
docs/TXXX-nume-scurt    # documentație
```

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
chore(T021): add conv.outbox DDL to 002_schema_fixes.sql
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

## Pull Requests

- Titlu: același format ca commit-ul principal
- Branch: `feat/TXXX-...` → `main`
- Reviewer: celălalt membru (S revieuiește J, J revieuiește S)
- Merge: Squash and merge, după aprobare + CI verde
- Nu forța merge fără review, indiferent de urgență

## Reguli permanente (din CLAUDE.md)

1. Orice query SQL are `WHERE business_id = $1` — fără excepție
2. Lookup-urile în faq / response_cache / clarification_templates / wa_templates includ și `language`
3. PII (telefoane) doar în `conv.channel_identities` și `conv.orders.customer_phone` — niciodată în loguri
4. LLM se apelează DOAR din stagiile triaj și agent
5. Un singur scriitor per câmp din `TurnContext` — respectă docstring-ul câmpului
6. `conv.outbox` e singurul punct de ieșire — nicio trimitere directă la Meta din stagii

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
