# NX-246 (felia 1) — Observabilitate web: traces, metrici, `slo_policy.v1`

> **Stare:** cod livrat, **flag OFF** (`OBSERVABILITY_ENABLED=false`). Cu flagul stins nu se
> acumulează nimic în memorie și nu iese nimic din proces — calea fierbinte e byte-identică.
> Feliile 2 (feedback write-side) și 3 (gate de calitate „personal shopper") sunt separate.

Documentul ăsta e contractul. Dacă un panel, o alertă sau un raport spune altceva decât ce scrie
aici, documentul câștigă și instrumentul se repară.

---

## 1. De ce nu e „încă un dashboard"

Un sistem de telemetrie nu moare din lipsă de date. Moare din trei cauze, toate adresate explicit
mai jos:

1. **cardinalitate** — o etichetă construită dintr-o valoare de client (`business_id`, `turn_id`,
   un query, un URL) înmulțește seriile temporale până când backendul costă mai mult decât
   produsul pe care îl măsoară;
2. **scurgere** — backendul de observabilitate e cel mai lung-trăitor și cel mai larg citit sink
   dintr-un sistem: replicat, indexat, ținut luni, vizibil oricui are acces la dashboard. Ce intră
   acolo iese din perimetrul de conformitate al conversației;
3. **fals verde** — un dashboard care arată sănătos peste o fereastră fără date. Ăsta e cel mai
   periculos, fiindcă seamănă cu succesul.

---

## 2. Trace: un turn = un trace, fără nicio migrare

`web_turns.id` e un UUID, adică **exact 128 de biți — fix dimensiunea unui trace-id W3C**.
Derivăm determinist, cu secret server-owned:

```
trace_id = HMAC-SHA256(OBSERVABILITY_TRACE_SECRET, "nx246.trace:" + turn_id)[:16 octeți]
span_id  = HMAC-SHA256(secret, "nx246.span:" + turn_id + ":" + attempt)[:8 octeți]
```

Consecințe (toate sunt cerințe din card, obținute prin construcție, nu prin disciplină):

| cerință | cum se ține |
|---|---|
| traceul supraviețuiește acceptului async și restartului | orice proces recalculează același trace din `turn_id`. Nimic de propagat, nimic de persistat |
| reclaim = attempt nou, același trace | `attempt` intră în span-id-ul rădăcinii |
| zero DDL | nu există coloană `traceparent`; alternativa clasică ar fi cerut una |
| clientul nu poate deriva traceul | secretul e server-owned. Cu secret gol rămâne determinist, dar public-derivabil (pierdere de confidențialitate a corelării, nu de izolare) |
| corelarea de suport | pe ID-urile PUBLICE NX-228 (`turn_id`), nu pe trace/span ids interne |

**`traceparent`-ul din browser se REFUZĂ.** Un context public nesemnat care ar deveni părinte ar
lăsa pe oricine să lipească spans în traceul altui tenant. Îl numărăm
(`web_observability_dropped_total{signal=trace_context,reason=untrusted_inbound}`) și pornim
rădăcina noastră. O integrare legitimă (cu semnătură) e o decizie viitoare, nu un default.

### Eșantionare pe COADĂ

Spans-urile unui tur se acumulează într-un buffer mărginit (64). La închiderea rădăcinii:
**iese tot dacă traceul a fost eșantionat SAU dacă vreun span a eșuat.** Așa nu plătim pentru
traficul sănătos, dar avem traceul ÎNTREG exact pentru turele care au mers prost — singurele pe
care le deschide cineva. Decizia de eșantionare e deterministă pe `trace_id`, deci identică în
orice proces (un `random()` per span ar produce traces ciobite, care arată ca pierdere de date).

### Spans emise azi

| span | emis din | felia |
|---|---|---|
| `web.turn.execute` (rădăcină) | `src/web/turn_executor.py` | 1 ✅ |
| `web.agent.call` | punte din `turn_latency.span("model")` | 1 ✅ |
| `web.tool.call` | punte din `turn_latency.span("tools")` | 1 ✅ |
| `web.validate` | punte din `turn_latency.span("validation")` | 1 ✅ |
| `web.view_project` · `web.result.commit` · `web.aftercare.schedule` | punte din fazele NX-241 | 1 ✅ |
| `web.turn.accept` · `web.turn.queue_wait` | măsurate ca METRICI la margine/executor (nu ca spans: acceptul e sincron și scurt, coada n-are cod propriu) | 1 ▲ |
| `web.retrieval.lexical` · `.embedding` · `.fusion` | portul NX-238 — o fază NX-241 acoperă toate trei, iar a o publica sub unul dintre nume ar fi o etichetă falsă | **neemis** |
| `web.context.load` · `web.fast_path` · `web.evidence.hydrate` · `web.answer_plan` | căi fără fază proprie azi | **neemis** |

Ultimele două rânduri sunt **declarate, nu emise**, și sunt scrise aici tocmai ca nimeni să nu
citească „16 nume în taxonomie" drept „16 spans în producție". Instrumentarea lor cere o fază
proprie în NX-241 sau un seam în portul de retrieval.

**Puntea `turn_latency` → spans** e alegerea de arhitectură care ține regula din card („stagiile de
business nu importă exporterul"): `turn_latency.span()` era deja seam-ul comun prin care trec
model, tools, validare și proiecție. Bridge-ul stă acolo, deci **zero import de observabilitate în
codul de business**.

---

## 3. Metrici: cardinalitate mărginită prin construcție

Registrul e `src/observability/contract.py`. O metrică absentă din el nu se poate emite; o
etichetă nedeclarată pentru acea metrică e refuzată; o valoare în afara vocabularului devine
`other` și se numără.

Două regimuri de valori, fiindcă realitatea are două cazuri:

- **set închis** (`outcome`, `status`, `attempt_bucket`, `release_track`, `model_role`) — le știm
  pe toate; orice altceva e bug de instrumentare;
- **buget de valori distincte** (`model_id` 24, `tool_name` 32, `safe_error_code` 32) — mulțimea e
  mărginită de configurație/registru, dar nu e o constantă de cod. Peste plafon totul devine
  `other`: seriile deja create rămân valide, noutățile se izolează, iar
  `web_observability_dropped_total{reason=cardinality_budget}` arată exact unde s-a atins.

**Interzise ca etichete, peste tot:** `business_id`, `conversation_id`, `turn_id`,
`client_turn_id`, IP, visitor, product id, eroare free-form, URL. Există un test care scanează
registrul și pică dacă vreuna apare (`test_business_id_nu_e_eticheta_in_nicio_metrica`).
Drill-down-ul pe tenant se face prin query tenant-scoped pe datele durabile autorizate — nu prin
explozia cardinalității.

`turn_id` **există** ca atribut de TRACE. Distincția e chiar cerința cardului: un trace e
per-request (drill-down autorizat, retenție scurtă), o metrică e agregat.

### Contractul minim (unități + bucket-uri)

| metrică | tip | unitate | etichete |
|---|---|---|---|
| `web_turn_requests_total` | counter | 1 | `outcome`, `release_track` |
| `web_turn_accept_duration_seconds` | histogram | s | `outcome` |
| `web_turn_queue_wait_seconds` | histogram | s | `attempt_bucket` |
| `web_turn_execution_seconds` | histogram | s | `route_mode`, `outcome` |
| `web_turn_end_to_end_seconds` | histogram | s | `turn_class`, `outcome`, `release_track` |
| `web_turn_terminal_total` | counter | 1 | `status`, `safe_error_code`, `release_track` |
| `web_turn_replay_total` · `web_turn_idempotency_conflict_total` | counter | 1 | `result_status` / — |
| `web_turn_reclaim_total` · `web_turn_fenced_completion_total` · `web_turn_deadline_total` | counter | 1 | `attempt_bucket` / `phase` / `stage`+`reason` |
| `web_model_calls_total` + latency/tokens/cost | counter+histogram | 1/s/USD | `model_role`, `model_id`, `outcome` |
| `web_tool_calls_total` + latency | counter+histogram | 1/s | `tool_name`, `outcome` |
| `web_retrieval_outcomes_total` · `web_validation_total` | counter | 1 | `mode`/`check`, `outcome` |
| `web_observability_dropped_total` · queue depth · flush | counter+histogram | 1/s | `signal`, `reason` |

Bucket-uri: latență `(0.05 … 30)s` dens sub 3s; accept `(0.005 … 2.5)s` (peste 1s e deja
incident); coadă separată de execuție, altfel un backlog arată ca un model lent.

---

## 4. Privacy: poartă în DOUĂ etaje

`src/observability/sanitize.py` **nu reimplementează** redactarea — deleagă la NX-230
(`src/privacy/`, profil `telemetry`, cel mai strict). Adaugă doar ce ține de forma tehnică:

| suprafață | ce iese | ce NU iese |
|---|---|---|
| excepție | lanțul de TIPURI (`runtime_error<timeout_error`) | mesajul, args, traceback |
| URL | `scheme://host/forma/caii`, segmentele-id → `:id` | query string (integral) |
| headere | nume allowlistate + `present` pentru cele sensibile | orice valoare de autorizare (nu e citită deloc) |
| argumente de tool | cheia + TIPUL/mărimea (`{"concerns": "list[2]"}`) | valorile |
| text liber | redactat NX-230, apoi trunchiat la 120 | restul |
| id intern de corelare | hash trunchiat (`correlation_ref`) | valoarea brută |

**Lecția care a schimbat designul** (găsită de testul de canary, nu de review):

1. *Allowlist pe cheie nu e de ajuns.* `tool_name` e un atribut legitim;
   `tool_name="search_products?q=maria@example.ro"` e o scurgere de PII cu nume aprobat. Poarta e
   acum și pe VALOARE.
2. *Forma nu e de ajuns.* `sk-proj-AbCdEf…` e un identificator perfect valid ca formă. Valorile
   trec și prin detectorul NX-230; ce e semnalat se respinge.
3. *Dar detectorul nu se aplică orbește.* `11111111-2222-3333-4444-555555555555` conține un run de
   16 cifre care începe cu `4` și trece Luhn ⇒ e raportat drept CARD. Aplicat pe `turn_id`, asta ar
   fi șters ALEATORIU cheia de corelare pentru care există traceul. Identificatorii **structurali
   generați de server** (`turn_id`, `conversation_ref`) se validează pe formă și NU se scanează —
   nu vin din input de client, deci scanarea poate produce doar coincidențe.

Proba e executabilă: `tests/test_observability_privacy.py` pune telefon RO, email, bearer, cheie
OpenAI, CNP valid, IBAN, card, prompt și product-id în excepții imbricate, URL-uri, headere,
argumente de tool, atribute de span și etichete de metrică, apoi caută fiecare canary în
**toată** suprafața sink-ului (`CaptureSink.all_text()`), nu doar în câmpurile pe care le știe.

---

## 5. Export: mărginit, non-blocant, cu drop numărat

`enqueue` e sincron, O(1), nu ridică niciodată. Exportul trăiește într-un task de fundal pe care
nimeni nu-l așteaptă — un `await export(...)` într-un span ar lega latența turului de sănătatea
unui serviciu terț.

**Coadă plină ⇒ aruncăm cel mai NOU.** Într-un incident, ce explică incidentul e ÎNCEPUTUL
rafalei; păstrând prefixul rămâi cu o poveste coerentă plus un contor care spune cât s-a pierdut.
Un sink care ridică pierde batch-ul curent (numărat), nu coada și cu siguranță nu bucla.
Shutdown-ul are plafon: un proces care refuză să moară din cauza telemetriei e o pană de
disponibilitate cauzată de instrumentul care ar trebui să o măsoare.

Sink-uri: `NullSink` (default) · `CaptureSink` (memorie, teste/drive) · OTLP/HTTP
(`src/observability/otel_sink.py`, **import LENEȘ** — singurul modul din `src/` care știe că
OpenTelemetry există; cu exportul stins, graful de import al workerului nu îl atinge).

---

## 6. `slo_policy.v1` — denominatorii sunt cod

`src/observability/slo.py`. Pur: aceleași fapte ⇒ același verdict, mereu.

| SLI | sursă | numitor | numărător | prag |
|---|---|---|---|---|
| `accept_availability` | **metrics** | requesturi acceptate + eșuate la margine | `accepted` + `replayed` | ≥99,9% |
| `durable_terminal` | ledger | ture acceptate (inclusiv reclaimed) | ajunse terminal durabil | ≥99,5% |
| `non_empty_terminal` | ledger | toate terminalele | ViewModel randabil (P6) | **100%**, zero toleranță |
| `latency_p90` | ledger | ture completate | accept → terminal | **neratificat** (vezi mai jos) |
| `first_attempt_success` | ledger | terminale | `attempt=1` și `completed` | ≥99% |

**Excluderi explicite, raportate separat:** anulare de client / input invalid
(`EXCLUDED_CODES`). Eșecul intern, timeoutul, bug-ul de exporter și eroarea de deploy rămân ÎN
denominator — exact ele sunt promisiunea.

**Moduri de a NU ști** (niciunul nu poate produce `PASS`):

| situație | verdict |
|---|---|
| fereastră fără rânduri / denominator zero | `UNKNOWN` |
| sub `MIN_SAMPLES` (30) | `INSUFFICIENT` |
| set trunchiat de `--limit` | orice `PASS` de ledger devine `UNKNOWN` |
| prag neratificat | `UNKNOWN(unratified_threshold)` |
| `completed_at < accepted_at` (clock skew) | eșantion invalid, exclus din percentile + numărat |

`non_empty_terminal` e singurul care NU așteaptă eșantion: la 100% cerut, o singură violare e deja
verdict. Un `INSUFFICIENT` acolo ar ascunde exact bug-ul căutat.

### De ce latența e raportată, nu judecată

NX-241 a propus pragurile Stage 1 (3s exact / 6s recomandare / 10s complex, global p90 < 12s,
hard 15s) și a scris explicit că **NX-246 le ratifică**. Ratificarea cere o fereastră reală de
baseline, pe care nu o avem încă (trafic demo). Până atunci `RATIFIED = False`: cifra apare în
raport, verdictul e `UNKNOWN`. Alternativa — a alege pragul după ce se văd cifrele — e exact ce
interzice cardul. Ratificarea e o schimbare de o linie, într-un PR de policy separat, DUPĂ
baseline.

### Lipsuri declarate în fiecare raport (`completeness.missing`)

- `turn_class_breakdown` — `turn_class` trăiește doar în runtime (NX-241), nu în `web_turns`, deci
  latența nu se poate defalca pe clasă din ledger;
- `per_row_release_sha` — ledgerul are `pipeline_version` (contract de răspuns), nu SHA-ul de
  release. A-l suprascrie ar însemna doi proprietari pe un câmp (P3), deci nu o facem;
- `accept_metrics` — când nu se dă snapshot de metrici;
- `complete_window` / `ledger_rows` — trunchiere / fereastră goală.

Ambele prime lipsuri se închid cu un câmp nou în ledger. **Felia 1 nu adaugă niciun DDL**, deci
numărul de migrare 042 rămâne liber pentru felia 2 (feedback).

---

## 7. Rulare

```bash
# raport pe demo, fereastră de 7 zile (tenant-scoped, obligatoriu)
PYTHONPATH=. python scripts/slo_report.py \
    --business-id 6098812a-50fc-44bd-a1ba-bc77e6399158 --window 7d

# artefact JSON + accept SLI dintr-un snapshot de metrici
PYTHONPATH=. python scripts/slo_report.py --business-id <uuid> --window 1h \
    --metrics-json reports/metrics_snapshot.json --out reports/slo/2026-08-17.json --json
```

Exit codes (pentru gate-ul NX-247): `0` PASS · `1` FAIL · `2` UNKNOWN/INSUFFICIENT.

Agregarea se face **în SQL**: `renderable` e o expresie care oglindește `turn_service.renderable`,
tocmai ca `response_json` — conținut de conversație — să nu iasă din DB în memoria unui proces de
raportare, de unde ar ajunge într-un artefact de CI sau într-un traceback.

---

## 8. Flags și rollout

| flag | default | ce face |
|---|---|---|
| `OBSERVABILITY_ENABLED` | `false` | master switch, **absorbant**: stins ⇒ zero span, zero contor |
| `OBSERVABILITY_TRACES_ENABLED` | `true` | kill-switch traces (efectiv doar cu master ON) |
| `OBSERVABILITY_METRICS_ENABLED` | `true` | kill-switch metrici |
| `OBSERVABILITY_EXPORTER` | `none` | `none` \| `capture` (memorie) \| `otlp` (rețea) |
| `OBSERVABILITY_OTLP_ENDPOINT` | — | setat fără master ON ⇒ **boot REFUZAT** |
| `OBSERVABILITY_SAMPLE_RATIO` | `0.05` | doar turele de SUCCES; erorile ies mereu |
| `OBSERVABILITY_QUEUE_MAX` / `_EXPORT_BATCH` / `_FLUSH_TIMEOUT_MS` | 2048 / 256 / 2000 | coada mărginită |
| `OBSERVABILITY_TRACE_SECRET` | — | HMAC pentru derivarea trace-id |
| `RELEASE_SHA` / `RELEASE_TRACK` / `SERVICE_NAME` | — / `champion` / `nativx-assistant` | markeri pe fiecare span |

Trei kill-switch-uri **independente**, deliberat: în incident vrei „taie exportul, păstrează
măsurarea locală". Un singur buton ți-ar lua exact datele care explică incidentul.

**Poarta de boot** (`Settings._observability_relations`, ca la NX-233/241): endpoint fără master
switch, `otlp` fără endpoint, endpoint care nu e URL absolut, `release_track` necunoscut, batch
peste coadă ⇒ procesul **nu pornește**, cu cod de fix în mesaj. Un endpoint scris greșit care
eșuează tăcut produce exact patologia pe care o combatem: dashboard verde peste un sistem mut.

Ordinea de activare: (1) OFF, verifică zero schimbare · (2) `capture` în test/staging + verifică
redactarea · (3) `otlp` pe staging, sampling mic · (4) demo: 100% erori/deadline/reclaim ·
(5) o fereastră completă de baseline ÎNAINTE ca SLO-ul să blocheze ceva · (6) ratifică pragurile
de latență, într-un PR de policy separat.

---

## 9. Ce NU e în felia asta

- **feedback write-side** (thumbs/reason, token NX-236, idempotency, RLS, migrarea 042) — felia 2;
- **gate de calitate „personal shopper"** (corpus de journeys, holdout sigilat, pairwise blind,
  praguri fail-closed) — felia 3. Verdictul lui rămâne oricum blocat pe corpus (NX-203 pauzat,
  H3 sigilat 0/50), nu pe cod;
- **livrarea alertelor** (NX-248) și **decizia de canary/promovare** (NX-249);
- **export de metrici pe OTLP** — contractul e definit, dar transportul cere un colector (NX-248);
  azi metricile se citesc din `snapshot()` și din raportul SLO.
