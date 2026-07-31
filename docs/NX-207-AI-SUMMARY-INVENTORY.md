# NX-207 — inventar `products.ai_summary`

Inventar făcut la 2026-07-27 înainte de orice activare a `SEARCH_SHADOW_ENABLED`.
`ai_summary` rămâne sursa live pentru textul de conversație și pentru embeddings-ul legacy;
`search_document_v1` îl completează, nu îl înlocuiește încă.

## Consumatori runtime

| Consumator | Rol curent | Înlocuit de artefact NX-207? | Condiție de migrare |
| --- | --- | --- | --- |
| `src/jobs/embed_products.py:_embed_text` | generează embedding-ul legacy `doc_type='product'` | Da, prin `embed_shadow_pending` / `search_document_v1` | benchmark H1 favorabil și switch controlat |
| `src/db/queries/catalog.py:_SELECT`, `_DETAIL_SELECT` | proiectează câmpul în obiectul produsului | Nu direct | păstrat până se mută toți prezentatorii |
| `src/tools/catalog_tools.py:_brief`, `_detail` | context factual compact pentru agent | Parțial: `evidence_chunks`, nu documentul pozitiv | hydration de evidence + teste de grounding |
| `src/agent/finalize.py:_products_context` | descriere pentru răspunsul agentului | Parțial: `card_blurb`/evidence | evaluare conversațională NX-210 |
| `src/agent/fallbacks.py` | text determinist când agentul eșuează | Da, prin `card_blurb` | fallback nou trebuie să păstreze P6 |
| `src/worker/compose.py` | câmpul `RichItem.details` trimis front-endului | Da, prin `card_blurb` | contract front-end actualizat și testat |

## Procese offline / operaționale

| Consumator | Rol | Plan |
| --- | --- | --- |
| `scripts/audit_catalog_v2.py` | audit R7/R12 al afirmațiilor din rezumat | se păstrează până la deprecarea coloanei |
| `scripts/audit_pilot_data.py`, `scripts/spot_check.py` | control de calitate și readiness pilot | se adaptează după migrarea prezentării |
| `scripts/enrich_catalog_v3.py`, `scripts/enrich_catalog.py`, `scripts/seed_catalog_v2.py` | produc / persistă rezumatul legacy | nu se schimbă înainte de o migrare de write explicită |
| `scripts/summarize_reviews.py` | context pentru sumarul recenziilor | decide separat; nu este read-path de search |
| `scripts/export_pilot_data_pack.py` | export operațional | păstrează compatibilitate până la versiunea nouă a pachetului |

## Ordinea obligatorie de deprecere

1. Se rulează benchmarkul NX-203 pe H1; fără etichete validate uman nu se pornește switch-ul.
2. Se activează gradual `SEARCH_SHADOW_ENABLED` numai dacă rezultatele cresc; OFF revine imediat
   la `doc_type='product'`.
3. Se mută separat fiecare consumator de prezentare la `card_blurb` sau `evidence_chunks`, cu
   teste de fallback și grounding.
4. Se oprește writerul/embedding-ul legacy doar după ce nu mai există consumatori runtime.
5. Abia apoi se propune eliminarea coloanei într-o migrare separată, după auditul acestui inventar.

## Interdicții

- `positive_search_document` nu devine text de prezentare: exclude deliberat warnings și limitări.
- `card_blurb` nu devine sursă de adevăr: este artefact derivat.
- Nu se șterge și nu se rescrie `ai_summary` în cadrul rollout-ului shadow.
