"""NX-196 — compune fișa de produs COMPLETĂ din faptele catalogului + biblioteca scrisă de om.

Ținta (fișa de tip La Roche-Posay, tradusă în câmpurile noastre):

    short_description   150-250 car.
    description         1.500-4.000 car., narativ, cu subtitluri
    sections(features)  7 bullets
    sections(benefits)  4-6 bullets
    sections(usage)     mod de aplicare concret
    sections(scenarios) 5 situații „pentru cine și când"
    attributes.specs    key-value (Volum, Textură, Tip ten, SPF, Fără, …)

DETERMINIST și FĂRĂ LLM: textul vine din `content_library` + `content_categories` (scrise de om),
asamblat după faptele produsului. Aceeași intrare → același text, deci fișierul de catalog e
diff-abil în PR și trece o poartă de audit ca orice cod.

Varietatea e reală, nu lexicală: paragraful de ingredient e al ingredientului pe care produsul
CHIAR îl are, paragraful de nevoie e al nevoii lui, scenariile sunt ale categoriei lui. Introducerea
se alege dintre variantele categoriei după hash-ul slug-ului, ca două produse din aceeași categorie
să nu înceapă identic.

PORȚI (rulează în `validate`, nu la runtime):
  • zero claim medical în blocurile `voice='assistant'` (`has_medical_claim`);
  • zero preț/stoc/livrare — cifre volatile care ar deveni minciună în câteva zile;
  • lungimi în contract.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.content_categories import CATEGORY  # noqa: E402
from scripts.content_library import (  # noqa: E402
    CONCERN_PARA,
    HAIR_PARA,
    TEXTURE_PARA,
    USAGE_PARA,
    ingredient_para,
)

MIN_DESCRIPTION = 1500
MAX_DESCRIPTION = 4000
MIN_SHORT = 150
MAX_SHORT = 250

#: cuvinte care NU au voie în text — se schimbă zilnic, deci ar deveni false
VOLATILE = re.compile(r"\b(lei|ron|reducere|promo[țt]ie|stoc|livrare|livrăm|gratuit)\b", re.I)

CONCERN_RO = {
    "hydration": "hidratare",
    "dry": "ten uscat",
    "oily": "ten gras",
    "sensitive": "ten sensibil",
    "combination": "ten mixt",
    "acne": "ten predispus la imperfecțiuni",
    "anti_aging": "ten matur",
    "hyperpigmentation": "ten cu pete",
    "normal": "ten normal",
}
CONCERN_RO_CORP = {
    "hydration": "hidratare",
    "dry": "piele uscată",
    "sensitive": "piele sensibilă",
    "normal": "piele normală",
}
CONCERN_RO_BUZE = {
    "hydration": "hidratare",
    "dry": "buze uscate",
    "sensitive": "buze sensibile",
    "normal": "îngrijire zilnică",
}
FINISH_RO = {"matte": "mat", "dewy": "luminos", "satin": "satinat", "natural": "natural"}
COVERAGE_RO = {"light": "lejeră", "medium": "medie", "full": "mare", "buildable": "modulabilă"}
USAGE_RO = {
    "morning": "dimineața",
    "evening": "seara",
    "daily": "zilnic",
    "occasional": "ocazional",
}


def _h(key: str, salt: str = "") -> int:
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:8], 16)


def _join_ro(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " și " + items[-1]


def _concern_vocab(zone: str) -> dict[str, str]:
    return {"corp": CONCERN_RO_CORP, "buze": CONCERN_RO_BUZE}.get(zone, CONCERN_RO)


def _audience(a: dict, zone: str) -> str:
    """„Pentru cine" — construit din faptele produsului, în vocabularul ZONEI de aplicare."""
    if a.get("hair_type"):
        return f"păr {a['hair_type']}"
    vocab = _concern_vocab(zone)
    ro = [vocab[c] for c in (a.get("concerns") or []) if c in vocab]
    if ro:
        return _join_ro(ro)
    if a.get("finish"):
        return f"cine caută un finish {FINISH_RO.get(a['finish'], a['finish'])}"
    return ""


