# Prompt de predare — „Deep Dive pe înțeles" (motorul masterclass-ului)

> Ce e ăsta: șablonul (prompt-ul) pe care îl aplic la FIECARE componentă (fișier / funcție / caz) când
> aprofundez codul. Garantează că explicația e mereu: pe înțeles, cu exemplu concret la fiecare caz, cu
> „de ce așa" ÎNAINTE de „cum". Îl poți refolosi în orice sesiune sau da altui dev.
>
> **Cum se folosește:** „Aplică PROMPT-ul de predare pe `<fișier:funcție>`" → primești explicația în formatul
> de mai jos. Se aplică la o funcție, un bloc de decizie, sau un fișier întreg (pe rând, funcție cu funcție).

---

## Reguli de ton (obligatorii)

1. **Limbaj simplu întâi, jargon după** — orice termen tehnic (idempotent, RLS, embedding, RRF) e explicat în
   paranteză prima dată când apare, cu o vorbă de zi cu zi.
2. **Analogie din viața reală** la fiecare componentă — o metaforă de 1-2 propoziții (magazin, restaurant, poștă).
3. **DE CE înainte de CUM** — întâi problema pe care o rezolvă, apoi codul.
4. **Exemplu CONCRET la FIECARE caz** — pentru orice ramură/if/decizie: *input real → ce se întâmplă → output real*.
   Exemplele folosesc magazinul demo (seruri, vitamina C, ten gras, sub 100 lei) ca să fie palpabile.
5. **Cod real, nu pseudocod** — citate din fișier cu `fișier:linie`. Fiecare linie ne-evidentă, explicată.
6. **Fără să sar peste raționament** — dacă ceva pare arbitrar, explic trade-off-ul (ce alternativă a fost respinsă).
7. **Leagă de diagramă** — spun ce nod din `ARCHITECTURE-WORKFLOWS.md` corespunde.
8. **Junior-friendly** — presupun că nu știi codebase-ul. Nimic „evident".

---

## Structura fixă a fiecărui Deep Dive (9 secțiuni)

Când aprofundez o componentă, produc EXACT aceste 9 secțiuni, în ordine:

### 0. 📍 Unde suntem
- `fișier:linie` + numele funcției.
- Ce stagiu / subsistem. Ce nod din care diagramă.
- Ce primește (input) și ce lasă în urmă (ce câmp din `TurnContext` scrie / ce efect are).

### 1. 🎭 Analogia
- O metaforă de zi cu zi (1-2 propoziții) care surprinde esența. Reperul mental la care te întorci.

### 2. ❓ DE CE există (problema)
- Ce s-ar strica / ce ar fi greșit fără componenta asta. Problema concretă pe care o rezolvă.
- Dacă a fost adăugată ca fix la un bug real, spun care (NX-XXX / PR).

### 3. ⚙️ CUM funcționează (codul, bucată cu bucată)
- Codul real, împărțit în felii mici. Fiecare felie: ce face, în cuvinte simple.
- Variabilele cheie: ce conțin, de unde vin.

### 4. 🔀 FIECARE caz, cu exemplu concret
- Pentru fiecare ramură/decizie (if/elif/else, fiecare condiție dintr-un guard):
  - **Condiția:** ce se verifică, în română.
  - **Exemplu de intrare:** un mesaj/stare concretă.
  - **Ce se întâmplă:** pas cu pas.
  - **Ieșirea:** ce primește clientul / ce se schimbă în state/DB.
- Tabel când sunt multe cazuri.

### 5. 🧠 DE CE așa (și nu altfel)
- Trade-off-urile. Ce design alternativ ar fi părut mai simplu și de ce a fost respins.
- Legătura cu principiile (1-12) din CLAUDE.md.

### 6. 💥 CE-AR FI DACĂ (îl strici)
- Dacă ștergi componenta → ce se rupe (scenariu concret).
- Dacă o modifici greșit (schimbi o condiție, muți o linie) → ce bug apare, cu exemplu.

### 7. 🐛 Bug-uri comune + cum depanezi
- Simptome tipice. Ce `grep`-uiești în log. Unde pui breakpoint. Ce variabile inspectezi.

### 8. ✅ Test rapid (fără răspuns)
- 1-3 întrebări (grilă / „ce se întâmplă dacă" / „găsește bug-ul") pe care le rezolvi tu, apoi te corectez.

---

## Progresia (ordinea de aprofundare)

Aplicăm prompt-ul pe componente în ordinea în care execuția le atinge, de la simplu la complex:

1. **`agent.py`** — cele 3 faze (PRE-loop intenții deterministe → tool loop → POST-loop compunere + validator),
   funcție cu funcție.
2. **`catalog_tools.py`** — căutarea hibridă (moștenire sesiune → scara de relaxare → lexical+semantic → fuse →
   diversify → dedupe).
3. **`compose.py`** — grounding-ul (membership → scrub câmp-cu-câmp → medical → badge → pick → chips).
4. (opțional, la cerere) orice alt fișier: `dispatcher.py`, `commerce_tools.py`, `web/app.py`, etc.

După fiecare funcție aprofundată: un test rapid. La finalul fiecărui fișier: un recap + o schemă mentală.

---

## Exemplu de aplicare (format de referință)

Vezi mai jos o aplicare completă a prompt-ului pe `_handle_link_intent` (`agent.py:894`) — așa arată fiecare
Deep Dive. Dacă formatul e bun, îl rulez identic peste tot restul.
