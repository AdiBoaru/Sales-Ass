# Wave H — predare de sesiune

**Actualizat:** 2026-09-03 (a doua sesiune) · **Branch:** `feat/NX-264-domain-leak-gate`
**Tenant de lucru:** `sole-ro` / `99fe1292-f9ed-469e-8183-f994ea5b59c0`

Documentul ăsta există ca o sesiune nouă să continue fără să redescopere nimic. Dovezile complete
sunt în [`docs/RETRIEVAL-QUALITY-PLAN.md`](../../docs/RETRIEVAL-QUALITY-PLAN.md); aici e doar starea.

---

## Starea celor 10 carduri

| card | stare | ce mai lipsește |
|---|---|---|
| [NX-264](NX-264.md) porți de generalitate | **LIVRAT** | — |
| [NX-265](NX-265.md) instrument de măsură | **harness LIVRAT** | **o zi de judecată umană** (Adi) |
| [NX-266](NX-266.md) constrângeri tipizate | **LIVRAT** (flag OFF) | doar rerularea NX-265 |
| [NX-267](NX-267.md) rerankare | **NEÎNCEPUT, blocat** | baseline-ul NX-265 — vezi mai jos |
| [NX-268](NX-268.md) fapte derivate | **LIVRAT ca unealtă** | `--apply` + auditul de precizie |
| [NX-269](NX-269.md) nuanță + finish | **LIVRAT ca unealtă** | `--apply` + auditul de precizie |
| [NX-270](NX-270.md) graf de relații | **LIVRAT ca unealtă**; migrarea 048 APLICATĂ | `--apply` |
| [NX-271](NX-271.md) aprinderea excluderii | **poarta LIVRATĂ**, aprinderea blocată | auditul NX-268 |
| [NX-272](NX-272.md) măsurătoare continuă | **LIVRAT** | — |
| [NX-273](NX-273.md) prompt din pachet | **LIVRAT**; cricul NX-264 e **GOL** (34 → 0) | — |

**NX-267 e singurul neînceput, și e blocat cu motiv, nu din lipsă de timp.** Cardul cere baseline-ul
NX-265 înainte, iar D15 spune explicit că motorul de relevanță nu se schimbă pe intuiție. Un
reranker fără cifra de dinainte n-ar putea fi nici justificat, nici dat înapoi.

---

## Ce trebuie să facă Adi

### 1. Ziua de adnotare (deblochează patru lucruri)

```bash
python scripts/goldset_annotate.py --business 99fe1292-f9ed-469e-8183-f994ea5b59c0
python scripts/goldset_report.py --business 99fe1292-f9ed-469e-8183-f994ea5b59c0 --label baseline
```

134 de fraze extrase și stratificate. `0 2 3` = corecte, `g:1` = greșit, `+<text>` = adaugă,
`q` = ieși. Progresul se salvează după fiecare frază; reluabil oricând.

Deblochează: NX-267 (rerankarea), și punctul „metricile nu scad" din NX-266/268/269/271/273.

### 2. Decizia de scriere în catalog

Toate cele trei rulează în dry-run azi și au măsurat ce AR scrie. Niciuna n-a scris nimic.

```bash
python scripts/set_domain_pack.py --business sole-ro --pack db/seed/domain_pack_sole_ro.json --apply
python scripts/derive_product_attributes.py --business 99fe1292-... --apply
python scripts/derive_shade_finish.py --business 99fe1292-... --apply
python -m src.jobs.build_relations --business 99fe1292-... --apply
```

Ordinea contează: pachetul întâi (unitățile NX-266 și fațetele `shade`/`finish` trebuie să existe
înainte ca derivarea să le poată folosi), apoi faptele, apoi graful (care le citește).

### 3. Auditul de precizie (după `--apply`)

```bash
python scripts/derived_precision_audit.py --business 99fe1292-... --facet concerns
python scripts/derived_precision_audit.py --business 99fe1292-... --report
```

Fără el, nicio fațetă nu primește `enforce_ready`, deci NX-271 nu se poate aprinde. Pragurile sunt
preînregistrate în `tests/derived_precision_policy.json` și amprentate în raport.

