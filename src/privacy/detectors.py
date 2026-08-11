"""NX-230 — detectoarele de date sensibile. Pur: fără DB, fără LLM, fără I/O.

**Consolidare, nu reinventare.** Înainte de cardul ăsta, aceleași concepte trăiau în cinci locuri:
`worker/stages/gates.py` (`mask_pii` — cea completă), `safety/external_data.py`,
`worker/memory_safety.py`, `worker/profile.py` și `worker/summarizer.py`. Cinci regexuri pentru
„telefon" înseamnă că într-o zi doar unul primește fixul, iar celelalte patru rămân găuri pe care
nimeni nu le mai caută. Nucleul de aici e cel din `gates.py`, mutat aproape neatins — era deja
gândit bine — plus categoriile pe care cardul le cere în plus.

**Principiul de proiectare: fals-negativul e mai bun decât fals-pozitivul distructiv.** Un detector
prea lacom strică date legitime — sparge un cod de produs, un număr de comandă, un SKU — și strică
răspunsul clientului. De aceea fiecare detector cere STRUCTURĂ, nu doar formă:

  • cardul cere lungime PAN reală + IIN 3-6 + Luhn (un EAN-13 trece Luhn ~10% din timp);
  • telefonul cere prefix de țară sau `0` local (altfel orice cod de 10 cifre devine telefon);
  • **CNP-ul are exact 13 cifre, la fel ca un EAN-13** — fără dată validă, județ valid și cifră de
    control, aș masca sistematic coduri de produs din catalog. Vezi `_valid_cnp`.
"""

from __future__ import annotations

import re

from src.privacy.contracts import CATEGORIES

# ── Nucleul mutat din gates.py (NX-121) ─────────────────────────────────────────────────────
# Ordinea contează: EMAIL/IBAN întâi (semn distinctiv), CARD înaintea TELEFON (un card de 16 cifre
# nu trebuie spart de regexul de telefon), CNP înaintea CARD (13 cifre, dar structură proprie).
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_CARD_CAND = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_CARD_LEN = frozenset({15, 16, 19})
_PHONE = re.compile(
    r"(?<!\d)(?:"
    r"(?:\+|00)\d{1,3}(?:[ .-]?\d){8,11}"  # +40 / 0040 712 345 678
    r"|0(?:[ .-]?\d){9}"  # 0712 345 678 (RO local)
    r")(?!\d)"
)

# ── CNP (cod numeric personal, RO) ──────────────────────────────────────────────────────────
_CNP_CAND = re.compile(r"(?<!\d)\d{13}(?!\d)")
_CNP_WEIGHTS = (2, 7, 9, 1, 4, 6, 3, 5, 8, 2, 7, 9)
# Județe valide: 01-46 (județe + sectoare București 41-46), 51/52 (Călărași/Giurgiu), 70 (străini).
_CNP_COUNTIES = frozenset(list(range(1, 47)) + [51, 52, 70])
_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _valid_cnp(digits: str) -> bool:
    """Structură + cifră de control. Fără asta, orice EAN-13 din catalog ar fi mascat ca CNP.

    Verifică, în ordine: sexul/secolul (1-9), luna 01-12, ziua în intervalul lunii, județul dintr-o
    listă închisă, și în final checksumul oficial. Un EAN-13 aleator trece rareori toate patru.
    """
    if len(digits) != 13:
        return False
    if digits[0] not in "123456789":
        return False
    month = int(digits[3:5])
    day = int(digits[5:7])
    if not 1 <= month <= 12 or not 1 <= day <= _MONTH_DAYS[month - 1]:
        return False
    if int(digits[7:9]) not in _CNP_COUNTIES:
        return False
    total = sum(int(d) * w for d, w in zip(digits[:12], _CNP_WEIGHTS, strict=True))
    control = total % 11
    return int(digits[12]) == (1 if control == 10 else control)


