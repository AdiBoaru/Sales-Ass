# NX-235 — `ConversationStateV2`: memoria conversației ca stare redusă

**Status:** implementat, flaguri OFF (rollout în trei trepte, vezi §6).
**Cod:** `src/conversation/` (state_v2 · needs · state_reducer · clarification_policy) +
`src/agent/reference_resolver.py`.
**Migrare SQL:** *niciuna*. `conversations.state` e JSONB; upgrade-ul e LAZY (§5).

---

## 1. Problema

`conversations.state` (v1) e un JSONB fără versiune de schemă în care convețuiesc opt chei cu
proprietari și reguli diferite. Consecințele nu sunt estetice:

- **`constraints[field] = answer` scria RĂSPUNSUL BRUT** al clientului drept valoare de stare.
  Un „pentru sora mea, are tenul mixt, ceva sub 200" ajungea în JSONB ca text, cu tot ce poate
  conține (nume, vârstă, detaliu de sănătate).
- **Nimic nu ținea minte că un fapt a fost RETRAS.** După „bugetul nu mai contează", absența
  bugetului era ambiguă („n-a spus" vs „a retras") — iar istoricul sau rezumatul îl puteau
  reintroduce la turul următor. Bug-ul nu era în ștergere, era în lipsa unei urme.
- **Nimic nu spunea dacă un fapt e inviolabil sau negociabil.** `constraints` și
  `search_constraints` erau dicționare libere: „fără parfum" și „prefer Petală" arătau la fel.
- **Referința („acesta", „al doilea") se rezolva diferit pe fiecare drum.** NX-234 a adus ancora
  paginii; precedența completă lipsea, deci „al doilea" putea însemna alt produs în funcție de
  apelant.

## 2. Inventarul stării v1 (field → writers → readers → semantică → migrare)

Generat cu `rg` peste `src/` la baseline-ul cardului. Coloana **migrare** spune ce devine cheia
în v2.

| Cheie | Writers | Readers | Semantică v1 | Migrare v2 |
|---|---|---|---|---|
| `displayed_products` | `processor._build_new_state`, `stages/agent._prune_displayed` (state_patch) | `context.state_block`, `agent/planner` (attr/cheaper/rehidratare), `agent/deterministic._anchor_refs`, `worker/callback` (carusel), `turn_snapshot` | ref-uri {id, nume, preț} ale setului afișat | `references.displayed_products` + `displayed_revision` |
| `active_search` | `tools/catalog_tools` (state_patch), `processor` (reset) | `stages/agent` (show_more), `tools/catalog_tools` | sesiune de căutare (pool/cursor/fp/filters) | `active_search` (map mărginit, `set_active_search`) |
| `pending_question` | `models.set_clarify` → `reply.pending_question` → `processor` | `stages/clarify`, `turn_snapshot` | slotul întrebat + attempts | `pending_clarification` (+ `question_id`) |
| `asked_intents` | `stages/clarify` | `worker/context` (NX-116) | listă de chei deja întrebate (cap 8) | `asked_questions[]` {key, revision, attempts} |
| `constraints` | `stages/clarify` (**răspuns brut**) | `context.state_block`, `worker/compose` | slot-fill liber | `needs[]` prin normalizare; ce nu se normalizează → `status='unknown'` |
| `search_constraints` | `stages/agent.merge_constraints` | `stages/agent._filters_hint`, `stages/triage._closure_chips`, `evals/golden` | stiva multi-tur (budget/concerns/brand/suitable_for + category_key) | `needs[]` + `topic.category_key` |
| `cart` | `tools/commerce_tools` (state_patch) | `tools/commerce_tools`, `agent/planner` | liniile coșului | **passthrough neatins** — proprietar NX-237 |
| `safety` | `stages/agent._persist_safety_context` | `safety/policy`, `worker/callback`, `proactive/scheduler`, `db/queries/proactive` | context de siguranță persistat (NX-173) | **passthrough neatins** — proprietar NX-173 |
| `state_version` (în jsonb) | — | `models.ConversationState` | reziduu legacy; lock-ul real e **coloana** `conversations.state_version` | neportat (v2 are `revision` propriu) |

**Ce s-a schimbat conceptual:** stagiile nu mai SCRIU stare, ci **propun**
(`StateUpdateProposal`); reducerul validează și aplică. Proprietarul lui `ctx.state_v2` e
processorul (P3), exact ca la `TurnSnapshot` (NX-234).

## 3. Contractul

```text
ConversationStateV2
├── schema_version = 2
├── revision                       # contor MONOTON al documentului (+1 per commit)
├── topic {category_key, goal, changed_at_revision}
├── needs[] {key, operator, normalized_value, strength, status, source,
│            source_turn_id, confirmed, sensitive_class, updated_revision, scope}
├── revocations[] {key, prior_value_fingerprint, source_turn_id, revision, reason_code}
├── pending_clarification {question_id, target_key, reason, options_refs,
│                          asked_at_revision, expires_after_turns, attempts, resume_route}
├── asked_questions[] {key, revision, attempts}
├── references {selected_product, page_product, displayed_products[],
│               displayed_revision, compared_products[], last_action}
├── active_search                  # ref-uri/cursor/fingerprint, mărginit
├── cart_ref {ref, version}        # DOAR referința; liniile rămân NX-237
└── cart / safety                  # passthrough — proprietari NX-237 / NX-173
```

