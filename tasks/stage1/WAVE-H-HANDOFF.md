# Wave H — predare de sesiune

**Actualizat:** 2026-09-03 · **Branch:** `feat/NX-264-domain-leak-gate` (4 commit-uri, nepushat, fără PR)
**Tenant de lucru:** `sole-ro` / `99fe1292-f9ed-469e-8183-f994ea5b59c0`

Documentul ăsta există ca o sesiune nouă să continue fără să redescopere nimic. Dovezile complete
sunt în [`docs/RETRIEVAL-QUALITY-PLAN.md`](../../docs/RETRIEVAL-QUALITY-PLAN.md); aici e doar starea.

---

## Starea celor 10 carduri

| card | stare | ce mai lipsește |
|---|---|---|
| [NX-264](NX-264.md) porți de generalitate | **LIVRAT** (`db871ae`) | — |
| [NX-265](NX-265.md) instrument de măsură | **harness LIVRAT** (`f353999`) | **o zi de judecată umană** (Adi) |
| [NX-266](NX-266.md) constrângeri tipizate | de făcut | **următorul**, nu depinde de nimeni |
| [NX-267](NX-267.md) rerankare | de făcut | cere baseline-ul din NX-265 |
| [NX-268](NX-268.md) fapte derivate | de făcut | poate porni în paralel cu NX-266 |
| [NX-269](NX-269.md) nuanță + finish | de făcut | după NX-268 |
| [NX-270](NX-270.md) graf de relații | de făcut | după NX-268 + NX-269 |
| [NX-271](NX-271.md) aprinderea excluderii | de făcut | după NX-265 + NX-268 |
| [NX-272](NX-272.md) măsurătoare continuă | de făcut | poate porni oricând după NX-265 |
| [NX-273](NX-273.md) prompt din pachet | de făcut | **produs de NX-264**, nu era în planul inițial |

**Două ordini nenegociabile:** NX-265 înaintea lui NX-267 (fără baseline nu se poate dovedi că
rerankarea a ajutat) și NX-268 înaintea lui NX-270 (un graf peste fapte goale e un raft).

---

## Ce trebuie să facă Adi, o singură dată

```bash
python scripts/goldset_annotate.py --business 99fe1292-f9ed-469e-8183-f994ea5b59c0
```

134 de fraze deja extrase din catalog și stratificate. Pentru fiecare: `0 2 3` = corecte,
`g:1` = greșit, `+<text>` = adaugă un produs care nu apare, `q` = ieși (progresul se salvează după
fiecare frază). Reluabil oricând.

Apoi baseline-ul:

```bash
python scripts/goldset_report.py --business 99fe1292-f9ed-469e-8183-f994ea5b59c0 --label baseline
```

Fără pasul ăsta, NX-267 nu are cum să intre în `main`: nu există cifră față de care să se compare.

---

## De unde continuă o sesiune nouă

**NX-266 — constrângeri tipizate cu unități.** Nu depinde de judecata umană și e prerechizit pentru
reranker: fără el, un model care citește text poate urca un produs SPF 15 la o cerere „SPF minim 30",
iar nicio poartă de adevăr nu-l prinde (produsul chiar are SPF 15 și o spune cinstit).

```text
/task stage1/NX-266
```

Sau, dacă vrei paralelism, NX-268 atinge alte fișiere (derivare offline) și poate merge simultan.

---

## Ce trebuie știut înainte de a atinge codul

**Poarta NX-264 e activă în CI.** Orice literal de string din `src/` sau `scripts/` care conține
vocabular de domeniu pică testul. Trei feluri de a scuti ceva, în ordinea preferinței: pragma pe linie
(`# domain-leak: ok — motiv`), cale în `tests/domain_leak_allowlist.json`, sau cricul
`tests/domain_leak_baseline.json` pentru datorie recunoscută. Fiecare cere motiv scris.

**Cricul are 34 de intrări îngheţate** — scurgeri reale în `prompt_builder`, `tool_definitions`,
`greeting`, `triage`, `brain_models`. Pot doar să scadă. Când NX-273 le repară, intrările TREBUIE
șterse, altfel testul pică pe intrări moarte.

**Suita locală are ~108 roșii care NU sunt regresii.** `.env`-ul local are stiva de flaguri aprinsă;
CI e verde pe OFF. Verifică numărul înainte și după orice schimbare, nu valoarea absolută.

**Migrări:** ultima aplicată e `047`. Următorul număr liber e **048**, rezervat de NX-270
(provenance pe muchie + extinderea CHECK-ului de `kind`).

**Nu porni rulări care consumă credite OpenAI.** Pregătește comanda, o rulează Adi.

---

## Cifrele care justifică tot lanțul

| | |
|---|---|
| fațete la 0% acoperire | **6 din 9** |
| produse epuizate fără substitut | **391** (din care 381 AU alternativă în ±30% preț) |
| produse `machiaj` fără axa de decizie | **681** (nuanța e în nume, variantele toate „Standard") |
| semnal „ten gras": `name+description` vs secțiuni `aura` | **26 vs 1.078** |
| potrivire frază exactă vs stem | **93 vs 1.120** |
| recenzii reale, zero rezumate | **183.003** (în afara Wave H, dar cel mai mare activ neatins) |

Premisa care leagă totul: **sistemul are porți de ADEVĂR peste tot și niciuna de POTRIVIRE.** Poate
recomanda onest un ser de ten uscat cuiva cu ten gras — fiecare propoziție verificabilă, întregul
greșit. Mecanismul de potrivire există (NX-257, `RELEVANCE_MASK_ENABLED`) și e stins fiindcă fațetele
pe care s-ar sprijini sunt goale.
