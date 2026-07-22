"""NX-194 — FAQ per produs, DERIVAT din fapte (6/produs). Modul PUR: fără DB, fără LLM, testabil.

Decizia userului: 6 întrebări per produs. 6 × 300 = 1.800 de răspunsuri — imposibil de scris de
mână, dar nici nu trebuie: majoritatea sunt proiecții ale faptelor pe care le avem deja
(gramaj, tip de ten/păr, moment de folosire, ingrediente, nuanțe, parfum, SPF).

Trei reguli fără de care FAQ-ul devine o problemă:

  1. **Niciun preț, stoc sau termen de livrare în răspuns.** Alea se schimbă zilnic; FAQ-ul e text
     static. „Costă 89 de lei" scris aici devine minciună peste o săptămână — prețul se citește din
     coloană, la runtime.
  2. **Verificarea de claim medical se face la INGESTION** (caller-ul), nu la runtime: un răspuns
     care ar fi tăiat de validator în conversație n-are ce căuta în DB ca afirmație a botului.
  3. **Regenerabile.** Cele derivate se recalculează din fapte la fiecare rulare; prima schimbare
     de gramaj ar lăsa altfel în urmă un FAQ care minte.

Întrebările sunt formulate ca ale CLIENTULUI („E bun pentru părul meu?"), nu ca titluri de secțiune
— așa arată un FAQ real și așa se potrivesc mai târziu dacă le pornim căutarea semantică.
"""

from __future__ import annotations

CONCERN_RO = {
    "hydration": "hidratare",
    "dry": "ten uscat",
    "oily": "ten gras",
    "sensitive": "ten sensibil",
    "combination": "ten mixt",
    "acne": "ten cu tendință acneică",
    "anti_aging": "ten matur",
    "hyperpigmentation": "ten cu pete",
    "normal": "ten normal",
}
USAGE_RO = {
    "morning": "dimineața",
    "evening": "seara",
    "daily": "zilnic",
    "occasional": "ocazional",
}
ROUTINE_RO = {
    "cleanse": "curățare",
    "tone": "tonifiere",
    "treat": "tratament",
    "moisturize": "hidratare",
    "protect": "protecție solară",
    "makeup_base": "bază de machiaj",
    "makeup_color": "machiaj de culoare",
    "finish": "fixare",
}
FINISH_RO = {"matte": "mat", "dewy": "luminos", "satin": "satinat", "natural": "natural"}
COVERAGE_RO = {"light": "lejeră", "medium": "medie", "full": "mare", "buildable": "modulabilă"}

HAIR_CATEGORIES = (
    "sampoane",
    "balsamuri-de-par",
    "masti-de-par",
    "uleiuri-pentru-par",
    "ingrijire-fara-clatire",
    "sampon-uscat",
)

