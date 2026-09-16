# NX-239 — Agent principal unic: control plane determinist + `AnswerPlanV2`

**Status: implementat DARK.** `SINGLE_BRAIN_ENABLED=false` (default) = pipeline-ul de azi,
byte-identic. Producția rămâne OFF până la GO-ul pairwise+holdout **NX-246** — codul dark NU e
echivalent cu aprobare de producție.

## Arhitectura (flag ON)

```
TurnSnapshot durabil (NX-234)
  → pipeline-ul liniar de azi, dar cu POARTĂ pe fiecare early-exit:
      control_plane.gate_early_exit(ctx, stage)
        • FastPathDecision{path, complete, covered, uncovered, reason, version}
        • complet  → early-exit ca azi (greeting pur, alias exact, FAQ exact pe o singură
          obligație, action kernel, handoff/order gates, gates/fallback)
        • incomplet → reply-ul devine BrainSignal (ctx.brain_signals), pipeline-ul continuă
  → MainBrain (src/agent/brain.py) — DOAR dacă fast path-ul nu acoperă TOATE obligațiile
      • tool calls prin ToolRun; search_products EXCLUSIV prin portul NX-238
        (selector.select_provider: NOT-READY/NO-GO → CurrentLiveRetrievalAdapter; brain-ul
        nu știe providerul)
      • răspunsul FINAL al ACELEIAȘI bucle = AnswerPlanV2 structurat (llm.run_tool_loop_structured)
  → validatori deterministici: validate_answer_plan_v2 (REFOLOSEȘTE validatorul NX-211 prin
    to_v1()) + validate_revised_draft (proza contra retrieval) + poarta de clarificare NX-235
  → critic selectiv codes-only (reuse run_semantic_critic; doar pe triggeri)
  → cel mult UN repair bounded al aceluiași brain → fallback determinist non-gol (P6)
  → reply (NX-240 va înlocui render-ul cu proiecția ViewModel)
```

## Contracte

- **Obligațiile turului** sunt SEMNALE extrase din cod (`brain_models.extract_obligations`):
  semne de întrebare, clauze mixte, acțiune opacă, clarificare pending, salut, context de
  siguranță. Modelul le declară în plan, dar validatorul le confruntă cu ce a extras codul —
  o obligație cerută și neacoperită = `obligation_uncovered` → repair → fallback.
- **`AnswerPlanV2`** (`src/agent/answer_plan.py`, `schema_version=2`, `extra=forbid`, caps):
  `intent_summary`, `obligations[]`, `direct_answer`, `claims[]` + `recommendations[]` (fiecare
  motiv cu evidence + need refs), `comparison` (refs/axes/cells sourced, fără winner), 
  `constraints_applied/unknowns/relaxations`, `clarification?` UNICĂ (structural), `no_results?`
  cu taxonomia închisă (`no_match | insufficient_data | dependency_unavailable`),
  `state_update_proposals[]` (decide reducerul NX-235, sursă `model_inferred`),
  `action_intents[]` (doar registrul NX-236), `disclosures[]`, `style_signals` limitate.
  Fără CoT, fără payload brut de tool, fără instrucțiuni către frontend, PII-guard la parsare.
- **Reguli V2 în validator** (nu în prompt): `hard_relaxation` (o cheie hard în relaxations),
  `revoked_need_used` (nevoie revocată reînviată), `unknown_action_intent`,
  `missing_direct_answer` (plan fără nimic pentru client).
- **`BrainInput`** (`worker/context.build_brain_input`): mesaj + obligații + transcript BUGETAT +
  blocuri de context (PII-redactate) + semnale demote-uite + proiecțiile de nevoi
  (active/hard/revocate). Fără conexiune DB, fără istoric nelimitat, fără fapte de frontend.
- **`evidence_id` se ARATĂ, nu se ghicește.** Rândurile de evidence se atașează la REZULTATUL
  fiecărui tool care aduce produse (`_PortedExecute.__call__` → `_evidence_block`, plafon
  `MAX_TOOL_EVIDENCE`), nu doar în promptul de repair. Construcția e a ACELUIAȘI
  `build_answer_plan_context` care validează, deci id-urile arătate sunt prin construcție cele
  acceptate. Se listează doar produsele NOI din apel: restul sunt deja în conversație, iar
  relistarea lor ar crește cu fiecare rundă exact partea plătită de fiecare dată.

  **De ce e un contract, nu o optimizare de prompt** (defect măsurat în producție pe `sole-ro`,
  2026-09-16): registrul se construia abia DUPĂ bucla de tool-calling, deci la momentul în care i
  se cerea planul modelul nu văzuse niciun id, deși instrucțiunile îi cer să citeze „evidence_ids
  din evidence-ul serverului". Le inventa (`search-1`, `search:<product_id>`) — **6 din 6 id-uri
  emise la primul apel, 100%** — iar validatorul respingea planul cu `unknown_evidence`. Fiecare
  tur cu produse plătea obligatoriu un repair (3 runde de model, 32-82 s e2e), iar când și acela
  rata, clientul primea fallback-ul determinist în locul recomandării scrise de model. Calea v1 nu
  avea defectul (`_plan_prompt` pune evidence-ul în promptul de la primul apel); creierul unic l-a
  introdus, iar flagul l-a făcut vizibil.
