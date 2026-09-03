"""NX-269 — nuanța se descoperă COMPARATIV, nu lexical. Pur: fără DB, fără I/O.

Pentru un sfert din catalog (681 de produse de machiaj), criteriul pe care se ia decizia de
cumpărare nu există ca dată nicăieri: toate cele 2.755 de variante au eticheta „Standard",
`attributes->>'shade'` e 0, `color_hex` e 0. Nuanța trăiește în NUMELE produsului — „LAKA Fruity
Glam Tint nuantator pentru buze **116 Candid**".

**Tentația e o listă de culori** („roșu", „nude", „coral"). Ar fi scurgere de domeniu (pică poarta
NX-264) și ar fi și greșită: nuanțele catalogului sunt coduri de producător („116 Candid", „No. 1
Oatmeal Brown"), nu culori din limba română. O listă de culori ar rata exact ce trebuie găsit.

Forma generală, agnostică de vertical: **numele conține un segment VARIABIL care distinge rândurile
ce împart aceeași linie.** Se derivă comparativ:

* se grupează produsele după (brand, rădăcină de categorie);
* se compară fiecare produs cu ceilalți din grup, pereche cu pereche;
* ce rămâne DIFERIT între doi membri ESTE axa de variație a liniei.

Segmentul nu e neapărat la coadă: măsurat, nuanța apare în toate pozițiile („…Cushion, **22N Shell
Beige**, 18 gr - fond de ten…"). Un produs unic în linia lui n-are cu cine să difere, deci **nu
primește nuanță** — nu o extragere forțată din coada numelui. Aceeași procedură produce „116
Candid" la rujuri și ar produce capacitatea la un vertical cu capacități. Nu știe ce e o
culoare, și nu trebuie.

Invariantul care ține totul: **fiecare nuanță apare LITERAL în numele produsului.** Nu e o
convenție de scriere, e o consecință a construcției — nuanța e o felie din nume — și e testată,
fiindcă asta e diferența dintre un fapt derivat și unul inventat.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

# Un trunchi mai scurt de atât nu e o linie de produs, e o coincidență de primul cuvânt. Cu prag 1,
# „LAKA <orice>" ar deveni un grup, iar tot restul numelui ar fi „nuanța".
MIN_TRUNK_TOKENS = 2

# O nuanță are câteva cuvinte („116 Candid", „No. 1 Oatmeal Brown"). Peste plafon, ce rămâne după
# trunchi nu e o nuanță — sunt două produse diferite care se întâmplă să înceapă la fel.
MAX_SHADE_TOKENS = 4


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ShadeAssignment:
    """Ce s-a derivat pentru un produs. `group` leagă nuanțele aceleiași linii FĂRĂ să atingă
    modelul de date: e o valoare în `attributes`, nu o muchie. Muchia `variant_of` cere migrarea
    din NX-270 (CHECK-ul de pe `product_relations.kind` admite azi patru valori), deci cardul de
    față produce gruparea, iar NX-270 o transformă în graf."""

    product_id: str
    shade: str  # exact cum apare în nume (invariant testat)
    shade_code: str | None  # prima parte, dacă e un cod („116", „No. 1")
    group: str  # amprenta liniei (brand + rădăcină + trunchi)
    trunk: str  # trunchiul comun, pentru raport și audit


def tokenize(name: str) -> list[str]:
    """Tokenii unui nume, PĂSTRÂND forma originală. Normalizarea se face separat, la comparație:
    dacă am normaliza aici, nuanța scrisă în `attributes` n-ar mai fi literalmente cea din nume, iar
    invariantul „apare în nume" ar deveni o aproximație."""
    return _TOKEN_RE.findall(name or "")


def _is_measurement(
    tokens: Sequence[str], start: int, end: int, unit_aliases: Mapping[str, str]
) -> bool:
    """Blocul e de fapt un gramaj („4.5 gr", „50 ml")? Atunci nu e o nuanță.

    Se uită și la tokenul de DUPĂ bloc, nu doar înăuntru: la două produse din aceeași linie care
    diferă doar prin gramaj („…Intens **15** gr" vs „…Intens **30** gr"), unitatea e identică, deci
    rămâne în afara blocului — iar un test care privește doar înăuntru ar scrie „15" ca nuanță.

    Unitățile vin din registrul NX-266 (pachetul tenantului), nu dintr-o listă de aici: aceeași
    tabelă care spune ce e un mililitru la o constrângere de client spune și ce nu e o nuanță."""
    block = [t.lower() for t in tokens[start:end]]
    if not any(re.fullmatch(r"\d+([.,]\d+)?", t) for t in block):
        return False
    following = tokens[end].lower() if end < len(tokens) else ""
    return any(t in unit_aliases for t in block) or following in unit_aliases


