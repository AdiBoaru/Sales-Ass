"""Proza unui produs: ce afirmă DESPRE SINE și ce te sfătuiește să faci cu altceva (NX-286).

Problema, măsurată pe catalogul SOLE
------------------------------------
`spf` se derivă căutând „SPF <n>" în numele produsului și, dacă numele tace, în descriere.
Comentariul care însoțea regula numea deja riscul — *„Un «SPF 30» pomenit în proza unei rutine e
despre alt produs"* — dar nimic nu-l verifica. Rezultatul, pe 182 de produse cu `spf`, dintre care
44 derivate din descriere: **10 poartă un SPF care aparține altui produs.**

    „Sfaturi pentru utilizare sigura: Folositi crema cu SPF 50+ in timpul zilei, deoarece
     retinolul face pielea mai sensibila la soare."   → spf=50 pe o cremă cu RETINOL

Fraza nu descrie produsul, te trimite la altul. Iar produsele prinse așa sunt exact cele care
sensibilizează pielea la soare: seruri cu retinol, exfolianți AHA/BHA, plasturi cu retinol. Un
astfel de produs prezentat ca protecție solară nu e o recomandare slabă, e un sfat care contrazice
propria fișă a produsului.

De ce nu se scoate pur și simplu fallback-ul pe descriere
---------------------------------------------------------
Fiindcă 33 din cele 44 sunt CORECTE: protecții solare și BB cream-uri care își declară SPF-ul doar
în descriere („au integrat si protectie solara SPF 30", „Protectie UV puternica SPF 50+"). A
renunța la descriere ar schimba 10 valori greșite pe 33 de valori pierdute.

Ce e în cod și ce e în pachet (P9 + P11)
----------------------------------------
Codul știe că proza are FRAZE și că o frază poate fi un sfat. Nu știe CE cuvinte marchează un
sfat — alea sunt ale limbii, nu ale produsului, deci vin din pachet. Un `"folositi"` scris aici ar
fi și scurgere de domeniu (poarta NX-264) și românească hardcodată (P11).

Segmentarea în fraze nu e un detaliu
------------------------------------
Prima versiune a gărzii a marcat greșit o protecție solară reală (*SKIN1004 Air-Fit Suncream
SPF30*), fiindcă descrierile SOLE folosesc `&nbsp;` în loc de spațiu și uneori nu pun spațiu după
punct: fraza extrasă avea **908 de caractere** și înghițea textul de sfat de mai jos. Cu entitățile
HTML decodate și punctul lipit de majusculă tratat ca graniță, fraza medie e de 155 de caractere,
iar falsul pozitiv dispare. Deci `sentences()` e parte din corectitudine, nu din cosmetică.
"""

from __future__ import annotations

import html
import re

__all__ = ["is_advice", "sentences", "sentence_with"]

#: Punct/întrebare/exclamare urmat direct de majusculă („…volumizare.Deoarece produsul…") — graniță
#: reală de frază pe care un split naiv pe `[.!?]\s+` o ratează. Diacriticele românești majuscule
#: sunt incluse explicit: `\w` cu `re.UNICODE` le prinde, dar enumerarea face intenția verificabilă.
_GLUED = re.compile(r"([.!?;:])(?=[A-ZȘȚĂÎÂ])")

#: Graniță de frază: punctuație terminală + spațiu, SAU linie nouă.
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str | None) -> list[str]:
    """Proza → fraze. Decodează entitățile HTML ÎNAINTE de segmentare.

    `&nbsp;` nu e spațiu pentru un regex, deci o descriere plină de ele arată ca o singură frază
    uriașă — exact cum a apărut falsul pozitiv descris în antetul modulului.
    """
    if not text:
        return []
    plain = html.unescape(text).replace("\xa0", " ")
    plain = _GLUED.sub(r"\1 ", plain)
    return [s.strip() for s in _SPLIT.split(plain) if s.strip()]


def sentence_with(text: str | None, pattern: re.Pattern[str]) -> str | None:
    """Prima frază care conține potrivirea, sau None. Unitatea pe care se judecă contextul.

    Există ca funcție separată fiindcă „unde a apărut valoarea" e o întrebare distinctă de „ce
    valoare e" — iar a le amesteca e cum a apărut defectul: regexul căuta în TOT textul.
    """
    for sentence in sentences(text):
        if pattern.search(sentence):
            return sentence
    return None


def is_advice(sentence: str | None, markers: tuple[str, ...]) -> bool:
    """Fraza e un SFAT (te trimite la altă acțiune sau alt produs), nu o afirmație despre produs?

    `markers` sunt RĂDĂCINI normalizate, venite din pachet — rădăcini, nu forme, fiindcă româna
    flexionează: „se recomandă" / „recomandat" / „recomandăm" sunt aceeași instrucțiune, iar o
    listă de forme ratează exact a treia. Lipsa lor ⇒ `False`: fără vocabular declarat, garda nu
    presupune nimic (P6 — tăcerea nu acordă putere de respingere).

    Potrivirea e ancorată la ÎNCEPUT de cuvânt, nu oriunde în șir. Un `in` simplu pare echivalent
    și nu e: prima versiune a gărzii a marcat greșit o protecție solară reală (*Beauty of Joseon
    Ginseng Moist Sun Serum*), fiindcă markerul „aplica" se potrivea în interiorul substantivului
    „aplicarea" — „face aplicarea protectiei solare usoara" e o descriere, nu un imperativ. Un
    marker poate fi doar PREFIX de cuvânt, niciodată infix.
    """
    if not sentence or not markers:
        return False
    low = sentence.lower()
    return any(
        re.search(r"\b" + re.escape(marker.strip()), low) for marker in markers if marker.strip()
    )
