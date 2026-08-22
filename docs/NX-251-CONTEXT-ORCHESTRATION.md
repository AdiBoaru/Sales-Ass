# NX-251 — Context conversațional, autoritatea faptelor și un singur creier pe drumul sincron

**Status:** implementat, DARK (`TRIAGE_SYNC_SHADOW_ENABLED=false`) · **Baseline:** `origin/main@6ec0274`
**Cod:** `src/conversation/needs.py` (`corroborated_by`), `src/conversation/state_reducer.py`,
`src/agent/brain.py`, `src/worker/context.py`, `src/worker/stages/triage.py`,
`src/worker/stages/agent.py`, `src/worker/runner.py`, `src/worker/aftercare.py`
**Probă:** `pytest tests/test_context_journeys.py tests/test_context_orchestration.py
tests/test_need_corroboration.py -q`

---

## 1. Problema, exact

Direcția aprobată (D1) spune că mesajul ajunge la agentul principal **fără ca vreun model mic să-l
clasifice înainte**. NX-239 a livrat creierul unic, dar a rezolvat doar jumătatea de *scriere*:
control plane-ul demotează reply-ul triajului în semnal, deci nano nu mai e writer. **Apelul a
rămas.** Cu `SINGLE_BRAIN_ENABLED=true`, fluxul real era:

```
mesaj → nano (system ~86 linii + context_blocks + transcript) → brain (ACELEAȘI blocuri, încă o dată)
```

adică exact cascada „un model mic vede tot contextul înaintea creierului", cu contextul plătit de
două ori. În jurul ei, patru defecte care aveau în comun un lucru: **fiecare era invizibilă cât timp
te uitai la un singur tur.**

| # | Defect | De ce nu se vedea |
|---|---|---|
| 1 | Propunerile de state ale planului erau `model_inferred` **peste tot** | Cu triaj, `user_explicit` venea de la nano (`_filter_proposals`). Scoți triajul → **nicio nevoie nu mai poate fi `hard`** (D7 coboară `model_inferred` la `soft`), iar constrângerile inviolabile dispar fără niciun semnal |
| 2 | Modelul putea **REVOCA** o nevoie declarată de client | `set_need` era apărat (`hard_downgrade`); `revoke` nu verifica nimic. Relaxarea interzisă de D7, pe altă operație |
| 3 | Clarificarea pusă de brain nu se persista | Răspunsul scurt care urma repornea de la zero, iar `attempts` rămânea 0 → cu poarta de gain stinsă (defaultul), aceeași întrebare la infinit |
| 4 | Repair-ul rula fără evidence | `complete_schema` e în afara conversației cu tool results: i se cerea să citeze `evidence_ids` pe care nu le mai avea |

Plus: sub starea v2, `state_block` (proiecția v1 a constrângerilor) și `memory_block` (nevoile
canonice) trimiteau **aceleași fapte de două ori**, iar copia fără tărie e cea care invită la
relaxare.

## 2. Ce s-a schimbat

### 2.1 Sursa unui fapt o decide CODUL (`corroborated_by`)

Modelul nu-și poate alege sursa — ar fi D7 pe cuvântul lui. O declară codul, dintr-o singură
întrebare verificabilă: **valoarea asta apare în ce a scris clientul ACUM?**

- numeric → numărul a fost rostit (ambele lecturi ale separatorului: `1.500` = 1500 **și** 1,5);
- text → toți tokenii valorii canonice apar în mesaj, cu potrivire pe **prefix** peste 3 caractere
  (româna flexionează: „ten gras" ⊆ „am **tenul** gras"; un „g" nu coroborează „gras").

Coroborat ⇒ `user_explicit` (poate deveni `hard`, poate corecta un `hard`, poate reafirma o cheie
revocată). Necoroborat ⇒ `model_inferred` ⇒ `soft`. **Conservatoare prin construcție:** orice dubiu
dă `False`, iar consecința unui `False` e o nevoie mai slabă, niciodată una mai tare.

> **Limita, explicit:** coroborarea confirmă că valoarea a fost **rostită**, nu că modelul a
> interpretat-o corect. Un „200ml" citit ca buget rămâne o eroare a modelului — exact ca la
> extracția din triaj. Ce garantează e că **nimic ne-rostit nu devine fapt al clientului**.

### 2.2 Ce poate ȘTERGE modelul

`_handle_revoke` refuză (`unsupported_revoke`) o revocare din sursă necapabilă asupra unei nevoi
`user_explicit`/`hard`. Poarta e simetrică cu `hard_downgrade` de la `set_need`.

**Ce NU blochează**, și de ce contează: nevoile create de model se nasc `soft` + `model_inferred`
(D7), deci „nu vreau Sony" → „de fapt accept Sony" trece neatins. Se apără ce a **afirmat clientul**,
nu memoria în general.

