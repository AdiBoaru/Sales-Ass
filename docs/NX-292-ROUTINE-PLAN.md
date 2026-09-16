# NX-292 — rutina ca OBIECT al conversației, nu ca proză într-un tur

**Status:** DESIGN · **Depinde de:** NX-280 (fațeta `routine_step`), NX-261/262 (graful de relații),
NX-236 (acțiuni opace), NX-237 (`CartService` ca șablon), NX-240 (grounding + projector)
**Migrare:** 052 · **Flag:** `ROUTINE_ENABLED` (default OFF)

---

## 1. Problema, pusă exact

Două capabilități, nu una:

- **(A)** clientul cere o rutină, iar botul o spune frumos, pas cu pas;
- **(B)** clientul intervine pe UN pas („asta e prea scumpă, ceva mai ieftin") fără să piardă rutina.

(B) e cea grea, și ea dictează arhitectura lui (A). Dacă rutina e doar proză plus un bloc de
afișare, atunci „mai ieftin" cade pe `_CHEAPER_RE` din
[`deterministic.py:61`](../src/agent/deterministic.py) → `search_cheaper_than`, care e ancorat pe
**categoria setului afișat** și întoarce o listă plată sortată pe preț. Adică rutina ar fi
înlocuită cu produse ieftine, iar turul următor ar recompune alta de la zero.

Deci regula de design, din care decurge tot restul:

> O rutină nu e un răspuns. E un obiect cu sloturi, pe care conversația îl modifică.

## 2. Ce e deja construit (și nefolosit)

| Piesă | Unde | Stare |
|---|---|---|
| `RoutineBlock` (contract v2) | [`contracts_v2.py:378`](../src/web/contracts_v2.py) | există, **niciun producător** |
| Forma rutinei (pași + ordine) | `domain_pack.routine_steps` | 4 familii, 49 de tipuri mapate |
| Inventar per pas | `products.attributes.routine_step` | **2.019/2.758** produse |
| Graf de secvență | `product_relations.kind='routine_next'` | **7.244** muchii, `mode: chain` |
| Traversare de lanț | `traverse_relation_chain` + `walk_chain` | un query, drum real |
| Enum de rafinare | `REFINE_FILTERS = {cheaper, in_stock, top_rated}` | acțiune `refine_search`, `available=False` |
| Obiect versionat + receipts | `CartService`, migrarea 041 | șablon direct aplicabil |

Familiile din pachetul SOLE, măsurate azi pe baza live:

```
fata     curatare → tonifiere → esenta → tratament → hidratare → protectie   (1.253 produse)
par      spalare → conditionare → tratament → styling                          (158)
corp     curatare → exfoliere → hidratare                                      (125)
machiaj  ten → pudra → obraz → ochi → buze                                     (483)
```

## 3. Constatarea care dictează designul

Muchiile `routine_next` **nu afirmă „exact produsul ăsta urmează"**.
[`build_relations.py:348`](../src/jobs/build_relations.py) e explicit: pașii vin din secțiunea
`routine_integration`, care referă *tipuri* de produs, deci muchia se instanțiază pe un
**REPREZENTANT** al categoriei următoare, iar motivul o declară (`representative: true`).

Asta nu e o limitare, e specificația. Înseamnă:

> **graful e autoritatea pe FORMĂ, fațeta e autoritatea pe INVENTAR.**

Și, implicit, înseamnă că produsul dintr-un slot e prin construcție *un candidat*, nu *răspunsul*.
Schimbarea lui nu e corectarea unei greșeli, e utilizarea normală a obiectului. Slotul trebuie să
fie schimbabil din prima, nu printr-o ramură de excepție adăugată mai târziu.

---

## 4. Modelul de date

```sql
-- migrarea 052
conversation_routines
    id uuid pk, business_id uuid not null, conversation_id uuid not null,
    family text not null,            -- 'fata' | 'par' | 'corp' | 'machiaj'
    locale text not null,
    version int not null default 1,  -- crește la FIECARE mutație
    status text not null,            -- 'active' | 'superseded'
    created_at, updated_at

conversation_routine_slots
    routine_id uuid, business_id uuid not null, position int,   -- pk compus
    step text not null,              -- 'curatare' (fără familie: familia e a rutinei)
    product_id uuid null,            -- NULL = pas neacoperit, DECLARAT
    pinned boolean not null default false,
    origin text not null,            -- 'facet' | 'graph' | 'swap'
    uncovered_reason text null       -- 'no_candidate' | 'budget' | 'safety' | 'out_of_stock'

routine_action_receipts
    business_id, routine_id, action_id, ...   -- idempotență, ca `commerce_action_receipts`
```

RLS + FK compus pe tenant, ca la 041. `bot_runtime` are grant; nimic nu se citește pe `admin_conn`.

**Starea ține doar `routine_ref {id, version}`** — exact ca `cart_ref`.

### De ce tabel și nu `state` jsonb

Patru argumente, toate făcute deja de NX-237 pentru coș:

1. **Versiune.** Fiecare swap incrementează. Un `expected_version` stale trebuie să dea conflict cu
   snapshot proaspăt, nu suprascriere tăcută.
2. **Idempotență.** Dublu-click pe „mai ieftin" trebuie să producă UN swap. Cheia de receipt e o
   proprietate a scrierii, nu a memoriei.
3. **Buget.** 4-6 sloturi × refs + motive nu încap onest în cei 8KB alături de nevoi și referințe.
4. **Degradare.** Scara din [`state_v2.py:798`](../src/conversation/state_v2.py) aruncă
   `active_search` + `cart_ref` sub presiune. O rutină ținută în `state` ar dispărea tăcut exact în
   conversațiile lungi, adică în cele în care contează. Cu `routine_ref`, pierderea e VIZIBILĂ
   (`degraded=True`) și se poate spune onest.

`routine_ref` intră pe aceeași treaptă cu `cart_ref` în scara de degradare.

---

## 5. Compunerea — cod pur, zero decizii de model

Ordinea pașilor nu e o părere a modelului. E în pachet.

```
compose_routine(family, constraints, anchor?) -> RoutinePlan        # PUR, testabil fără DB
  1. steps = pack.routine_steps.families[family]          # ordinea E în pachet
  2. dacă există ancoră (PDP NX-234, produs afișat, „am deja crema X"):
        seed = walk_chain(traverse_relation_chain(anchor, 'routine_next'))
        sloturile acoperite de seed primesc origin='graph'
  3. pentru fiecare pas RĂMAS:
        candidați = search cu fațeta routine_step = f'{family}:{step}'
                    + constrângerile HARD ale turului (buget, concerns, skin_type)
                    + SafetyPolicy.evaluate(...)          # NX-173, ÎNAINTE de scriere
        pick = blended rank existent;  origin='facet'
  4. pas fără candidat vandabil ⇒ slot cu product_id NULL + uncovered_reason
```

**Precedența: fațetă întâi, graf ca ancoră.** Fără ancoră reală, umplem toți pașii familiei din
fațetă — acoperire completă și previzibilă. Graful intră doar când clientul e ancorat pe un produs
(pagină de produs, produs deja arătat, produs pe care îl are), fiindcă acolo lanțul spune ceva ce
fațeta nu poate spune: ce urmează *după ăsta*, conform propriei fișe a produsului.

**Regula de onestitate (punctul 4).** Un pas fără candidat e un slot DECLARAT, nu un pas sărit.
E aceeași regulă pe care `build_routine` o respectă deja prin `anchors_without_edges`: „pentru
pasul de esență n-am nimic care să intre în bugetul tău" e un răspuns, iar un pas dispărut în
tăcere e o minciună structurală. Un slot neacoperit NU blochează rutina (P6).

### Alegerea familiei

Deterministă, din obligațiile NX-208 + fațetele cerute: „rutină de păr" → `par`, „rutină de
dimineață" → `fata` + `routine_time=am`. Familie ambiguă ⇒ **o singură** întrebare de clarificare,
prin poarta de information gain existentă ([`clarification_policy.py`](../src/conversation/clarification_policy.py)),
nu o rutină ghicită.

---

## 6. Randarea — structura e a serverului, proza e a modelului

`RoutineStep` e azi `{title, detail}`. Nu poate purta un slot schimbabil. **Extindere aditivă**
(contractul v2 e încă OFF, deci ieftină acum și scumpă după cutoverul NX-249):

```python
class RoutineStep(_Base):
    title: Title                               # „1. Curățare"
    detail: Text | None = None                 # proza modelului, în vocea lui
    product: ProductItemView | None = None     # display-ready: „89,00 lei", nu 89.0
    actions: list[ActionView] = []             # butoanele SLOTULUI
    status: Literal["filled", "uncovered", "pinned"] = "filled"
```

Împărțirea e cea din NX-240, neschimbată: **modelul scrie proza, codul deține faptele.**
`grounding_guard` primește faptele fiecărui slot, deci o cifră inventată în proză pică exact ca
oriunde altundeva. `render_v2` rămâne funcție PURĂ: proiectează obiectul persistat, nu îl recalculează.

> **Consecință FE.** `RoutineBlock` e deja în registrul finit de 11 tipuri al rendererului pasiv
> (repo `Sales MVP Frontend Final`). Extinderea cere spec + FE, nu doar backend.

---

## 7. „Prea scump, ceva mai ieftin" — miezul

### 7.1 Înțelesul lui „mai ieftin" într-un slot

Într-o listă, „mai ieftin" e vag. Într-un slot de rutină e precis:

> același `family:step`, preț strict mai mic decât pick-ul curent, în stoc, trecând ACELEAȘI
> constrângeri hard + safety.

Aia e o interogare, nu un prompt. Și e exact motivul pentru care `refine_search` a fost rezervat cu
`available=False` în [`action_models.py:147`](../src/web/action_models.py): *„nu există încă o
rafinare DETERMINISTĂ server-side care să nu fie, de fapt, un prompt"*. Într-un slot, în sfârșit
există una.

### 7.2 Acțiunea

```python
"routine_swap_step": ActionSpec(
    "routine_swap_step", mutating=True,
    required=("routine_ref", "position", "filter"),   # filter ∈ REFINE_FILTERS
    stale_sensitive=True,   # un buton emis peste v2 nu are voie să mute v5
)
```

`stale_sensitive` nu e precauție: butonul depinde de STAREA rutinei, nu doar de id-uri explicite —
același criteriu prin care `show_more` îl are și `select_product` nu.

### 7.3 Cine alege produsul, și cine vorbește

**Decizie: click-ul produce un tur normal prin brain** (modelul explică schimbarea în vocea lui).
Asta NU înseamnă că modelul alege produsul. Seam-ul care le separă există deja: kernelul de acțiuni
rulează **înaintea triajului** ([`action_kernel.py:9`](../src/agent/action_kernel.py) — *„o acțiune
nu e o intenție de ghicit: e deja o decizie"*) și are două feluri de rezultat, dintre care al doilea
e exact ce ne trebuie:

```
click „mai ieftin" pe pasul 3
  ↓
action_service           token verificat, one-shot consumat, argumente canonice
  ↓
action_kernel            SWAP DETERMINIST: query pe slot, version+1, receipt scris
  ↓  Continue(input structurat)
brain                    primește rutina NOUĂ ca fapte și scrie proza despre ea
  ↓
render_v2                proiectează obiectul PERSISTAT
```

Deci: **produsul e ales de cod, schimbarea e narată de model.** Exact tiparul pe care NX-237 îl
folosește deja pentru `cart_*`. Modelul nu poate emite o rutină nouă, fiindcă rutina randată vine
din obiect, nu din `plan.selected_products`.

Riscul rămas — modelul rearanjează rutina în proză, deși obiectul n-a fost rearanjat — se închide
cu o poartă de plan: **pe un tur de rutină, `selected_products` trebuie să fie submulțime a
sloturilor rutinei active**, altfel `routine_drift`. E o linie în `validate_answer_plan_v2`, lângă
`routine_without_sequence`.

**Costul asumat:** un apel de model per click (clasa `mutation` din manifestul NX-241, plafon 8s).
Se măsoară prin `turn_latency` și prin numărul de swapuri per conversație.

### 7.4 Pinning — fără el, al doilea swap e enervant

Orice slot atins de client devine `pinned=True`. Recompunerea nu mișcă niciodată un slot pinuit.
Fără regula asta, un al doilea „ceva mai ieftin" pe alt pas ar putea re-alege tăcut primul, iar
clientul ar vedea cum i se anulează alegerea anterioară.

### 7.5 Când nu există nimic mai ieftin

**Butonul nu se emite deloc pe slotul ăla.** Headroom-ul (câți candidați strict mai ieftini,
vandabili, pe același pas) e deja calculat din pool la compunere, deci condiția e gratuită. Măsurat
pe catalogul SOLE (felia 1): pe `fata` mediana e **44** de alternative per slot și **zero** sloturi
fără niciuna, dar pe `machiaj` două sloturi (`pudra`, `ochi`) n-au nicio alternativă mai ieftină. Un
buton care nu poate face nimic e mai rău decât unul absent: clientul apasă și primește un refuz.

Dacă totuși se ajunge la un swap fără rezultat (catalog schimbat între emitere și click), slotul
rămâne neschimbat, `version` NU crește, iar răspunsul o spune direct. Textul determinist există deja
pentru cazul analog (`_CHEAPEST_ALREADY` din [`fallbacks.py:17`](../src/agent/fallbacks.py)),
adaptat la slot. Un swap care „reușește" înlocuind cu ceva egal sau mai scump e un eșec tăcut.

---

## 8. Calea de TEXT (nu de buton)

„asta e prea scumpă", scris. Referința trebuie să se rezolve la un **SLOT**, nu la un produs din
pool-ul de căutare.

[`reference_resolver.py`](../src/agent/reference_resolver.py) are deja precedență unică
(`action > named > ordinal > page > selected > single`). Rutina adaugă un DOMENIU:

> cât timp o rutină e activă, ordinalele („a doua", „ultimul pas") înseamnă **sloturi**, nu
> produse din ultima căutare.

E o intrare de precedență, nu un mecanism nou. Dar e o schimbare de comportament care cere test
dedicat, fiindcă azi „a doua" înseamnă altceva.

`_CHEAPER_RE` + rutină activă + referință rezolvată la slot ⇒ **aceeași** mutație ca butonul,
același receipt, același cod. Două căi de intrare, un singur drum de scriere (lecția NX-237).

---

## 9. Coșul

„Adaugă toată rutina" = N × `cart_add_line` prin `CartService`-ul existent, sub un singur receipt.
Poarta NX-240 se aplică neatinsă: token doar pentru produsele pe care guardul le declară vandabile.
Un slot neacoperit nu produce linie și **se spune** în confirmare.

## 10. Flag și rollout

`ROUTINE_ENABLED=false` (default) ⇒ byte-identic cu azi. Aprins, aprinde DOAR profilul `routine` +
unealta lui, nu toate cele 5 profile — restul traficului (`exact`/`recommend`/`compare`/`mutation`)
rămâne pe drumul de azi, deci aprinderea nu poate regresa ce merge acum.

Cere `SINGLE_BRAIN_ENABLED` (validat la boot, ca celelalte lanțuri de flaguri).

## 11. Matricea de eșec

| Situație | Ce se întâmplă |
|---|---|
| Familie ambiguă | o singură clarificare, prin poarta de gain |
| Pas fără candidat | slot `uncovered` + motiv, rutina continuă (P6) |
| Nimic mai ieftin | slot neschimbat, `version` neatins, se spune direct |
| Buton peste versiune veche | `action_stale`, re-randare cu rutina curentă |
| Dublu-click | un swap, același receipt, același răspuns |
| Produs devenit indisponibil | revalidare la citire (ca la coș), slot → `uncovered` |
| `routine_ref` pierdut la degradare | se spune onest, se oferă recompunerea |
| Model care rearanjează în proză | `routine_drift` → repair → fallback |
| Pachet fără `routine_steps` | capabilitatea lipsește, se cade pe `recommend` |

## 12. Out of scope

1. **Rutine salvate între conversații.** Rutina e a conversației, ca și coșul. Persistența pe
   contact cere consent și un loc în profil — alt card.
2. **Rutine `am`/`pm` simultane.** `routine_time` există ca fațetă (86,8% acoperire), dar două
   rutine active în paralel dublează suprafața de referință („a doua" din care?). O rutină activă
   la un moment dat.
3. **Rescrierea muchiilor `routine_next`.** Reprezentanții sunt o proprietate a datelor, nu a
   codului. Dacă ordinea lor se dovedește greșită (vezi NX-280, *Out of scope* punctul 1), se
   repară în job, nu aici.
4. **`refine_search` general.** Rămâne `available=False`. Rafinarea deterministă e validă în
   interiorul unui slot, nu peste o listă oarecare.

## 13. DoD măsurabil

- `compose_routine` pur, testat fără DB, pe pachetul REAL al tenantului;
- acoperire măsurată pe catalogul SOLE: câte familii dau o rutină completă, câte sloturi rămân
  neacoperite și **de ce** (raport, nu impresie);
- swap determinist verificat prin MUTAȚIE (lecția NX-173: o protecție inertă e teatru) — se
  verifică rândul din DB și `version`, nu doar textul;
- dublu-click ⇒ un singur swap (probă reproductibilă, ca `cart_receipt_recovery.py`);
- `routine_drift` prins de un test cu plan construit manual;
- ordinalele în domeniul rutinei, cu test care pinuiește și comportamentul VECHI în afara ei.
