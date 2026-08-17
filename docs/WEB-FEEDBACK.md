# NX-246 (felia 2) — Feedback one-tap: server-owned, autorizat, idempotent

> **Stare:** cod livrat, **flag OFF** (`WEB_FEEDBACK_ENABLED=false`). Cu flagul stins nu se emite
> niciun prompt, deci nu există niciun token, deci endpointul n-are ce autoriza — poartă dublă.
> Migrarea **042** e scrisă, **neaplicată**.

---

## 1. Ce poate minți un „👍"

Un vot one-tap pare banal până întrebi ce anume ar putea falsifica un client. Răspunsul e: aproape
tot. Poate trimite `{"rating": "positive"}` pentru turul altcuiva, de o mie de ori, cu un motiv
inventat, din altă sesiune, pentru un turn care nu i-a cerut niciodată părerea. Dacă oricare
reușește, semnalul pe care se ia decizia de rollout devine zgomot — și e cel mai rău fel de zgomot,
fiindcă arată exact ca date.

De aceea **nu există „endpoint care primește un rating"**:

| ce ar vrea clientul să spună | de ce nu poate |
|---|---|
| „ratingul e pozitiv" | ratingul e în **KIND**, iar kind-ul e **sigilat** (`feedback_up` / `feedback_down`). Browserul primește două tokenuri opace și le retrimite neschimbate |
| „motivul e «pentru că da»" | `reason` e vocabular ÎNCHIS (`FEEDBACK_REASONS`), validat la parse. Un motiv necunoscut e **respingere**, nu `other` tăcut — altfel taxonomia ar crește din date de client |
| „votez pentru turul X" | turul vine din **legăturile plicului** (`source_turn_id`), nu dintr-un câmp |
| „am primit butonul ăsta" | **dovada de emitere** (NX-236): `action_id` se re-derivă din planul persistat în `web_turns.response_json["actions"]`. Sigiliu perfect + id absent din plan = n-a fost oferit niciodată |
| „sunt aceeași sesiune" | tenant/sesiune/conversație sunt **pseudonime criptografice** în plic; un token mutat nu se potrivește, fără niciun lookup |
| „mai votez o dată" | unicitatea trăiește în **DB** (unique pe `(business_id, feedback_prompt_id)`), nu într-o secvență de apeluri |

Corpul rutei are **două câmpuri și niciunul semantic**. `extra="forbid"`: un client care încearcă
`{"rating": "positive"}` primește 422, nu o ignorare tăcută care l-ar face să creadă că a votat.

---

## 2. `feedback_prompt_id` e derivat, nu random