# ── Adresă (RO/HU/EN, conservativ) ──────────────────────────────────────────────────────────
# Cere un cuvânt de stradă URMAT de un număr: „strada" singur într-o propoziție nu e o adresă
# („locuiesc pe strada mea" nu se maschează). Diacriticele sunt opționale — clienții scriu fără.
_ADDRESS = re.compile(
    r"\b(?:str\.?|strada|bd\.?|b-?dul|bulevardul|calea|aleea|sos\.?|[sș]oseaua|"
    r"intrarea|utca|t[eé]r|street|road|avenue)\s+"
    r"[\wÀ-ɏ.\- ]{2,40}?"
    r"(?:\s*,?\s*(?:nr\.?|no\.?|n[ao]\.?)?\s*\d{1,4}[A-Za-z]?)"
    r"(?:\s*,?\s*(?:bl\.?|sc\.?|et\.?|ap\.?)\s*[\w\d]{1,5})*",
    re.IGNORECASE,
)

# ── Secrete / credențiale ───────────────────────────────────────────────────────────────────
# Forme cu prefix cunoscut (fără ambiguitate) + orice șir lung lângă un cuvânt-cheie.
_SECRET_PREFIXED = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9_-]{16,}"  # OpenAI
    r"|sb_secret_[A-Za-z0-9_-]{8,}"  # Supabase service key
    r"|ghp_[A-Za-z0-9]{20,}"  # GitHub PAT
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # JWT
    r")\b"
)
_SECRET_LABELLED = re.compile(
    r"\b(?:api[_ -]?key|token|secret|password|parol[aă]|jelsz[oó])\b\s*[:=]?\s*"
    r"[\"']?([A-Za-z0-9_\-/+]{12,})[\"']?",
    re.IGNORECASE,
)

# ── Referințe de comandă ────────────────────────────────────────────────────────────────────
# NU se redactează din corpul mesajului: `check_order` are nevoie de ele, iar un număr de comandă
# fără cont e de valoare mică. Se redactează în TELEMETRIE (vezi policy.py), unde n-au ce căuta.
_ORDER_REF = re.compile(r"\b(?:comanda|comenzii|order|awb|rendel[eé]s)\s*#?\s*(\d{4,12})\b", re.I)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str, *, categories: frozenset[str] | None = None) -> tuple[str, dict[str, int]]:
    """`(text_redactat, contoare)`. Pur și idempotent pe rezultatul propriu.

    `categories` restrânge ce se caută (vezi `policy.py`); implicit, tot ce știm. Ordinea e fixă și
    deliberată — vezi comentariul de sus.
    """
    active = CATEGORIES if categories is None else (categories & CATEGORIES)
    counts: dict[str, int] = {}

    def _bump(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    def _sub(pattern: re.Pattern, name: str, label: str, body: str) -> str:
        if name not in active:
            return body

        def _repl(_m: re.Match) -> str:
            _bump(name)
            return label

        return pattern.sub(_repl, body)

    out = text
    out = _sub(_SECRET_PREFIXED, "secret", "[secret]", out)

    if "secret" in active:

        def _labelled(m: re.Match) -> str:
            _bump("secret")
            return m.group(0).replace(m.group(1), "[secret]")

        out = _SECRET_LABELLED.sub(_labelled, out)

    out = _sub(_EMAIL, "email", "[email]", out)
    out = _sub(_IBAN, "iban", "[iban]", out)

    if "cnp" in active:

        def _cnp(m: re.Match) -> str:
            if _valid_cnp(m.group(0)):
                _bump("cnp")
                return "[cnp]"
            return m.group(0)  # 13 cifre care nu-s CNP (EAN, cod) → neatinse

        out = _CNP_CAND.sub(_cnp, out)

    if "card" in active:

        def _card(m: re.Match) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) in _CARD_LEN and digits[0] in "3456" and _luhn(digits):
                _bump("card")
                return "[card]"
            return m.group(0)

        out = _CARD_CAND.sub(_card, out)

    out = _sub(_PHONE, "phone", "[telefon]", out)
    out = _sub(_ADDRESS, "address", "[adresa]", out)

    if "order_ref" in active:

        def _order(m: re.Match) -> str:
            _bump("order_ref")
            return m.group(0).replace(m.group(1), "[comanda]")

        out = _ORDER_REF.sub(_order, out)

    return out, counts


def detect(text: str, *, categories: frozenset[str] | None = None) -> dict[str, int]:
    """Doar contoarele, fără textul redactat. Pentru shadow-mode: comparăm CÂTE, niciodată CE."""
    return redact(text, categories=categories)[1]
