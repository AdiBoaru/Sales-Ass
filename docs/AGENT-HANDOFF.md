# Agent handoff — build (Claude) → verify (Codex)

Protocol pentru doi agenți care lucrează pe același repo **fără să se calce**. Regula de aur:
**niciodată doi agenți în același working directory.** Fiecare are worktree-ul lui; handoff-ul
se face prin **branch-ul pushed / PR**, nu prin fișiere partajate.

De ce: doi agenți în același tree se corup reciproc — `git stash`/`checkout` surpriză,
commit-uri parțiale, branch-uri suprascrise. (S-a întâmplat: o felie pierdută în stash, un
branch deturnat → PR merged cu alt conținut.)

---

## Roluri

| | **Claude = BUILD** | **Codex = VERIFY** |
|---|---|---|
| Scrie cod | DA | **NU** (read-only pe branch-ul lui Claude) |
| Working dir | al lui | **al lui, separat** (worktree/clone) |
| Output | branch + PR | findings pe PR (review comments) sau „approve" |
| Merge | nu | **nu** — doar userul dă merge |

---

## Fluxul unui task

### 1. Claude (build)
1. Branch NOU din `origin/main` (nu stacked). Implementează STRICT ce cere cardul.
2. Gate local verde: `ruff check .` **și** `ruff format --check .` **și** `pytest -x -q`.
3. Commit **des** (nu lăsa muncă necomisă — supraviețuiește dacă altcineva atinge tree-ul).
4. `git push` + `gh pr create`. În corpul PR-ului, pune:
   - **Task ID** + link card;
   - **DoD checklist** (bifabil);
   - **„De verificat"**: 2-4 puncte concrete pe care Codex să le atace (edge cases, izolare,
     grounding, PII);
   - **Fișiere atinse** (ca Codex să știe granițele).
5. Anunță userul: `PR #N gata de verify (branch feat/...)`.

### 2. Codex (verify) — READ-ONLY, IZOLAT
Nu edita niciodată working dir-ul lui Claude. Lucrează pe o copie izolată:
```bash
git fetch origin
git worktree add ../verify-N origin/feat/NX-XXX-nume   # copie izolată a branch-ului
cd ../verify-N
```
Apoi:
1. **Gate**: `ruff check .`, `ruff format --check .`, `pytest -x -q` — rulează, nu presupune.
2. **DoD linie cu linie**: fiecare punct din „Definition of Done" chiar se ține? (nu doar
   „testele trec"). Bifează sau respinge cu motiv.
3. **Drive end-to-end**: rulează efectiv scriptul/fluxul schimbat și observă comportamentul
   real (nu doar testele). Ex.: `PYTHONPATH=. python scripts/<x>.py ...`.
4. **Adversarial**: caută UN input/edge care rupe schimbarea. Verifică explicit:
   - izolare tenant (`WHERE business_id = $1` pe orice query nou);
   - PII/secrete în output/loguri (zero telefon, zero cheie);
   - scope creep (a atins fișiere din afara cardului?);
   - „niciodată tăcere" (P6) unde e cazul.
5. **Raport pe PR** (nu pe tree-ul lui Claude):
   ```bash
   gh pr review N --comment --body "..."      # findings
   gh pr review N --approve                    # dacă e curat
   ```
   Findings ordonate: **CONFIRMED** (cu pași de reproducere) înaintea celor **PLAUSIBLE**.
   Fiecare finding: fișier:linie + ce se rupe + input concret.
6. Curăță worktree-ul: `git worktree remove ../verify-N`.
7. **NU** face push pe branch-ul lui Claude. **NU** merge.

### 3. Claude (address)
- Citește findings. Dacă PR-ul e `OPEN`, fixează pe **același branch** + push (verifică întâi
  `gh pr view N --json state` = OPEN). Dacă e deja merged, branch nou + PR de follow-up.
- La fix major, cere re-verify.

### 4. User (gate)
- Merge DOAR după: Codex `approve` **și** CI verde. Nu forța fără verify.

---

## Instrucțiune permanentă de dat lui Codex (paste o dată)

> Ești **verifier**, nu implementer, pe repo-ul Sales-Ass. Nu scrii cod de producție și nu
> editezi niciodată working dir-ul principal. Pentru fiecare `PR #N` pe care ți-l dau:
> lucrează izolat (`git worktree add ../verify-N origin/<branch>`), rulează gate-ul complet
> (`ruff check .` + `ruff format --check .` + `pytest -x -q`), verifică DoD-ul linie cu linie,
> rulează efectiv fluxul schimbat, fă o trecere adversarială (izolare `business_id`, PII, scope
> creep, P6), și raportează pe PR cu `gh pr review` — findings CONFIRMED (cu reproducere)
> înaintea celor PLAUSIBLE, sau `--approve` dacă e curat. Nu face push pe branch-ul meu, nu da
> merge, curăță worktree-ul la final.

---

## De ce funcționează

- **Serializat pe fișiere, paralel pe timp**: Claude scrie, Codex citește un snapshot imutabil
  (branch pushed). Nu există fereastră în care să se calce.
- **Verify ≠ re-review de CI**: CI rulează testele; Codex verifică *intenția* (DoD real, edge
  cases, drive end-to-end) — prinde ce trece de pytest dar pică în producție.
- **Un singur scriitor per branch**: elimină branch-hijacking-ul.