Cazul rămas deschis, deliberat: „bugetul nu mai contează" după un buget declarat de client nu se
coroborează (mesajul nu conține „200"), deci revocarea se respinge. Alegem partea sigură a lui D7 și
o facem **măsurabilă** (`need_update_rejected{reason=unsupported_revoke}`). Dacă metrica arată că se
întâmplă des, ăla e argumentul pentru un card de detecție a retragerii explicite — pe date, nu pe
intuiție (D15).

### 2.3 O întrebare pusă poate fi reluată

`_persist_clarification` scrie în **ambele** reprezentări, fiindcă ambele au cititori:
`reply.pending_question` (v1 — persistat de processor, citit de `clarify_resume_stage` și de
marginea web pentru tokenul NX-236) și propunerea typed `set_pending_question` (v2 — `question_id`,
`attempts`). `attempts` crește când se reîntreabă același slot.

### 2.4 Repair-ul vede evidence

Digest mărginit (`MAX_REPAIR_EVIDENCE=24`): `evidence_id | tip | product_id | valoare`, doar
`current`, cu **trunchierea declarată** în prompt — un digest tăiat în tăcere l-ar face să creadă că
restul nu există și ar produce un al doilea plan invalid, din alt motiv.

### 2.5 Triajul iese de pe drumul sincron

`TRIAGE_SYNC_SHADOW_ENABLED` (cere `SINGLE_BRAIN_ENABLED`, **verificat la boot**):

| Aspect | Înainte | Sub flag |
|---|---|---|
| apel nano sincron | 1/tur | **0** |
| proprietarul lui `ctx.route` | `triage_stage` | `agent_stage` (P3: un singur writer, doar altul) |
| clasa de tur | din rută | din **obligațiile deterministe** (`compare` rămâne `COMPLEX`) |
| toolset | al rutei | **reuniunea** sales+order (fără triaj nu știm dacă turul e vânzare sau comandă; `check_order` are zidul lui de login) |
| clasificare | pe calea răspunsului | POST-tur, ca măsurătoare |

`classify_message` e **extras**, nu duplicat: shadow-ul rulează exact aceeași clasificare. Două
prompturi care „ar trebui să fie identice", întreținute separat, divergează — și atunci comparația
măsoară diferența dintre copii, nu dintre arhitecturi.

Shadow-ul compară **verdicte structurale** (ce a făcut turul: `answered` / `clarify` / `order` /
`handoff`), cu hartă de acord **strictă**: una indulgentă ar coborî rata de dezacord exact acolo
unde vrem s-o vedem, și ar valida promovarea din construcție.

### 2.6 Contextul nu se mai repetă

Sub v2, constrângerile aparțin exclusiv lui `memory_block`; `state_block` rămâne proprietarul
produselor afișate. `context_bytes{consumer, total, summary, profile, facts, state, memory, page}` —
**un** event per consumator, cu defalcare în properties (ca `turn_latency`/`llm_usage`), fără nimic
din conținut (P12).

## 3. Numărul de apeluri LLM pe tur (sub flags ON)

| Tip de tur | Apeluri sincrone | Note |
|---|---|---|
| salut / alias / cache / FAQ single-obligation | 0 (FAQ: 1 embed) | fast path exact |
| acțiune opacă | 0 | decizia e deja luată |
| recomandare | 2-3 requests, **un singur model** | rundă de tool + sinteză; chat completions e stateless, deci sinteza E un request nou — bounded de manifestul NX-241 |
| complex / mixt | ≤4 + ≤1 repair + ≤1 critic | runde 3, tool calls 6 |
| degradat | 0 | fallback determinist (P6) |
| **post-tur (aftercare)** | summarizer + profil + **shadow triaj** | bounded de `AFTERCARE_DEADLINE_MS`; shadow-ul e ULTIMUL — dacă bugetul se termină, se pierde comparația, nu rezumatul |

## 4. Rollout

| Treaptă | Flags | Ce obții |
|---|---|---|
| 0 | *(niciunul)* | comportamentul de azi, byte-identic |
| 1 | `SINGLE_BRAIN_ENABLED` | NX-239 dark (triajul încă rulează sincron) |
| 2 | `+ TRIAGE_SYNC_SHADOW_ENABLED` | nano iese de pe calea răspunsului; dezacordul se măsoară |
| 3 | `+ TRIAGE_SHADOW_ENABLED=false` | se oprește și măsurătoarea (după ce shadow-ul și-a spus cuvântul) |

**Rollback:** stinge flagul → traseul curent pentru trafic nou. Nicio migrare, niciun format nou
persistat, niciun default de producție schimbat.

**Ce NU deblochează cardul:** promovarea rămâne a lanțului existent (NX-246 pairwise+holdout →
NX-249 canary cu evidence packet). Cardul livrează mecanismul și măsurătoarea, nu decizia de ramp.

## 5. Ce a rămas în afara cardului (granițe)

- **Fast path determinist „factual exact"** (preț/stoc 0-LLM, D2) — are contract propriu + validator
  propriu, aparține fazelor Quality Overhaul.
- **Ruta ORDER/HANDOFF fără triaj:** brain-ul primește uneltele (reuniunea) și câmpul `handoff` din
  plan, dar nu există încă un gate determinist care să escaladeze pe baza lui. Ăsta e primul lucru pe
  care shadow-ul trebuie să-l arate (`triage_shadow{shadow_route=handoff, agrees=false}`).
- **Regenerarea rezumatului la corecție** — tombstone-ul o face deja inutilă în majoritatea cazurilor;
  dacă `state drift` arată altceva, e un card separat, pe date.
- **`confidence` numeric per valoare** — respins deliberat: `(source, confirmed, strength)` spune
  același lucru fără să ceară modelului să se auto-certifice.

## 6. Observabilitate

`state_proposal_source{op,source}` · `need_update_rejected{reason=unsupported_revoke}` ·
`clarify_asked{field,attempts,source=main_brain}` · `context_bytes{consumer,…}` ·
`triage_deferred{reason}` · `route_defaulted{reason}` · `triage_shadow{shadow_route,outcome,agrees,confidence}`.

Toate low-cardinality, vocabular închis, zero conținut de client.

## 7. Verificare

```bash
pytest tests/test_need_corroboration.py tests/test_context_orchestration.py \
       tests/test_context_journeys.py tests/test_state_reducer.py -q
```

Journey-urile rulează pe `run_pipeline` cu stagiile REALE și cu starea cărată între tururi prin
**același reducer** ca în processor — un fake de state ar testa fake-ul.
