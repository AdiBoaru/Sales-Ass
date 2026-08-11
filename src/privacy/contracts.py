"""NX-230 — tipurile frontierei de privacy. Pur: fără DB, fără LLM, fără I/O.

**Problema pe care o rezolvă tipurile, nu disciplina.** În v1 masca de PII exista
(`gates.mask_pii`, NX-121) și funcționa — dar acoperea doar promptul turului CURENT. Comentariul
care o însoțea spunea răspicat ce rămâne descoperit:

> „`messages.body` stocat rămâne RAW (PII legitim în storage — explicit out-of-scope) → istoricul
> îl poate reintroduce în prompt la turul următor"

Bucla e închisă în cod: `processor.py:359` persistă `event["body"]` brut, iar `context.py:32`
citește exact acel body înapoi în transcript. Turul 1 maschează telefonul; turul 2 îl citește din
DB și îl pune în prompt. Masca era o perdea în fața unei uși deschise.

**Ideea centrală: `RawText`.** Textul brut nu mai e un `str` care circulă liber, ci un tip care
refuză să se afișeze. `repr()`, `str()`, f-string, `log.info("%s", raw)`, `json.dumps` — toate
produc un placeholder sau eșuează. Ca să obții valoarea trebuie să scrii `.value`, explicit, într-un
loc pe care un `grep` îl găsește și un review îl vede.

Asta e diferența dintre „am uitat să maschez aici" și „am scris deliberat `.value` aici". Prima e
invizibilă; a doua e o decizie.

**D6 rămâne respectat:** agentul principal POATE vedea query-ul brut în memoria turului. Ce nu mai
poate face nimeni e să-l persiste, să-l logheze, să-l pună într-un event sau într-o cheie de cache
fără să scrie `.value` — moment în care întrebarea „de ce?" devine inevitabilă.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Categoriile pe care le recunoaștem. Închise: un detector nou se adaugă aici, deci apare în
# diff-ul care îl introduce, nu doar în comportament.
CATEGORIES: frozenset[str] = frozenset(
    {
        "phone",
        "email",
        "iban",
        "card",
        "cnp",
        "address",
        "secret",
        "order_ref",
    }
)


class RawText:
    """Text brut de la client. Se poate citi DOAR prin `.value`, explicit.

    Toate căile prin care un `str` scapă accidental într-un sink sunt închise:
      - `repr()` / `str()` / f-string → placeholder cu bucket de lungime, niciodată conținut;
      - `json.dumps` → `TypeError` (nu e un tip serializabil cunoscut);
      - `%`-formatting în logging → trece prin `__str__`, deci tot placeholder;
      - `==` cu un `str` → `False`, ca o comparație distrată să nu devină un oracol.

    Nu e criptografie și nu pretinde să fie. E o barieră împotriva scurgerii din NEATENȚIE, care e
    modul în care PII-ul chiar ajunge în loguri.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value or ""

    @property
    def value(self) -> str:
        """Valoarea brută. Fiecare apel e un punct de decizie — caută-le cu `grep -rn '.value'`."""
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    @property
    def length_bucket(self) -> str:
        """Bucket de lungime pentru telemetrie. Lungimea EXACTĂ a unui text scurt e ea însăși un
        semnal identificator (un mesaj de 11 caractere e probabil un număr de telefon)."""
        n = len(self._value)
        if n == 0:
            return "0"
        if n < 32:
            return "xs"
        if n < 128:
            return "s"
        if n < 512:
            return "m"
        return "l"

    def __repr__(self) -> str:
        return f"<RawText redacted len={self.length_bucket}>"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        # Deliberat: `RawText("x") == "x"` e False. O comparație cu un `str` ar fi un oracol prin
        # care se poate ghici conținutul o literă pe rând.
        return isinstance(other, RawText) and other._value == self._value

    def __hash__(self) -> int:
        # Hash peste conținut, dar tipul intră în hash → nu coincide cu `hash(str)`.
        return hash(("RawText", self._value))

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class Detection:
    """O potrivire de detector. `sample` NU există intenționat: un fragment „pentru debug" e tot
    PII, iar fragmentele sunt exact ce ajunge în bug reports."""

    category: str
    count: int


@dataclass(frozen=True)
class SafeInbound:
    """Reprezentarea PERSISTABILĂ a unui mesaj de intrare. Tot ce se scrie în DB, logs, analytics,
    cache, trace sau ledger vine de aici — niciodată din `RawInbound`.

    `text` e deja redactat. `categories` spune CE s-a găsit, nu CE anume era.
    """

    text: str
    categories: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    # `True` dacă detectorul a eșuat și am aplicat politica fail-safe. Persistăm faptul, ca un
    # incident să fie vizibil în date, nu doar într-un log care se rotește.
    degraded: bool = False

    @property
    def had_pii(self) -> bool:
        return bool(self.categories)

    def count_bucket(self) -> str:
        """Bucket low-cardinality pentru evenimente. Numărul exact de potriviri e un semnal fin."""
        total = sum(self.counts.values())
        if total == 0:
            return "0"
        if total == 1:
            return "1"
        if total <= 3:
            return "2-3"
        return "4+"


@dataclass(frozen=True)
class RawInbound:
    """Mesajul brut, request-scoped. Ține `RawText`, nu `str`.

    `frozen=True` + `RawText` înseamnă că nici măcar un `asdict()` distrat nu produce text:
    serializarea dă obiectul `RawText`, care la rândul lui refuză să se afișeze.
    """

    body: RawText
    content_type: str = "text"

    def __repr__(self) -> str:  # nu lăsăm dataclass-ul să genereze repr-ul implicit
        return f"<RawInbound content_type={self.content_type!r} body={self.body!r}>"


@dataclass
class SensitiveTokenMap:
    """Mapare token → valoare originală, STRICT request-scoped.

    Există numai pentru cazul legitim în care răspunsul trebuie să reflecte o valoare înapoi
    aceluiași utilizator în același tur („am notat numărul 07…"). Nu se serializează, nu iese din
    tur, nu se persistă.

    Tokenii sunt secvențiali în cadrul turului (`[phone_1]`, `[phone_2]`), NU derivați din valoare:
    un token derivat prin hash e reversibil pentru un atacator cu un dicționar de numere de telefon.
    """

    _values: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def issue(self, category: str, value: str) -> str:
        self._counters[category] = self._counters.get(category, 0) + 1
        token = f"[{category}_{self._counters[category]}]"
        self._values[token] = value
        return token

    def resolve(self, token: str) -> str | None:
        return self._values.get(token)

    def __repr__(self) -> str:
        return f"<SensitiveTokenMap tokens={len(self._values)}>"

    def __reduce__(self) -> Any:
        # Anti-pickle: dacă cineva încearcă să treacă harta printr-o coadă sau un cache, eșuează
        # ZGOMOTOS. Un token map serializat e un vault raw pe care nimeni nu l-a aprobat.
        raise TypeError(
            "SensitiveTokenMap e request-scoped și nu se serializează "
            "(un token map persistat = vault raw neaprobat — vezi NX-230)"
        )