#: 1-2 întrebări de JUDECATĂ per familie de categorii — singurele scrise de om. Se instanțiază pe
#: toate produsele familiei, nu se scriu per produs (80 de texte, nu 1.800).
FAMILY_FAQ: dict[str, tuple[tuple[str, str], ...]] = {
    "skincare": (
        (
            "Cum se aplică?",
            "Pe pielea curată, în strat subțire, înainte de produsele mai groase din rutină. "
            "Lasă-l să se absoarbă înainte de pasul următor.",
        ),
        (
            "Cum se păstrează?",
            "La temperatura camerei, ferit de lumina directă. Închide bine ambalajul "
            "după folosire.",
        ),
        (
            "Se poate folosi împreună cu retinol?",
            "Da, dacă le folosești în momente diferite ale zilei — retinolul seara, acesta "
            "dimineața. Introdu produsele noi pe rând, ca să vezi cum reacționează pielea.",
        ),
        (
            "În cât timp se văd rezultatele?",
            "Depinde de rutină și de tipul de piele. În general, produsele de îngrijire se "
            "evaluează după câteva săptămâni de folosire constantă, nu după primele zile.",
        ),
    ),
    "hair": (
        (
            "Cum se aplică?",
            "Se aplică pe lungimi și vârfuri, evitând rădăcina, apoi se lasă să acționeze un "
            "minut înainte de clătire.",
        ),
        (
            "Se poate folosi pe păr fin?",
            "Da, dar folosește o cantitate mică — altfel îl poate îngreuna.",
        ),
        (
            "Cât de des se folosește?",
            "Poate intra în rutina obișnuită de spălare. Dacă părul e fin, folosește o cantitate "
            "mică și evită zona rădăcinii, ca să nu îl îngreuneze.",
        ),
        (
            "E potrivit pentru păr vopsit?",
            "Da, poate fi folosit și pe păr vopsit. Pentru menținerea culorii, alternează-l cu un "
            "produs dedicat părului colorat.",
        ),
    ),
    "makeup": (
        (
            "Cum se aplică pentru un rezultat uniform?",
            "Aplică în strat subțire și construiește treptat, în loc să pui mult dintr-o dată. "
            "Estompează bine marginile.",
        ),
        (
            "Se poate folosi și pentru un machiaj de zi?",
            "Da — pentru zi folosește o cantitate mai mică; pentru seară poți intensifica peste "
            "același strat.",
        ),
        (
            "Rezistă toată ziua?",
            "Ține bine în condiții obișnuite. Pentru zile lungi sau căldură, fixează machiajul la "
            "final și retușează punctual unde e nevoie.",
        ),
        (
            "Se poate aplica peste cremă?",
            "Da — lasă crema să se absoarbă complet înainte, ca machiajul să stea uniform.",
        ),
    ),
    "body": (
        (
            "Se poate folosi zilnic?",
            "Da, e gândit pentru folosire zilnică. Aplică pe pielea încă ușor umedă, după duș, "
            "ca să rețină mai bine hidratarea.",
        ),
        (
            "E potrivit pentru piele sensibilă?",
            "Poate fi folosit și pe piele sensibilă. Dacă știi că reacționezi ușor, testează întâi "
            "pe o porțiune mică.",
        ),
        (
            "Cum se aplică?",
            "Se aplică pe piele curată, cu mișcări circulare, până se absoarbe. Insistă pe zonele "
            "mai uscate, cum sunt coatele și genunchii.",
        ),
        (
            "Cum se păstrează?",
            "La temperatura camerei, ferit de soare direct. Închide bine ambalajul după folosire.",
        ),
    ),
    "tool": (
        (
            "Pentru ce se folosește?",
            "E gândit pentru aplicarea și estomparea machiajului. Forma și densitatea firelor "
            "decid cât de precisă e aplicarea.",
        ),
        (
            "Fibrele sunt sintetice?",
            "Da, sunt fibre sintetice — potrivite atât pentru produse cremoase, cât și pentru "
            "pudre, și mai ușor de curățat.",
        ),
        (
            "Cât de des trebuie curățat?",
            "Pentru produsele cremoase, o dată pe săptămână; pentru pudre, la două săptămâni.",
        ),
        (
            "Cum se păstrează?",
            "Depozitează-l cu firele în sus sau pe orizontală, într-un loc uscat, ca să-și "
            "păstreze forma.",
        ),
        (
            "Cum se curăță?",
            "Spală cu apă călduță și un săpun delicat, clătește bine și lasă la uscat pe "
            "orizontală. O curățare la 1-2 săptămâni păstrează forma și aplicarea uniformă.",
        ),
    ),
}


def _family(cat: str, root: str) -> str:
    if cat == "pensule-si-bureti-de-machiaj":
        return "tool"
    if cat in HAIR_CATEGORIES or root == "ingrijirea-parului":
        return "hair"
    if root == "machiaj":
        return "makeup"
    if root in ("ingrijire-corp", "buze"):
        return "body"
    return "skincare"


def _net_content_label(p: dict) -> str | None:
    nc = (p.get("attributes") or {}).get("net_content")
    if isinstance(nc, dict) and nc.get("value") and nc.get("unit"):
        v = nc["value"]
        v = int(v) if isinstance(v, float) and v.is_integer() else v
        return f"{v} {nc['unit']}"
    for v_ in p.get("variants") or []:
        nc = v_.get("net_content")
        if isinstance(nc, dict) and nc.get("value") and nc.get("unit"):
            val = nc["value"]
            val = int(val) if isinstance(val, float) and val.is_integer() else val
            return f"{val} {nc['unit']}"
    return None


