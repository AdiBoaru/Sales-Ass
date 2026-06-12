## Task

- ID: <!-- TXXX -->
- Card: [`tasks/TXXX.md`](../tasks/TXXX.md)

## Ce face

<!-- Descrie pe scurt ce aduce acest PR -->

## Cum s-a testat

```
ruff check . && ruff format --check . && pytest -x -q
```

<!-- Altceva relevant: queries rulate manual, comportament verificat etc. -->

## Checklist

- [ ] Definition of Done din card bifat punct cu punct
- [ ] `pytest -x -q` verde local
- [ ] Fără secrete sau PII în cod/loguri
- [ ] `WHERE business_id = $1` pe orice query nou
- [ ] Lookup-urile în faq/cache/templates includ `language` (dacă e cazul)
