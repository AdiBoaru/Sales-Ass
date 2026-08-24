"""Vocea răspunsului: cum SUNĂ textul care ajunge la client (formă, nu fapte).

Două piese, în ordinea în care lucrează:

  • `VOICE_RULES` — contractul SCURT injectat în toate prompturile de compunere (bucla de
    tool-calling, recompunerea, recomandarea structurată, statusul de comandă, triajul nano,
    MainBrain). Stratul PREVENTIV: modelul scrie de la început cum trebuie.
  • `naturalize()` — plasa DETERMINISTĂ, funcție PURĂ, aplicată pe textul ieșit din model.
    Modelele pun liniuță de pauză și punct-și-virgulă chiar când li se cere să nu, iar o regulă
    de prompt fără plasă e o speranță, nu o garanție.

Regula (Adi, fermă): un mesaj nu trebuie să „se vadă că e făcut cu AI". Semnul de AI numărul unu
în română e liniuța de pauză folosită ca aside („ten sensibil — cu niacinamidă"), urmată de
punctul și virgula. Un om scrie virgulă sau începe o frază nouă.

CE NU ATINGE (deliberat):
  • Cratima din cuvinte: „să-ți", „nu-s", „ți-l" sunt ortografie corectă, nu stil.
  • Intervalele numerice: „2 - 3 zile" devine „2-3 zile" (forma tipografică corectă), NU „2, 3".
  • Liniuța de la începutul unui rând (bullet de listă): nu e „în timpul propoziției".
  • En dash lipit între cifre („2–19"): e interval, nu pauză.
  • Faptele. Aici se schimbă DOAR punctuația; prețurile, numele și linkurile rămân ce erau, deci
    normalizarea nu poate invalida un text care tocmai a trecut de validator (P2).

Idempotentă: `naturalize(naturalize(t)) == naturalize(t)`. Se poate aplica pe mai multe straturi
(scrub în `compose`, plus plasa din `TurnContext.set_reply`) fără efecte cumulate.

NB de stil: docstring-urile și comentariile din repo folosesc em dash ca peste tot în cod. Regula
de mai sus e despre textul CĂTRE CLIENT, nu despre sursă.
"""

from __future__ import annotations

import re

# Contractul de voce pentru prompturi. SCURT și fără zid de majuscule: ce urlă în prompt nu e
# garantat oricum (plasa e `naturalize`), iar un prompt care urlă strică restul instrucțiunilor.
# TENANT-INVARIANT → parte din prefixul static, nu strică prompt-caching-ul.
# D3/principiul 11: regula de PUNCTUAȚIE e universală; „cuvinte firești" se rezolvă prin „în limba
# clientului", nu prin hardcodarea românei.
VOICE_RULES = """
Voce: scrii ca un om, nu ca un AI. În limba clientului, cu cuvintele firești ale acelei limbi,
fraze scurte și corecte gramatical (acorduri, diacritice).
- Fără liniuță de pauză în frază („—", „–" sau „-" între spații). Pune virgulă, începe o frază
  nouă sau leagă cu „că", „fiindcă", „așa că", „iar". Cratima din cuvinte („să-ți", „nu-s") rămâne.
- Fără punct și virgulă („;"), fără săgeți („→"), fără înșiruiri cu „+".
- Fără limbaj de fișă tehnică și fără să-ți anunți procesul."""


# Liniuța de PAUZĂ: em/en dash sau unul-două minusuri, izolate între spații pe același rând.
# `(?<=\S)` la început ⇒ un bullet de listă („- Produs", chiar indentat) nu se prinde.
# Lookahead-ul cere spațiu/final după liniuță ⇒ „-50%" și „ten-ul" rămân neatinse.
_PAUSE_DASH = re.compile(r"(?<=\S)[ \t]+(?:[—–―]|-{1,2})(?=[ \t]|$|\n)[ \t]*")

# Em dash LIPIT între litere („cuvânt—cuvânt"). En dash lipit e exclus intenționat: între cifre e
# interval („2–19"), iar `[^\W\d_]` cere litere de ambele părți.
_EM_TIGHT = re.compile(r"(?<=[^\W\d_])[—―](?=[^\W\d_])")

# Punct și virgulă, cu spațiile din jur.
_SEMICOLON = re.compile(r"[ \t]*;[ \t]*")

#: După aceste semne, pauza se șterge fără să mai adăugăm virgulă (ar dubla punctuația).
_ALREADY_PUNCTUATED = (",", ":", ".", "!", "?", "…", ";")


def _pause_repl(m: re.Match[str]) -> str:
    """Cu ce înlocuim liniuța de pauză: interval numeric → liniuță lipită; sfârșit de rând →
    nimic; după punctuație → doar spațiu; altfel → virgulă."""
    src = m.string
    before = src[: m.start()].rstrip()
    after = src[m.end() :]
    if before[-1:].isdigit() and after[:1].isdigit():
        return "-"  # interval: „2 - 3 zile" → „2-3 zile"
    if not after or after.startswith("\n"):
        return ""  # liniuță orfană la capăt de rând
    if before.endswith(_ALREADY_PUNCTUATED):
        return " "
    return ", "


def _semicolon_repl(m: re.Match[str]) -> str:
    """`;` devine virgulă. NU punct: ar cere recapitalizarea frazei următoare, iar într-o
    enumerare („X; Y; Z") punctul ar rupe lista. Virgula e corectă în ambele situații."""
    src = m.string
    before = src[: m.start()].rstrip()
    after = src[m.end() :]
    if not after or after.startswith("\n"):
        return ""
    if before.endswith(_ALREADY_PUNCTUATED):
        return " "
    return ", "


def naturalize(text: str | None) -> str | None:
    """Scoate din text semnele care îl fac să sune a AI, fără să atingă faptele.

    `None`/gol trec nemodificate (apelanții tratează deja absența textului). Funcție PURĂ: zero
    I/O, zero ceas, zero stare ⇒ două apeluri pe același input dau aceiași bytes.
    """
    if not text:
        return text
    out = _PAUSE_DASH.sub(_pause_repl, text)
    out = _EM_TIGHT.sub(", ", out)
    return _SEMICOLON.sub(_semicolon_repl, out)
