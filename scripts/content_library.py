"""NX-196 — BIBLIOTECA DE CONȚINUT: paragrafele scrise de om, din care se compun fișele de produs.

De ce așa și nu „un LLM scrie 300 de descrieri”: textul de aici e **scris**, revizuit și versionat
ca orice cod. Nu există niciun apel de generare la rulare — compunerea e deterministă, deci aceeași
intrare dă mereu același text, poate fi diff-uită într-un PR și poate trece o poartă de audit.

Varietatea NU vine din sinonime, ci din combinatorică reală: fiecare produs primește paragraful
ingredientului-erou pe care CHIAR îl are, paragraful nevoii pe care o adresează, fraza de textură
a texturii lui și scenariile categoriei lui. Două seruri din aceeași gamă ajung la texte diferite
pentru că faptele lor diferă — nu pentru că am rescris aceleași idei cu alte cuvinte.

REGULI DE SCRIERE (respectate în tot fișierul):
  • zero claim medical — fără „tratează”, „vindecă”, „recomandat de dermatolog”, „hipoalergenic”;
    formulările sunt cosmetice („ajută la”, „susține”, „lasă senzația de”);
  • zero cifre volatile — niciun preț, stoc sau termen de livrare; alea se citesc din coloane;
  • zero superlative negarantabile — fără „cel mai bun”, „garantat”, „în 3 zile”;
  • ton de consultant, nu de reclamă: spune CE face și PENTRU CINE, nu cât de minunat e.
"""

from __future__ import annotations

import unicodedata


def norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower().strip())
    return "".join(c for c in d if not unicodedata.combining(c))


# --------------------------------------------------------------------------------------------- #
# INGREDIENTE — paragraful ingredientului-erou. Cheia e forma NORMALIZATĂ; sinonimele din catalog
# („pantenol”/„panthenol”, „centella”/„centella asiatica”) trimit la aceeași intrare.
# --------------------------------------------------------------------------------------------- #
_ING_ALIASES = {
    "pantenol": "panthenol",
    "provitamina b5": "panthenol",
    "centella asiatica": "centella",
    "aloe": "aloe vera",
    "ceai verde": "extract de ceai verde",
    "hamamelis": "extract de hamamelis",
    "proteine de grau": "proteine din grau",
    "proteine hidrolizate": "proteine din grau",
    "acid glicolic/lactic (aha)": "acid glicolic",
    "aha din fructe": "acid glicolic",
    "acid lactic": "acid glicolic",
    "filtru uv": "filtre uv",
    "filtre uva/uvb": "filtre uv",
    "filtre minerale-organice": "filtre uv",
    "zahar brun": "zahar",
    "zinc pca": "zinc",
    "extract de papaya": "enzime de papaya",
    "extract de menta": "extract de menta",
}