- **Dovedirea produsului e pe APARTENENȚĂ, nu pe tipul rândului** (`validate_answer_plan`): orice
  rând de evidence al produsului îi dovedește existența, fiindcă rândurile sunt construite de
  server din catalogul viu, nu afirmate de model. A cere EXACT `kind == "identity"` era un ritual
  de formă pe care modelul nu-l putea deduce — nici promptul, nici codul de eroare nu-l numeau —
  iar `product:<id>:identity` are ca `value` chiar product_id-ul, deci nu susține nimic în plus
  față de `:price` sau `:url`. În producție, reparația cita dovezile RELEVANTE (prețul și linkul
  produsului recomandat) și pica cu `missing_product_evidence` după ce citase corect. Varianta se
  dovedește la fel, dar strict: doar un rând LEGAT de exact varianta aleasă. Poarta rămâne închisă
  pe id inventat, pe rând al ALTUI produs (care acum nu mai dovedește nimic, indiferent de tip) și
  pe variantă numită fără dovadă a ei.

## Cine mai scrie ce (single writer)

- **triage** = clasificator + extractor de sloturi. Reply-urile lui `simple`/`clarify` sunt
  writer LLM concurent → control plane le demotează ÎNTOTDEAUNA; sub flag, rutele
  `simple`/`clarify` sunt servite de brain (agent_stage le acceptă doar cu flag ON).
- **faq/cache/alias** = conținut canonic, fast path DOAR pe mesaj cu o singură obligație
  (mesajul mixt merge la brain cu răspunsul FAQ ca semnal). Context de siguranță activ → brain.
- **greeting** = fast path exact prin construcție (`is_greeting` e match exact pe tot mesajul).
- **gates/handoff/action_kernel/clarify_resume/fallback** = gates de corectitudine, finalizează.
- **ORDER** rămâne pe calea existentă (order gate + zidul de login = fast path determinist).

## Versionare + observabilitate

`main_brain_call`/`answer_plan_validation` cară `prompt_version` (`BRAIN_PROMPT_VERSION`),
`prompt_hash`, `tool_schema_hash`, `plan_schema`, `model` ca trace attrs (nu labels).
Modelul de runtime vine din settings și se schimbă NUMAI prin eval blind (D15/NX-246).
Evenimente low-cardinality: `control_plane_decision{path,reason,complete}`,
`turn_obligations{count_bucket,covered,missing}`, `main_brain_call{phase,outcome}`,
`main_brain_tool_rounds_bucket`, `answer_plan_validation{outcome,reason}`,
`conversation_quality{check,outcome}`, `clarification_decision{reason,gain_bucket}`,
`no_results{reason_class}`, `constraint_handling{strength,outcome}`,
`critic_triggered{reason,outcome}`, `repair{outcome}`, `retrieval_gate{decision,blocking_code}`.

## Limitări cunoscute (dark, deblocate de NX-240/241)

- Render-ul din plan e text determinist (`direct_answer` + no-results onest + disclosures +
  clarificare); proiecția ViewModel/grounding-ul final = NX-240.
- Un fapt FAQ cu cifre (ex. prețul livrării) nu are încă evidence de retrieval → validatorul de
  proză îl respinge corect; groundarea evidence-ului de cunoștințe vine cu NX-240.
- Sesiunile de căutare („mai arată-mi") își păstrează calea deterministă existentă.
- Reply-urile brain sunt `cacheable=False` în v1 (specifice obligațiilor/nevoilor turului).

## Manual drive + rollout

- `python scripts/sim/single_brain_drive.py` — stub, $0, offline (12 scenarii; capturi PII-safe
  în `reports/nx239/drive.json`).
- `python scripts/sim/single_brain_drive.py --live` — provider real; **o rulează Adi** (credite).
- Rollout: dark → shadow → NX-246 preînregistrează pairwise+holdout → DOAR verdict GO explicit
  permite canary (stable pe business+conversation, pipeline version capturat la accept).
  Rollback: `SINGLE_BRAIN_ENABLED=false` → traseul curent pentru trafic nou. Orice hard
  violation / factual invention / blank P6 = kill switch; nu se „repară" în frontend.