**Abatere deliberată de la litera cardului** („emite un `feedback_prompt_id` random"), cu motiv.

Proiecția v2 e o funcție **pură** (NX-240) și tokenurile NX-236 sunt deterministe tocmai ca două
citiri ale aceluiași rând să producă aceiași bytes. Un id random ar rupe exact asta: un `GET`
repetat sau un SSE reconectat ar produce alt prompt pentru același turn, iar *„un singur vot per
prompt"* ar deveni *„un vot per reîncărcare de pagină"*.

```
feedback_prompt_id = HMAC-SHA256(WEB_FEEDBACK_PROMPT_SECRET,
                                 "nx246.feedback:" + schema + ":" + turn_id)[:32 hex]
```

Proprietatea pe care o voia cardul (clientul nu îl poate ghici) se păstrează — cheia e
server-owned. Cea de care aveam nevoie (determinism) se adaugă. Același raționament ca `trace_id`
în felia 1.

---

## 3. Sink: de ce feedbackul are rută proprie

Un „👍" **nu e un tur**. Pe `/web/v2/turns` ar consuma slotul de single-flight al conversației, ar
porni pipeline-ul și ar produce un răspuns conversațional pentru un click care nu cere niciunul.

Separarea e **structurală**, nu un `if` pe rută: `ActionSpec.sink` (`turn` | `feedback`). Fiecare
rută compară sink-ul specului cu al ei și refuză restul. Consecința practică: o acțiune nouă nu
poate ajunge din greșeală pe drumul greșit, iar testul o prinde la adăugare, nu în producție.

Verificările **nu sunt duplicate**. NX-246 a adus a doua rută care consumă tokenuri; a copia
secvența de verificări ar fi însemnat două locuri de ținut sincronizate la fiecare schimbare de
model de amenințare — exact patologia pe care NX-230 a consolidat-o (cinci regexuri de telefon,
unul singur primea fixul). De aceea secvența e spartă în două funcții **pure**, folosite de ambele
rute:

```
verify_envelope  — crypto, audiență/expirare, tenant, sesiune, SINK.    (fără DB)
verify_source    — terminal, deținut, aceeași conversație, DOVADA.      (fără DB)
```

Fiecare rută își face singură partea de DB, deci **niciun checkout în plus** pe calea fierbinte.

---

## 4. Idempotență: în schemă, nu în cod

`upsert_feedback` e **un singur statement** cu `ON CONFLICT`. Două cereri concurente nu pot citi
amândouă „nu există" și scrie amândouă — una pierde pe unique și cade determinist pe update.

| situație | rezultat |
|---|---|
| rândul nu există | INSERT, `revision = 1` |
| există, **același** `action_id` | nicio schimbare, `revision` NEATINS, **același receipt** (retry de rețea, double-click, două taburi) |
| există, **alt** `action_id` | UPDATE + `revision + 1` (corecție autorizată), fără rând nou |
| `revision >= 5` | `feedback_locked` — un flip-flop automat nu poate scrie la infinit |

Regula „retry identic nu incrementează" trăiește în clauza `where` a `do update`, nu într-un `if`
în Python. Dovada e în `test_web_feedback_db.py`: 20 de cereri concurente cu același `action_id`
lasă **un rând, revizia 1**.

---

## 5. Ce NU conține un rând (P12, listă, nu intenție)

Fără text liber, fără comentariu, fără IP, fără user-agent, fără token, fără identitate brută.
Doar refs opace (uuid-uri), enum-uri din vocabular închis și timestamps. Un vot e „acest turn,
pozitiv, motiv X" — nimic despre CINE.

Verificat de două teste: unul pe dataclass (`FeedbackRow.__dataclass_fields__`), unul pe schema
reală (`information_schema.columns`). Coloanele interzise pur și simplu nu există.

Legătura cu persoana e prin conversație (ca la `conversation_carts`); `on delete cascade` duce
rândurile odată cu erase-ul de contact.

---

## 6. Raportul: `positive_feedback_rate`, niciodată „CSAT"

Un procent fără `n` e o minciună politicoasă. „87% pozitiv" din 8 voturi și din 8000 sunt afirmații
complet diferite, iar diferența decide dacă promovezi un release.

- sub **30 de voturi** verdictul e `insufficient_sample` — **nu** 87%, **nu** 0%, **nu** „n/a";
- peste prag, procentul vine ÎNTOTDEAUNA cu `n` și interval de încredere;
- intervalul e **Wilson**, nu Wald. Wald e formula pe care o știe toată lumea și e exact cea care
  se strică la extreme: la 10/10 dă un interval de lățime zero („între 100% și 100%"), la 0/12 dă
  un interval negativ. Wilson rămâne onest la capete — fix acolo unde e un produs nou;
- **defalcarea pe `release_track` are propriul ei prag**: un cohort de 4 voturi nu primește procent
  doar fiindcă totalul general e mare. Altfel exact comparația champion-vs-candidate, care e scopul
  întregului mecanism, s-ar face pe zgomot.

**Nu se numește CSAT.** CSAT are o metodologie (scală, moment, populație, rată de răspuns) pe care
nu o îndeplinim: strângem voturi one-tap, de la cine vrea, pe turele care au primit prompt.
`positive_feedback_rate` spune exact ce e. „CSAT 87%" într-un raport de vânzări e o promisiune
despre clienți; `positive_feedback_rate` e o observație despre butoane. Un test verifică pe
artefactul serializat că șirul „csat" nu apare.

```bash
PYTHONPATH=. python scripts/feedback_report.py --business-id <uuid> --window 7d
PYTHONPATH=. python scripts/feedback_report.py --business-id <uuid> --window 30d --json
```

---

## 7. Ce s-a emis, și ce NU

Stage 1 planifică exact **două** acțiuni: `feedback_up` + `feedback_down`, **ultimele** în listă.

Nu emitem un buton per motiv, deși `reason` e modelat, validat, persistat și raportat. Motivul e
aritmetic: cele 6 motive ar consuma 6 din cele **16** acțiuni ale unui ViewModel (`MAX_ACTIONS_PER_TURN`)
și ar împinge afară acțiunile conversaționale — un card cu 6 produse planifică deja 13. Pasul doi
(chips de motiv după un vot negativ) e o decizie de UX care aparține NX-244, nu o jumătate de flux
strecurată aici. Cardul cere „două/mai multe" tokenuri; astea sunt cele două.

Feedbackul se cere **doar pe ture `completed`**. A întreba despre un `failed` ar strânge voturi
despre un mesaj de eroare scris de noi — semnal despre infrastructură, prezentat ca semnal despre
calitatea răspunsului.

---

## 8. Ce nu face feedbackul

Nu schimbă răspunsul curent. Nu atinge rankingul. Nu intră în prompt. Nu devine training data.
Scrie un rând și întoarce un receipt. Dacă storage-ul e jos, refuză onest (`feedback_unavailable`)
și **conversația rămâne neatinsă** — un vot care nu se poate salva nu are voie să strice turul.

NX-249 consumă trendurile; o observație singulară nu schimbă producția.

---

## 9. Flags, migrare, rollout

| flag | default | ce face |
|---|---|---|
| `WEB_FEEDBACK_ENABLED` | `false` | emite prompturi + deschide `/web/v2/feedback` |
| `WEB_FEEDBACK_PROMPT_SECRET` | — | HMAC pentru `feedback_prompt_id` |

Poartă de boot: `WEB_FEEDBACK_ENABLED` cere `WEB_TURN_V2_ENABLED` + `WEB_ACTIONS_ENABLED` —
promptul E o acțiune opacă semnată, iar un flag aprins singur ar sugera că se strâng voturi când,
de fapt, nu se strânge nimic.

**Migrarea 042** (`docs/042_web_feedback.sql`) e scrisă și **neaplicată**. Atenție la ordine: 041
(NX-237, coșuri) e tot pending, iar `scripts/migrate.py` le aplică ordonat — deci 041 intră prima.
Poarta de boot (`assert_migrations_current`) cere ambele aplicate înainte ca workerul să pornească.

Rollout: (1) aplică 041+042 · (2) pornește flagurile pe demo · (3) o fereastră completă înainte ca
raportul să însemne ceva (sub 30 de voturi verdictul e `insufficient_sample`, deliberat) ·
(4) NX-249 decide ce face cu trendul.

Rollback: stinge flagul. Prompturile nu se mai emit, endpointul refuză onest, **voturile deja
strânse rămân citibile**. Tabelul nu se dropează — un vot e o dovadă; se corectează prin `revision`,
nu prin ștergere (de aceea `bot_runtime` n-are `DELETE`).