def _is_named(token: str) -> bool:
    """Tokenul arată ca un NUME propriu (inițială mare) în forma originală a numelui de produs?

    E un criteriu TIPOGRAFIC, nu lexical, și de aia poate sta în cod: nu spune ce e o culoare,
    spune că într-un nume de produs proza e cu literă mică, iar denumirile nu. Măsurat pe catalogul
    real, ăsta e singurul lucru care separă blocul „118 **Adore**" de blocul „din 10" — ambele
    conțin cifre, ambele variază între produsele aceleiași linii."""
    return bool(token) and token[0].isupper()


def _varying_block(
    a: Sequence[str], b: Sequence[str], original: Sequence[str]
) -> tuple[int, int] | None:
    """Blocul care VARIAZĂ între două nume din aceeași linie și arată ca o denumire.

    Presupunerea „numele diferă într-un singur loc" e falsă pe catalogul real: la LAKA, fiecare
    nuanță are și proza ei rescrisă de mână, deci un diff întoarce trei blocuri diferite — „de" vs
    „din 10", „mentinerea buzelor moi" vs „metinerea confortului", și abia al treilea „118 Adore"
    vs „114 Harmony". Un „ia ce diferă" naiv ar fi luat proza.

    Se ia deci ULTIMUL bloc care arată a DENUMIRE, cu trei condiții — toate tipografice, niciuna
    lexicală, fiindcă cardul interzice explicit o listă de culori:

    * blocul se STRÂNGE la miezul care arată a denumire (inițială mare sau cifră) — diff-ul lipește
      proza de nuanță când amândouă se schimbă, iar un bloc respins pentru un cuvânt de proză ar fi
      pierdut nuanța;
    * blocul conține un token cu cifră SAU se termină odată cu numele. Fără condiția asta, un
      acronim din mijlocul descrierii („formulat cu AHA si **BHA**") trecea drept nuanță —
      măsurat, era clasa dominantă de fals pozitiv la 95% acoperire;
    * ultimul astfel de bloc, fiindcă în formatul ăsta de nume denumirea vine după descriere.

    Dacă niciun bloc nu trece, produsul NU primește nuanță. Cardul cere precizie, nu acoperire:
    o bucată de propoziție scrisă în catalog ca „nuanță" e o afirmație falsă pe care nimic din aval
    n-o mai prinde."""
    blocks = [
        _trim_to_name(original, i1, i2)
        for tag, i1, i2, _, _ in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
        if tag != "equal" and i2 > i1
    ]
    named = [
        (i1, i2)
        for i1, i2 in blocks
        if i1 >= MIN_TRUNK_TOKENS
        and 1 <= i2 - i1 <= MAX_SHADE_TOKENS
        and (any(any(c.isdigit() for c in t) for t in original[i1:i2]) or i2 == len(original))
    ]
    return named[-1] if named else None


def _trim_to_name(original: Sequence[str], i1: int, i2: int) -> tuple[int, int]:
    """Taie din bloc, de la ambele capete, cuvintele care nu arată a denumire.

    Diff-ul nu separă curat: când proza se schimbă chiar lângă nuanță, blocul iese lipit —
    „**hidratare** 116 Candid" vs „**stralucire** 117 Harmony". Un bloc respins pentru un singur
    cuvânt de proză ar fi pierdut nuanța; un bloc acceptat cu el ar fi scris proza în catalog. Se
    strânge deci la miezul cu inițială mare sau cifră, iar dacă nu rămâne nimic, blocul e gol și
    cade singur la verificarea de mărime."""
    while i1 < i2 and not (_is_named(original[i1]) or original[i1][0].isdigit()):
        i1 += 1
    while i2 > i1 and not (_is_named(original[i2 - 1]) or original[i2 - 1][0].isdigit()):
        i2 -= 1
    return (i1, i2)


