# NX-249 — ritualul de calitate în producție

Bucla care transformă feedbackul real, eșecurile și evaluările de journey în **regresii
reproductibile** — nu în tuning online și nu în modificări intuitive de prompt.

Regula care ține tot ritualul în picioare: **nimic din producție nu schimbă direct promptul,
modelul sau rankerul.** Un semnal devine un caz de test; cazul intră în corpus; fixul e un candidate
nou care trece toate porțile. Altfel bucla ar fi antrenament online pe feedback de utilizator,
adică exact ce interzice cardul.

---

## 1. Zilnic — automat

Fără om. Produce artefacte, nu alarme pe email.

```bash
python scripts/slo_report.py --business-id <uuid> --window 1h --out reports/slo/hourly.json
python scripts/feedback_report.py --business-id <uuid> --window 7d --out reports/nx246/feedback.json
python scripts/canary_report.py --business-id <uuid> --window 24h --out reports/nx249/daily.json
```

Ce se urmărește:

| Semnal | Sursă | Ce înseamnă când se mișcă |
|---|---|---|
| SLO/burn/completeness pe ambele cohorturi | NX-246 `slo_policy.v1` | promisiunea de disponibilitate |
| failures / reclaims / replays / P6 / grounding | ledger + `release_gate_total` | sănătatea execuției |
| receipts de acțiuni | NX-237 | mutațiile nu se dublează și nu se pierd |
| rate de feedback cu interval Wilson | NX-246 felia 2 | percepția, cu `n` publicat |
| drift catalog/index/prompt/model/config vs manifest | NX-248 | „ce rulează" ≠ „ce am aprobat" |
| top cohorturi degradate | `canary_report` | unde să te uiți, fără să imprimi mesaje |

**Nu se imprimă niciun mesaj de client în artefacte.** Rapoartele numără; nu citesc conversații.

---

## 2. Săptămânal — uman, cu doi evaluatori

### 2.1 Selecție stratificată

Din turele SAFE (trecute prin politica de privacy NX-230/246 și prin autorizare), se aleg:

- feedback negativ;
- `no_result` (căutări fără rezultat);
- clarificare repetată (semnul unei bucle);
- ture cu acțiune / coș;
- ture lente sau atinse de deadline;
- **și un eșantion de control POZITIV** — fără el, revizia vede doar eșecuri și calibrează greșit.

### 2.2 Clasificare oarbă

Doi evaluatori, fără să știe care variantă e candidate. Cod de motiv din taxonomia închisă +
rubricile NX-246. Dezacordul se **adjudecă**, nu se mediază: o pereche fără adjudecare nu intră în
scor (`quality_gate` o impune).

### 2.3 Maximum TREI cauze

Se aleg cel mult trei cauze, după impact × reproductibilitate. Nu zece schimbări simultane — cu
zece, nu se poate atribui nicio îmbunătățire și nicio regresie.

---

## 3. Lunar — revizuirea instrumentului

- acoperirea taxonomiei (apar motive care nu încap în vocabular?);
- acordul între evaluatori (dacă scade, rubrica e ambiguă, nu oamenii);
- leakage și bias de selecție (holdoutul e încă sigilat? eșantionul e încă stratificat?);
- pragurile: se poate ridica un SLO? bugetul de cost mai e realist?;
- prospețimea drill-urilor RPO/RTO (NX-248) și a drill-ului de rollback (NX-249).

**Schimbarea de policy se face ÎNAINTE de candidate, printr-un PR aprobat.** Nu se „mută poarta"
după ce s-au văzut rezultatele — un prag mutat după cifre nu e un prag, e o justificare.

---

## 4. De la semnal la regresie — drumul obligatoriu

```
semnal în producție
    ↓  (redactat: fără PII, fără ID-uri, fără transcript brut)
testcase în corpusul de DEZVOLTARE (tests/golden/web_journeys sau qrels NX-203)
    ↓  holdoutul rămâne SIGILAT — nu primește niciodată cazuri din producție
fix pe card/PR SEPARAT
    ↓  rulează TOATE porțile
candidate release NOU
    ↓  champion-vs-candidate, orb
decision log publicat
    ↓
urmărire: fixul rezolvă cohortul FĂRĂ să regreseze altul?
```

Pașii care nu se sar, oricât de evident ar părea fixul:

1. **cazul intră în corpus înainte de fix.** Altfel n-ai cum să dovedești că fixul îl rezolvă.
2. **holdoutul nu se atinge.** Un caz din producție băgat în holdout îl transformă din măsură
   independentă în oglindă a bugurilor pe care le știm deja.
3. **fixul e un candidate nou.** Nu un hotfix pe champion, nu o schimbare de prompt din dashboard.
4. **se verifică și cohorturile NEatinse.** Un fix care repară acțiunile și strică FAQ-ul e o
   regresie netă, dar arată ca o victorie în raportul cohortului reparat.

---

## 5. Ce e INTERZIS explicit

- feedbackul ca input direct de antrenament sau de prompt;
- schimbarea promptului / modelului / rankerului din dashboard sau „pe loc" în incident;
- multi-armed bandit, reinforcement, orice formă de învățare online;
- publicarea transcriptelor sau a conținutului de holdout;
- mutarea pragurilor după ce s-au văzut cifrele;
- „hai să dăm drumul la 20%, oricum arată bine" fără evidence packet și aprobare.

---

## 6. Ownership

| Ritual | Owner | Artefact |
|---|---|---|
| zilnic automat | on-call | `reports/nx249/daily.json` |
| săptămânal uman | owner de calitate + un al doilea evaluator | decision log + cazuri noi în corpus |
| lunar | owner de produs + on-call | PR de policy (praguri, taxonomie, bugete) |
| orice apply de trafic | userul, explicit | `audit_log` + evidence packet |