#: închiderea fișei, pe zona de aplicare — ultimul paragraf, cel care rămâne în minte
ZONE_CLOSING = {
    "ten": (
        "O rutină de îngrijire funcționează prin repetiție, nu prin intensitate. Un produs "
        "folosit constant câteva săptămâni spune mai multe despre potrivirea lui decât primele "
        "două aplicări — iar dacă pielea reacționează, pauza de câteva zile e mereu o opțiune "
        "mai bună decât insistența."
    ),
    "par": (
        "Părul răspunde lent: schimbările se văd după câteva spălări, nu după prima. Aplicarea "
        "pe lungimi și vârfuri, cu o cantitate mică, dă de obicei rezultate mai bune decât "
        "produsul folosit din abundență la rădăcină."
    ),
    "corp": (
        "Pielea corpului se îngrijește cel mai bine prin obicei: aplicarea imediat după duș, pe "
        "pielea încă umedă, face mai mult decât orice formulă folosită din când în când."
    ),
    "buze": (
        "Buzele cer reaplicare, nu produse complicate. Un strat aplicat seara, înainte de culcare, "
        "e cel mai eficient moment din toată ziua."
    ),
    "machiaj": (
        "Machiajul arată cel mai bine peste o piele pregătită: hidratarea absorbită complet și "
        "straturile subțiri, construite unde e nevoie, fac diferența mai mult decât produsul în "
        "sine. Iar la final, estomparea rezolvă aproape orice."
    ),
}


#: sfaturi de aplicare pe zonă — blocul care ține loc de „ce ar spune un consultant la raft".
#: Contează mai ales la machiaj și la unelte, unde faptele tehnice sunt puține și textul ar
#: rămâne subțire fără el.
ZONE_TIPS = {
    "ten": (
        "Ordinea în rutină e simplă: de la texturile cele mai apoase spre cele mai bogate. "
        "Produsele noi se introduc pe rând, la câteva zile distanță, ca să știi ce ți-a priit "
        "și ce nu. Iar dacă pielea se irită, pauza de trei-patru zile rezolvă de obicei mai mult "
        "decât un produs calmant adăugat peste."
    ),
    "par": (
        "Cantitatea contează mai mult decât produsul: majoritatea oamenilor folosesc de două ori "
        "mai mult decât e nevoie. Începe cu puțin, distribuie pe lungimi cu palmele și adaugă "
        "doar dacă simți că nu ajunge. Apa prea fierbinte la clătire anulează jumătate din efect."
    ),
    "corp": (
        "Momentul aplicării face diferența: pe pielea încă umedă, produsul se absoarbe mult mai "
        "bine și hidratarea ține mai mult. Zonele care se usucă cel mai tare — coate, genunchi, "
        "călcâie — merită un strat în plus, aplicat seara."
    ),
    "buze": (
        "Buzele se exfoliază singure dacă sunt hidratate constant; frecarea agresivă face mai mult "
        "rău decât bine. Un strat aplicat seara și reaplicarea în timpul zilei rezolvă aproape "
        "orice problemă de uscăciune."
    ),
    "machiaj": (
        "Trei lucruri schimbă rezultatul mai mult decât produsul ales: pielea pregătită dedesubt, "
        "straturile subțiri construite treptat și estomparea marginilor. Un produs mediu aplicat "
        "corect arată mai bine decât unul scump aplicat în grabă. Iar uneltele curate fac "
        "diferența dintre o aplicare uniformă și una în pete."
    ),
}