ING_PARA: dict[str, str] = {
    "acid hialuronic": (
        "Acidul hialuronic atrage apa în straturile de suprafață ale pielii și o ține acolo, "
        "așa că tenul arată mai plin imediat după aplicare. Efectul e cel mai vizibil pe liniile "
        "fine de deshidratare — alea care apar seara, după o zi lungă, și dispar când pielea e "
        "bine hidratată."
    ),
    "glicerina": (
        "Glicerina e unul dintre cele mai vechi și mai bine documentate ingrediente de hidratare: "
        "trage apa spre piele și reduce senzația de strângere după curățare. Nu face spectacol, "
        "dar se simte în confortul de peste zi."
    ),
    "niacinamida": (
        "Niacinamida lucrează pe două direcții deodată: ajută la echilibrarea sebumului și "
        "uniformizează treptat aspectul tenului. E unul dintre puținele ingrediente active pe "
        "care majoritatea tipurilor de piele le tolerează bine, inclusiv cele reactive."
    ),
    "vitamina c": (
        "Vitamina C aduce luminozitate: în timp, tenul arată mai odihnit și petele lăsate în urmă "
        "de imperfecțiuni se estompează. Se folosește dimineața, sub protecție solară, unde își "
        "face treaba cel mai bine."
    ),
    "acid ferulic": (
        "Acidul ferulic însoțește vitamina C și îi stabilizează acțiunea, ca formula să rămână "
        "eficientă mai mult timp după deschidere."
    ),
    "retinol": (
        "Retinolul e ingredientul cu cea mai lungă listă de studii în spate pentru textura pielii "
        "și liniile fine. Se introduce treptat — două seri pe săptămână la început — și se "
        "folosește doar seara, pentru că sensibilizează pielea la soare."
    ),
    "retinal": (
        "Retinalul e o formă care acționează mai direct decât retinolul clasic, deci rezultatele "
        "apar de obicei mai repede. Cere aceeași disciplină: seara, treptat, cu protecție solară "
        "dimineața."
    ),
    "acid salicilic": (
        "Acidul salicilic e solubil în ulei, așa că ajunge în interiorul porului și ajută la "
        "desfundarea lui. De asta apare aproape mereu în produsele pentru ten gras și predispus "
        "la imperfecțiuni."
    ),
    "acid glicolic": (
        "Acizii AHA desprind delicat celulele moarte de la suprafață, iar pielea arată mai netedă "
        "și reflectă mai bine lumina. Se folosesc seara, iar în zilele următoare protecția solară "
        "nu e opțională."
    ),
    "acid azelaic": (
        "Acidul azelaic e alegerea pentru cine vrea uniformizare fără iritare: lucrează pe "
        "roșeață și pe urmele lăsate de imperfecțiuni, dar e blând cu pielea reactivă."
    ),
    "ceramide": (
        "Ceramidele sunt cărămizile care țin bariera pielii etanșă. Când sunt puține, apa se "
        "pierde și tenul devine sensibil la orice; când sunt completate, pielea se simte "
        "din nou confortabilă și rezistă mai bine la frig și vânt."
    ),
    "peptide": (
        "Peptidele sunt lanțuri scurte de aminoacizi care susțin fermitatea pielii. Efectul lor "
        "e cumulativ — se vede după săptămâni de folosire constantă, nu după prima aplicare."
    ),
    "colagen": (
        "Colagenul din formulă lucrează la suprafață: reține apa și lasă pielea mai netedă la "
        "atingere imediat după aplicare."
    ),
    "panthenol": (
        "Panthenolul calmează și reface confortul pielii după orice agresiune — soare, vânt, "
        "un produs prea puternic. E ingredientul pe care îl cauți când pielea „arde” ușor."
    ),
    "bisabolol": (
        "Bisabololul, extras din mușețel, e unul dintre cele mai blânde ingrediente calmante. "
        "Reduce senzația de disconfort fără să lase film pe piele."
    ),
    "centella": (
        "Centella asiatica e ingredientul-vedetă al îngrijirii pentru piele reactivă: liniștește "
        "senzația de usturime și susține refacerea confortului."
    ),
    "aloe vera": (
        "Aloe vera răcorește imediat și aduce un strat ușor de hidratare, fără să încarce. "
        "Se simte mai ales pe pielea încălzită de soare sau de apă fierbinte."
    ),
    "ovaz coloidal": (
        "Ovăzul coloidal formează un strat fin care reduce senzația de mâncărime și de piele "
        "întinsă. E o alegere clasică pentru pielea foarte uscată sau reactivă."
    ),
    "alantoina": (
        "Alantoina netezește și calmează, fiind adesea pusă în formule alături de ingrediente "
        "mai active, ca să echilibreze senzația de pe piele."
    ),
    "extract de ceai verde": (
        "Extractul de ceai verde aduce antioxidanți care ajută pielea să facă față mai bine "
        "poluării și stresului de peste zi."
    ),
    "extract de hamamelis": (
        "Hamamelisul strânge aspectul porilor și lasă pielea cu senzație de prospețime, fără "
        "alcoolul care usca tonicele de altădată."
    ),
    "apa termala": (
        "Apa termală aduce minerale și o senzație imediată de calmare — utilă când pielea e "
        "iritată de soare, ras sau tratamente mai puternice."
    ),
    "apa de trandafiri": (
        "Apa de trandafiri împrospătează și lasă un parfum discret, natural, fără să usuce."
    ),
    "squalan": (
        "Squalanul imită un lipid pe care pielea îl produce singură, așa că se absoarbe rapid și "
        "nu lasă senzație grasă. E o alegere sigură inclusiv pentru pielea care se congestionează "
        "ușor."
    ),
    "unt de shea": (
        "Untul de shea e ingredientul care face diferența pe pielea foarte uscată: formează un "
        "strat protector care ține hidratarea înăuntru ore bune."
    ),
    "unt de cacao": (
        "Untul de cacao e bogat și învăluitor, potrivit pentru zonele care se usucă cel mai tare "
        "— coate, genunchi, mâini după spălări repetate."
    ),
    "ulei de jojoba": (
        "Uleiul de jojoba e apropiat ca structură de sebumul natural, motiv pentru care se "
        "absoarbe uniform și nu lasă senzația de film gras."
    ),
    "ulei de argan": (
        "Uleiul de argan aduce suplețe și luciu, fără să îngreuneze. Se folosește în cantitate "
        "mică — o picătură-două fac diferența."
    ),
    "ulei de macadamia": (
        "Uleiul de macadamia e ușor și pătrunde repede, potrivit când vrei nutriție fără senzație "
        "de greutate."
    ),
    "ulei de maceșe": (
        "Uleiul de măceșe e apreciat pentru aportul de acizi grași și pentru senzația de piele "
        "netedă pe care o lasă după absorbție."
    ),
    "ulei de masline": (
        "Uleiul de măsline hrănește intens și e o alegere bună pentru pielea care se descuamează "
        "iarna."
    ),
    "ulei de cocos": (
        "Uleiul de cocos învăluie firul de păr și reduce aspectul uscat al lungimilor."
    ),
    "ulei de floarea-soarelui": (
        "Uleiul de floarea-soarelui e ușor și neutru, folosit ca bază care transportă restul "
        "ingredientelor fără să încarce."
    ),
    "ulei esential de lavanda": (
        "Uleiul esențial de lavandă aduce un parfum liniștitor, potrivit pentru rutina de seară."
    ),
    "ceara de albine": (
        "Ceara de albine sigilează hidratarea și dă consistența aceea confortabilă, care rămâne "
        "pe piele fără să se șteargă imediat."
    ),
    "vitamina e": (
        "Vitamina E protejează formula de oxidare și lasă pielea mai suplă — de asta apare des "
        "alături de uleiuri și de vitamina C."
    ),
    "cofeina": (
        "Cofeina are efect de tonifiere locală și e folosită mai ales în zona ochilor, unde "
        "aspectul umflat de dimineață se reduce vizibil."
    ),
    "zinc": (
        "Zincul ajută la controlul luciului și e des folosit în formulele pentru ten gras sau "
        "predispus la imperfecțiuni."
    ),
    "argila": (
        "Argila absoarbe excesul de sebum și lasă pielea mată — de aceea măștile cu argilă se "
        "folosesc punctual, nu zilnic."
    ),
    "sare de mare": (
        "Sarea de mare exfoliază mecanic și lasă pielea netedă; e potrivită pentru zonele "
        "rezistente ale corpului, nu pentru față."
    ),
    "zahar": (
        "Cristalele de zahăr se dizolvă treptat în timpul masajului, așa că exfolierea începe "
        "ferm și se termină blând."
    ),
    "arrowroot": (
        "Amidonul de arrowroot absoarbe umezeala și lasă senzația de piele uscată, fără pudra "
        "vizibilă."
    ),
    "enzime de papaya": (
        "Enzimele de papaya desprind celulele moarte fără frecare — o exfoliere blândă, potrivită "
        "când pielea nu tolerează granulele."
    ),
    "extract de menta": (
        "Extractul de mentă lasă o senzație răcoritoare imediată, plăcută mai ales vara."
    ),
    "ferment de orez/galactomyces": (
        "Fermentul de orez aduce senzația aceea de piele „odihnită” și e folosit în formulele "
        "care țintesc luminozitatea, nu exfolierea."
    ),
    "keratina": (
        "Keratina completează exact proteina din care e făcut firul de păr, așa că lungimile "
        "deteriorate se simt mai pline și se rup mai greu la pieptănat."
    ),
    "proteine din grau": (
        "Proteinele hidrolizate se așază pe firul de păr și îi dau corp — util mai ales pe părul "
        "fin, care se aplatizează repede."
    ),
    "proteine din matase": (
        "Proteinele din mătase lasă părul neted și cu luciu discret, fără să îl îngreuneze."
    ),
    "filtre uv": (
        "Filtrele UV protejează împotriva razelor UVA și UVB. Protecția e reală doar dacă se "
        "aplică într-un strat suficient și se reînnoiește la expunere prelungită."
    ),
}


