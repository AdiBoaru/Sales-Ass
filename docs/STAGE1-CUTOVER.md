# NX-249 — cutover: închiderea rutei v1

Etapa 7. Singura ireversibilă ieftin: după ce ruta publică se închide, un client cu un turn în zbor
rămâne fără răspuns, iar P6 („niciodată tăcere") nu se mai poate respecta retroactiv.

Runbookul de canary: [`STAGE1-CANARY-RUNBOOK.md`](STAGE1-CANARY-RUNBOOK.md).

---

## 1. Ce înseamnă „v1 in-flight" — criteriu structural, nu euristică pe timp

Requestul HTTP al unui turn v1 e demult terminat când te uiți. Deci nu se poate întreba „mai vine
trafic pe `/web/chat`?" și nu se poate aștepta „încă 5 minute, probabil s-a terminat".

Criteriul e structural: **controllerul capturează un `release_track` la FIECARE accept v2; acceptul
sincron v1 nu capturează niciodată.** Deci un rând `accepted|running` FĂRĂ captură e, prin
construcție, ori un turn v1, ori unul acceptat înainte ca controllerul să fie pornit. Amândouă
trebuie să blocheze închiderea.

Consecință deliberată: pe un sistem unde controllerul tocmai a fost aprins, checkerul refuză până
când turele vechi se termină. Nu e un fals pozitiv — e chiar garanția.

---

## 2. Checkerul

```bash
python scripts/cutover_check.py --business-id <uuid> --out reports/nx249/cutover.json
```

Exit: `0` se poate închide · `1` NU (există trafic) · `2` nu se poate stabili (fără policy).

Patru condiții, toate necesare:

| # | Condiție | De ce |
|---|---|---|
| 1 | policy-ul e la etapa ≥6 | nu se sare de la 20% la închidere |
| 2 | zero ture active fără captură | v1 in-flight (§1) |
| 3 | zero ture active pe `champion` | drenarea controlului nu s-a terminat |
| 4 | soak ≥336h de la aplicarea policy-ului | cerința etapei 6 (14 zile) |

Un `READY` **nu închide nimic**: e o constatare. Închiderea e un policy nou cu `mode: "closed"`,
`stage: 7`, care trece prin același `apply` cu evidence packet și aprobare explicită.

---

## 3. Secvența completă

```bash
# 1. confirmă că etapa 6 e stabilă și drenată
python scripts/cutover_check.py --business-id <uuid>

# 2. evidence packet pe fereastra de soak
python scripts/canary_report.py --business-id <uuid> --window 14d \
  --slo ... --quality ... --e2e ... --deploy ... --feedback ... \
  --out reports/nx249/packet-stage7.json

# 3. drill de rollback recent (nu mai vechi de o săptămână)
python scripts/rollback_drill.py --business-id <uuid> --dry-run

# 4. dry-run al închiderii
python scripts/release_control.py apply --policy policies/close-v1.json \
  --expected-revision <curentă> --actor adi --reason "cutover NX-249" \
  --evidence reports/nx249/packet-stage7.json

# 5. APROBAREA EXPLICITĂ A USERULUI, apoi:
python scripts/release_control.py apply --policy policies/close-v1.json ... --confirm
```

`policies/close-v1.json` diferă de policy-ul etapei 6 prin exact două câmpuri:

```json
{ "mode": "closed", "stage": 7 }
```

La `mode: "closed"`, allowlistul nu mai filtrează: dacă ar filtra, tenanții din afara lui ar rămâne
pe o rută care tocmai s-a închis.

---

## 4. Ce se întâmplă cu codul v1

**Nu se șterge la cutover.** Ordinea e:

1. **etapa 7 aplicată** — `mode: closed`, toate conversațiile noi merg pe candidate;
2. **fereastra de rollback rămâne deschisă** cel puțin un release suplimentar, sau până când userul
   o închide explicit. În ea, ruta v1 rămâne montată și funcțională: dacă e nevoie de rollback,
   `force_control` trebuie să aibă unde să întoarcă traficul;
3. **abia după** — retragerea rutei publice (`src/web/app.py`), marcarea
   [`FRONTEND-CONTRACT-IZI.md`](FRONTEND-CONTRACT-IZI.md) drept legacy, și scoaterea fixture-urilor
   v1. Contractul NU se rescrie: se marchează.
4. **cleanupul distructiv** (ștergerea codului v1, a randorului, a validatorului) cere review
   separat. Nu e parte din NX-249.

Cardul e explicit: „Păstrează code/read compatibility un release suplimentar sau până când userul
închide rollback window".

---

## 5. Ce NU se atinge niciodată

- ledgerul `web_turns`, receipts-urile de comerț, feedbackul — se păstrează integral;
- schema rămâne expandată: **zero down migration, zero ștergere de rânduri**;
- turele completate nu se rerulează și nu se rescriu;
- contractul v1 (`FRONTEND-CONTRACT-IZI.md`) nu se modifică in-place — se marchează legacy.

---

## 6. Dacă checkerul refuză

| Motiv raportat | Ce faci |
|---|---|
| `N ture active FĂRĂ captură (v1 in-flight)` | așteaptă drenarea; verifică sweeperul NX-233 |
| `N ture active pe control` | idem — cohortul champion nu s-a golit |
| `policy-ul e la etapa N; închiderea cere etapa 6` | promovează etapele lipsă, cu porțile lor |
| `soak Xh < 336h` | așteaptă; nu se scurtează fără excepție de risc aprobată de user |
| `nu există policy în vigoare` | controllerul nu e pornit sau storeul e jos — repară întâi asta |
