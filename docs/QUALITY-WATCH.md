# Cele cinci cifre (NX-272)

```bash
python scripts/quality_watch.py --business 99fe1292-f9ed-469e-8183-f994ea5b59c0 --days 7
```

Nu e o poartă de CI. Un raport care blochează merge-ul pe o metrică zgomotoasă va fi ocolit în două
săptămâni, iar atunci nu mai ai nici poartă, nici raport. Codul de ieșire e mereu 0.

---

## De ce există

Toate măsurătorile din Wave H sunt de unică folosință: rulează la sfârșitul unui card, produc o
cifră, apoi nimeni nu se mai uită. Calitatea nu se degradează cu zgomot — se degradează tăcut, la un
import de catalog, la o schimbare de model, la un flag aprins din greșeală.

Precedentele sunt toate în proiect, nu ipotetice:

* `concern_map` a trimis cinci săptămâni spre valori care nu existau în catalog;
* `search_tsv` conținea doar numele produsului fiindcă `ai_summary` era NULL pe toate cele 2.758 de
  rânduri — 13 din 18 fraze reale de client întorceau ZERO;
* `product_variants.stock` era `NOT NULL DEFAULT 0`, deci 2.364 de produse în stoc se prezentau ca
  epuizate;
* CI-ul e verde pe flagurile stinse, iar profilul de flaguri din producție n-a fost rulat niciodată.

Niciuna dintre astea n-a dat vreo eroare.

---

## Cifrele

| cifră | ce prinde | prag de alarmă |
|---|---|---|
| `zero_results_rate` | căutări care n-au întors nimic | 10% |
| `deaf_turn_rate` | ture în care n-am înțeles **și** n-am găsit | 15% |
| `facet_coverage.*` | un import care a șters atribute | se compară cu rularea precedentă |
| `catalog_staleness_days` | sursa a tăcut | 7 zile |
| `head_precision_top3` | setul NX-265 rerulat | — (cere adnotare) |

**„Tur surd" nu e „tur eșuat".** Sunt turele în care mesajul n-a produs niciun semnal structurat
(nicio nevoie, nicio constrângere, nicio referință) **și** n-a găsit niciun produs. Un tur care n-a
găsit nimic dar a înțeles perfect („n-avem SPF 50 sub 50 de lei") e un răspuns bun despre un catalog
incomplet, nu un eșec de sistem. Cele două condiții împreună arată unde doare.

**Prospețimea se măsoară pe `synced_at`, nu pe `updated_at`.** Al doilea se mișcă la orice scriere,
inclusiv la una făcută de noi: derivarea NX-268 ar face catalogul să pară proaspăt chiar dacă sursa
tace de o lună.

---

## Patru verdicte

`UNKNOWN` (instrumentul nu există sau e stricat) ≠ `INSUFFICIENT` (sub 30 de eșantioane) ≠ `FAIL`
(am măsurat și e peste prag) ≠ `PASS`.

Ordinea condițiilor din cod E contractul: instrumentul stricat înaintea eșantionului mic, eșantionul
mic înaintea judecății. Un `PASS` obținut pe date lipsă e mai periculos decât un `FAIL` — aceeași
formă ca la NX-238 și NX-246 felia 3.

---

## Reproductibilitate

`--until` face fereastra explicită. Fără el, două rulări la câteva ore distanță compară ferestre
diferite și diferența arată ca o schimbare de calitate. Verificat: aceeași fereastră ⇒ artefact
JSON identic.

---

## Ce NU măsoară

**Conversie, venit, rată de adăugare în coș.** Sunt metricile care contează și nu se pot calcula:
zero trafic. A pretinde că le măsurăm pe 40 de conversații ar fi mai rău decât a nu le măsura.

În ziua în care există trafic, intră prin infrastructura de canary care există deja (NX-249:
cohorte, non-inferioritate pe interval Wilson, hard stops), nu printr-un al doilea sistem.

---

## Starea de azi (2026-09-03, fereastră de 30 de zile)

| metrică | valoare | verdict |
|---|---|---|
| `zero_results_rate` | — | `UNKNOWN` (zero ture în fereastră) |
| `deaf_turn_rate` | — | `UNKNOWN` (idem) |
| `facet_coverage.concerns` | **0,000** | măsurat |
| `facet_coverage.skin_type` | **0,000** | măsurat |
| `facet_coverage.shade` | **0,000** | măsurat |
| `catalog_staleness_days` | 6,2 | `PASS` |
| `head_precision_top3` | — | `UNKNOWN` (setul NX-265 nu e adnotat) |

Acoperirile la zero sunt corecte și sunt exact ce arată raportul că trebuie reparat: NX-268 și
NX-269 au derivat faptele și au măsurat ce ar produce, dar `--apply` n-a rulat, deci catalogul nu le
poartă încă. Prima rulare de după `--apply` ar trebui să arate ~0,91 / ~0,69 / ~0,21.
