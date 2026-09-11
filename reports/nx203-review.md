# NX-203 — revizuire adversarială a etichetelor

Etichete verificate: **1735** pe **111** familii.
Metoda e alta decât cea care le-a produs: atributele STRUCTURATE ale catalogului
(`product_type`, `key_ingredients`), pe care etichetatorul nu le-a văzut — el a judecat
din numele trunchiat la 76 de caractere, iar numele SOLE au 191 în medie.

| tip de semnalare | n |
| --- | ---: |
| `flat_family` | 31 |
| `ingredient_unsupported` | 35 |
| `type_mismatch` | 167 |

`UNKNOWN` nu e `MISMATCH`: produsele fără `product_type` (24,3% din catalog) nu sunt
raportate ca nepotrivire, fiindcă absența înseamnă nu știm.

## Nepotriviri de tip, pe PERECHI: 28 relatii, 167 note

O pereche in care o cheie e prefix al celeilalte (`balsam` /
`balsam de curatare`) sau in care ambele numesc acelasi raft (`crema de ochi` /
`crema contur ochi`) nu e o
eticheta gresita: e VOCABULARUL care trateaza ca disjuncte doua chei ale aceluiasi
lucru. Constrangerea dura pe una o exclude pe cealalta, si asta se vede direct in
baseline: regimul `with_constraints` pierde recall (0,574 la 0,475) tocmai fiindca
filtreaza dur pe o cheie care are frate.

| note | cerut | produsul e | relatie |
| ---: | --- | --- | --- |
| 30 | `ruj` | `nuantator pentru buze` |  |
| 18 | `plasturi` | `benzi pentru ochi` |  |
| 16 | `crema de ochi` | `crema contur ochi` |  |
| 13 | `exfoliant` | `toner de fata` |  |
| 12 | `fond de ten` | `cushion` |  |
| 11 | `balsam` | `balsam de curatare` | ierarhie |
| 10 | `cushion` | `fond de ten` |  |
| 10 | `luciu de buze` | `nuantator pentru buze` |  |
| 9 | `set` | `balsam de curatare` |  |
| 6 | `cushion` | `fard de obraz` |  |
| 5 | `exfoliant` | `gel de curatare` |  |
| 3 | `crema contur ochi` | `crema de ochi` |  |
| 3 | `esenta` | `fard de obraz` |  |
| 3 | `crema de fata` | `ser de fata` |  |
| 2 | `crema de corp` | `lotiune de corp` |  |
| 2 | `gel de curatare` | `spuma de curatare` |  |
| 2 | `set` | `spuma de curatare` |  |
| 2 | `exfoliant` | `gel de fata` |  |
| 1 | `gel de curatare` | `gel de dus` |  |
| 1 | `gel de dus` | `gel de curatare` |  |
| 1 | `ruj` | `gel de curatare` |  |
| 1 | `set` | `gel de curatare` |  |
| 1 | `set` | `toner de fata` |  |
| 1 | `set` | `exfoliant` |  |
| 1 | `esenta` | `ser de fata` |  |
| 1 | `exfoliant` | `ser de fata` |  |
| 1 | `luciu de buze` | `ulei de buze` |  |
| 1 | `ruj` | `crema coloranta de fata` |  |

## `ingredient_unsupported` — 35

- „balsam demachiant pentru machiaj rezistent la apa” · nota 3 dar `key_ingredients` n-are ['apa']
  ↳ Dr. Althea Pure Grinding Cleansing Balm - balsam de curatare formulat 
- „balsam demachiant pentru machiaj rezistent la apa” · nota 3 dar `key_ingredients` n-are ['apa']
  ↳ BANILA CO Clean it Zero Original Cleansing Balm SNOOPY Edition - balsa
- „balsam demachiant pentru machiaj rezistent la apa” · nota 3 dar `key_ingredients` n-are ['apa']
  ↳ BANILA CO Clean it Zero Pore Clarifying Cleansing Balm SNOOPY Edition 
- „balsam demachiant pentru machiaj rezistent la apa” · nota 3 dar `key_ingredients` n-are ['apa']
  ↳ ARENCIA Fresh Green Rice Mochi Cleanser, 120 gr - balsam de curatare f
- „balsam demachiant pentru machiaj rezistent la apa” · nota 3 dar `key_ingredients` n-are ['apa']
  ↳ BANILA CO Clean it Zero Pore Clarifying Cleansing Balm - balsam de cur
- „balsam demachiant pentru machiaj rezistent la apa” · nota 3 dar `key_ingredients` n-are ['apa']
  ↳ ARENCIA Fresh Rosehip Rice Mochi Cleanser, 120 gr - balsam de curatare