def derived_faqs(p: dict, root: str, routine_next: list[str] | None = None) -> list[dict]:
    """Întrebările compuse din fapte. Ordinea = prioritatea în care le-ar pune un client."""
    a = p.get("attributes") or {}
    out: list[dict] = []

    def add(q: str, ans: str) -> None:
        out.append({"question": q, "answer": ans, "source": "derived", "derived": True})

    # 1. potrivire — prima întrebare a oricărui client
    if a.get("hair_type"):
        add(
            "E potrivit pentru părul meu?",
            f"E gândit pentru păr {a['hair_type']}. Dacă ai alt tip de păr, îl poți folosi, dar "
            f"rezultatul cel mai bun îl dă pe cel pentru care a fost formulat.",
        )
    elif a.get("concerns"):
        ro = [CONCERN_RO[c] for c in a["concerns"] if c in CONCERN_RO]
        if ro:
            add(
                "E potrivit pentru tipul meu de ten?",
                f"E gândit pentru {', '.join(ro)}. Dacă tenul tău e altfel, spune-mi și îți "
                f"propun o variantă mai apropiată.",
            )

    # 2. compoziție
    ing = a.get("key_ingredients") or []
    if ing:
        add(
            "Ce conține?",
            f"Ingredientele-cheie sunt {', '.join(str(i) for i in ing)}. "
            f"Lista completă e pe pagina produsului.",
        )

    # 3. mod de folosire
    times = (a.get("usage") or {}).get("time") or []
    if times:
        ro = [USAGE_RO.get(t, t) for t in times]
        step = ROUTINE_RO.get(a.get("routine_step", ""), "")
        extra = f" În rutină e pasul de {step}." if step else ""
        add("Când se folosește?", f"Se folosește {', '.join(ro)}.{extra}")

    # 4. gramaj
    nc = _net_content_label(p)
    if nc:
        add("Ce cantitate are?", f"Ambalajul are {nc}.")

    # 5. nuanțe (doar unde există)
    variants = p.get("variants") or []
    labels = [v.get("label") for v in variants if v.get("label")]
    if len(labels) > 1:
        add(
            "Ce nuanțe are?",
            f"Sunt disponibile {len(labels)} variante: {', '.join(str(x) for x in labels)}. "
            f"Dacă nu știi care ți se potrivește, spune-mi ce cauți și te ajut să alegi.",
        )

    # 6. fațete de machiaj
    if a.get("finish"):
        fin = FINISH_RO.get(a["finish"], a["finish"])
        cov = COVERAGE_RO.get(a.get("coverage", ""), "")
        tail = f" Acoperirea e {cov}." if cov else ""
        add("Ce finish are?", f"Finishul este {fin}.{tail}")

    # 7. parfum — întrebare frecventă la ten sensibil
    if a.get("fragrance_free"):
        add("Are parfum?", "Nu, formula este fără parfum adăugat.")

    # 8. SPF
    if a.get("spf"):
        add(
            "Ce protecție solară oferă?",
            f"Are SPF {a['spf']}. La expunere prelungită la soare, reaplică pe parcursul zilei.",
        )

    # 9. ce se poartă cu el (din relațiile de rutină, dacă există)
    if routine_next:
        add(
            "Cu ce se combină?",
            f"Merge bine cu {', '.join(routine_next[:2])} — sunt pașii următori din rutină.",
        )

    return out


def build_faqs(
    p: dict, root: str, routine_next: list[str] | None = None, limit: int = 6
) -> list[dict]:
    """6 întrebări: derivatele întâi (sunt cele mai concrete), completate cu cele de familie.
    Fără duplicate de întrebare; poziția e ordinea finală."""
    faqs = derived_faqs(p, root, routine_next)
    seen = {f["question"] for f in faqs}
    fam = _family(p.get("primaryCategorySlug", ""), root)
    for q, ans in FAMILY_FAQ.get(fam, ()):
        if len(faqs) >= limit:
            break
        if q in seen:
            continue
        seen.add(q)
        faqs.append({"question": q, "answer": ans, "source": "curated", "derived": False})
    for i, f in enumerate(faqs[:limit]):
        f["position"] = i
    return faqs[:limit]