#: „cum alegi" pe zonă — se adaugă doar acolo unde faptele n-au fost destule pentru contract.
#: E tot text scris, nu umplutură: răspunde la întrebarea reală a cuiva care compară două produse.
ZONE_CHOOSE = {
    "machiaj": (
        "Când alegi între două produse asemănătoare, uită-te la trei lucruri: finishul (mat ține "
        "mai mult, luminos arată mai proaspăt), cât de mult se poate construi culoarea și cât de "
        "ușor se estompează. Un produs care se estompează bine iartă greșelile de aplicare, iar "
        "asta contează mai mult decât pigmentarea maximă. Nuanța se testează pe linia maxilarului, "
        "la lumină naturală — nu pe dosul palmei, unde pielea are alt ton."
    ),
    "ten": (
        "Când compari două produse din aceeași categorie, ingredientul-erou și textura spun mai "
        "mult decât prețul. Textura decide dacă îl vei folosi zilnic; ingredientul decide ce "
        "schimbă în timp. Un produs pe care îl aplici constant bate un produs mai bun uitat în "
        "sertar."
    ),
    "par": (
        "Alegerea se face după tipul de păr, nu după promisiunea de pe ambalaj. Părul fin cere "
        "formule ușoare, cel deteriorat cere proteine și lipide, iar cel vopsit cere blândețe la "
        "spălare. Restul e preferință de textură și de parfum."
    ),
    "corp": (
        "Diferența dintre produsele de corp o face textura: cele ușoare se absorb repede și sunt "
        "bune pentru dimineață, cele bogate lucrează mai bine seara. Iar ce folosești zilnic "
        "contează mai mult decât ce folosești o dată pe lună. Parfumul e al doilea criteriu, dar "
        "nu unul minor: dacă mirosul nu-ți place, produsul rămâne în dulap indiferent cât de bună "
        "e formula. Iar pe pielea reactivă, o variantă fără parfum e aproape mereu alegerea mai "
        "sigură — mai ales pentru zonele care se rad sau se epilează."
    ),
    "buze": (
        "Un balsam bun se judecă după cât de des îl reaplici: dacă simți nevoia din oră în oră, "
        "probabil hidratează superficial. Formulele cu unturi și ceruri țin mai mult, dar se simt "
        "mai gros pe buze — alege în funcție de ce te deranjează mai puțin."
    ),
}