- „balsam demachiant pentru machiaj rezistent la apa” · nota 3 dar `key_ingredients` n-are ['apa']
  ↳ TIRTIR Hydro Boost Enzyme Cleansing Balm - balsam de curatare formulat
- „cremă contur ochi acid hialuronic” · nota 3 dar `key_ingredients` n-are ['acid hialuronic']
  ↳ ROUND LAB 1025 Dokdo - crema de ochi formulata cu niacinamida si acid 
- „crema de ochi cu Centella Asiatica” · nota 3 dar `key_ingredients` n-are ['centella asiatica']
  ↳ PURITO Wonder Releaf Centella Eye Cream - crema de ochi formulata cu C
- „crema de ochi cu Centella Asiatica” · nota 3 dar `key_ingredients` n-are ['centella asiatica']
  ↳ PURITO Wonder Releaf Centella Unscented Eye Cream - crema de ochi form
- „ser de față cu vitamina C” · nota 3 dar `key_ingredients` n-are ['vitamina c']
  ↳ ANUA Vitamin C 20 + Vitamin E Blemish Serum - Iluminare intensa si red
- „ser de față cu vitamina C” · nota 3 dar `key_ingredients` n-are ['vitamina c']
  ↳ MEDICUBE Deep Vita C Ampoule 2.0 - ser de fata formulat cu vitamina C 
- „ser de față cu vitamina C” · nota 3 dar `key_ingredients` n-are ['vitamina c']
  ↳ ALLIES OF SKIN 20% Vitamin C Ser de fata  - Iluminare intensa si fermi
- „ser de față cu vitamina C” · nota 3 dar `key_ingredients` n-are ['vitamina c']
  ↳ MEDICUBE Age-R Vita C Pro Ampoule - ser de fata formulat cu vitamina C
- „ser de față cu vitamina C” · nota 3 dar `key_ingredients` n-are ['vitamina c']
  ↳ ALLIES OF SKIN Vitamin C 20% & Citrus Cells Advanced Light Reflecting 
- „ser de față cu vitamina C” · nota 3 dar `key_ingredients` n-are ['vitamina c']
  ↳ PURITO Pure Vitamin C Serum - ser de fata formulat cu acid ascorbic si
- „crema de fata cu Centella Asiatica” · nota 3 dar `key_ingredients` n-are ['centella asiatica']
  ↳ SKIN1004 Madagascar Centella Cream - crema de fata formulata cu extrac
- „crema de fata cu Centella Asiatica” · nota 3 dar `key_ingredients` n-are ['centella asiatica']
  ↳ IUNIK Centella  Calming Gel Cream 60 ml - Crema de fata formulata cu a
- „crema de fata cu Centella Asiatica” · nota 3 dar `key_ingredients` n-are ['centella asiatica']
  ↳ SKIN1004 Madagascar Centella Tone Brightening Capsule Cream - crema de
- „crema de fata cu Centella Asiatica” · nota 3 dar `key_ingredients` n-are ['centella asiatica']
  ↳ SKIN1004 Madagascar Centella Teca Cream 75 ml - crema de fata formulat
- … încă 15

## `flat_family` — 31

- „balsam demachiant pentru machiaj rezistent la apa” · toate cele 7 note sunt 3
- „crema de fata cu SPF 50” · toate cele 24 note sunt 3
- „blush cu 50% esență hidratantă” · toate cele 16 note sunt 3
- „gel autobronzant cu acid hialuronic” · toate cele 7 note sunt 3
- „luciu de buze mat culoare intensă” · toate cele 6 note sunt 3
- „mască de păr cu miere macadamia” · toate cele 3 note sunt 3
- „nuantator pentru buze cu finisaj glossy” · toate cele 24 note sunt 3
- „pasta de dinti cu aminoacizi L-arginina” · toate cele 3 note sunt 2
- „săpun lichid cu alantoină Sodium PCA” · toate cele 2 note sunt 3
- „corector cearcăne cu acoperire modulabilă” · toate cele 13 note sunt 3
- „nuantator pentru buze glossy si hidratant” · toate cele 24 note sunt 3
- „pasta de dinti 1400 ppm fluor” · toate cele 4 note sunt 3
- „săpun lichid pH neutru piele” · toate cele 5 note sunt 3
- „ser de față cu retinol 0,1%” · toate cele 8 note sunt 3
- „set 8 pensule machiaj cu husă” · toate cele 4 note sunt 3
- „toner de față pentru piele matură” · toate cele 5 note sunt 2
- „balsam cu extract de moringa par blond” · toate cele 3 note sunt 2
- „corector dual lichid si stick blur” · toate cele 13 note sunt 3
- „TFIT Fluffy Velvet Cushion Blush N01” · toate cele 6 note sunt 3
- „blush lichid cu esenta hidratanta 50%” · toate cele 16 note sunt 3
- … încă 11
