"""NX-240 — formatarea și copy-ul server-owned: singurul loc în care o cifră devine text.

**De ce e un modul, nu o funcție ad-hoc.** În v1 regula de reducere trăia în browser
(`Math.round(((list-price)/list)*100)`), pluralul recenziilor era o concatenare, iar prețul se
parsa înapoi din string cu euristici de virgulă. Fiecare dintre ele e o regulă comercială fără
validator. Aici mută TOATE, cu trei proprietăți impuse de tip:

  1. **`Decimal`, niciodată `float`.** Banii nu se rotunjesc binar: `0.1 + 0.2` e o glumă în
     contabilitate. Intrarea e `Decimal`, cuantizarea e explicită (`ROUND_HALF_UP`), iar ieșirea
     e `str`. Nu există cale prin care un `float` să ajungă în ViewModel.
  2. **Fail-safe localizat, niciodată excepție.** Un formatter care crapă pe un input ciudat ar
     transforma un răspuns bun într-un 500. Fiecare funcție publică întoarce `None` (câmp OMIS)
     sau un fallback localizat — decizia „ce se întâmplă când nu știm" e a apelantului, dar
     „aruncăm spre client" nu e niciodată o opțiune (P6).
  3. **Fără ceas, fără I/O, fără config.** Doar stdlib. `now` se pasează; locale-ul e argument.
     Modulul e importabil din orice strat fără să tragă după el `src.config` sau un ciclu.

**Locale, nu română hardcodată (D3).** Pilotul e `ro`, dar fiecare tabel are cele trei locale
Stage 1 și un fallback EXPLICIT (`ro`), nu un `KeyError` mascat. Pluralul românesc are trei forme
reale (1 / 2–19 / „de" la ≥20) — regula CLDR, nu „adaugă un s".

**Ce NU e aici:** decizia dacă un câmp are voie să apară. Asta e a `GroundingGuard`-ului:
formatarea presupune că faptul e deja dovedit. Un preț fără sursă nu ajunge niciodată la
`format_money` — se omite mai devreme.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

# ── Locale ──────────────────────────────────────────────────────────────────────────────────
#: Localele Stage 1. Pilotul e `ro`; celelalte există ca nucleul să rămână locale-aware (D3).
SUPPORTED_LOCALES: Final[tuple[str, ...]] = ("ro", "hu", "en")
DEFAULT_LOCALE: Final[str] = "ro"

Locale = Literal["ro", "hu", "en"]


def normalize_locale(raw: Any) -> str:
    """`"ro-RO"` / `"RO"` / `None` → `"ro"`. Un locale necunoscut cade pe pilot, EXPLICIT: mai
    bine text în română decât un `KeyError` pe drumul de randare."""
    if not isinstance(raw, str):
        return DEFAULT_LOCALE
    short = raw.strip().lower().replace("_", "-").split("-")[0]
    return short if short in SUPPORTED_LOCALES else DEFAULT_LOCALE


# ── Separatoare numerice per locale ─────────────────────────────────────────────────────────
# `en` folosește convenția anglo-saxonă; `ro`/`hu` pe cea continentală. Grupurile de mii sunt
# aceleași (3), deci diferă doar simbolurile — un tabel, nu trei implementări.
_SEPARATORS: Final[dict[str, tuple[str, str]]] = {
    "ro": (".", ","),
    "hu": (" ", ","),  # maghiara folosește spațiu ca separator de mii
    "en": (",", "."),
}

#: RON se rostește „lei" în RO/HU. Alte monede rămân cod ISO — nu inventăm simboluri pe care
#: nu le putem susține (un „€" pentru EUR ar fi o presupunere de formatare, nu un fapt).
_CURRENCY_WORDS: Final[dict[str, dict[str, str]]] = {
    "ro": {"RON": "lei"},
    "hu": {"RON": "lei", "HUF": "Ft"},
    "en": {},
}

_CENTS = Decimal("0.01")


def to_decimal(value: Any) -> Decimal | None:
    """Orice scalar numeric → `Decimal` exact, sau `None`. `float` intră prin `str()` (deci
    `0.1` devine `Decimal("0.1")`, nu reprezentarea binară) — singurul loc din sistem în care
    conversia asta e permisă, exact ca să nu se facă altundeva prost."""
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return d if d.is_finite() else None
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        if not text:
            return None
        try:
            d = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        return d if d.is_finite() else None
    return None


def _group(digits: str, sep: str) -> str:
    if len(digits) <= 3 or not sep:
        return digits
    head, rest = len(digits) % 3 or 3, []
    rest.append(digits[:head])
    rest.extend(digits[i : i + 3] for i in range(head, len(digits), 3))
    return sep.join(rest)


def format_amount(amount: Any, locale: str, *, grouping: bool = True) -> str | None:
    """Sumă → text localizat FĂRĂ monedă (`1234.5` → `"1.234,50"` în ro). `None` = nu se poate
    formata, deci câmpul se omite; niciodată o excepție pe drumul de randare."""
    value = to_decimal(amount)
    if value is None:
        return None
    loc = normalize_locale(locale)
    group_sep, decimal_sep = _SEPARATORS[loc]
    try:
        quantized = value.quantize(_CENTS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    sign = "-" if quantized < 0 else ""
    integer, _, fraction = f"{abs(quantized):f}".partition(".")
    fraction = (fraction + "00")[:2]
    if grouping:
        integer = _group(integer, group_sep)
    return f"{sign}{integer}{decimal_sep}{fraction}"


def amount_text(amount: Any, locale: Any) -> str:
    """Suma ca text pentru PROZĂ: `str`, niciodată `None`, niciodată o excepție.

    Contractul lui `format_amount` („`None` ⇒ câmpul se omite") e potrivit pentru un ViewModel, în
    care un câmp poate lipsi. Într-o frază nu poate: textul e deja compus în jurul cifrei, iar un
    `None` acolo ar produce „costă None lei". De aceea calea de text are propria ușă, nu un `or`
    repetat la fiecare apel.

    O sumă neformatabilă întoarce `""`, NU `"0,00"`: un preț pe care nu-l putem citi e UNKNOWN, iar
    „0 lei" ar fi un fapt inventat, exact ce păzește validatorul (P2). Apelanții verifică oricum
    prezența prețului înainte, deci cazul ăsta e o plasă, nu un drum.

    Folosită și de calea v1 (`compose`, `fallbacks`, tool results), nu doar de web-view.v2:
    modulul stă sub `src/web/` din motive istorice (NX-240), dar e locul canonic în care o cifră
    devine text."""
    return format_amount(amount, locale) or ""


def currency_word(currency: Any, locale: str) -> str | None:
    """Codul de monedă → cuvântul afișabil. `None` (monedă necunoscută) NU devine „lei": o sumă
    fără monedă e o sumă fără înțeles, deci apelantul omite prețul."""
    if not isinstance(currency, str) or not currency.strip():
        return None
    code = currency.strip().upper()
    if not code.isalpha() or len(code) > 8:
        return None
    return _CURRENCY_WORDS[normalize_locale(locale)].get(code, code)


def format_money(amount: Any, currency: Any, locale: str, *, grouping: bool = True) -> str | None:
    """Sumă + monedă → `"89,00 lei"`. `None` dacă ORICARE dintre cele două lipsește: un preț fără
    monedă și o monedă fără preț sunt amândouă UNKNOWN, nu jumătate de fapt."""
    text = format_amount(amount, locale, grouping=grouping)
    word = currency_word(currency, locale)
    if text is None or word is None:
        return None
    return f"{text} {word}"


def format_discount(current: Any, previous: Any) -> str | None:
    """`-18%`, calculat server-side din DOUĂ fapte. `None` dacă reducerea nu e reală (list ≤
    current), dacă vreunul lipsește sau dacă procentul e sub 1% — „-0%" e zgomot, nu ofertă.

    **Rotunjire în JOS, nu la cel mai apropiat.** O reducere de 25,8% afișată ca „-26%" e o
    afirmație comercială mai mare decât faptul care o susține. Direcția sigură a unei promisiuni e
    întotdeauna sub adevăr: nimeni nu se supără că a primit 25,8% când i-am promis 25%.

    Egalitatea monedelor NU se verifică aici (nu le primim): e responsabilitatea apelantului,
    care are faptele cu proveniență. Semnătura o spune — două numere, un procent."""
    now_ = to_decimal(current)
    before = to_decimal(previous)
    if now_ is None or before is None or before <= 0 or before <= now_ or now_ < 0:
        return None
    try:
        pct = ((before - now_) / before * 100).quantize(Decimal("1"), rounding=ROUND_DOWN)
    except (InvalidOperation, ZeroDivisionError, ValueError):
        return None
    return f"-{int(pct)}%" if pct > 0 else None


# ── Plural (CLDR, nu „adaugă un s") ─────────────────────────────────────────────────────────
def plural_form(count: int, locale: str) -> Literal["one", "few", "other"]:
    """Categoria de plural CLDR pentru un întreg. Româna are trei forme reale: `1 recenzie`,
    `2 recenzii`, `20 DE recenzii` — a le colapsa la două produce text care sună a traducere."""
    n = abs(int(count))
    loc = normalize_locale(locale)
    if loc == "ro":
        if n == 1:
            return "one"
        if n == 0 or 1 <= n % 100 <= 19:
            return "few"
        return "other"
    return "one" if n == 1 else "other"


def _plural(count: int, forms: dict[str, str], locale: str) -> str:
    category = plural_form(count, locale)
    return forms.get(category) or forms.get("other") or forms.get("one") or ""


# ── Copy server-owned, localizat ────────────────────────────────────────────────────────────
# Tot ce se vede în widget trăiește AICI, nu în frontend și nu în promptul modelului. Un label
# lipsă e un bug de contract (NX-228 cere `chrome`/`a11y` complete), nu „FE pune ceva implicit".
_COPY: Final[dict[str, dict[str, Any]]] = {
    "ro": {
        "chrome": {
            "launcher_label": "Deschide asistentul",
            "dialog_title": "Asistent de cumpărături",
            "dialog_description": "Întreabă despre produse, comenzi sau livrare.",
            "close_label": "Închide",
            "new_chat_label": "Conversație nouă",
        },
        "composer": {
            "label": "Mesajul tău",
            "placeholder": "Scrie un mesaj…",
            "send_label": "Trimite",
        },
        "announcements": {
            "accepted": "Mesajul a fost primit.",
            "working": "Asistentul pregătește răspunsul.",
            "validating": "Asistentul verifică răspunsul.",
            "completed": "Răspunsul este gata.",
            "failed": "A apărut o problemă la pregătirea răspunsului.",
            "cancelled": "Cererea a fost anulată.",
        },
        "progress": {
            "accepted": "Am primit mesajul",
            "working": "Pregătesc răspunsul",
            "validating": "Verific răspunsul",
        },
        "labels": {
            "view_product": "Vezi produsul",
            "retry": "Încearcă din nou",
            "memory_title": "Ce știu despre căutarea ta",
            "cart_title": "Coșul tău",
            "cart_total": "Total",
            "routine_title": "Pași recomandați",
            "comparison_title": "Comparație",
            "unknown_cell": "—",
            "add_to_cart": "Adaugă în coș",
            "checkout": "Finalizează comanda",
            "yes": "Da",
            "no": "Nu",
            # NX-246: confirmarea unui vot. Server-owned — frontendul nu inventează „Mulțumim!".
            "feedback_thanks_positive": "Mă bucur că te-am ajutat.",
            "feedback_thanks_negative": "Mulțumesc, țin cont.",
        },
        # Criteriile active, ca text. DOAR sloturile care au o formă afișabilă onestă: un buget e
        # o sumă, un brand e un nume propriu. `concerns`/`suitable_for` sunt slug-uri de vocabular
        # (DomainPack) — până există un dicționar slug → etichetă, ele NU se afișează.
        "needs": {"budget_max": "Buget: până în {value}", "brand": "Brand: {value}"},
        "rating": "{rating} din 5",
        "reviews": {"one": "({n} recenzie)", "few": "({n} recenzii)", "other": "({n} de recenzii)"},
        "quantity": {"one": "{n} buc.", "few": "{n} buc.", "other": "{n} buc."},
        "availability": {
            "in_stock": "În stoc",
            "low_stock": "Stoc limitat",
            "out_of_stock": "Stoc epuizat",
            "preorder": "Precomandă",
            "discontinued": "Nu se mai comercializează",
        },
        "availability_units": {
            "one": "Ultima bucată",
            "few": "Ultimele {n} bucăți",
            "other": "Ultimele {n} de bucăți",
        },
        "freshness": {
            "now": "verificat acum",
            "minutes": {
                "one": "verificat acum un minut",
                "few": "verificat acum {n} minute",
                "other": "verificat acum {n} de minute",
            },
            "hours": {
                "one": "verificat acum o oră",
                "few": "verificat acum {n} ore",
                "other": "verificat acum {n} de ore",
            },
            "days": {
                "one": "verificat ieri",
                "few": "verificat acum {n} zile",
                "other": "verificat acum {n} de zile",
            },
        },
        "no_results": {
            "no_match": "Nu am găsit produse care să respecte toate criteriile cerute.",
            "insufficient_data": "Nu pot verifica acum toate criteriile cerute, nu am datele "
            "necesare.",
            "dependency_unavailable": "Căutarea nu e disponibilă momentan. Te rog încearcă din "
            "nou puțin mai târziu.",
        },
        "errors": {
            "processing_error": "A apărut o problemă la pregătirea răspunsului.",
            "deadline_exceeded": "Răspunsul a durat prea mult. Te rog încearcă din nou.",
            "empty_result": "Nu am reușit să pregătesc un răspuns pentru mesajul tău.",
            "attempts_exhausted": "Nu am reușit să pregătesc răspunsul. Te rog încearcă din nou.",
            "projection_error": "A apărut o problemă la afișarea răspunsului.",
            "grounding_failed": "Nu pot confirma datele necesare pentru un răspuns corect acum.",
        },
    },
    "en": {
        "chrome": {
            "launcher_label": "Open the assistant",
            "dialog_title": "Shopping assistant",
            "dialog_description": "Ask about products, orders or delivery.",
            "close_label": "Close",
            "new_chat_label": "New conversation",
        },
        "composer": {
            "label": "Your message",
            "placeholder": "Type a message…",
            "send_label": "Send",
        },
        "announcements": {
            "accepted": "Your message was received.",
            "working": "The assistant is preparing a reply.",
            "validating": "The assistant is checking the reply.",
            "completed": "The reply is ready.",
            "failed": "Something went wrong while preparing the reply.",
            "cancelled": "The request was cancelled.",
        },
        "progress": {
            "accepted": "Message received",
            "working": "Preparing the reply",
            "validating": "Checking the reply",
        },
        "labels": {
            "view_product": "View product",
            "retry": "Try again",
            "memory_title": "What I know about your search",
            "cart_title": "Your cart",
            "cart_total": "Total",
            "routine_title": "Recommended steps",
            "comparison_title": "Comparison",
            "unknown_cell": "—",
            "add_to_cart": "Add to cart",
            "checkout": "Checkout",
            "yes": "Yes",
            "no": "No",
            "feedback_thanks_positive": "Glad I could help.",
            "feedback_thanks_negative": "Thanks, I will keep that in mind.",
        },
        "needs": {"budget_max": "Budget: up to {value}", "brand": "Brand: {value}"},
        "rating": "{rating} out of 5",
        "reviews": {"one": "({n} review)", "other": "({n} reviews)"},
        "quantity": {"one": "{n} pc.", "other": "{n} pcs."},
        "availability": {
            "in_stock": "In stock",
            "low_stock": "Limited stock",
            "out_of_stock": "Out of stock",
            "preorder": "Pre-order",
            "discontinued": "Discontinued",
        },
        "availability_units": {"one": "Last one left", "other": "Only {n} left"},
        "freshness": {
            "now": "checked just now",
            "minutes": {"one": "checked a minute ago", "other": "checked {n} minutes ago"},
            "hours": {"one": "checked an hour ago", "other": "checked {n} hours ago"},
            "days": {"one": "checked yesterday", "other": "checked {n} days ago"},
        },
        "no_results": {
            "no_match": "I could not find products matching all the requested criteria.",
            "insufficient_data": "I cannot verify all the requested criteria right now, data is "
            "missing.",
            "dependency_unavailable": "Search is temporarily unavailable. Please try again "
            "shortly.",
        },
        "errors": {
            "processing_error": "Something went wrong while preparing the reply.",
            "deadline_exceeded": "The reply took too long. Please try again.",
            "empty_result": "I could not prepare a reply for your message.",
            "attempts_exhausted": "I could not prepare the reply. Please try again.",
            "projection_error": "Something went wrong while displaying the reply.",
            "grounding_failed": "I cannot confirm the data needed for a correct answer right now.",
        },
    },
    "hu": {
        "chrome": {
            "launcher_label": "Asszisztens megnyitása",
            "dialog_title": "Vásárlási asszisztens",
            "dialog_description": "Kérdezz termékekről, rendelésekről vagy szállításról.",
            "close_label": "Bezárás",
            "new_chat_label": "Új beszélgetés",
        },
        "composer": {
            "label": "Üzeneted",
            "placeholder": "Írj egy üzenetet…",
            "send_label": "Küldés",
        },
        "announcements": {
            "accepted": "Az üzenet megérkezett.",
            "working": "Az asszisztens készíti a választ.",
            "validating": "Az asszisztens ellenőrzi a választ.",
            "completed": "A válasz elkészült.",
            "failed": "Hiba történt a válasz elkészítése közben.",
            "cancelled": "A kérés meg lett szakítva.",
        },
        "progress": {
            "accepted": "Üzenet megérkezett",
            "working": "Válasz készítése",
            "validating": "Válasz ellenőrzése",
        },
        "labels": {
            "view_product": "Termék megtekintése",
            "retry": "Próbáld újra",
            "memory_title": "Amit a kereséseddel kapcsolatban tudok",
            "cart_title": "Kosarad",
            "cart_total": "Összesen",
            "routine_title": "Ajánlott lépések",
            "comparison_title": "Összehasonlítás",
            "unknown_cell": "—",
            "add_to_cart": "Kosárba",
            "checkout": "Megrendelés",
            "yes": "Igen",
            "no": "Nem",
            "feedback_thanks_positive": "Örülök, hogy segíthettem.",
            "feedback_thanks_negative": "Köszönöm, figyelembe veszem.",
        },
        "needs": {"budget_max": "Keret: legfeljebb {value}", "brand": "Márka: {value}"},
        "rating": "{rating} / 5",
        "reviews": {"one": "({n} értékelés)", "other": "({n} értékelés)"},
        "quantity": {"one": "{n} db", "other": "{n} db"},
        "availability": {
            "in_stock": "Raktáron",
            "low_stock": "Korlátozott készlet",
            "out_of_stock": "Elfogyott",
            "preorder": "Előrendelés",
            "discontinued": "Kifutott",
        },
        "availability_units": {"one": "Az utolsó darab", "other": "Már csak {n} db"},
        "freshness": {
            "now": "most ellenőrizve",
            "minutes": {"one": "egy perce ellenőrizve", "other": "{n} perce ellenőrizve"},
            "hours": {"one": "egy órája ellenőrizve", "other": "{n} órája ellenőrizve"},
            "days": {"one": "tegnap ellenőrizve", "other": "{n} napja ellenőrizve"},
        },
        "no_results": {
            "no_match": "Nem találtam a kért feltételeknek megfelelő terméket.",
            "insufficient_data": "Most nem tudom ellenőrizni az összes feltételt, hiányzanak az "
            "adatok.",
            "dependency_unavailable": "A keresés átmenetileg nem érhető el. Kérlek, próbáld újra "
            "kicsit később.",
        },
        "errors": {
            "processing_error": "Hiba történt a válasz elkészítése közben.",
            "deadline_exceeded": "A válasz túl sokáig tartott. Kérlek, próbáld újra.",
            "empty_result": "Nem tudtam választ készíteni az üzenetedre.",
            "attempts_exhausted": "Nem tudtam elkészíteni a választ. Kérlek, próbáld újra.",
            "projection_error": "Hiba történt a válasz megjelenítésekor.",
            "grounding_failed": "Most nem tudom megerősíteni a válaszhoz szükséges adatokat.",
        },
    },
}


def copy_for(locale: str) -> dict[str, Any]:
    """Tabelul de copy pentru un locale (fallback pilot). Rezultatul e READ-ONLY prin convenție:
    apelanții copiază ce au nevoie (`dict(...)`), nu mutează tabelul de modul."""
    return _COPY[normalize_locale(locale)]


def label(key: str, locale: str) -> str | None:
    """Un label din vocabularul ÎNCHIS de mai sus. Cheie necunoscută → `None` (câmp omis), nu o
    cheie afișată ca text — un `"cart_total"` pe ecran e mai rău decât un câmp lipsă."""
    return copy_for(locale)["labels"].get(key)


def error_message(code: Any, locale: str) -> str:
    """Copy pentru un cod stabil de eroare. Un cod NECUNOSCUT primește mesajul generic — clientul
    nu are ce face cu numele codului, iar tăcerea nu e o opțiune (P6)."""
    errors = copy_for(locale)["errors"]
    key = code if isinstance(code, str) and code in errors else "processing_error"
    return errors[key]


def no_results_text(reason_class: Any, locale: str) -> str:
    """Formularea ONESTĂ per clasă: „n-am găsit" ≠ „nu știu" ≠ „nu pot verifica acum" (D7)."""
    texts = copy_for(locale)["no_results"]
    key = (
        reason_class
        if isinstance(reason_class, str) and reason_class in texts
        else ("insufficient_data")
    )
    return texts[key]


# ── Formatări compuse ───────────────────────────────────────────────────────────────────────
def format_rating(rating: Any, review_count: Any, locale: str) -> str | None:
    """`"4,8 din 5 (120 recenzii)"`. `None` dacă ratingul lipsește SAU dacă e în afara scalei —
    un 7/5 nu e o notă, e un bug de date, iar afișarea lui ar valida bug-ul.

    Numărul de recenzii se adaugă DOAR dacă e > 0: „(0 recenzii)" e exact afirmația falsă pe care
    D8 o interzice (absența dovezii nu e dovada absenței)."""
    value = to_decimal(rating)
    if value is None or value < 0 or value > 5:
        return None
    loc = normalize_locale(locale)
    try:
        rounded = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    # O zecimală, dar fără „,0" decorativ: nota 5 se scrie `5`, nota 4,75 se scrie `4,8`.
    whole = rounded.to_integral_value()
    if rounded == whole:
        text = str(int(whole))
    else:
        _, decimal_sep = _SEPARATORS[loc]
        text = f"{rounded:f}".replace(".", decimal_sep)
    out = copy_for(loc)["rating"].format(rating=text)
    if isinstance(review_count, int) and not isinstance(review_count, bool) and review_count > 0:
        out = f"{out} {_plural(review_count, copy_for(loc)['reviews'], loc).format(n=review_count)}"
    return out


def format_availability(availability: Any, stock: Any, locale: str) -> str | None:
    """Disponibilitatea ca TEXT. `None` = necunoscut ⇒ câmpul se omite; NU devine „indisponibil"
    (UNKNOWN ≠ MISMATCH) și nici „în stoc" prin lipsă de dovadă.

    Când stocul e mic ȘI cunoscut, textul devine mai util („Ultimele 3 bucăți") — dar numai peste
    o disponibilitate care confirmă că produsul chiar e vandabil."""
    loc = normalize_locale(locale)
    if not isinstance(availability, str) or not availability.strip():
        return None
    key = availability.strip().lower()
    labels = copy_for(loc)["availability"]
    if key not in labels:
        return None
    if (
        key in ("in_stock", "low_stock")
        and isinstance(stock, int)
        and not isinstance(stock, bool)
        and 0 < stock <= 5
    ):
        return _plural(stock, copy_for(loc)["availability_units"], loc).format(n=stock)
    return labels[key]


def format_quantity(count: Any, locale: str) -> str | None:
    """`2` → `"2 buc."`. Text, nu număr: frontendul nu are ce înmulți."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    loc = normalize_locale(locale)
    return _plural(count, copy_for(loc)["quantity"], loc).format(n=count)


#: Sloturile de nevoie care AU o formă afișabilă. Restul nu se afișează — un slug de vocabular
#: („acnee_usoara") pe ecran e mai rău decât un criteriu lipsă.
DISPLAYABLE_NEEDS: Final[tuple[str, ...]] = ("budget_max", "brand")


def format_need(key: Any, value: Any, currency: Any, locale: str) -> str | None:
    """O nevoie activă → criteriu afișabil („Buget: până în 200,00 lei"). `None` pentru orice slot
    fără formă onestă: memoria arătată clientului e un rezumat, nu un dump de stare."""
    loc = normalize_locale(locale)
    template = copy_for(loc)["needs"].get(key if isinstance(key, str) else "")
    if template is None:
        return None
    if key == "budget_max":
        text = format_money(value, currency, loc)
    elif isinstance(value, str) and value.strip():
        text = " ".join(value.split())[:40]
    else:
        text = None
    return template.format(value=text) if text else None


def format_freshness(age_s: Any, locale: str) -> str | None:
    """Vechimea unui fapt → text („verificat acum 2 minute"). Primește SECUNDE, nu un timestamp:
    diferența se calculează unde există `now` explicit, nu aici (modulul n-are ceas)."""
    if isinstance(age_s, bool) or not isinstance(age_s, (int, float)) or age_s < 0:
        return None
    loc = normalize_locale(locale)
    fresh = copy_for(loc)["freshness"]
    seconds = int(age_s)
    if seconds < 60:
        return fresh["now"]
    if seconds < 3600:
        n = seconds // 60
        return _plural(n, fresh["minutes"], loc).format(n=n)
    if seconds < 86400:
        n = seconds // 3600
        return _plural(n, fresh["hours"], loc).format(n=n)
    n = seconds // 86400
    return _plural(n, fresh["days"], loc).format(n=n)


__all__ = [
    "DEFAULT_LOCALE",
    "DISPLAYABLE_NEEDS",
    "SUPPORTED_LOCALES",
    "copy_for",
    "currency_word",
    "error_message",
    "format_amount",
    "format_availability",
    "format_discount",
    "format_freshness",
    "format_money",
    "format_need",
    "format_quantity",
    "format_rating",
    "label",
    "no_results_text",
    "normalize_locale",
    "plural_form",
    "to_decimal",
]
