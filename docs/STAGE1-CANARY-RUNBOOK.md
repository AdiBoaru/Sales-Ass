# NX-249 — runbook de canary: cum se mută trafic pe WebWidget v2

Operațional. De ce arată așa: [`STAGE1-RELEASE-DECISIONS.md`](STAGE1-RELEASE-DECISIONS.md).
Închiderea rutei v1: [`STAGE1-CUTOVER.md`](STAGE1-CUTOVER.md).

> **Nimic din acest runbook nu se execută fără aprobarea explicită a userului.** Cardul autorizează
> implementarea și probele sigure, nu mutarea automată a traficului de producție.

---

## 0. Precondiții — se verifică, nu se presupun

```bash
# artefactele upstream, pe EXACT digestul candidate
python scripts/release/evidence.py --out reports/nx248/evidence.json     # NX-248 → READY?
python scripts/web_quality_eval.py gate --suite tests/golden/web_journeys # NX-246 → PASS?
python -m pytest tests/e2e -q                                            # NX-247 → verde?
python scripts/stage1_contract_manifest.py --check                       # contractul n-a driftat
```

Starea de azi (2026-08-18): **toate patru sunt blocate** (`NOT_READY` / `NO-GO` / `NOT-READY`).
Vezi tabelul din §9 al documentului de decizii. Până se deblochează, restul runbookului e
antrenament pe staging, nu operare pe producție.

Config minim pentru ca controllerul să existe:

```bash
RELEASE_CONTROLLER_ENABLED=true
WEB_TURN_V2_ENABLED=true            # candidate = contractul v2; poarta e validată la boot
RELEASE_ENVIRONMENT=prod            # sau staging — policy-ul e legat de mediu
RELEASE_ASSIGNMENT_SALT=<secret>    # OBLIGATORIU în prod (refuz la boot fără el)
RELEASE_POLICY_REFRESH_S=15         # și fereastra maximă de propagare a kill-switchului
```

---

## 1. Etapele. Fiecare cere timp **ȘI** eșantion

| # | Etapă | Candidate | Minim | Poartă suplimentară |
|---|---|---|---|---|
| 0 | offline | 0% | suite complete | NX-246/247 PASS |
| 1 | internal | allowlist intern, 100% | ≥100 journey runs | review uman pe transcript safe |
| 2 | demo | tenant demo, 100% | ≥24h **și** ≥100 ture | zero hard stop + spot-check manual |
| 3 | pilot | 5% | ≥48h **și** ≥200 ture candidate | SLO/burn/feedback non-inferior |
| 4 | expand | 20% | ≥72h **și** ≥500 | porți pe cohorturi/turn class |
| 5 | majority | 50% | ≥7 zile **și** ≥1.000 | rollback drill recent |
| 6 | default | 100% conversații noi | ≥14 zile **și** ≥2.000 | drain v1 + sign-off on-call |
| 7 | close-v1 | 0 accepturi publice v1 | zero v1 activ + soak | aprobarea explicită a userului |

„Și" e literal, impus în cod (`gates.gate_stage_window`). La trafic mic verdictul e `INSUFFICIENT`
și etapa se prelungește — nu se promovează pe timp scurs.

**Procentele sunt eligibilitatea conversațiilor NOI**, nu promisiunea că exact atât din ture apar
instant. Raportul publică ambele (`allocation.policy_percent` vs `allocation.observed_turn_share`).

---

## 2. Un pas de promovare, cap-coadă

### 2.1 Scrie policy-ul

`policies/pilot-5.json` (fișier în repo sau în secret store — nu conține secrete):

```json
{
  "policy_id": "nx249-stage1",
  "revision": 3,
  "environment": "prod",
  "created_at": "2026-09-01T08:00:00+00:00",
  "not_before": "2026-09-01T09:00:00+00:00",
  "expires_at": "2026-09-15T09:00:00+00:00",
  "control_release_sha": "<sha champion>",
  "control_pipeline_version": "web-chat.v1",
  "candidate_release_sha": "<sha candidate>",
  "candidate_pipeline_version": "web-view.v2",
  "mode": "canary",
  "percent": 5,
  "stage": 3,
  "eligible_business_ids": ["<uuid tenant>"],
  "internal_business_ids": [],
  "stable_salt_id": "salt-2026-09",
  "quality_packet_hash": "sha256:...",
  "e2e_packet_hash": "sha256:...",
  "deploy_manifest_hash": "sha256:...",
  "slo_policy_version": "slo_policy.v1",
  "quality_policy_version": "nx246-gate-v1",
  "rollback_compatible": false,
  "approved_by": "adi",
  "approved_at": "2026-09-01T08:30:00+00:00",
  "change_ticket": "NX-249-stage3"
}
```

`revision` trebuie să fie **exact următoarea** (curentă + 1). Nu o completăm noi: dacă am face-o,
amprenta calculată la `validate` n-ar mai fi cea persistată, iar evidence packetul ar cita alt hash.

### 2.2 Validează și planifică (fără DB, fără efect)

```bash
python scripts/release_control.py validate --policy policies/pilot-5.json
python scripts/release_control.py plan --policy policies/pilot-5.json --ids 10000
```

`plan` arată distribuția pe 10.000 de ID-uri sintetice: proporția în canary, bucketurile ocupate și
χ². O distribuție strâmbă e un bug de hash care se vede ACUM, nu peste două zile într-un raport.
Raportul nu conține niciun identificator real.

### 2.3 Produ evidence packetul

```bash
python scripts/canary_report.py --business-id <uuid> --window 48h \
  --slo reports/slo/$(date +%F).json \
  --quality reports/nx246/quality_gate.json \
  --e2e qa-suite/stage1/web-v2/run-certificate.json \
  --deploy reports/nx248/evidence.json \
  --feedback reports/nx246/feedback.json \
  --out reports/nx249/packet-stage3.json
```

