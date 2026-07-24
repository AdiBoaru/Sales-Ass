# NX-187 — Match Gate (shadow, post-retrieval) + recall vs scan exhaustiv

**Faza P2 (Selection Correctness).** Owner: Claude (build) / Codex (verify). Depinde de NX-185 (QuerySpec
via NX-208) + NX-186 (facets tipizate). Consumat de NX-209 (search tool gras). **ZERO enforcement**
(acela e NX-188, înghețat până la NX-210).

## Ce livrează
`src/agent/match_gate.py` — PUR, testabil pe dict. Pentru fiecare **produs × constrângere** un verdict
tri-state **MATCH | MISMATCH | UNKNOWN** (enum canonic UNIC, D7: UNKNOWN ≠ MISMATCH), apoi clasare
într-un `MatchSet` cu **mulțimi DISJUNCTE** și precedență strictă:

1. `rejected`    — ≥1 hard MISMATCH (datele contrazic o constrângere dură);
2. `alternative` — zero hard MISMATCH, dar ≥1 hard UNKNOWN (nu putem confirma → clarificare/disclosure);
3. `exact`       — toate hard constraints sunt MATCH.

**Soft constraints influențează DOAR scorul (`soft_penalty`), NU apartenența** — un produs all-hard-MATCH
cu un soft mismatch rămâne `exact`, doar rang penalizat. Verdictul per candidat (`constraint_results`) se
**PĂSTREAZĂ** — NX-209 îl consumă ca să spună „nu pot confirma X la produsul Y". Tipul/operatorii vin din
registrul tipizat (NX-186), verdictul nu se ghicește. Post-retrieval, in-memory (SQL tri-state = NX-189).

## Recall vs scan exhaustiv (`scripts/match_recall_scan.py`)
`MAX_SEARCH_POOL=24` poate exclude un produs care satisface toate hard constraints → un gate care judecă
DOAR pool-ul ar declara fals „zero exact". Scriptul rulează hard constraints (qrels NX-208) pe adevărul
NX-202 și compară cu pool-ul real de retrieval. „Potriviri reale" = produse **judecate relevante** care
ȘI satisfac hard constraints (nu „orice respectă bugetul" — un filtru pe tot catalogul umflă cu preț-ok
irelevante).

**Rezultat pilot (6 query-uri cu hard constraints):** `relevant_exact` = 12, **ratate de pool = 8 →
pool_recall = 0,333**. Cele mai mari misses: `antilucire-rezistent-caldura` (3/3 ratate) și
`fara-parfum-rutina-fata` (4/4) — exact query-urile pe care NX-208 le-a marcat retrieval-bound.
`facet_miss` = {price: 2, fragrance_free: 1}. **Concluzie pentru NX-188:** pool-ul de 24 ascunde 2/3 din
potrivirile reale — un Match Gate doar-pe-pool ar rata masiv; recall-ul cere pool mai mare / search mai bun
(NX-209), nu enforcement pe pool-ul actual. Raport: `reports/match-recall-scan-compound.json`.

## Shadow + kill-switch
`match_gate_shadow_enabled` (default **OFF**). ON → planner-ul (post-retrieval, `planner.py:_match_gate_shadow`)
construiește constrângerile din query (contractul NX-208) + registrul tipizat, clasează candidații, scrie
`ctx.match_set` (owner unic: planner) și emite telemetrie FĂRĂ PII: `match_gate_shadow` (numere: candidați/
exact/alternative/rejected/hard) + `match_gate_outcome` (per fațetă hard: cheie canonică + status). **ZERO
schimbare de răspuns** — nu atinge `ctx.reply`. Best-effort (orice eroare înghițită, P6). OFF → byte-identic.

## Note
- Evaluatorul din `facet_coverage.py` (NX-186) era un preview de MĂSURARE; `match_gate.py` e evaluatorul
  canonic de runtime (agent layer). Direcția stack-ului (186 < 187) nu permite import invers → duplicare minoră, intenționată.
- Reconciliere chei: QuerySpec emite `concern`/`key_ingredient` (singular); registrul are plural →
  `_FACET_KEY_ALIASES` le leagă.

## Out of scope (per card)
Enforcement + alternatives UX (NX-188, înghețat) · typed SQL tri-state (NX-189) · folosirea de agentul nou (NX-210).