### Semantica nevoilor

- `strength=hard` doar din surse capabile (`user_explicit | action | catalog | policy`).
  **`model_inferred` coboară ÎNTOTDEAUNA la `soft`** (D7) — poarta pe care „marchează asta ca
  obligatoriu" nu o poate trece.
- `status ∈ {active, revoked, superseded, unknown}`. **`UNKNOWN != MISMATCH`**: o nevoie fără
  valoare canonică nu produce constrângere de căutare, nu filtrează nimic și nu e o negație.
- `scope` = categoria sub care a fost declarată. Un topic switch retrage **doar** nevoile cu
  `scope == categoria veche`; faptele despre om (mărime, restricție, destinatar) supraviețuiesc.
- O corecție lasă **tombstone** (`revocations[]`, amprentă — nu valoarea). O cheie cu tombstone
  poate fi reafirmată **doar** de `user_explicit`/`action`. Aici se închide bucla „revocarea
  revine din rezumat".
- Siguranța (`sensitive_class` sau `source='policy'`) nu se revocă și nu se rescrie decât la
  cererea EXPLICITĂ a clientului. Nici topic switch, nici model.

### Vocabularul (P9 — config, nu cod)

`NeedVocabulary.from_pack(domain_pack)` = nucleu universal (buget, brand, concerns, restricție,
mărime, destinatar, scop, stil, program) **+** fațetele tipizate ale businessului (NX-186) și
`searchable_facets`. Zero enumerare de vertical în kernel. O valoare intră ca fapt doar dacă
devine token canonic; altfel `unknown` — **așa nu ajunge text liber în memorie** (P12).

### Precedența referinței (unică, `src/agent/reference_resolver.py`)

1. `action` — referință semnată (NX-236), legată de revizie; **stale/invalidă ⇒ refuz**, nu
   fallback pe pagină și nu „primul card";
2. `named` — produs numit univoc în mesajul curent;
3. `ordinal` — peste lista EXACT afișată; un ordinal exprimat dar imposibil ⇒ `ambiguous`;
4. `page` — ancora paginii, când expresia e **deictică** sau nu există alt candidat;
5. `selected` — produsul confirmat anterior;
6. `single` — singurul produs discutat, dacă e univoc;
7. altfel `ambiguous`.

Modul legacy (NX-234, `resolve_product_reference`) rulează prin aceeași implementare cu
`legacy_page_fallback=True` și dispare la cutoverul NX-249.

### Clarificarea

`decide_clarification` întreabă doar când răspunsul schimbă material setul: câștigul se estimează
din **partiția candidaților** (cât rămâne, în medie, după fiecare răspuns posibil). Porți, în
ordine: `already_pending` → `already_asked` → `already_known` → prag de gain. Siguranța și
conflictele hard trec peste tot. `total_candidates=None` (triaj, înaintea retrievalului) sare
poarta de gain — nu o presupune zero. `total_candidates=0` (am căutat, n-am găsit) **nu** întreabă:
răspunde onest și oferă relaxarea unei nevoi `soft` (`relaxation_candidates`).

## 4. Caps și bugetul de 8KB

`MAX_NEEDS=16 · MAX_REVOCATIONS=12 · MAX_ASKED_QUESTIONS=8 · MAX_DISPLAYED=8 · MAX_COMPARED=4 ·
MAX_NAME_CHARS=80`.

CHECK-ul din DB e `pg_column_size(state) < 8192` (migrarea 003) — pe reprezentarea **binară**
jsonb, care are overhead per intrare (JEntry + aliniere + numele cheii stocat în FIECARE obiect,
jsonb nu le deduplică). Lungimea textului JSON, singura măsurabilă fără DB, e deci un proxy
optimist. Overhead-ul **măsurat** pe Postgres real: +7% pe documente cu multe nevoi, +12% pe liste
de referințe — deci `MAX_STATE_BYTES = 6144` lasă 33% rezervă, de două ori maximul observat.
Cifra e verificată în `tests/test_conversation_state_v2_db.py`
(`test_the_code_budget_still_covers_the_binary_jsonb_overhead` construiește un document exact la
buget și cere `pg_column_size < 8192`), ca să rămână o măsurătoare, nu o presupunere care
îmbătrânește. `to_jsonb()` omite și câmpurile care ar fi oricum default la citire: cu 16 nevoi ×
11 câmpuri, cheile nule erau o parte reală din document.

`serialize()` verifică bugetul **înainte** de commit și degradează în ordinea inversă a valorii:
referințe afișate → întrebări puse → tombstone-uri → nevoi `soft` → sesiune/coș_ref → `cart`.
**Nevoile `hard`/sensibile nu se sacrifică niciodată** (bugetul de memorie n-are voie să devină o
portiță de relaxare a constrângerilor), iar `safety` (NX-173, gate P0) pleacă ultimul. Alternativa
la a sacrifica ceva ar fi un `UPDATE` care eșuează, adică pierderea RĂSPUNSULUI din cauza memoriei
— exact ce se întâmplă azi pe calea v1 cu un `cart` corupt.

