"""Stagiul 6 (logică) — Context builder: pregătește contextul conversației pentru
prompturile LLM (triaj + agent), cu BUGET impus în cod (principiul 4).

Istoricul e deja încărcat în `ctx.history` de processor (max 8 mesaje, cel mai
recent ultimul — INCLUSIV mesajul curent). Aici îl formatăm compact + bugetat, plus
blocuri de **rezumat de conversație** (`conversation_summaries`, felia 2), **profil client**
(`contacts.profile`) și **state references** (produse arătate + constrângeri, principiul 8).
Rezumatul e DOAR citit aici (din `ctx.summary`, seedat de processor) — generarea lui rulează
post-tur async (vezi `src.worker.summarizer`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.config import get_settings
from src.db.queries.messages import HISTORY_LIMIT
from src.models import Direction, Message
from src.privacy import make_safe
from src.web.localization import amount_text

if TYPE_CHECKING:
    from src.models import Contact, ConversationState, TurnContext


def conversation_transcript(
    history: list[Message],
    *,
    max_turns: int = 6,
    max_chars: int = 1200,
    emit: Callable[..., None] | None = None,
    consumer: str | None = None,
) -> str:
    """Transcript compact „Client/Asistent" al mesajelor ANTERIOARE (fără cel curent
    — ultimul din `history` e mesajul în curs de procesare). Gol dacă nu există
    context anterior. Bugetat: ultimele `max_turns` mesaje.

    NX-230 — REDACTARE LA CITIRE. De aici încolo mesajele se scriu deja redactate (frontiera din
    `processor.handle_turn`), dar rândurile SCRISE ÎNAINTE de cardul ăsta sunt brute și nu dispar
    singure. Fără trecerea asta, exact bucla pe care o repară cardul ar supraviețui în datele
    vechi: un telefon scris luna trecută s-ar întoarce în promptul de mâine.

    Redactarea la citire e ieftină și idempotentă — un text deja redactat trece neschimbat, deci
    nu plătim de două ori pentru rândurile noi. E și plasa care ține dacă vreun drum de scriere
    scapă neconvertit: istoricul e ultimul loc de dinaintea promptului.

    NX-255 — cu `structured_history_enabled` (ON implicit) se schimbă DOUĂ lucruri (flag stins ⇒
    byte-identic cu tăierea veche):

    1. **Nicio tăiere de caractere.** Vechiul `[-max_chars:]` pe stringul unit era orb: nu știa de
       rol, de granițe de mesaj sau de cuvânt. Clientul scrie ~12-28 de caractere, botul
       1.200-1.600, deci tăierea de coadă arunca exact întrebările (ieftine, esențiale) ca să
       păstreze coada prozei, și tăia la mijlocul cuvântului. Fereastra încărcată
       (`HISTORY_LIMIT`) rămâne singura margine.
    2. **Turul botului poartă și ce a ARĂTAT** (`messages.payload.shown`, NX-255), cu vechimea în
       ture. Proza rămâne INTEGRALĂ, deliberat: modelul care își recitește propriul răspuns bun
       continuă la același nivel, iar asta întărește `VOICE_RULES` cu exemple reale în locul unei
       reguli abstracte. Separarea rolurilor e regula care face păstrarea prozei sigură: proza
       spune CUM vorbești, blocul de produse spune CE e adevărat.
    """
    prior = history[:-1] if history else []
    if not get_settings().structured_history_enabled:
        lines: list[str] = []
        for m in prior[-max_turns:]:
            body = (m.body or "").strip()
            if not body:
                continue
            safe_body = make_safe(body).text
            role = "Client" if m.direction == Direction.INBOUND else "Asistent"
            lines.append(f"{role}: {safe_body}")
        return "\n".join(lines)[-max_chars:]
    # NX-255: toată fereastra încărcată (`HISTORY_LIMIT`), nu doar `max_turns`. Rezumatul acoperă
    # ce e ÎNAINTEA mesajelor încărcate, deci o fereastră de 6 din 8 lăsa un tur (mesajele 7-8 în
    # urmă) în afara ambelor (recenzia NX-255).
    return _structured_transcript(prior[-HISTORY_LIMIT:], emit=emit, consumer=consumer)


# NX-255 — legenda blocurilor `[a aratat]`. Emisă O SINGURĂ dată și DOAR când există cel puțin un
# bloc: o regulă despre date inexistente e zgomot care invită la invenție. Spune exact ce e demn
# de încredere (id + nume, pentru referințe ordinale) și ce nu (cifrele, care sunt datate).
#
# Principiul 13: textul ăsta e PROMPT, deci se scrie în vocea pe care o cere. Fără liniuță de pauză
# și fără punct și virgulă, fiindcă un exemplu cu semnul interzis îl învață pe model exact ce îi
# interzicem în altă parte. Din același motiv eticheta e „a aratat", fără diacritice: apare și în
# datele randate, iar potrivirea pe ea nu trebuie să depindă de normalizarea Unicode.
_SHOWN_LEGEND = (
    "(Produsele marcate „a aratat” sunt ce ai afișat deja clientului. Id-ul și numele sunt de "
    "încredere pentru referințe de tipul „al doilea”. Prețurile de acolo sunt de atunci: când "
    "rostești un preț, ia-l din produsele turului de acum, nu din istoric.)"
)


def _shown_refs(m: Message, cap: int) -> list[dict]:
    """Ref-urile produselor afișate în turul `m`, din `messages.payload.shown` (NX-255).
    Defensiv pe orice formă veche/parțială: un rând scris înainte de card n-are cheia, iar un
    payload stricat nu trebuie să rupă promptul — degradează la „fără bloc", nu la excepție.

    NX-230: numele trece prin `make_safe` ca și corpul mesajului. Un nume de produs nu e PII, dar
    poarta de redactare nu se ocolește pe un drum nou doar fiindcă azi datele par curate: `payload`
    e scris de cod, însă istoricul e ultimul loc dinaintea promptului, iar plasa de acolo trebuie
    să acopere TOT ce pleacă, nu doar câmpurile despre care presupunem că sunt riscante."""
    payload = m.payload or {}
    raw = payload.get("shown")
    if not isinstance(raw, list):
        return []
    refs: list[dict] = []
    for p in raw[:cap]:
        if not isinstance(p, dict):
            continue
        pid, name, price = p.get("product_id"), p.get("name"), p.get("price")
        if pid is None or name is None or price is None:
            continue
        refs.append({"product_id": str(pid), "name": make_safe(str(name)).text, "price": price})
    return refs


def _render_shown(refs: list[dict], *, age: int) -> str:
    """Blocul `[a aratat]` — JSON compact, cu vechimea în TURE. Vechimea e explicită fiindcă un
    preț de acum patru ture nu e o minciună, e un fapt datat: diferența trebuie să stea în date,
    nu în presupunerea modelului. Cheile sunt scurte (`id`/`n`/`p`) doar aici, la randare; forma
    canonică persistată rămâne cea din `state.displayed_products`, ca să nu existe două
    adevăruri."""
    items = json.dumps(
        [{"id": r["product_id"], "n": r["name"], "p": r["price"]} for r in refs],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    when = "în turul precedent" if age <= 1 else f"acum {age} ture"
    return f"Asistent [a aratat, {when}]: {items}"


@dataclass
class _Entry:
    """O linie de transcript și rolul ei (`client` | `assistant` | `shown`), pentru măsurătoare."""

    role: str
    text: str


def _structured_transcript(
    msgs: list[Message],
    *,
    emit: Callable[..., None] | None,
    consumer: str | None = None,
) -> str:
    """Transcriptul NETĂIAT (NX-255): fereastra de mesaje e singura margine.

    Nu există plafon de caractere, deliberat. Varianta cu buget pe rol (3.500 de caractere, proza
    botului cedând prima) tăia tot proza turelor vechi: pe `sole-ro` un răspuns are 1.200-1.600 de
    caractere, deci trei ture de bot trec de orice plafon care nu e practic nelimitat. Iar un plafon
    care se atinge pe fiecare conversație e exact defectul reparat aici, doar mutat mai încolo. Pe
    conversația `1748f988` (2026-09-29) tăierea veche `[-1200:]` lăsa modelului, la turul 3, doar
    coada listei de produse de la turul 2: niciun mesaj al clientului, deci nici „cremă”, nici „ten
    uscat”. Mărimea rămâne mărginită în amonte: fereastra (`HISTORY_LIMIT`), mesajul clientului la
    intrare (`src/web/app.py`, 2.000 de caractere), răspunsul botului de forma lui.
    """
    s = get_settings()
    entries: list[_Entry] = []
    shown_turns = 0
    for i, m in enumerate(msgs):
        body = (m.body or "").strip()
        if m.direction == Direction.INBOUND:
            if body:
                entries.append(_Entry("client", f"Client: {make_safe(body).text}"))
            continue
        if body:
            entries.append(_Entry("assistant", f"Asistent: {make_safe(body).text}"))
        refs = _shown_refs(m, s.history_shown_max_products)
        if refs:
            shown_turns += 1
            # Vechimea se numără în TURE de la mesajul curent (care nu e în `msgs`), nu în mesaje:
            # „acum 3 ture" e ce înțelege un om și ce poate compara modelul cu `age_turns` din
            # evidence bundle (NX-240). Un tur = o pereche client/asistent, deci jumătate din pași.
            age = max(1, (len(msgs) - i + 1) // 2)
            entries.append(_Entry("shown", _render_shown(refs, age=age)))

    if not entries:
        return ""

    legend = [_SHOWN_LEGEND] if shown_turns else []
    if emit is not None:
        # `consumer`: transcriptul îl cer mai mulți consumatori pe același tur (agentul, compunerea
        # comparației, executorii kernelului). Fără eticheta care spune cine l-a cerut, evenimentele
        # sunt indistinctibile și orice agregare dublează tăcut mărimea istoricului.
        emit(
            "history_budget",
            consumer=consumer or "unknown",
            client_chars=sum(len(e.text) for e in entries if e.role == "client"),
            assistant_chars=sum(len(e.text) for e in entries if e.role == "assistant"),
            shown_chars=sum(len(e.text) for e in entries if e.role == "shown"),
            shown_turns=shown_turns,
            window_messages=len(msgs),
        )
    return "\n".join(legend + [e.text for e in entries])


def customer_profile_block(contact: Contact, *, max_chars: int = 300) -> str:
    """Bloc compact de profil din `contacts.profile` (+ stadiu lifecycle dacă ≠ new).
    Sare valorile goale; listele se taie la 4 elemente. Gol → "" (nimic injectat).
    Profilul NU conține PII de canal (telefonul stă în channel_identities, P12)."""
    profile = contact.profile or {}
    parts: list[str] = []
    for key, value in profile.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value[:4])
        parts.append(f"{key}: {value}")
    if contact.lifecycle and contact.lifecycle != "new":
        parts.append(f"stadiu: {contact.lifecycle}")
    if not parts:
        return ""
    return ("Profil client: " + "; ".join(parts))[:max_chars]


# NX-160: etichete prezentabile pt cheile canonice frecvente (RO). Fallback = cheia humanizată
# (snake_case → „snake case"). GENERIC — o cheie necunoscută nu crapă, doar se afișează humanizat.
_FACT_LABELS: dict[str, str] = {
    "budget_band": "Buget",
    "fav_brands": "Brand preferat",
    "restriction": "Restricție",
    "size": "Mărime",
    "use_case": "Scop",
    "recipient": "Pentru",
    "style_pref": "Stil",
    "preferred_time": "Program preferat",
    "skin_type": "Tip de ten",
    "hair_type": "Tip de păr",
    "concerns": "Nevoie",
    "vehicle_model": "Mașină",
    "fuel_type": "Combustibil",
    "part_category": "Piesă",
    "diet_preference": "Preferință alimentară",
}


def _fact_label(f: dict) -> str:
    """Eticheta afișată a unui fact: preferă `canonical_key` (label RO), apoi `raw_key`/`fact_type`
    humanizat. Nu expunem snake_case brut clientului în prompt."""
    key = f.get("canonical_key") or f.get("raw_key") or f.get("fact_type") or ""
    return _FACT_LABELS.get(key, key.replace("_", " ").strip().capitalize())


def facts_block(ctx: TurnContext, *, max_facts: int = 6, max_chars: int = 400) -> str:
    """Bloc compact de facts STABILE știute despre client (buget/brand/restricții/…), memoria
    structurată peste mesajele ieșite din istoricul de 8. Bugetat (P4). Seed de processor în
    `ctx.facts` (gol când memoria e OFF → bloc gol, degradare).

    NX-160: `ctx.facts` conține DOAR facts `visibility='inject'` (PII/medical filtrate la sursă +
    la citire), formatate cu etichete prezentabile (nu snake_case brut). Fără PII (P12).

    NX-235: un fapt pe o cheie REVOCATĂ nu se mai injectează. Memoria structurată e o sursă
    paralelă, alimentată post-tur din conversații mai vechi: fără poarta asta, „bugetul nu mai
    contează" ar dispărea din stare, dar s-ar întoarce a doua zi prin `facts_block` — exact bucla
    pe care cardul o închide, doar pe alt drum."""
    revoked = _revoked_keys(ctx)
    parts: list[str] = []
    for f in (ctx.facts or [])[:max_facts]:
        value = f.get("fact_value")
        if value in (None, "", [], {}) or not (
            f.get("canonical_key") or f.get("raw_key") or f.get("fact_type")
        ):
            continue
        if revoked and _need_key_of(f) in revoked:
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value[:4])
        parts.append(f"{_fact_label(f)}: {value}")
    if not parts:
        return ""
    return ("Ce știu despre client: " + "; ".join(parts))[:max_chars]


def _revoked_keys(ctx: TurnContext) -> frozenset[str]:
    """Cheile retrase explicit, în vocabularul NEVOILOR. Gol când v2 e stins (fără schimbare)."""
    from src.conversation.needs import norm_key
    from src.conversation.state_v2 import ConversationStateV2

    state_v2 = getattr(ctx, "state_v2", None)
    if not isinstance(state_v2, ConversationStateV2):
        return frozenset()
    return frozenset(norm_key(k) for k in state_v2.revoked_keys())


def _need_key_of(fact: dict) -> str:
    """Cheia unui fact, tradusă în vocabularul nevoilor (`budget_band` → `budget_max`), ca poarta
    de revocare să prindă și sinonimele — altfel ar fi o poartă cu numele scris greșit."""
    from src.conversation.needs import KEY_ALIASES, norm_key

    key = norm_key(fact.get("canonical_key") or fact.get("raw_key") or fact.get("fact_type") or "")
    return KEY_ALIASES.get(key, key)


def memory_block(ctx: TurnContext, *, max_needs: int = 8, max_chars: int = 500) -> str:
    """NX-235 — proiecția SIGURĂ a stării curente: ce știm, cât de tare, și ce s-a retras.

    Nu e istoricul deciziilor și nu e transcript: e SNAPSHOTUL. Modelul primește tăria fiecărei
    nevoi ca informație structurală (o limită nu se negociază), dar enforcement-ul rămâne al
    reducerului și al validatorului — un prompt nu e o poartă.

    Revocările apar EXPLICIT. O absență e ambiguă („n-a spus" vs „a retras"), iar ambiguitatea e
    exact ce permite unui rezumat să reintroducă faptul retras. Gol când v2 e stins."""
    from src.conversation.state_v2 import ConversationStateV2

    state_v2 = getattr(ctx, "state_v2", None)
    if not isinstance(state_v2, ConversationStateV2):
        return ""
    lines: list[str] = []
    needs = state_v2.active_needs()[:max_needs]
    if needs:
        rendered = "; ".join(
            f"{n.key} {n.operator} {n.normalized_value}"
            + (" (obligatoriu)" if n.strength == "hard" else "")
            for n in needs
        )
        lines.append("Nevoi active (obligatoriile nu se pot relaxa): " + rendered)
    revoked = sorted(state_v2.revoked_keys())
    if revoked:
        lines.append(
            "Retrase de client (NU le reintroduce, nici din rezumat): " + ", ".join(revoked[:6])
        )
    if state_v2.topic.category_key:
        lines.append(f"Subiect curent: {state_v2.topic.category_key}")
    return "\n".join(lines)[:max_chars]


#: Prefixul liniei de produse. Extras ca să intre în calculul bugetului: fără el, bugetul s-ar
#: cheltui pe text pe care îl scriem noi și l-am uita.
_SHOWN_PREFIX = "Produse arătate recent (folosește id-ul din [] pt detalii/comparație/checkout): "

#: Cât din buget poate lua linia de constrângeri. Se rezervă ÎNAINTEA produselor: e scurtă,
#: mărginită, și sub state v2 nici nu se emite (`include_constraints=False`).
_CONSTRAINTS_BUDGET = 200


def state_block(
    state: ConversationState,
    *,
    max_products: int = 8,
    max_chars: int = 1200,
    include_constraints: bool = True,
    language: str | None = None,
) -> str:
    """Bloc de state references: produse arătate recent (id + nume + preț, ref-uri — principiul 8)
    + constrângeri știute (buget, tip de ten…). Memoria scurtă pt follow-up coerent. Gol → "".

    R3: expunem `product_id`-ul (UUID) ca agentul să poată chema get_product_details /
    compare_products / checkout_link pe produsele DEJA arătate, fără re-căutare. Fără id în
    context, un follow-up de tip „care e cea mai bună?" pasa un id inventat → DataError pe cast.

    NX-251 — `include_constraints=False` când starea v2 e activă: constrângerile de aici sunt
    proiecția v1 a acelorași nevoi pe care `memory_block` le emite canonic (cu tărie și revocări).
    Ambele blocuri în același prompt înseamnă aceleași fapte de două ori, iar copia mai săracă e
    exact cea care poate contrazice: „buget: 200" fără „(obligatoriu)" invită la relaxare.

    ## De ce numele e SCURT și tăierea e pe INTRARE (măsurat pe catalogul SOLE, 2026-09-11)

    Vechea formă (3 produse, 600 de caractere, nume ÎNTREG, `[:max_chars]` orb) își rata propriul
    scop pe primul catalog real. Numele de catalog are **mediana 200 de caractere** (p90 242,
    max 325), fiindcă e nume PLUS frază de marketing — aceeași constatare care a cerut migrarea
    049. Deci trei produse reale ocupau **837** de caractere, iar tăierea la 600 lăsa **2 din 3**
    id-uri intacte: al treilea era retezat la mijloc.

    Nu era o pierdere de informație, era o CORUPERE. Tăierea oarbă poate secționa un UUID, iar un
    id pe jumătate arată ca un id — modelul îl pasează unui tool și primim `DataError` pe cast,
    adică exact defectul pe care R3 îl reparase expunând id-urile. Garda exista
    (`test_state_block_exposes_full_uuids_without_truncation`), dar rula pe un nume de 46 de
    caractere din catalogul demo: invariantul pe care îl apăra era deja fals în producție.

    Două schimbări, în ordinea asta:

    * **`display_name`** (capul dinaintea primului ` - `) duce mediana de la 200 la **35** de
      caractere. Nu e o economie oportunistă: promptul e deja locul unde produsul e numit scurt
      (axele de comparație, vederile `llm_view` ale substitutelor și ale relațiilor), fiindcă
      coada e frază de reclamă, nu identitate. Validatorul nu potrivește NUME (preț, link, cifre
      bare, claim-uri, stoc), deci scurtarea nu poate invalida nimic.
    * **tăiere pe INTRARE** — o intrare intră întreagă sau nu intră deloc. Degradarea rămâne
      onestă (lipsesc produse, se vede) în loc să fie coruptă (un id rupt, care nu se vede).

    Abia după astea are sens plafonul de 8: aliniat cu `MAX_DISPLAYED` din starea v2, ca promptul
    să arate ce ȚINE starea, nu o a doua limită, mai strânsă, pe care nimeni n-o mai citește.

    ## De ce 1200 și nu „cât mai mult"

    `max_chars` e bugetul ACESTUI bloc, nu al promptului: un singur paragraf, vecin cu transcriptul
    (1200), rezumatul, profilul (300) și `memory_block` (500). La ~4 caractere pe token, blocul
    plin costă ~300 de tokeni pe tur, și e dinamic, deci nu atinge prefixul cacheabil (NX-275).

    Cifra nu e rotunjită din intuiție, e pragul la care bugetul ÎNCETEAZĂ să lege. Măsurat pe cele
    2.019 produse cu pas: `display_name` are mediana 37, p90 53, p99 69, **max 83**. În cazul cel
    mai rău (cele 8 cele mai lungi nume din catalog) blocul plin are **1.133** de caractere — deci
    la 1000 pierdeam 2 din 8 produse, iar peste 1200 nu se mai schimbă nimic, fiindcă limita
    devine `MAX_DISPLAYED`.

    Asta e și regula: **bugetul de caractere e o plasă, nu o politică.** Câte produse ține memoria
    e o decizie luată o dată, în `MAX_DISPLAYED` (unde e păzită de CHECK-ul de 8KB din 003). Un
    plafon de caractere care taie înaintea ei ar fi a doua politică, ascunsă, pe care nimeni n-o
    citește când se întreabă „de ce nu-și amintește al patrulea produs".
    """
    from src.catalog.render_text import display_name  # noqa: PLC0415 — evită cuplaj la import

    lines: list[str] = []

    constraints_line = ""
    if include_constraints and state.constraints:
        cons = "; ".join(
            f"{k}: {v}" for k, v in state.constraints.items() if v not in (None, "", [], {})
        )
        if cons:
            constraints_line = f"Constrângeri știute: {cons}"[:_CONSTRAINTS_BUDGET]

    # Constrângerile se rezervă întâi: sunt scurte și mărginite, iar produsele sunt cele care pot
    # umple orice buget. Invers, un set mare de produse ar împinge afară o constrângere de buget.
    budget = max_chars - (len(constraints_line) + 1 if constraints_line else 0) - len(_SHOWN_PREFIX)
    entries: list[str] = []
    used = 0
    for p in state.displayed_products[:max_products]:
        entry = f"[{p.product_id}] {display_name(p.name)} ({amount_text(p.price, language)} lei)"
        cost = len(entry) + (2 if entries else 0)  # "; " între intrări
        if used + cost > budget:
            break
        entries.append(entry)
        used += cost
    if entries:
        lines.append(_SHOWN_PREFIX + "; ".join(entries))
    if constraints_line:
        lines.append(constraints_line)
    return "\n".join(lines)


def page_context_block(ctx: TurnContext, *, max_chars: int = 300) -> str:
    """NX-234 — unde se află clientul ACUM, din `TurnSnapshot` (rehidratat server-side).

    Fără el, un „ce părere ai despre acesta?" de pe o pagină de produs n-are ancoră: modelul fie
    ghicește din istoric, fie cere o clarificare pe care clientul o consideră absurdă (se uită la
    produs). Cu el, ancora e un ID canonic pe care tool-urile îl pot rezolva.

    **Ce NU intră aici, deliberat: CIFRE.** Prețul din snapshot e canonic, dar grounding-ul
    validatorului (stagiul 8) e `ctx.retrieval`, scris de stagiul de Retrieval (P3). Un preț
    injectat în prompt fără să existe în retrieval ar fi rostit de model și respins de validator —
    adică un retry garantat pe fiecare tur cu context. Blocul dă ANCORA (id + nume + categorie);
    faptele se iau prin tool-uri, unde sunt și verificate. Ce se marchează totuși e ce NU știm:
    un `UNKNOWN` explicit e mai util decât o absență tăcută, pe care modelul o poate umple.

    Gol când: flagul de prompt e stins (`without_evidence` a golit faptele), canalul n-are context
    de pagină, sau contextul n-a rezolvat nimic — degradare la comportamentul de dinainte."""
    surface = getattr(ctx.snapshot, "surface", None)
    product = getattr(surface, "product", None)
    if product is None:
        return ""
    parts = [f"Clientul se află pe pagina produsului [{product.product_id}] {product.name}"]
    if getattr(product, "category_name", None):
        parts.append(f"categorie: {product.category_name}")
    variant = getattr(surface, "variant", None)
    if variant is not None and getattr(variant, "label", None):
        parts.append(f"varianta selectată: {variant.label}")
    freshness = getattr(product, "freshness", None)
    if freshness is not None and freshness.stale:
        parts.append("date de catalog vechi — confirmă prin tool înainte să afirmi")
    unknown = sorted(getattr(product, "unknown", ()) or ())
    if unknown:
        parts.append("necunoscut: " + ", ".join(unknown[:4]))
    text = (
        "Context pagină: "
        + "; ".join(parts)
        + ". Folosește id-ul din [] pentru detalii/comparație; nu inventa prețuri sau stoc."
    )
    return text[:max_chars]


def summary_block(ctx: TurnContext, *, max_chars: int | None = None) -> str:
    """Bloc de rezumat al conversației anterioare (felia 2), din `ctx.summary` (seedat de
    processor — fără I/O aici). Acoperă mesajele de dinaintea ultimelor 8 (care rămân în
    transcript). Bugetat (P4); gol/lipsă → "" (degradare: doar ultimele 8)."""
    text = (ctx.summary or "").strip()
    if not text:
        return ""
    cap = max_chars if max_chars is not None else get_settings().summary_max_chars
    return ("Rezumat conversație anterioară: " + text)[:cap]


def _emit_context_bytes(ctx: TurnContext, consumer: str, sections: list[tuple[str, str]]) -> None:
    """UN event per consumator, cu octeții fiecărui bloc — ca `turn_latency`/`llm_usage`: o
    defalcare, nu N evenimente. Numele blocurilor sunt vocabular ÎNCHIS, iar valorile sunt lungimi:
    nimic din CONȚINUT nu ajunge în telemetrie (P12).

    `consumer` spune cui i s-a trimis contextul. Cât timp triajul și agentul îl construiesc
    amândoi, aceeași informație pleacă de două ori pe același tur — iar asta trebuie să se vadă
    într-o cifră, nu doar într-un comentariu."""
    props = {name: len(text.encode("utf-8")) for name, text in sections}
    ctx.emit("context_bytes", consumer=consumer, total=sum(props.values()), **props)


def context_blocks(ctx: TurnContext, *, consumer: str | None = None) -> str:
    """Unește blocurile ne-goale de context (rezumat + profil + state) pentru prompturile
    triaj/agent. Ordine CRONOLOGICĂ: rezumatul (fundalul vechi) ÎNAINTEA profilului/state-ului;
    transcriptul ultimelor 8 e concatenat downstream (în triage/agent), deci rezumat→…→recent.
    Stă în mesajul USER (dinamic), nu în system — promptul static rămâne byte-identic (prompt
    caching neatins). Gol → "" (nimic de adăugat).

    `consumer` (NX-251) e pur observabilitate: dat, emite `context_bytes`. Îl pasează DOAR
    apelantul al cărui text ajunge efectiv la model — `build_brain_input` compune aceleași blocuri
    pentru contractul `BrainInput`, dar promptul brain-ului e cel construit în `agent_stage`, deci
    a le număra de acolo ar raporta un context trimis de două ori când el pleacă o dată."""
    from src.conversation.state_v2 import ConversationStateV2  # noqa: PLC0415 — evită ciclu

    # NX-251: cu v2 activ, constrângerile aparțin EXCLUSIV lui `memory_block` (canonice, cu tărie
    # și revocări). `state_block` rămâne proprietarul produselor afișate. Fără poarta asta, același
    # fapt apare de două ori, iar varianta fără tărie e cea care invită la relaxare.
    v2 = isinstance(getattr(ctx, "state_v2", None), ConversationStateV2)
    sections = [
        ("summary", summary_block(ctx)),
        ("profile", customer_profile_block(ctx.contact)),
        ("facts", facts_block(ctx)),  # NX-148: memorie structurată (după profil, înainte de state)
        ("state", state_block(ctx.state, include_constraints=not v2, language=ctx.language)),
        # NX-235: snapshotul redus (nevoi active + revocări) DUPĂ state_block — ce e aici e
        # canonic și bate ce s-ar putea deduce din blocurile de mai sus. Gol când v2 e stins.
        ("memory", memory_block(ctx)),
        # NX-234: ULTIMUL, fiindcă e cel mai recent context — unde se află clientul ACUM, după
        # tot ce s-a întâmplat înainte. Gol pe orice canal fără context de pagină.
        ("page", page_context_block(ctx)),
    ]
    if consumer is not None:
        _emit_context_bytes(ctx, consumer, sections)
    return "\n".join(text for _, text in sections if text)


def build_brain_input(ctx: TurnContext):
    """NX-239 — `BrainInput` din snapshot/state SAFE: mesajul, obligațiile deterministe,
    transcriptul BUGETAT, blocurile de context (deja PII-redactate) și proiecțiile de nevoi
    (active/hard/revocate). FĂRĂ conexiune DB, FĂRĂ istoric nelimitat, FĂRĂ fapte de frontend
    (contextul de pagină e deja rehidratat canonic în snapshot → `page_context_block`)."""
    from src.agent.brain_models import BrainInput, obligations_from_ctx  # noqa: PLC0415 — ciclu
    from src.conversation.state_v2 import ConversationStateV2  # noqa: PLC0415 — import ușor

    state_v2 = ctx.state_v2
    if isinstance(state_v2, ConversationStateV2):
        actives = state_v2.active_needs()
        active_keys = tuple(n.key for n in actives)
        hard_keys = tuple(n.key for n in actives if n.strength == "hard")
        revoked = tuple(state_v2.revoked_keys())
    else:
        active_keys, hard_keys, revoked = (), (), ()
    route = ctx.route
    return BrainInput(
        business_id=ctx.business.id,
        locale=ctx.language,
        message=(ctx.message.body or "").strip(),
        obligations=obligations_from_ctx(ctx),
        history=conversation_transcript(ctx.history),
        context=context_blocks(ctx),
        signals=tuple(ctx.brain_signals),
        category_hint=route.category_key if route else None,
        active_need_keys=active_keys,
        hard_need_keys=hard_keys,
        revoked_need_keys=revoked,
    )


def search_query(history: list[Message], current: str, *, n: int = 2) -> str:
    """Textul pentru căutare = ultimele `n` mesaje ale CLIENTULUI (inclusiv cel
    curent), ca follow-up-urile scurte („ceva mai ieftin", „și pentru păr") să
    caute în contextul corect, nu izolat. Fallback: mesajul curent."""
    users = [
        (m.body or "").strip()
        for m in history
        if m.direction == Direction.INBOUND and (m.body or "").strip()
    ]
    if not users:
        return current.strip()
    return " ".join(users[-n:])