---

## Cifrele măsurate în sesiunea asta

| ce | înainte | după |
|---|---|---|
| `concerns` derivat | 2.354 (85,4%) | **2.506 (90,9%)** |
| `skin_type` derivat | 1.702 (61,7%) | **1.892 (68,6%)** |
| `key_ingredients` | 99,1%, **10.392 valori** | 87,7%, **250 valori** |
| produse fără nicio nevoie | 306 (11,1%) | **202 (7,3%)** |
| `machiaj` cu nuanță | 0 | **581 (85,3%)**, zero inventate |
| `finish` | 0 | **222 (32,6%)**, max 11,5% pe o valoare |
| muchii de graf | 0 | **35.008** (ar scrie) |
| epuizate cu substitut | 0 | **155/157** dintre cele cu nevoi cunoscute |
| scurgeri de domeniu în cod | 34 înghețate | **0** |

---

## Trei lucruri pe care le-am aflat lovindu-mă de ele

**1. Premisa lui NX-268 nu se confirmă.** „Stemuri = 12× semnal" e volum, nu semnal: stemul `gras`
pică testul de discriminare al NX-264 (prinde „acizi grași"), iar `pielii` ar fi urcat `barrier` de
4,8× fără să însemne nimic. Ce funcționează e fraza cu tokeni pe prefix: **+23%, zero pierderi**.
Consecință: lista de excluderi e goală pe măsurătoare, fiindcă falsul pozitiv pe care trebuia să-l
repare nu se mai poate produce.

**2. Cifra „≥380 din 391" a lui NX-270 nu se atinge, și n-ar trebui forțată.** 234 dintre produsele
epuizate n-au nicio nevoie derivată. Cifra din card a fost măsurată FĂRĂ cerința de nevoie comună,
adică măsura raftul — exact ce restul cardului respinge. Pe ancorele pe care regula poate lucra,
rata e 98,7%.

**3. Pachetul declara fațete cu `source: "name"`,** o valoare pe care `FacetSource` n-o are. Loader-ul
le respingea fail-closed și nimeni n-ar fi observat că `finish` nu există pentru `facet_coverage`,
poarta de relevanță sau comparație. Acum `source` (de unde se CITEȘTE) și `derived_from` (de unde a
fost EXTRASĂ) sunt două câmpuri.

---

## Ce trebuie știut înainte de a atinge codul

**Poarta NX-264 e activă în CI, iar cricul e GOL.** Orice literal de string din `src/` sau
`scripts/` care conține vocabular de domeniu pică testul, și nu mai există nicio intrare de baseline
sub care să se ascundă. Scutiri: pragma pe linie (`# domain-leak: ok — motiv`), cale în
`tests/domain_leak_allowlist.json`. Fiecare cere motiv scris.

**Suita locală are ~19+108 roșii care NU sunt regresii.** `.env`-ul local are stiva de flaguri
aprinsă și `ENV=prod`; CI e verde pe OFF. Verifică NUMĂRUL înainte și după, nu valoarea absolută.

**Migrări:** ultima aplicată e **048** (aplicată în sesiunea asta). Următorul număr liber: **049**.
`ENV=prod` în `.env` blochează `scripts/migrate.py`; rulează cu `ENV=dev python scripts/migrate.py`.

**Flaguri noi, toate OFF:** `TYPED_CONSTRAINTS_ENABLED`, `RELATION_GRAPH_ENABLED`,
`RELEVANCE_MASK_FACETS` (listă, goală).

**Nu porni rulări care consumă credite OpenAI.** Pregătește comanda, o rulează Adi.

---

## Documente noi

* [`docs/RELEVANCE-MASK-ROLLOUT.md`](../../docs/RELEVANCE-MASK-ROLLOUT.md) — jurnalul aprinderii
  excluderii, criteriul de oprire preînregistrat, ordinea fațetelor
* [`docs/QUALITY-WATCH.md`](../../docs/QUALITY-WATCH.md) — cele cinci cifre, patru verdicte
* [`docs/048_relation_provenance.sql`](../../docs/048_relation_provenance.sql) — provenance pe
  muchie + `variant_of` în CHECK
