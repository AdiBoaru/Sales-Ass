# NX-202 — Schema „business truth" pentru cazuri compuse (truth-first, decuplat de contract)

**Status:** DRAFT — Claude propune structura + constrângerile; **Adi etichetează adevărul** · 2026-07-23
**Card:** [tasks/NX-202.md](../tasks/NX-202.md) · **Direcție:** review Codex (truth-first, nu legat de tool vechi)

## De ce truth-first

Cazurile golden vechi exprimă „adevărul" prin forma contractului de tool (`expect.expected_product_ids`,
`tool_calls`, `ai_summary` în fixture). Când vine `search_entities` (NX-209), forma aia se rescrie —
dar **adevărul de business nu trebuie să depindă de ea**. Deci fiecare caz compus nou își declară
adevărul într-un bloc `truth` SEPARAT, stabil peste rescrierea contractului. Fixture-ul poate folosi
temporar `search_products` ca să ruleze pe pipeline-ul actual, dar `truth` e sursa care supraviețuiește.

## Schema blocului `truth` (per caz)

```jsonc
{
  "id": "compound-...",
  "language": "ro",
  "input": "<query-ul clientului>",
  "dimensions": ["multi-constraint", "free_of", "budget", ...],  // trasabilitate la cerințele NX-202
  "truth": {
    "hard_constraints": [                 // NU pot fi încălcate (D7). Claude propune din text; Adi validează.
      {"facet": "price", "op": "lte", "value": 100, "unit": "RON"},
      {"facet": "fragrance_free", "op": "eq", "value": true}
    ],
    "soft_preferences": [                  // influențează ranking, pot fi relaxate cu trade-off explicat.
      {"facet": "finish", "value": "matte", "weight": "high"}
    ],
    "expected_products": [],               // ⟵ ADI: id-uri/ nume de produs ACCEPTABILE (din catalogul de 300).
    "forbidden_products": [],              // ⟵ ADI: produse care NU trebuie recomandate (off-constraint/off-category).
    "required_disclosures": [],            // ⟵ ADI confirmă textul: ex. „nu pot confirma «fără parfum» la X".
    "forbidden_claims": [                  // reguli (Claude): niciun claim medical/terapeutic, niciun preț/stoc inventat.
      "medical", "invented_price", "invented_stock"
    ],
    "on_unknown": "…",                     // comportament când o constrângere e UNKNOWN (nu MISMATCH): clarifică/disclose.
    "on_no_exact_match": "…",              // când NU există exact: alternativă cu trade-off EXPLICAT, niciodată tăcere.
    "correct_answer_sketch": ""            // ⟵ ADI: schița răspunsului corect (1-2 fraze), NU text exact.
  },
  "fixture_note": "search_products temporar (pipeline actual); se rescrie pe search_entities la NX-209"
}
```

## Ce completează cine
- **Claude (acum):** `input`, `dimensions`, `hard_constraints`+`soft_preferences` (derivate din text, ca
  propunere), `forbidden_claims` (reguli), schița `on_unknown`/`on_no_exact_match`.
- **Adi (etichetare adevăr):** `expected_products`, `forbidden_products`, textul din `required_disclosures`,
  `correct_answer_sketch`, ȘI validarea/ajustarea hard-vs-soft (ce e cu adevărat inviolabil).

## Acoperirea cerută (Codex) — bifată în `dimensions`
- 3-4 constrângeri simultane · imposibilitatea satisfacerii tuturor · alternativă cu trade-off explicat ·
  `free_of` · buget · context de utilizare · comparații cu ≥2 diferențe demonstrate.

Dataset: [`tests/golden/compound_truth_draft.json`](../tests/golden/compound_truth_draft.json).
Nu e wired în gate-ul CI încă (produsele sunt TODO-Adi); devine caz golden rulabil la NX-209.