def ingredient_para(name: str) -> str | None:
    key = _ING_ALIASES.get(norm(name), norm(name))
    return ING_PARA.get(key)


# --------------------------------------------------------------------------------------------- #
# NEVOI (concerns) — paragraful „ce problemă adresează”. Cheile sunt canonice (enum v3).
# --------------------------------------------------------------------------------------------- #
CONCERN_PARA: dict[str, str] = {
    "hydration": (
        "Pielea deshidratată nu e neapărat piele uscată: poate fi grasă și, în același timp, "
        "lipsită de apă. Se recunoaște după senzația de strângere după curățare și după liniile "
        "fine care apar seara și dispar a doua zi dimineața."
    ),
    "dry": (
        "Pielea uscată produce mai puțini lipizi, așa că pierde apa mai repede și se descuamează "
        "la frig sau după spălări dese. Are nevoie de formule care refac stratul protector, nu "
        "doar de apă."
    ),
    "oily": (
        "Tenul gras produce sebum în exces, mai ales în zona T. Soluția nu e uscarea agresivă — "
        "care declanșează și mai mult sebum — ci echilibrarea și o hidratare ușoară."
    ),
    "combination": (
        "Tenul mixt cere două lucruri în același timp: control al luciului pe frunte și nas, și "
        "confort pe obraji. De aceea texturile ușoare, care hidratează fără să încarce, "
        "funcționează cel mai bine."
    ),
    "sensitive": (
        "Pielea sensibilă reacționează repede la parfum, alcool și la activi prea puternici. "
        "Se simte bine cu formule scurte, calmante, introduse pe rând — nu toate deodată."
    ),
    "acne": (
        "Pielea predispusă la imperfecțiuni are nevoie de constanță, nu de intensitate: curățare "
        "blândă, un activ care ține porii liberi și hidratare care nu îi încarcă."
    ),
    "anti_aging": (
        "Odată cu timpul, pielea își pierde din fermitate și textura devine mai neregulată. "
        "Ingredientele care contează aici lucrează lent și cumulativ — de asta constanța bate "
        "concentrația."
    ),
    "hyperpigmentation": (
        "Petele și urmele lăsate de imperfecțiuni se estompează încet, cu ingrediente care "
        "uniformizează și, obligatoriu, cu protecție solară zilnică — fără ea, progresul se pierde."
    ),
    "normal": (
        "Pielea echilibrată nu are nevoie de intervenții puternice, ci de o rutină simplă și "
        "constantă, care o menține așa cum este."
    ),
}

