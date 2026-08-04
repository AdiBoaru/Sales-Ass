# Pending Verification — registru muncă neverificată (fără PR încă)

> **De ce există:** lucrăm local cât timp Codex e fără credite (Codex face verificarea
> prin PR). **NU deschidem PR-uri** acum — un PR ar sta degeaba în așteptare de verificare.
> Aici ține evidența a tot ce e construit-dar-neverificat, ca la revenirea creditelor
> Codex să dăm push/PR în lot și să verificăm ordonat.
>
> **Regula:** fiecare task terminat/în lucru se adaugă aici — branch, commit-uri,
> ce am testat local, ce rămâne de verificat cu Codex.
>
> Legendă status: `🔨 în lucru` · `✅ gata local (așteaptă Codex)` · `🔍 în verificare Codex` · `🟢 verificat/merged`

---

## Coadă de verificat (când revin creditele Codex)

### NX-176a — clarify conversațional pentru cereri sub-specificate (GENERAL, orice vertical)
- **Branch:** `feat/NX-176a-clarify-first` (worktree: `D:/Work/nx-176a`; **stacked pe NX-179**, DOAR local, nepushuit)
- **Status:** ✅ gata local
- **Card:** [tasks/NX-176a.md](../tasks/NX-176a.md)
- **Commit-uri:**
  - `9b2d97c` feat(NX-176a): clarify conversațional pentru cereri sub-specificate (general, orice vertical) *(local)*
  - *(în spate: cele 2 commit-uri NX-179 — la rebază pe main după merge NX-179, ele cad)*
- **Ce face:** cerere sub-specificată („vreau un laptop", „o cremă", „fă-mi o rutină") → nano întreabă CONVERSAȚIONAL (text-first, chips secundare), nu aruncă produse. DOAR prompt nano (model+context, vertical-agnostic prin categorii+concerns DomainPack).
- **Reparat în timpul dezvoltării:** regresie **P0 siguranță** — cererea cu sarcină devenea clarify și sărea gate-ul NX-173; adăugat EXCEPȚIE DE SIGURANȚĂ (sarcină/alăptare/afecțiune → mereu sales). Prins de `sc_safety` din audit.
- **Testat local:** 169 teste verzi (triage+clarify+golden+render); ruff curat; audit `/web/chat` live: rutină→clarify OK, sarcină→sales+farmacist (0 findings), fără over-clarify pe „ce șampon aveți?"/discovery.
- **De verificat Codex:** calibrarea pragului clarify (over/under-clarify pe verticale reale), robustețea excepției de siguranță, că golden n-are regresii mascate.
- **⚠️ Atenție la merge:** rebază pe main DUPĂ NX-179 (are dependență `_web_chips` + tweak-ul de suggestions din NX-179).
- **Rămas:** **NX-176b** = constructorul de regimen ordonat (pași × produs real per pas din DomainPack) — neînceput.

### NX-179 — refocus 100% web widget + audit conversațional web
- **Branch:** `feat/NX-179-web-focus` (worktree: `D:/Work/nx-179`; `28082ef` pushed la PR #231 DRAFT, `db4371b` DOAR local)
- **Status:** ✅ gata local
- **Commit-uri:**
  - `28082ef` feat(NX-179): refocus 100% pe WEB WIDGET + audit conversațional pe calea web reală *(pushed)*
  - `db4371b` fix(NX-179): chips web = etichete scurte, nu întrebări (audit conversațional) *(local, nepushuit)*
- **Testat local:** audit `/web/chat` real rulat (9 scenarii); 58 teste triage + 27 render verzi; ruff check+format curate
- **Ce am reparat (din audit):** chips lungi pe clarify (gardă `_web_chips` în render + prompt nano); strâns checkerele audit (chip-len, routine, compare)
- **De verificat Codex:** gardă `_web_chips` (prag 40c + cap 4), promptul nano de suggestions, checkerele din `web_audit.py`
- **Findings rămase (NU pe branch-ul ăsta):** `faq_retur` → NX-175 (branch propriu); `routine` accesorii → NX-176 (task separat, neînceput)

### NX-175 — FAQ motor de selecție corect (top-k + rerank + marjă → clarify)
- **Branch:** `feat/NX-175-faq-rerank` (pushed, PR #232 DRAFT)
- **Status:** ✅ gata local
- **Commit-uri:**
  - `443030f` feat(NX-175): FAQ — motor de selecție corect (top-k + rerank calificatori + marjă → clarify)
  - `646a797` fix(NX-175): fix_faq_data folosește admin_conn (bot_runtime n-are scriere pe faqs)
- **Testat local:** CI verde
- **De verificat Codex:** `src/knowledge/faq_rerank.py` — praguri rerank + marjă→clarify

### NX-177 — igienă teste (pytest -x -q verde pe DB live, 14 → 0)
- **Branch:** `feat/NX-177-test-hygiene` (pushed, PR #230 DRAFT)
- **Status:** ✅ gata local
- **Commit-uri:**
  - `4312c61` fix(NX-177): igienă teste — pytest -x -q verde pe DB live (14 → 0)
- **Testat local:** CI verde
- **De verificat Codex:** cele 14 teste reparate erau contracte stale, nu bug-uri de mediu

---

## Note
- Branch stale `feat/NX-164-demand-queries` = DEJA merged (PR #212); commit-urile locale sunt pre-squash. De curățat, nu de verificat.
- La revenirea creditelor Codex: trece PR-urile din DRAFT în ready în ordinea de mai sus.