def derive_shades(
    products: Iterable[Mapping[str, str]],
    *,
    unit_aliases: Mapping[str, str] | None = None,
    roots: frozenset[str] | None = None,
) -> dict[str, ShadeAssignment]:
    """Produse → nuanțe, per (brand × rădăcină de categorie).

    `roots` restrânge derivarea la rădăcinile unde nuanța ÎNSEAMNĂ ceva (machiaj). E o listă de
    date, nu de cod: un produs de îngrijire cu un cod în nume nu primește nuanță pentru că
    rădăcina lui nu e declarată, nu pentru că am recunoscut noi că e o cremă. `None` = toate
    rădăcinile (util pentru măsurătoare, nu pentru scriere).

    Gruparea include BRANDUL în cheie: două branduri cu trunchi comun („Pure Vitamin C") sunt două
    linii, nu una, iar o grupare peste branduri ar produce „nuanțe" care sunt de fapt alt produs."""
    aliases = unit_aliases or {}
    buckets: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
    for product in products:
        root = str(product.get("root") or "")
        if roots is not None and root not in roots:
            continue
        tokens = tokenize(str(product.get("name") or ""))
        if not tokens:
            continue
        key = (str(product.get("brand") or "").strip().lower(), root)
        buckets.setdefault(key, []).append((str(product["id"]), tokens))

    out: dict[str, ShadeAssignment] = {}
    for (brand, root), members in buckets.items():
        if len(members) < 2:
            continue  # o linie cu un singur produs n-are axă de variație
        lowered = [[t.lower() for t in tokens] for _, tokens in members]
        for (product_id, tokens), low in zip(members, lowered):
            # Nuanța e SEGMENTUL VARIABIL, nu neapărat coada. Măsurat pe catalogul real, apare în
            # toate pozițiile: „…Cushion, **22N Shell Beige**, 18 gr - fond de ten…" (mijloc),
            # „…4.5 gr - **24N Latte**" (coadă), „…Pencil **No. 1 Oatmeal Brown** - precizie…".
            #
            # Două variante mai simple au fost măsurate și au picat, amândouă pe 20-24%: „cel mai
            # lung prefix comun" (numele variază la MIJLOC — „formulat" vs „formulata" — deci
            # prefixul se oprea devreme) și „prefix + coadă de șablon" (coada de marketing diferă
            # de la un produs la altul, deci nu se putea tăia). Ce funcționează e chiar definiția
            # din card: **ce rămâne diferit între doi membri ai liniei ESTE axa de variație.**
            # Se compară PERECHI, se ia fereastra cea mai mică. E O(n²) pe brand, dar jobul e
            # offline, iar cel mai mare brand are 139 de produse.
            best: tuple[int, int] | None = None  # (mărimea ferestrei, poziția de start)
            for other in lowered:
                if other is low:
                    continue
                span = _varying_block(low, other, tokens)
                if span is None:
                    continue
                candidate = (span[1] - span[0], span[0])
                if best is None or candidate < best:
                    best = candidate
            if best is None:
                continue
            trunk_len = best[1]
            shade_tokens = tokens[trunk_len : trunk_len + best[0]]
            if _is_measurement(tokens, trunk_len, trunk_len + best[0], aliases):
                continue
            trunk = " ".join(tokens[:trunk_len])
            fingerprint = hashlib.sha1(
                "\x00".join([brand, root, *low[:trunk_len]]).encode("utf-8")
            ).hexdigest()[:12]
            out[product_id] = ShadeAssignment(
                product_id=product_id,
                shade=" ".join(shade_tokens),
                shade_code=_shade_code(shade_tokens),
                group=fingerprint,
                trunk=trunk,
            )
    return out


def _shade_code(tokens: Sequence[str]) -> str | None:
    """Codul de producător dintr-o nuanță, dacă există: primul token care conține o cifră.

    „116 Candid" → „116"; „No. 1 Oatmeal Brown" → „1"; „Peach Glow" → `None`. Codul e ce se poate
    cere la telefon și ce se potrivește între magazine; numele nuanței e ce înțelege clientul.
    Amândouă se păstrează, fiindcă niciuna nu o înlocuiește pe cealaltă."""
    for token in tokens:
        if any(ch.isdigit() for ch in token):
            return token
    return None


def shade_appears_in_name(shade: str, name: str) -> bool:
    """Invariantul: nuanța derivată apare LITERAL în nume. Prin construcție e adevărat; testul
    există fiindcă e singura garanție că un fapt scris în catalog e derivat, nu inventat, iar
    validatorul din aval (stagiul 8) verifică adevărul FAȚĂ DE catalog — deci n-ar prinde-o."""
    return " ".join(tokenize(shade)).lower() in " ".join(tokenize(name)).lower()