HAIR_PARA: dict[str, str] = {
    "uscat": (
        "Părul uscat pierde apa repede: se electrizează, se încâlcește și își pierde luciul. "
        "Are nevoie de hidratare pe lungimi și de produse care închid solzii firului."
    ),
    "deteriorat": (
        "Părul deteriorat de decolorare, placă sau uscător are cuticula ridicată — de asta se "
        "rupe la pieptănat și se simte aspru. Se repară prin proteine și lipide, aplicate constant."
    ),
    "vopsit": (
        "Părul vopsit pierde pigment la fiecare spălare, mai ales cu apă fierbinte. Produsele "
        "blânde, fără sulfați agresivi, prelungesc culoarea între ședințe."
    ),
    "gras": (
        "Scalpul gras se reîncarcă rapid, iar spălatul prea des îl face să producă și mai mult. "
        "Ideea e să cureți scalpul bine și să lași lungimile în pace."
    ),
    "fin": (
        "Părul fin se aplatizează repede și nu suportă produsele grele. Cantitățile mici, "
        "aplicate pe lungimi și nu la rădăcină, fac diferența."
    ),
    "cret": (
        "Părul creț are nevoie de apă și de definire: se usucă mai repede decât cel drept și "
        "cere produse care mențin bucla fără să o încarce."
    ),
    "curly": (
        "Buclele cer hidratare constantă și cât mai puțină frecare — de asta produsele fără "
        "clătire și aplicarea pe părul umed contează atât de mult."
    ),
    "normal": (
        "Părul normal se menține cel mai bine cu o rutină simplă: curățare blândă și hidratare "
        "pe lungimi, fără exces."
    ),
    "toate tipurile": (
        "Formula e gândită să funcționeze pe majoritatea tipurilor de păr, ceea ce o face o "
        "alegere sigură când nu vrei să te complici."
    ),
}

TEXTURE_PARA: dict[str, str] = {
    "gel": "Textura de gel se absoarbe rapid și lasă pielea răcorită, fără film gras.",
    "cremă": (
        "Textura de cremă e confortabilă și rămâne pe piele suficient cât să se simtă hidratarea."
    ),
    "fluid": "Textura fluidă se întinde ușor și dispare repede, fără să încarce.",
    "apă": (
        "Consistența apoasă se aplică în strat subțire și pregătește pielea pentru pașii următori."
    ),
    "loțiune": "Loțiunea e ușoară și se absoarbe uniform, potrivită pentru suprafețe mari.",
    "balsam": "Balsamul se topește la contactul cu pielea și lasă un strat protector confortabil.",
    "lichid": "Consistența lichidă se dozează ușor și se distribuie uniform.",
    "ulei": "Uleiul se aplică în cantitate mică și se absoarbe fără să lase senzație grea.",
    "spumă": "Spuma se activează în palme și curăță cu o cantitate mică de produs.",
    "unt": "Textura de unt e densă și bogată, gândită pentru zonele care se usucă cel mai tare.",
    "pudră": "Textura pudrată se așază fin și se estompează ușor.",
    "stick": "Formatul stick se aplică direct, fără să-ți murdărești mâinile.",
}

USAGE_PARA: dict[str, str] = {
    "morning": ("Dimineața se aplică pe pielea curată, înainte de protecția solară și de machiaj."),
    "evening": (
        "Seara se aplică după curățare, când pielea are toată noaptea la dispoziție să lucreze."
    ),
    "daily": (
        "Se folosește în rutina obișnuită, fără pauze — constanța contează mai mult "
        "decât cantitatea."
    ),
    "occasional": "Se folosește punctual, de una-două ori pe săptămână, nu zilnic.",
}
