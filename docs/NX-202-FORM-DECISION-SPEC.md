# Specul deciziei de formă — „când ce formă", NU un template de mesaj

**Status:** APROBAT ca principiu (Adi, 2026-07-24: „să nu fie template… să răspundă de la caz la
caz… conversația foarte naturală") · **Card:** NX-202 · **ADR:** D1 (forma = decizia agentului)

## Principiul

Bogăția de tip iZi (frază-cadru, carduri, educație, recomandare cu motiv, chips, comparație) este
o **TRUSĂ**, nu o rețetă. Botul scoate din trusă DOAR ce cere situația — exact ca un consultant
real, care nu ține același discurs la orice întrebare. **Aplicarea uniformă a structurii complete
la fiecare mesaj = template = robot deghizat.** Naturalețea ÎNSEAMNĂ variabilitate de formă.

Reconciliere cu planul „motorului" (Codex, 2026-07-24): pașii lui 1-6 (găsește adevărul → decide →
răspuns → formulare naturală → validare → experiență vizuală) sunt arhitectura corectă și rămân.
**Structura lui „răspuns complet în 7 părți" se citește ca MENIU al plannerului, nu ca rețetă** —
plannerul alege subsetul per caz. Criteriul lui final rămâne litera legii:
> „După răspuns, clientul fie poate lua decizia, fie știe exact ce informație mai lipsește."

## Formele (enum `form`, per caz golden și per decizie de planner)

| `form` | Când | Ce conține tipic | Ce NU conține |
|---|---|---|---|
| `short_fact` | „cât costă X?", „e pe stoc?" | o linie + eventual 1 acțiune | carduri multiple, educație |
| `clarify` | cerere ambiguă/vagă unde răspunsul schimbă categoria | O întrebare scurtă (+ chips cu opțiunile) | **ZERO carduri** înainte de clarificare |
| `recommendation` | cerere specifică cu potriviri reale | text natural + carduri (câte MERITĂ, nu un număr fix) + motiv + pas următor | educație nesolicitată, pick forțat |
| `alternative_disclosure` | zero potrivire exactă confirmabilă (fațete UNKNOWN) | cea mai apropiată alternativă, etichetată onest + ce nu putem confirma + ofertă de verificare | prezentarea alternativei ca potrivire exactă |
| `impossibility_tradeoff` | constrângerile nu pot fi satisfăcute simultan (confirmat din date) | onestitate + alternative cu compromisul EXPLICAT + întrebare de prioritizare | a pretinde că există; a ascunde compromisul |
| `safety_refusal` | context de siguranță (sarcină/contraindicații) | refuz blând + referral (medic/farmacist) + deschidere sigură | **carduri împinse**; claims de siguranță |
| `comparison` | „X sau Y?" | diferențele REALE (≥2, doar pe axe cu date la ambele) + verdict personal | preambul educativ; diferențe inventate |
| `multi_product_set` | rutină/trusă/set | produsele pe pași + total + ordine | produse care încalcă constrângerea comună |

Lungimea NU e regulă globală („2-3 fraze" e ABROGAT ca regulă): lungimea o dictează cazul.

## Reguli negative (grading — anti-template)

Evaluatorul verifică nu doar ce E în răspuns, ci și ce NU trebuie să fie:
1. `clarify` → zero carduri, zero produse ghicite înainte de întrebare.
2. `safety_refusal` → zero carduri de produs; zero „sigur în sarcină".
3. Query vag → NU se aruncă N produse „să fie".
4. Obiecție („mai ieftin", „prea mat") → se ADAPTEAZĂ păstrând ce conta; NU se reia discursul.
5. Educația apare DOAR când ajută alegerea sau e cerută („ce e niacinamida?"), nu ca umplutură.
6. Recomandarea fermă apare când datele o susțin; când clientul doar explorează, nu se forțează.
7. Două cazuri diferite nu produc răspunsuri identice ca formă dacă situațiile diferă.

## Separarea straturilor (cheia anti-template în date)

Per caz golden, straturile sunt SEPARATE și au proprietari diferiți:
- **`exemplar_reply`** — textul natural (vocea Adi). LOCKED: nu se rescrie, nu se „îmbogățește".
  Textul e mesajul; e deliberat VARIAT ca formă între cazuri.
- **`form`** — eticheta formei corecte pentru situație (enum de mai sus).
- **`expected_cards`** — payload de randare LÂNGĂ text (nu în el): ce produse apar pe carduri,
  cu `presented_as: exact|alternative` (o alternativă afișată ca exactă = încălcare). Gol prin
  DESIGN la `clarify`/`safety_refusal`.
- **`follow_up_suggestions`** — chips, formulate ca replici ale CLIENTULUI, secundare textului
  (text-first). Opționale.
- **`negative_checks`** — ce nu are voie să apară în acest caz.
- **`must_convey`** — checklist-ul factual (evaluator; poate fi tehnic).

Deci bogăția vizuală (carduri/chips) NU se obține rescriind textul în „experiență completă", ci
prin payload separat pe care frontend-ul îl randează lângă textul natural (contract existent:
FRONTEND-CONTRACT-IZI, text-first, chips secundare).

## Cine decide forma la runtime

**Agentul** (creier unic, D1) — pe baza QuerySpec + rezultate + context. NU un renderer fix.
Un pipeline care pune mereu carduri nu poate fi natural prin construcție. Match Gate/AnswerPlan
furnizează `match_class` (exact/alternative) → eticheta de pe card vine din date, nu din stil.

## Genericitate

Formele sunt AGNOSTICE de domeniu (beauty/electronice/HVAC): „clarify" la „vreau căști" e același
mecanism ca la „vreau un fond". Referințele cross-domain (conversațiile „Aria", beauty +
electronice) confirmă că tiparul ține de vânzare, nu de vertical. DomainPack schimbă catalogul
și vocabularul, nu formele.