Exit: `0` PASS · `1` FAIL · `2` INSUFFICIENT/UNKNOWN. Packetul e agregat și publicabil: zero
identificatori (verificat prin scanare recursivă în teste), tenantul apare ca bucket hash-uit.

Dacă a fost un incident confirmat, adaugă-l — un singur cod trece verdictul pe FAIL:

```bash
python scripts/canary_report.py ... --hard-stop invented_fact --incident INC-42
```

### 2.4 Aplică (dry-run implicit)

```bash
# arată diff-ul, nu scrie nimic
python scripts/release_control.py apply --policy policies/pilot-5.json \
  --expected-revision 2 --actor adi --reason "etapa 3, packet 7f2c" \
  --evidence reports/nx249/packet-stage3.json

# scrie, după ce ai citit diff-ul
python scripts/release_control.py apply --policy policies/pilot-5.json \
  --expected-revision 2 --actor adi --reason "etapa 3, packet 7f2c" \
  --evidence reports/nx249/packet-stage3.json --confirm
```

Refuzuri posibile și ce înseamnă:

| Ieșire | Cod | Ce s-a întâmplat |
|---|---|---|
| `REFUZAT: revision_conflict` | 2 | altcineva a aplicat între timp — **recitește** (`show`), poate a apăsat kill-switchul |
| `REFUZ: verdictul packetului e ...` | 1 | packetul nu e PASS |
| `REFUZ: amprenta packetului nu corespunde` | 1 | packetul a fost editat manual |
| `policy_revision_must_be_N` | 1 | revizia din document nu e următoarea |

### 2.5 Observă

```bash
python scripts/release_control.py show
python scripts/canary_report.py --business-id <uuid> --window 48h
```

Metrici: `release_assignment_total{decision,reason,mode}`,
`release_policy_refresh_total{outcome,age_bucket}`, `release_override_total`,
`release_gate_total{gate,verdict}` + tot ce vine din NX-246 cu `release_track` real (nu env).

---

## 3. Hard stops — freeze imediat, indiferent de conversie

Vocabular închis (`gates.HARD_STOPS`): `cross_tenant_leak`, `authorization_bypass`,
`secret_or_pii_leak`, `invented_fact`, `empty_terminal`, `result_misattribution`,
`duplicate_execution`, `false_receipt`, `state_corruption`, `artifact_mismatch`, `slo_fast_burn`,
`rollback_impossible`.

Un singur cod ⇒ verdict `FAIL`, oricât de bune ar fi restul cifrelor. Un cod necunoscut ⇒ tot
`FAIL` (nu ignorăm ce nu înțelegem), semnalat separat în raport.

---

## 4. Kill-switch — ținta ≤5 minute

```bash
python scripts/release_control.py apply --force-control \
  --expected-revision <curentă> --actor oncall --reason "INC-42 grounding" --confirm
```

Kill-switchul e o **revizie de policy ca oricare alta** — același CAS, același audit, același
istoric. Un mecanism paralel de oprire ar fi un al doilea adevăr despre ce rulează.

Ce se întâmplă:

- accepturile candidate NOI se opresc în cel mult `RELEASE_POLICY_REFRESH_S` (TTL-ul cache-ului);
- turele deja acceptate **se drenează** pe versiunea capturată — nu se rerulează, nu se convertesc;
- conversațiile care erau pe candidate: `503 release_draining` la următorul accept, dacă
  `rollback_compatible=false`; trec pe control dacă e `true` (adică s-a dovedit compatibilitatea).

Măsoară-l:

```bash
python scripts/rollback_drill.py --business-id <uuid>              # dry-run
python scripts/rollback_drill.py --business-id <uuid> \
  --actor oncall --reason "drill lunar" --confirm --out reports/nx249/drill.json
```

Drill-ul verifică: modul e `force_control`, propagarea ≤300s, ledgerul nu a pierdut rânduri,
cohortul candidate s-a drenat. **Nu** verifică compatibilitatea imaginii precedente — aia e
`scripts/release/migration_drill.py` + `smoke_web_v2.py` (NX-248), declarat explicit în raport.

---

## 5. Rollback complet (incident de cod, nu doar de trafic)

1. `apply --force-control` — freeze accepturi candidate noi;
2. păstrează workerul candidate pornit ca să dreneze turele capturate;
3. observă `active/lease/deadline` până la terminal — **nu rerula, nu rescrie completed**;
4. promovează digestul precedent prin mecanismul NX-248 (`scripts/release/rollback.py`);
5. readiness + smoke + replay exact; verifică state/action/cart receipts;
6. păstrează schema expandată — **zero down migration, zero ștergere de rânduri**;
7. publică decision/incident packet și adaugă un caz de regresie ÎNAINTE de reluare.

---

## 6. Reluarea după un rollback

Nu e un pas de operare, e un release nou: policy nou (revizie nouă), evidence packet nou, aprobare
nouă. Cazul care a cauzat incidentul trebuie să fie deja în corpusul de dezvoltare ca test
(vezi [`STAGE1-QUALITY-RITUAL.md`](STAGE1-QUALITY-RITUAL.md) §4).

---

## 7. Ce nu face controllerul

- nu promovează singur (niciodată, în nicio configurație);
- nu acceptă `business_id`, procent, mod sau hash din requestul widgetului ori din output de model;
- nu are endpoint HTTP — singurul drum e CLI-ul, cu credential de control plane;
- nu atinge WhatsApp/Telegram (înghețate) și nu face canary pe proactiv.
