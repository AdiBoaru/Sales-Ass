"""NX-196 — blocurile de conținut per CATEGORIE: introduceri, rol în rutină, scenarii, mod de
aplicare. Scrise de om, nu generate.

Fiecare categorie are 2-3 variante de introducere, alese determinist după slug-ul produsului →
două produse din aceeași categorie NU încep cu aceeași frază, dar textul rămâne stabil între
rulări (diff-abil în PR).

`zone` decide formulările din restul compunerii: „ten” / „păr” / „corp” / „buze” / „machiaj”.
Fără ea ies aberații de tipul „Balsam de buze pentru ten uscat” (le-am avut deja o dată).
"""

from __future__ import annotations

# cheile: intro (variante), role (rolul în rutină), how (cum se aplică), scenarios (5),
# features (bullets adevărate pentru CATEGORIE, completate de fapte la compunere), zone
CATEGORY: dict[str, dict] = {
    # ---------------------------------------------------------------- ÎNGRIJIREA TENULUI ----
    "seruri-pentru-ten": {
        "zone": "ten",
        "intro": [
            "Serul e pasul în care pui concentrația. Vine după curățare și înainte de cremă, iar "
            "rolul lui nu e să hidrateze superficial, ci să ducă ingredientele active acolo unde "
            "contează, într-o textură care se absoarbe complet.",
            "Dintre toți pașii unei rutine, serul e cel care schimbă cel mai vizibil felul în "
            "care arată pielea în câteva săptămâni. E concentrat, se aplică în cantitate mică și "
            "se lasă să pătrundă înainte de pasul următor.",
        ],
        "role": "pas concentrat de îngrijire, între curățare și hidratare",
        "how": "Aplică 3-4 picături pe pielea curată și ușor umedă, bate ușor cu degetele până se "
        "absoarbe, apoi continuă cu crema.",
        "scenarios": [
            "În rutina de dimineață, sub cremă și protecție solară",
            "Seara, ca pas de tratament după curățare",
            "Când pielea arată obosită după o perioadă cu somn puțin",
            "Iarna, când aerul uscat din interior deshidratează tenul",
            "Ca prim pas de tratament, dacă vrei să introduci un singur activ nou",
        ],
        "features": ["Se absoarbe complet, fără film gras", "Se folosește sub cremă"],
    },
    "creme-hidratante": {
        "zone": "ten",
        "intro": [
            "Crema hidratantă e pasul care închide rutina: sigilează ce ai aplicat înainte și "
            "menține apa în piele până la următoarea curățare. E și cea mai simplă cale de a "
            "verifica dacă o rutină funcționează — o piele bine hidratată nu strânge niciodată.",
            "O cremă bună nu se simte. Se absoarbe, lasă pielea confortabilă câteva ore bune și "
            "nu împiedică nimic din ce vine după ea — nici serul de dedesubt, nici machiajul de "
            "deasupra.",
        ],
        "role": "hidratare și protecție, ultimul pas înainte de SPF sau machiaj",
        "how": "Aplică o cantitate de mărimea unui bob de mazăre pe toată fața, cu mișcări de jos "
        "în sus, dimineața și seara.",
        "scenarios": [
            "Dimineața, ca bază înainte de machiaj",
            "Seara, ca ultim pas al rutinei",
            "Iarna, când vântul și aerul uscat lasă pielea aspră",
            "După o zi la birou, în aer condiționat",
            "Ca singur pas de îngrijire, în zilele în care nu ai timp de rutină completă",
        ],
        "features": ["Se poartă bine sub machiaj", "Nu lasă senzație de piele încărcată"],
    },
    "curatarea-tenului": {
        "zone": "ten",
        "intro": [
            "Curățarea e pasul pe care îl faci de două ori pe zi, deci și cel care poate strica "
            "cel mai mult dacă e prea agresivă. Un produs bun scoate sebumul și impuritățile, dar "
            "lasă bariera pielii intactă — nu ar trebui să simți niciodată pielea „scârțâind”.",
            "Dacă pielea ta strânge după spălare, produsul e prea puternic, nu pielea prea "
            "sensibilă. O curățare corectă lasă tenul curat și calm, pregătit pentru ce urmează.",
        ],
        "role": "primul pas al rutinei, dimineața și seara",
        "how": "Masează pe pielea umedă 30 de secunde, insistând pe zona T, apoi clătește cu apă "
        "călduță — nu fierbinte.",
        "scenarios": [
            "Dimineața, ca prim pas al rutinei",
            "Seara, ca al doilea pas după demachiere",
            "După sport, când transpirația se amestecă cu sebumul",
            "Când tenul se congestionează într-o perioadă mai stresantă",
            "Ca pas unic de curățare, dacă nu porți machiaj",
        ],
        "features": ["Nu lasă senzația de piele care trage", "Se clătește ușor"],
    },
    "demachiante-pentru-ten": {
        "zone": "ten",
        "intro": [
            "Demachierea e pasul de care depinde tot restul rutinei: dacă machiajul și protecția "
            "solară rămân pe piele, niciun ser nu are cum să ajungă unde trebuie. Se face seara, "
            "înainte de curățarea propriu-zisă.",
            "Un demachiant bun ridică machiajul fără frecare. Zona ochilor, în special, cere "
            "răbdare — câteva secunde de așteptare fac mai mult decât zece treceri cu discul.",
        ],
        "role": "primul pas de seară, înainte de curățare",
        "how": "Aplică pe pielea uscată sau pe un disc, lasă câteva secunde să dizolve machiajul, "
        "apoi continuă cu gelul de curățare.",
        "scenarios": [
            "Seara, ca prim pas al dublei curățări",
            "Pentru demachierea ochilor, fără frecare",
            "După o zi cu protecție solară aplicată corect",
            "Când porți machiaj rezistent la transfer",
            "Rapid, în serile în care ești prea obosită pentru rutina completă",
        ],
        "features": ["Ridică machiajul fără frecare", "Potrivit și pentru zona ochilor"],
    },
    "lotiuni-tonice": {
        "zone": "ten",
        "intro": [
            "Tonicul de azi nu mai are nimic în comun cu cel din anii '90: nu mai e alcool care "
            "usucă, ci un pas de rehidratare care pregătește pielea pentru ser. Se aplică pe "
            "pielea încă umedă, imediat după curățare.",
            "Rolul tonicului e să readucă apa pe care curățarea a luat-o și să facă pașii "
            "următori mai eficienți. E opțional într-o rutină minimalistă, dar se simte în "
            "confortul de după.",
        ],
        "role": "rehidratare imediat după curățare",
        "how": "Aplică pe palme sau pe un disc și presează ușor pe față, fără să freci, cât timp "
        "pielea e încă umedă.",
        "scenarios": [
            "Imediat după curățare, dimineața și seara",
            "Ca pas de prospețime la mijlocul zilei",
            "Înainte de ser, ca să ajute absorbția",
            "Când pielea se simte strânsă după apă tare",
            "Vara, ținut la frigider pentru senzație de răcorire",
        ],
        "features": ["Fără alcool care usucă", "Pregătește pielea pentru pașii următori"],
    },
    "masti-pentru-ten": {
        "zone": "ten",
        "intro": [
            "Masca e pasul de intensitate, nu de rutină zilnică. Se folosește de una-două ori pe "
            "săptămână, când pielea are nevoie de o corecție rapidă — hidratare, calmare sau "
            "control al luciului.",
            "O mască nu înlocuiește rutina, o completează. Efectul e imediat și vizibil, dar ține "
            "câteva zile — de asta se folosește ciclic, nu în fiecare seară.",
        ],
        "role": "îngrijire intensivă, de 1-2 ori pe săptămână",
        "how": "Aplică un strat uniform pe pielea curată, lasă 10-15 minute, apoi clătește sau "
        "masează surplusul, în funcție de formulă.",
        "scenarios": [
            "Seara, ca ritual de îngrijire săptămânal",
            "Înainte de un eveniment, pentru un ten odihnit",
            "După o perioadă de stres sau somn puțin",
            "Iarna, când pielea are nevoie de un plus de confort",
            "Ca pas de reset, după câteva zile cu machiaj greu",
        ],
        "features": ["Efect vizibil de la prima folosire", "Se folosește de 1-2 ori pe săptămână"],
    },
    "exfoliante-pentru-ten": {
        "zone": "ten",
        "intro": [
            "Exfolierea îndepărtează celulele moarte care fac pielea să pară ternă și împiedică "
            "produsele să pătrundă. Făcută corect — rar și blând — schimbă vizibil textura; "
            "făcută prea des, strică bariera.",
            "Regula simplă a exfolierii: mai puțin înseamnă mai mult. De două ori pe săptămână e "
            "suficient pentru majoritatea tipurilor de piele, iar protecția solară în zilele "
            "următoare nu e negociabilă.",
        ],
        "role": "reînnoire a suprafeței, de 1-2 ori pe săptămână, seara",
        "how": "Aplică seara, pe pielea curată și uscată, evitând conturul ochilor. Nu combina cu "
        "alți acizi în aceeași seară.",
        "scenarios": [
            "Seara, de 1-2 ori pe săptămână",
            "Când tenul arată tern după iarnă",
            "Înainte de o mască hidratantă, ca să pătrundă mai bine",
            "Când machiajul nu mai stă uniform pe piele",
            "Ca pas de întreținere între tratamentele profesionale",
        ],
        "features": ["Se folosește seara", "Nu se combină cu alți acizi în aceeași seară"],
    },
    "creme-de-ochi": {
        "zone": "ten",
        "intro": [
            "Pielea din jurul ochilor e de câteva ori mai subțire decât restul feței și nu are "
            "aproape deloc glande sebacee. De asta se deshidratează prima și de asta cere o "
            "formulă gândită special pentru ea.",
            "Crema de ochi nu face minuni peste noapte, dar previne: menține zona hidratată, "
            "reduce aspectul obosit de dimineață și pregătește pielea pentru anticearcăn.",
        ],
        "role": "îngrijire punctuală pentru zona ochilor",
        "how": "Aplică o cantitate cât un bob de orez, tapotând cu inelarul de la colțul exterior "
        "spre cel interior. Nu întinde și nu apăsa.",
        "scenarios": [
            "Dimineața, înainte de anticearcăn",
            "Seara, ca ultim pas al rutinei",
            "După nopți scurte, pentru aspectul obosit",
            "Când zona se usucă iarna",
            "Ca prevenție, înainte să apară liniile fine",
        ],
        "features": ["Formulă pentru zona delicată a ochilor", "Se aplică prin tapotare"],
    },
    "tratament-local": {
        "zone": "ten",
        "intro": [
            "Tratamentul local se aplică punctual, doar acolo unde e nevoie. E mai concentrat "
            "decât un ser și tocmai de asta nu se întinde pe toată fața — ar usca zonele care "
            "n-au nicio problemă.",
            "Ideea unui tratament local e simplă: intensitate mare, suprafață mică, durată "
            "scurtă. Se folosește câteva zile, până trece episodul, nu permanent.",
        ],
        "role": "aplicare punctuală, pe zone mici",
        "how": "Aplică un strat subțire doar pe zona vizată, seara, după ser și înainte de cremă.",
        "scenarios": [
            "Seara, punctual, pe zonele congestionate",
            "Când apare o imperfecțiune înainte de un eveniment",
            "În perioadele cu schimbări hormonale",
            "Pe zona T, când se congestionează vara",
            "Ca pas suplimentar, nu ca înlocuitor al rutinei",
        ],
        "features": ["Se aplică punctual, nu pe toată fața", "Formulă concentrată"],
    },
    "mist-pentru-ten": {
        "zone": "ten",
        "intro": [
            "Mistul e cel mai simplu mod de a readuce apa în piele la mijlocul zilei, fără să "
            "strici machiajul. Se pulverizează de la 20 de centimetri și se lasă să se absoarbă.",
            "Un mist nu înlocuiește hidratarea, dar rezolvă senzația de piele uscată în momentele "
            "în care nu poți relua rutina — la birou, în avion, după sport.",
        ],
        "role": "împrospătare rapidă, oricând în timpul zilei",
        "how": "Pulverizează de la distanță pe fața curată sau peste machiaj și lasă să se "
        "absoarbă, fără să ștergi.",
        "scenarios": [
            "La birou, în aer condiționat",
            "Peste machiaj, pentru un aspect mai proaspăt",
            "După sport sau după plajă",
            "În avion, unde aerul e foarte uscat",
            "Înainte de ser, ca să ajute absorbția",
        ],
        "features": ["Se poate folosi peste machiaj", "Pulverizare fină, uniformă"],
    },
    "protectie-solara": {
        "zone": "ten",
        "intro": [
            "Protecția solară e singurul pas din rutină care are efect dovedit asupra felului în "
            "care pielea îmbătrânește. Se aplică zilnic, inclusiv iarna și în zilele înnorate, "
            "pentru că radiația UVA trece prin nori și prin geam.",
            "Dacă ar fi să păstrezi un singur produs din toată rutina, ăsta ar fi. Restul "
            "ingredientelor lucrează în plus; protecția solară lucrează împotriva a ceea ce "
            "strică pielea în fiecare zi.",
        ],
        "role": "ultimul pas al rutinei de dimineață",
        "how": "Aplică generos, ultimul pas dimineața, cu 15 minute înainte de ieșire, și "
        "reînnoiește la expunere prelungită.",
        "scenarios": [
            "Zilnic, dimineața, ca ultim pas înainte de machiaj",
            "Iarna, la munte, unde zăpada reflectă lumina",
            "În zilele înnorate — radiația UVA trece prin nori",
            "Când folosești seara retinol sau acizi",
            "La birou, lângă fereastră",
        ],
        "features": ["Se aplică zilnic, tot anul", "Se reînnoiește la expunere prelungită"],
    },
    # ------------------------------------------------------------------------- MACHIAJ ----
    "fond-de-ten": {
        "zone": "machiaj",
        "intro": [
            "Un fond de ten bun nu se vede — uniformizează, dar lasă pielea să arate a piele. "
            "Nuanța corectă dispare la linia maxilarului, iar textura potrivită rezistă fără să "
            "se așeze în linii.",
            "Diferența dintre un machiaj care ține și unul care se strică la prânz stă rareori în "
            "fond de ten și aproape mereu în pregătirea pielii de dedesubt.",
        ],
        "role": "baza machiajului, peste îngrijire și protecție solară",
        "how": "Aplică din centrul feței spre exterior, cu buretele umed sau cu degetele, în "
        "straturi subțiri pe care le construiești unde e nevoie.",
        "scenarios": [
            "Pentru machiajul de zi, în strat subțire",
            "Construit în straturi pentru un eveniment de seară",
            "Peste o cremă hidratantă bine absorbită",
            "Amestecat cu puțină cremă, pentru acoperire mai lejeră",
            "Fixat cu pudră doar pe zona T",
        ],
        "features": ["Se construiește în straturi", "Nu se așază în linii fine"],
    },
    "creme-bb-si-cc": {
        "zone": "machiaj",
        "intro": [
            "Crema BB e compromisul inteligent între îngrijire și machiaj: uniformizează discret "
            "și hidratează în același timp. E alegerea pentru zilele în care nu vrei să te "
            "machiezi, dar vrei să arăți odihnită.",
            "Pentru cine consideră fondul de ten prea mult, o cremă colorată rezolvă exact atât "
            "cât trebuie — tonul uniform, fără senzația că porți machiaj.",
        ],
        "role": "acoperire lejeră cu beneficii de îngrijire",
        "how": "Aplică cu degetele, ca pe o cremă obișnuită, și construiește doar pe zonele care "
        "au nevoie de mai mult.",
        "scenarios": [
            "În zilele de weekend, când vrei ceva rapid",
            "La sală sau după, pentru un aspect îngrijit",
            "Ca alternativă mai lejeră la fondul de ten",
            "Vara, când machiajul greu e inconfortabil",
            "Peste protecția solară, ca ultim pas",
        ],
        "features": ["Acoperire lejeră, construibilă", "Se aplică rapid, cu degetele"],
    },
    "anticearcan": {
        "zone": "machiaj",
        "intro": [
            "Anticearcănul lucrează pe suprafețe mici și de asta cere precizie, nu cantitate. "
            "Prea mult produs sub ochi se așază în linii; puțin, bine estompat, face zona să "
            "pară odihnită.",
            "Secretul nu e acoperirea maximă, ci potrivirea nuanței și estomparea. Un ton cu o "
            "idee mai deschis decât pielea luminează, fără să lase pete albe.",
        ],
        "role": "corecție punctuală, după fondul de ten",
        "how": "Aplică în strat subțire pe zonele vizate, estompează cu buretele umed și fixează "
        "cu foarte puțină pudră.",
        "scenarios": [
            "Sub ochi, după nopți scurte",
            "Punctual, pe roșeața din jurul nasului",
            "Peste imperfecțiuni, cu pensula fină",
            "Ca bază pe pleoape, înainte de fard",
            "Singur, în zilele fără fond de ten",
        ],
        "features": ["Acoperire construibilă", "Se estompează ușor"],
    },
    "pudre": {
        "zone": "machiaj",
        "intro": [
            "Pudra fixează ce ai construit deja și controlează luciul acolo unde apare. Aplicată "
            "peste tot, poate usca aspectul pielii — de asta se pune strategic, nu generalizat.",
            "O pudră bună e invizibilă: nu adaugă culoare, nu îngroașă machiajul, doar îl ține la "
            "locul lui câteva ore în plus.",
        ],
        "role": "fixare și control al luciului",
        "how": "Aplică cu pensula pufoasă doar pe zona T și pe zonele care lucesc, cu mișcări de "
        "presare, nu de măturare.",
        "scenarios": [
            "Peste fondul de ten, pe zona T",
            "Pentru retuș la mijlocul zilei",
            "Vara, când machiajul lucește mai repede",
            "Sub ochi, ca să fixeze anticearcănul",
            "Înainte de fardul de obraz, pentru o bază uniformă",
        ],
        "features": ["Fixează machiajul", "Nu adaugă culoare"],
    },
    "fard-de-obraz": {
        "zone": "machiaj",
        "intro": [
            "Fardul de obraz e pasul care readuce viață într-un machiaj care altfel arată plat. "
            "Puțin, pe partea de sus a pomeților, schimbă tot aspectul feței.",
            "Regula e simplă: mai puțin decât crezi și estompat mai mult decât crezi. Culoarea "
            "trebuie să pară că vine din interior, nu că a fost desenată.",
        ],
        "role": "culoare și relief, după bază",
        "how": "Aplică pe partea cea mai proeminentă a pomeților și estompează spre tâmple, cu "
        "mișcări scurte.",
        "scenarios": [
            "Pentru machiajul de zi, într-un strat subțire",
            "Intensificat pentru seară",
            "Combinat cu iluminator, pentru relief",
            "Aplicat și pe pleoape, pentru un look monocrom",
            "Singur, peste o piele îngrijită, fără fond de ten",
        ],
        "features": ["Culoare construibilă", "Se estompează ușor"],
    },
    "bronzer": {
        "zone": "machiaj",
        "intro": [
            "Bronzerul aduce căldură acolo unde lumina cade natural: pe frunte, pe pomeți, pe "
            "linia maxilarului. Nuanța potrivită e cu una-două tonuri mai închisă decât pielea, "
            "nu mai mult.",
            "Diferența dintre bronzat și pătat stă în estompare. Aplicat cu o pensulă mare, în "
            "straturi subțiri, arată ca și cum ai fi stat la soare — nu ca și cum ai fi desenat.",
        ],
        "role": "căldură și conturare discretă",
        "how": "Aplică cu pensula mare pe frunte, pe pomeți și pe linia maxilarului, în mișcări "
        "de tip „3”, și estompează bine marginile.",
        "scenarios": [
            "Vara, pentru un aspect însorit",
            "Iarna, ca să compensezi paloarea",
            "Pentru conturare discretă, sub pomeți",
            "Peste tot machiajul, ca ultim pas de armonizare",
            "Combinat cu fard de obraz, pentru un aspect natural",
        ],
        "features": ["Nuanță naturală, fără portocaliu", "Se estompează ușor"],
    },
    "iluminatoare": {
        "zone": "machiaj",
        "intro": [
            "Iluminatorul pune lumină exact acolo unde vrei să atragi privirea: pomeții de sus, "
            "arcul buzelor, colțul interior al ochiului. Puțin ajunge; mult devine strălucire.",
            "Un iluminator bun nu are sclipici vizibil, ci reflexie fină — pielea pare luminată "
            "din interior, nu acoperită cu particule.",
        ],
        "role": "punct de lumină, ultimul pas al bazei",
        "how": "Aplică cu degetul sau cu o pensulă mică pe punctele înalte ale feței și estompează "
        "marginile.",
        "scenarios": [
            "Pe pomeți, pentru un machiaj de seară",
            "În colțul interior al ochiului, pentru o privire trează",
            "Pe arcul buzelor, ca să pară mai pline",
            "Amestecat în fondul de ten, pentru luminozitate generală",
            "Pe claviculă și pe umeri, vara",
        ],
        "features": ["Reflexie fină, fără sclipici vizibil", "Se aplică punctual"],
    },
    "rujuri": {
        "zone": "machiaj",
        "intro": [
            "Rujul e cel mai rapid mod de a schimba un machiaj. O nuanță bine aleasă poate "
            "înlocui restul produselor într-o dimineață grăbită.",
            "Textura decide cât de des îl porți: mată pentru durată, satinată pentru confort, cu "
            "luciu pentru volum. Nuanța decide cât de mult îți place.",
        ],
        "role": "culoare pe buze",
        "how": "Aplică direct din stick, pornind din centrul buzei spre colțuri, și tamponează cu "
        "un șervețel dacă vrei un aspect mai discret.",
        "scenarios": [
            "Pentru machiajul de zi, într-un strat subțire",
            "Aplicat intens, pentru seară",
            "Tamponat cu degetul, pentru un efect natural",
            "Peste creionul de buze, pentru durată mai mare",
            "Singur, peste o piele îngrijită, fără alt machiaj",
        ],
        "features": ["Pigmentare uniformă", "Se aplică ușor, fără creion"],
    },
    "gloss-de-buze": {
        "zone": "machiaj",
        "intro": [
            "Glossul dă volum prin lumină: buzele par mai pline fără conturare și fără efort. E "
            "cel mai simplu produs de machiaj și, de multe ori, singurul de care ai nevoie.",
            "Un gloss modern nu mai e lipicios. Se poartă singur, peste ruj sau peste un balsam, "
            "și se reaplică fără oglindă.",
        ],
        "role": "luciu și volum optic pe buze",
        "how": "Aplică cu aplicatorul în centrul buzelor și întinde spre colțuri; reaplică oricând "
        "în timpul zilei.",
        "scenarios": [
            "Singur, pentru un look natural",
            "Peste ruj, pentru volum",
            "Pe pleoape, într-un machiaj lucios",
            "Într-o poșetă mică, pentru retuș rapid",
            "Iarna, când buzele se usucă",
        ],
        "features": ["Nu e lipicios", "Se reaplică ușor, fără oglindă"],
    },
    "mascara": {
        "zone": "machiaj",
        "intro": [
            "Mascara e produsul care schimbă cel mai mult privirea cu cel mai mic efort. Peria "
            "decide rezultatul mai mult decât formula: una fină separă, una densă adaugă volum.",
            "Dacă ar fi să porți un singur produs de machiaj, mascara ar fi acela: definește "
            "privirea și face fața să pară odihnită.",
        ],
        "role": "definirea genelor",
        "how": "Aplică din rădăcina genelor spre vârf, cu mișcări în zigzag, și adaugă un al "
        "doilea strat înainte ca primul să se usuce.",
        "scenarios": [
            "Singur, pentru un machiaj minimalist",
            "În două straturi, pentru seară",
            "Doar pe genele de sus, pentru un aspect natural",
            "După ondularea genelor, pentru curbură",
            "Ca ultim pas al machiajului de ochi",
        ],
        "features": ["Nu se aglomerează pe gene", "Se aplică în straturi"],
    },
    "creioane-si-tusuri-de-ochi": {
        "zone": "machiaj",
        "intro": [
            "Creionul de ochi e produsul care cere cel mai mult exercițiu și dă cel mai mult "
            "caracter. O linie fină pe rădăcina genelor face privirea mai intensă fără să se vadă "
            "că porți machiaj.",
            "Diferența dintre un contur reușit și unul ratat e viteza: linii scurte, una lângă "
            "alta, în loc de o singură trecere lungă.",
        ],
        "role": "contur și intensitate pentru privire",
        "how": "Trasează linii scurte pe rădăcina genelor, apoi unește-le; estompează imediat dacă "
        "vrei un efect mai blând.",
        "scenarios": [
            "Pe rădăcina genelor, pentru intensitate discretă",
            "Estompat, pentru un machiaj de zi",
            "Trasat precis, pentru seară",
            "În mucoasa inferioară, pentru privire definită",
            "Ca bază sub fardul de ochi, pentru durată",
        ],
        "features": ["Trasare precisă", "Se poate estompa imediat după aplicare"],
    },
    "farduri-de-ochi": {
        "zone": "machiaj",
        "intro": [
            "Fardul de ochi construiește adâncime: o nuanță mată în pliu, una mai deschisă pe "
            "pleoapa mobilă și privirea capătă imediat relief.",
            "Regula care salvează orice machiaj de ochi: estompează între straturi, nu la final. "
            "Culorile trebuie să se topească una în alta, nu să se atingă.",
        ],
        "role": "culoare și relief pe pleoape",
        "how": "Aplică nuanța deschisă pe toată pleoapa, cea mai închisă în pliu, și estompează "
        "granița dintre ele cu o pensulă curată.",
        "scenarios": [
            "Într-o singură nuanță, pentru un machiaj rapid",
            "În degrade, pentru seară",
            "Aplicat umed, pentru intensitate maximă",
            "Ca pudră peste creion, pentru durată",
            "Pe linia genelor inferioare, pentru definire",
        ],
        "features": ["Pigmentare bogată", "Se estompează fără să se tulbure"],
    },
    "primer-pentru-machiaj": {
        "zone": "machiaj",
        "intro": [
            "Primerul e stratul care decide cât ține machiajul. Nu se vede și nu se simte, dar "
            "netezește suprafața și dă fondului de ten ceva de care să se prindă.",
            "Dacă machiajul tău dispare până la prânz, problema nu e fondul de ten — e ce e sub "
            "el. Un primer potrivit tipului de piele rezolvă asta mai bine decât un produs mai "
            "scump.",
        ],
        "role": "bază între îngrijire și machiaj",
        "how": "Aplică un strat subțire pe pielea hidratată și lasă un minut să se așeze înainte "
        "de fondul de ten.",
        "scenarios": [
            "Înainte de fondul de ten, pentru durată",
            "Doar pe zona T, dacă lucește",
            "Pe pori, ca să netezească suprafața",
            "Vara, când machiajul se strică repede",
            "Înainte de un eveniment lung",
        ],
        "features": ["Netezește suprafața pielii", "Prelungește durata machiajului"],
    },
    "spray-de-fixare": {
        "zone": "machiaj",
        "intro": [
            "Spray-ul de fixare topește stratul de pudră în restul machiajului: aspectul devine "
            "mai natural, iar durata crește. E pasul care face diferența între „machiată” și "
            "„odihnită”.",
            "Se pulverizează la final, în formă de X și de T, de la 25-30 de centimetri. Nu se "
            "șterge și nu se atinge până nu s-a uscat.",
        ],
        "role": "ultimul pas al machiajului",
        "how": "Pulverizează de la 25-30 cm, în X și în T, și lasă să se usuce fără să atingi "
        "fața.",
        "scenarios": [
            "La final, peste tot machiajul",
            "Înainte de un eveniment lung",
            "Vara, împotriva umezelii",
            "Peste pudră, ca să pară mai natural",
            "Pe pensulă, înainte de fardul aplicat umed",
        ],
        "features": ["Prelungește durata machiajului", "Reduce aspectul pudrat"],
    },
    "pensule-si-bureti-de-machiaj": {
        "zone": "machiaj",
        "intro": [
            "O unealtă bună face un produs mediu să arate scump. Densitatea firelor și forma "
            "capului decid dacă produsul se depune uniform sau în pete.",
            "Pensulele nu sunt un accesoriu, sunt jumătate din rezultat. Iar curățarea lor "
            "regulată contează la fel de mult ca alegerea inițială.",
        ],
        "role": "aplicare și estompare",
        "how": "Folosește mișcări de presare pentru acoperire și mișcări circulare pentru "
        "estompare; spală la 1-2 săptămâni.",
        "scenarios": [
            "Pentru aplicarea uniformă a fondului de ten",
            "La estomparea anticearcănului",
            "Pentru pudră, cu mișcări de presare",
            "La aplicarea fardului umed",
            "Pentru estomparea marginilor, la final",
        ],
        "features": ["Fire sintetice, ușor de curățat", "Formă care estompează uniform"],
    },
    # ------------------------------------------------------------------- ÎNGRIJIREA PĂRULUI ----
    "sampoane": {
        "zone": "par",
        "intro": [
            "Șamponul curăță scalpul, nu lungimile. Aici se acumulează sebumul și reziduurile, "
            "iar restul firului se spală oricum când clătești.",
            "Alegerea șamponului contează mai mult decât a oricărui alt produs de păr: e singurul "
            "care ajunge la scalp de fiecare dată și care decide cât de repede se reîncarcă părul.",
        ],
        "role": "curățarea scalpului",
        "how": "Masează în scalp cu vârful degetelor, nu cu unghiile, 30-60 de secunde, apoi "
        "clătește bine. Lungimile se curăță la clătire.",
        "scenarios": [
            "În rutina obișnuită de spălare",
            "După sport, când transpirația se acumulează",
            "Vara, când scalpul se reîncarcă mai repede",
            "Ca primă spălare, urmată de a doua pentru curățare completă",
            "Alternat cu un șampon dedicat, în funcție de nevoie",
        ],
        "features": ["Curăță scalpul fără să usuce lungimile", "Spumează cu o cantitate mică"],
    },
    "balsamuri-de-par": {
        "zone": "par",
        "intro": [
            "Balsamul închide solzii firului deschiși de spălare, așa că părul se descurcă mai "
            "ușor și reflectă mai bine lumina. Se aplică pe lungimi și vârfuri, niciodată pe "
            "scalp.",
            "Dacă părul tău se încâlcește la pieptănat, balsamul e pasul care lipsește. Un minut "
            "de așteptare înainte de clătire face mai mult decât o cantitate dublă.",
        ],
        "role": "descurcare și netezire după spălare",
        "how": "Aplică pe lungimi și vârfuri, lasă un minut, apoi clătește cu apă mai rece — "
        "ajută la închiderea solzilor.",
        "scenarios": [
            "După fiecare spălare, pe lungimi",
            "Lăsat mai mult, ca tratament rapid",
            "Înainte de pieptănat, pe părul ud",
            "Vara, după apa sărată sau clorinată",
            "Alternat cu masca, în săptămânile mai solicitante",
        ],
        "features": ["Se aplică doar pe lungimi", "Descurcă la clătire"],
    },
    "masti-de-par": {
        "zone": "par",
        "intro": [
            "Masca de păr e tratamentul intensiv al rutinei: stă mai mult pe fir și livrează mai "
            "multe lipide și proteine decât un balsam. Se folosește o dată pe săptămână, nu la "
            "fiecare spălare.",
            "Diferența dintre o mască și un balsam nu e doar concentrația, ci timpul. Zece minute "
            "sub un prosop cald schimbă felul în care se simte părul până la următoarea spălare.",
        ],
        "role": "îngrijire intensivă săptămânală",
        "how": "Aplică pe părul spălat și stors, insistând pe vârfuri, lasă 10-15 minute, apoi "
        "clătește bine.",
        "scenarios": [
            "O dată pe săptămână, în locul balsamului",
            "După vopsire sau decolorare",
            "Vara, după soare și apă sărată",
            "Iarna, când părul se electrizează",
            "Înainte de un eveniment, pentru luciu",
        ],
        "features": ["Se lasă 10-15 minute", "Se folosește săptămânal"],
    },
    "uleiuri-pentru-par": {
        "zone": "par",
        "intro": [
            "Uleiul de păr lucrează pe două momente: pe părul ud, ca să sigileze hidratarea, sau "
            "pe cel uscat, ca să domolească vârfurile. Cantitatea e mereu mai mică decât pare.",
            "Două picături încălzite în palme sunt suficiente pentru lungimi medii. Mai mult "
            "înseamnă păr greu, nu păr hrănit.",
        ],
        "role": "sigilare și luciu pe lungimi",
        "how": "Încălzește 2-3 picături în palme și distribuie pe lungimi și vârfuri, evitând "
        "rădăcina.",
        "scenarios": [
            "Pe părul ud, înainte de uscare",
            "Pe părul uscat, pentru vârfuri",
            "Ca tratament înainte de spălare, lăsat 30 de minute",
            "Înainte de placă, împreună cu un termoprotector",
            "Vara, ca protecție împotriva soarelui și a sării",
        ],
        "features": ["Se folosește în cantitate mică", "Nu lasă senzație grea"],
    },
    "ingrijire-fara-clatire": {
        "zone": "par",
        "intro": [
            "Produsele fără clătire rămân pe fir și lucrează toată ziua: descurcă, protejează la "
            "căldură și reduc electrizarea. Se aplică pe părul umed, nu ud leoarcă.",
            "Dacă folosești placa sau uscătorul, un leave-in nu e opțional. E singurul strat care "
            "stă între căldură și fir.",
        ],
        "role": "protecție și descurcare, fără clătire",
        "how": "Pulverizează sau distribuie pe părul umed, pe lungimi și vârfuri, apoi piaptănă și "
        "usucă normal.",
        "scenarios": [
            "Înainte de uscarea cu foehnul",
            "Înainte de placă sau ondulator",
            "Pe părul umed, ca să descurce",
            "Între spălări, pentru vârfurile uscate",
            "Vara, ca protecție la soare și apă sărată",
        ],
        "features": ["Nu se clătește", "Protejează la styling termic"],
    },
    "sampon-uscat": {
        "zone": "par",
        "intro": [
            "Șamponul uscat cumpără o zi în plus între spălări: absoarbe sebumul de la rădăcină și "
            "ridică volumul aplatizat. Nu înlocuiește spălatul, dar amână necesitatea lui.",
            "Se pulverizează de la 20 de centimetri, se lasă un minut și abia apoi se masează. "
            "Grăbit, lasă film alb; făcut cu răbdare, nu se vede deloc.",
        ],
        "role": "prospețime între spălări",
        "how": "Pulverizează de la 20 cm pe rădăcinile împărțite în cărări, lasă un minut, apoi "
        "masează cu degetele.",
        "scenarios": [
            "Dimineața, între spălări",
            "După sport, pentru rădăcini proaspete",
            "Ca să ridici volumul la rădăcină",
            "În călătorii, când nu poți spăla părul",
            "Seara, înainte de culcare, pentru un efect mai natural dimineața",
        ],
        "features": ["Absoarbe sebumul de la rădăcină", "Adaugă volum"],
    },
    # ---------------------------------------------------------------- ÎNGRIJIRE CORP & BUZE ----
    "lotiuni-de-corp": {
        "zone": "corp",
        "intro": [
            "Pielea corpului are mai puține glande sebacee decât fața, așa că se usucă mai repede "
            "— mai ales pe brațe și pe picioare. Momentul aplicării contează: pe pielea încă "
            "umedă, după duș, se absoarbe mult mai bine.",
            "O loțiune de corp folosită constant schimbă textura pielii în câteva săptămâni. "
            "Trucul nu e produsul scump, ci obiceiul zilnic.",
        ],
        "role": "hidratare zilnică pentru corp",
        "how": "Aplică pe pielea încă ușor umedă, imediat după duș, cu mișcări circulare până se "
        "absoarbe.",
        "scenarios": [
            "Zilnic, după duș",
            "Iarna, când pielea se descuamează",
            "După epilare, pentru confort",
            "Pe coate și genunchi, în strat mai gros",
            "Seara, ca ritual de relaxare",
        ],
        "features": ["Se absoarbe rapid", "Potrivit pentru folosire zilnică"],
    },
    "scrub-de-corp": {
        "zone": "corp",
        "intro": [
            "Exfolierea corpului netezește pielea și pregătește terenul pentru hidratare: o "
            "loțiune aplicată după scrub pătrunde mult mai bine.",
            "De două ori pe săptămână e suficient. Mai des irită, mai rar nu se simte — iar "
            "presiunea contează mai puțin decât regularitatea.",
        ],
        "role": "exfoliere de 1-2 ori pe săptămână",
        "how": "Masează pe pielea umedă, sub duș, cu mișcări circulare, insistând pe coate și "
        "genunchi, apoi clătește.",
        "scenarios": [
            "Sub duș, de 1-2 ori pe săptămână",
            "Înainte de autobronzant, pentru aplicare uniformă",
            "Înainte de epilare",
            "Vara, pentru piele netedă",
            "Pe coate și genunchi, punctual",
        ],
        "features": ["Se folosește de 1-2 ori pe săptămână", "Se clătește ușor"],
    },
    "geluri-de-dus": {
        "zone": "corp",
        "intro": [
            "Gelul de duș e produsul pe care îl folosești zilnic, deci și cel care poate usca cel "
            "mai mult dacă e prea agresiv. O formulă blândă curăță la fel de bine, dar lasă "
            "pielea confortabilă.",
            "Dacă simți pielea uscată după duș, cauza e de obicei combinația dintre apa fierbinte "
            "și un gel prea puternic.",
        ],
        "role": "curățarea zilnică a corpului",
        "how": "Aplică pe burete sau direct pe piele, sub apă călduță, și clătește bine.",
        "scenarios": [
            "Zilnic, la duș",
            "După sport",
            "Vara, de mai multe ori pe zi",
            "Ca bază pentru un ritual de seară",
            "Împreună cu loțiunea din aceeași gamă",
        ],
        "features": ["Curăță fără să usuce", "Spumează cu o cantitate mică"],
    },
    "deodorante": {
        "zone": "corp",
        "intro": [
            "Deodorantul lucrează asupra mirosului, nu a transpirației în sine. Se aplică pe "
            "pielea curată și uscată — pe piele umedă, eficiența scade vizibil.",
            "Zona axilelor e sensibilă, mai ales după epilare. O formulă blândă, aplicată pe "
            "pielea uscată, e diferența dintre confort și iritație.",
        ],
        "role": "prospețime pe parcursul zilei",
        "how": "Aplică pe pielea curată și uscată, dimineața; evită aplicarea imediat după "
        "epilare.",
        "scenarios": [
            "Dimineața, după duș",
            "Înainte de sport",
            "În geantă, pentru retuș",
            "Vara, în zilele foarte calde",
            "După epilare, când pielea s-a liniștit",
        ],
        "features": ["Se aplică pe pielea uscată", "Prospețime de lungă durată"],
    },
    "creme-de-maini": {
        "zone": "corp",
        "intro": [
            "Mâinile trec prin cele mai multe spălări din toată ziua, deci pierd cel mai repede "
            "stratul protector. O cremă la îndemână, folosită după fiecare spălare, e cea mai "
            "simplă formă de prevenție.",
            "Pielea mâinilor arată vârsta mai devreme decât fața, tocmai pentru că e cel mai puțin "
            "îngrijită. Constanța rezolvă asta mai bine decât orice formulă.",
        ],
        "role": "hidratare și protecție pentru mâini",
        "how": "Aplică după fiecare spălare, insistând pe zona dintre degete și pe cuticule.",
        "scenarios": [
            "După fiecare spălare pe mâini",
            "Iarna, înainte de ieșit în frig",
            "Seara, într-un strat mai gros",
            "În geantă sau pe birou, la îndemână",
            "După curățenie sau lucru cu detergenți",
        ],
        "features": ["Se absoarbe rapid", "Nu lasă senzație lipicioasă"],
    },
    "buze": {
        "zone": "buze",
        "intro": [
            "Buzele nu au glande sebacee, deci nu se pot hidrata singure. De asta se usucă primele "
            "la frig și de asta au nevoie de un strat protector aplicat des.",
            "Un balsam bun face două lucruri: aduce hidratare și o sigilează. Cel mai important "
            "moment de aplicare e seara, când buzele au toată noaptea să se refacă.",
        ],
        "role": "hidratare și protecție pentru buze",
        "how": "Aplică ori de câte ori simți nevoia, iar seara într-un strat mai gros, înainte de "
        "culcare.",
        "scenarios": [
            "Iarna, împotriva frigului și vântului",
            "Seara, într-un strat mai gros",
            "Înainte de ruj, ca bază",
            "După exfolierea buzelor",
            "În geantă, pentru reaplicare pe parcursul zilei",
        ],
        "features": ["Se poate reaplica oricând", "Se poartă și sub ruj"],
    },
}


def category_block(slug: str) -> dict | None:
    return CATEGORY.get(slug)