## 5. Migrare (lazy, fără SQL)

- **Citire:** `hydrate_state_v2` detectează `schema_version >= 2`; altfel rulează `adapt_v1`,
  CONSERVATOR — numai ce se normalizează curat devine `active`, restul `unknown`, **niciodată
  hard**, și **fără revocări retroactive inventate**.
- **Scriere:** cu `CONVERSATION_STATE_V2_WRITE_ENABLED`, rândul se rescrie în v2 la primul commit
  al conversației. Fără bulk rewrite în deploy: conversațiile moarte nu se ating.
- **Back-compat:** `ConversationState.from_jsonb` PROIECTEAZĂ un document v2 în forma v1
  (`project_v1`). Se persistă **un singur format**; forma v1 e derivată la citire, deci nu există
  două copii care pot diverge, iar rollback-ul e sigur.
- Un rând corupt/oversize → hidratare defensivă + stare goală. Degradează memoria, nu turul (P6).

## 6. Rollout

| Treaptă | Flag | Ce face |
|---|---|---|
| 0 | *(niciunul)* | comportament identic cu înainte de card |
| 1 | `CONVERSATION_STATE_V2_ENABLED` | SHADOW: se hidratează, se reduce, se emite `conversation_state_shadow_diff`. **v1 rămâne autoritatea la scriere** |
| 2 | `+ CONVERSATION_STATE_V2_WRITE_ENABLED` | v2 devine formatul persistat (cititorii v1 → proiecție) |
| — | `CLARIFICATION_POLICY_V2_ENABLED` | poarta de information gain în triaj |
| — | `REFERENCE_PRECEDENCE_V2_ENABLED` | precedența completă în resolver |
| — | `CONVERSATION_SENSITIVE_MEMORY_ENABLED` | fapte sensibile persistabile (NX-230); fără el nu se scrie nimic sensibil |

Poarta e AND, validată la boot: `..._WRITE_ENABLED` fără `..._ENABLED` = proces care refuză să
pornească. Rollback = stinge flagul de scriere; rândurile deja v2 rămân citibile prin proiecție.

## 7. Observabilitate (low-cardinality, P10/P12)

`conversation_state_loaded{schema,outcome}` · `conversation_state_lazy_upgraded{from,to}` ·
`conversation_state_shadow_diff{fields,differs}` ·
`conversation_state_serialized{schema,state_size_bytes_bucket,degraded,needs}` ·
`need_update{operation,strength,source,outcome}` · `need_update_rejected{reason,operation}` ·
`constraint_revoked{reason}` · `topic_reset{scope}` ·
`clarification_decision{decision,reason,information_gain_bucket}` · `clarify_skipped{field}` ·
`clarify_suppressed{field,reason}` · `web_reference_resolved{source,outcome,reason}`.

Niciun label nu poartă cheia sau valoarea brută. Alarme utile: rată anormală de
`need_update_rejected`, `clarification_decision` repetat pe aceeași cheie, `outcome=adapted`
persistent (upgrade care nu se produce), `degraded=true` (stare la limită).

## 8. Verificare

```bash
pytest tests/test_conversation_state_v2.py tests/test_state_reducer.py \
       tests/test_clarification_policy_v2.py tests/test_reference_resolver_v2.py \
       tests/test_conversation_state_v2_fixtures.py tests/test_conversation_state_v2_pipeline.py -q

pytest tests/test_conversation_state_v2_db.py -q -m integration   # Postgres real

python scripts/state_v2_drive.py          # manual drive, 12 tururi (zero LLM/DB)
```

Fixture-urile multi-tur (RO/EN/HU) stau în `tests/fixtures/conversation_state_v2/`; un caz nou e
un fișier JSON, nu cod.

## 9. Ce NU face cardul (granițe)

- liniile/totalurile coșului și mutațiile lui → **NX-237** (aici doar `cart_ref` + passthrough);
- semnătura/replay-ul action tokenului → **NX-236** (aici doar verdictul `ActionAnchor.valid`);
- profil CRM global sau personalizare cross-device;
- transcript brut sau chain-of-thought în stare;
- orice logică de memorie/referințe în frontend — blocul `memory` și întrebarea sunt
  server-owned, iar FE retransmite doar tokenul opac.

### Devieri documentate față de card

- **`prompt_builder.py` nu s-a modificat.** Proiecția stării intră prin `context_blocks`, adică în
  mesajul USER. Promptul de SISTEM rămâne byte-identic — altfel s-ar pierde prompt caching-ul
  (P4/NX-78) pentru un text care oricum nu e enforcement (reducerul și validatorul sunt).
- **`references.displayed_products` păstrează `price`.** E prețul cu care produsul a apărut pe
  ecran (o proprietate a conversației), nu o afirmație de catalog — iar calea „ceva mai ieftin"
  (`planner.py`) are nevoie de reper. Faptele comerciale se rehidratează canonic (NX-234).