def _sentence(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    return t if t.endswith((".", "!", "?")) else t + "."


def build_description(p: dict) -> str:
    """Descrierea lungă, narativă, cu subtitluri — ca într-o fișă de magazin serioasă."""
    a = p.get("attributes") or {}
    cat = p.get("primaryCategorySlug", "")
    block = CATEGORY.get(cat)
    if not block:
        return ""
    zone = block["zone"]
    slug = p.get("slug", "")
    name = p.get("name", "")

    parts: list[str] = []

    # 1. deschidere SPECIFICĂ produsului + introducerea de categorie.
    #    Fraza de deschidere garantează că două produse din aceeași categorie nu încep identic —
    #    testul pe catalogul real a prins că, doar cu variantele de categorie, o categorie întreagă
    #    putea nimeri aceeași introducere.
    intros = block["intro"]
    aud_open = _audience(a, zone)
    # liniuță, nu „este X" — rolurile sunt sintagme („pas concentrat de îngrijire", „hidratare
    # și protecție"), iar articolul corect ar diferi de la una la alta
    opener = f"{name} — {block['role']}"
    if aud_open:
        opener += f", pentru {aud_open}"
    parts.append(
        _sentence(opener[0].upper() + opener[1:]) + " " + intros[_h(slug, "intro") % len(intros)]
    )

    # 2. ce face — nevoia adresată + beneficiul declarat pe produs
    need = ""
    if a.get("hair_type") and a["hair_type"] in HAIR_PARA:
        need = HAIR_PARA[a["hair_type"]]
    else:
        for c in a.get("concerns") or []:
            if c in CONCERN_PARA:
                need = CONCERN_PARA[c]
                break
    kb = _sentence(a.get("key_benefit") or "")
    if need or kb:
        body = " ".join(
            x
            for x in (
                need,
                f"{name} vine în întâmpinarea acestei nevoi: {kb.lower()}" if kb else "",
            )
            if x
        )
        parts.append("**Ce face**\n" + body)

    # 3. ingredientele-erou — paragraf pentru fiecare, dacă îl avem scris
    ings = [i for i in (a.get("key_ingredients") or []) if ingredient_para(i)]
    if ings:
        title = f"**{str(ings[0]).capitalize()}**" if len(ings) == 1 else "**Ingredientele-cheie**"
        paras = [ingredient_para(i) for i in ings[:2]]
        parts.append(title + "\n" + " ".join(x for x in paras if x))

    # 4. textură și mod de folosire
    tex = TEXTURE_PARA.get(a.get("texture") or "")
    times = (a.get("usage") or {}).get("time") or []
    use = " ".join(USAGE_PARA[t] for t in times if t in USAGE_PARA)
    if tex or use:
        parts.append("**Textură și folosire**\n" + " ".join(x for x in (tex, use) if x))

    # 5. fațete de machiaj (acolo unde textura/ingredientele nu au ce spune)
    mk = []
    if a.get("finish"):
        mk.append(
            f"Finishul {FINISH_RO.get(a['finish'], a['finish'])} decide cum se vede rezultatul "
            f"în lumină naturală."
        )
    if a.get("coverage"):
        mk.append(
            f"Acoperirea este {COVERAGE_RO.get(a['coverage'], a['coverage'])} și se poate "
            f"construi în straturi, în funcție de cât vrei să uniformizezi."
        )
    if a.get("spf"):
        mk.append(
            f"Protecția solară inclusă (SPF {a['spf']}) acoperă expunerea obișnuită de peste zi; "
            f"la soare puternic, reînnoiește aplicarea."
        )
    if mk:
        parts.append("**Ce obții**\n" + " ".join(mk))

    # 6. pentru cine
    aud = _audience(a, zone)
    if aud:
        parts.append(
            "**Pentru cine**\n"
            f"Formula e gândită pentru {aud}. Dacă profilul tău e diferit, produsul rămâne "
            f"folosibil, dar rezultatul cel mai bun îl dă acolo unde a fost gândit."
        )

    # 7. cum se integrează în rutină
    parts.append(f"**În rutină**\n{block['how']} Rolul lui: {block['role']}.")

    # 8. când îl folosești — scenariile categoriei, ca proză. Bloc PERMANENT, nu plasă de lungime:
    #    e exact informația căutată de cineva care compară două produse și nu știe ce să aleagă.
    parts.append("**Când îl folosești**\n" + " ".join(_sentence(s) for s in block["scenarios"]))

    # 8b. cealaltă perspectivă scrisă pentru categorie — ambele introduceri sunt scrise, așa că
    #     folosirea celei nealese ca paragraf separat aduce conținut real, nu reformulare.
    if len(intros) > 1:
        other = intros[(_h(slug, "intro") + 1) % len(intros)]
        parts.append("**De ce contează**\n" + other)

    # 8c. sfaturi de aplicare pe zonă
    parts.append("**Sfaturi de aplicare**\n" + ZONE_TIPS.get(zone, ZONE_TIPS["ten"]))

    # 9. particularitățile categoriei
    if block.get("features"):
        parts.append("**Bine de știut**\n" + " ".join(_sentence(f) for f in block["features"]))

    # 10. închiderea pe zona de aplicare — ultimul paragraf, cel care rămâne în minte
    parts.append("**De reținut**\n" + ZONE_CLOSING.get(zone, ZONE_CLOSING["ten"]))

    text = "\n\n".join(parts)
    # categoriile sărace în fapte (unelte, machiaj de culoare, ochi) primesc și blocul „cum alegi",
    # ca fișa lor să rămână la fel de utilă ca a unui produs cu ingrediente declarate
    if len(text) < MIN_DESCRIPTION:
        text += "\n\n**Cum alegi**\n" + ZONE_CHOOSE.get(zone, ZONE_CHOOSE["ten"])
    return text[:MAX_DESCRIPTION]


def build_short(p: dict) -> str:
    """1-2 propoziții: ce e, pentru cine, cu ce. 150-250 caractere."""
    a = p.get("attributes") or {}
    cat = p.get("primaryCategorySlug", "")
    block = CATEGORY.get(cat)
    if not block:
        return p.get("shortDescription") or ""
    zone = block["zone"]
    name = p.get("name", "")
    aud = _audience(a, zone)
    ings = [str(i) for i in (a.get("key_ingredients") or [])][:2]

    lead = f"{name} — {block['role']}"
    if aud:
        lead += f", pentru {aud}"
    lead = _sentence(lead)
    tail = ""
    if ings:
        tail = f"Formula are în prim-plan {_join_ro(ings)}."
    elif a.get("finish"):
        tail = f"Finish {FINISH_RO.get(a['finish'], a['finish'])}, ușor de estompat."
    out = (lead + " " + tail).strip()
    extras = (
        [a.get("key_benefit") or ""] + list(block.get("features", [])) + [block["scenarios"][0]]
    )
    for extra in extras:
        if len(out) >= MIN_SHORT:
            break
        if extra and _sentence(extra) not in out:
            out = (out + " " + _sentence(extra)).strip()
    return out.strip()[:MAX_SHORT]


def build_specs(p: dict) -> dict:
    """Technical Specifications, în format key-value — exact blocul din fișa-model."""
    a = p.get("attributes") or {}
    specs: dict[str, str] = {}
    nc = a.get("net_content") or {}
    if not nc:
        for v in p.get("variants") or []:
            if v.get("net_content"):
                nc = v["net_content"]
                break
    if nc.get("value") and nc.get("unit"):
        val = nc["value"]
        val = int(val) if isinstance(val, float) and float(val).is_integer() else val
        specs["Volum"] = f"{val} {nc['unit']}"
    if a.get("texture"):
        specs["Textură"] = str(a["texture"])
    zone = (CATEGORY.get(p.get("primaryCategorySlug", "")) or {}).get("zone", "ten")
    if a.get("hair_type"):
        specs["Tip de păr"] = str(a["hair_type"])
    elif a.get("concerns"):
        vocab = _concern_vocab(zone)
        vals = [vocab[c] for c in a["concerns"] if c in vocab]
        if vals:
            specs["Potrivit pentru"] = _join_ro(vals)
    if a.get("spf"):
        specs["SPF"] = str(a["spf"])
    if a.get("finish"):
        specs["Finish"] = FINISH_RO.get(a["finish"], str(a["finish"]))
    if a.get("coverage"):
        specs["Acoperire"] = COVERAGE_RO.get(a["coverage"], str(a["coverage"]))
    if a.get("key_ingredients"):
        specs["Ingrediente-cheie"] = _join_ro([str(i) for i in a["key_ingredients"]][:4])
    times = (a.get("usage") or {}).get("time") or []
    if times:
        specs["Moment de folosire"] = _join_ro([USAGE_RO.get(t, t) for t in times])
    if a.get("fragrance_free"):
        specs["Fără"] = "parfum adăugat"
    if p.get("variants"):
        specs["Variante disponibile"] = str(len(p["variants"]))
    return specs


def build_features(p: dict) -> list[str]:
    """Key Features — 7 bullets, toate susținute de un fapt din catalog."""
    a = p.get("attributes") or {}
    block = CATEGORY.get(p.get("primaryCategorySlug", "")) or {}
    out: list[str] = []
    if a.get("key_benefit"):
        out.append(str(a["key_benefit"]).rstrip("."))
    if a.get("key_ingredients"):
        out.append("Cu " + _join_ro([str(i) for i in a["key_ingredients"]][:3]))
    if a.get("texture"):
        out.append(f"Textură {a['texture']}")
    if a.get("finish"):
        out.append(f"Finish {FINISH_RO.get(a['finish'], a['finish'])}")
    if a.get("coverage"):
        out.append(f"Acoperire {COVERAGE_RO.get(a['coverage'], a['coverage'])}")
    if a.get("spf"):
        out.append(f"Protecție solară SPF {a['spf']}")
    if a.get("fragrance_free"):
        out.append("Fără parfum adăugat")
    specs = build_specs(p)
    if specs.get("Volum"):
        out.append(f"Ambalaj de {specs['Volum']}")
    for f in block.get("features", []):
        if len(out) >= 7:
            break
        out.append(f)
    # dedupe păstrând ordinea
    seen: set[str] = set()
    uniq = [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]
    return uniq[:7]


def build_benefits(p: dict) -> list[str]:
    """Benefits — de ce l-ai alege. Din nevoile adresate + rolul categoriei."""
    a = p.get("attributes") or {}
    block = CATEGORY.get(p.get("primaryCategorySlug", "")) or {}
    zone = block.get("zone", "ten")
    vocab = _concern_vocab(zone)
    out: list[str] = []
    if a.get("key_benefit"):
        out.append(str(a["key_benefit"]).rstrip("."))
    for c in (a.get("concerns") or [])[:3]:
        if c in vocab:
            out.append(f"Gândit pentru {vocab[c]}")
    if a.get("hair_type"):
        out.append(f"Potrivit pentru păr {a['hair_type']}")
    if block.get("role"):
        out.append(f"Acoperă pasul de {block['role']}")
    if a.get("fragrance_free"):
        out.append("Fără parfum adăugat — o alegere mai sigură pentru pielea reactivă")
    seen: set[str] = set()
    uniq = [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]
    return uniq[:6]


def build_sections(p: dict) -> list[dict]:
    """Blocurile de conținut, cu `voice`. Tot ce e compus din fapte e `assistant`; nu producem
    text de producător, deci nu apare `brand` decât dacă îl adaugă cineva explicit."""
    block = CATEGORY.get(p.get("primaryCategorySlug", ""))
    if not block:
        return []
    feats = build_features(p)
    bens = build_benefits(p)
    out = [
        {
            "kind": "features",
            "title": "Pe scurt",
            "voice": "assistant",
            "body": "\n".join(f"• {f}" for f in feats),
        },
        {
            "kind": "benefits",
            "title": "De ce să-l alegi",
            "voice": "assistant",
            "body": "\n".join(f"• {b}" for b in bens),
        },
        {
            "kind": "usage",
            "title": "Cum se folosește",
            "voice": "assistant",
            "body": block["how"],
        },
        {
            "kind": "scenarios",
            "title": "Pentru cine și când",
            "voice": "assistant",
            "body": "\n".join(f"• {s}" for s in block["scenarios"]),
        },
    ]
    return out


def compose(p: dict, medical_check=None) -> dict | None:
    """Fișa completă pentru un produs. None dacă nu avem bloc de categorie scris.

    `medical_check` (has_medical_claim) e opțional și, dacă e dat, CURĂȚĂ: un `key_benefit` din
    catalog care declanșează poarta („Tratează imperfecțiunile" lângă un nume care conține
    „Tratament") e scos din textul afirmabil de bot. Faptul rămâne în catalog — doar nu-l rostim
    noi ca afirmație proprie."""
    if p.get("primaryCategorySlug") not in CATEGORY:
        return None
    out = {
        "shortDescription": build_short(p),
        "description": build_description(p),
        "sections": build_sections(p),
        "specs": build_specs(p),
    }
    if medical_check is None:
        return out

    risky = medical_check(out["shortDescription"]) or any(
        s["voice"] == "assistant" and medical_check(s["body"]) for s in out["sections"]
    )
    if risky:
        stripped = dict(p)
        attrs = dict(p.get("attributes") or {})
        attrs.pop("key_benefit", None)
        stripped["attributes"] = attrs
        out["shortDescription"] = build_short(stripped)
        out["sections"] = build_sections(stripped)
        out["description"] = build_description(stripped)
    return out


def validate(p: dict, content: dict, medical_check) -> list[str]:
    """Porțile de conținut. `medical_check` e injectat (has_medical_claim) ca modulul să rămână
    pur și testabil fără importuri din `src`."""
    problems: list[str] = []
    slug = p.get("slug", "?")
    d, s = content["description"], content["shortDescription"]
    if not (MIN_DESCRIPTION <= len(d) <= MAX_DESCRIPTION):
        problems.append(
            f"{slug}: description are {len(d)} car. (cerut {MIN_DESCRIPTION}-{MAX_DESCRIPTION})"
        )
    if not (MIN_SHORT <= len(s) <= MAX_SHORT):
        problems.append(
            f"{slug}: short_description are {len(s)} car. (cerut {MIN_SHORT}-{MAX_SHORT})"
        )
    texts = [d, s] + [sec["body"] for sec in content["sections"]]
    for t in texts:
        hit = VOLATILE.search(t)
        if hit:
            problems.append(f"{slug}: text cu cifre volatile: «{hit.group()}»")
            break
    for sec in content["sections"]:
        if sec["voice"] == "assistant" and medical_check(sec["body"]):
            problems.append(f"{slug}: secțiunea «{sec['kind']}» face un claim medical")
    if medical_check(s):
        problems.append(f"{slug}: short_description face un claim medical")
    return problems
