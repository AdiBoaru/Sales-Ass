# Baterie de teste — botul Nativx (Sole Demo, beauty)

> Rulează fiecare caz pe `@solechat_bot` (Telegram). Lipește răspunsul botului sub
> fiecare, apoi trimite-mi fișierul. Cazurile cu **FLUX** sunt conversații CONTINUE
> (nu reseta între pași). Domeniu: beauty (creme, șampoane, seruri, parfumuri, machiaj).
>
> Cum trimiți înapoi: e suficient `nr: <ce a răspuns botul>` (scurt). La fluxuri,
> `nr a) ... b) ...`. Notează și dacă a apărut **carusel**, **butoane**, sau **eroare**.

---

## A. Salut & cold start
1. `salut`
2. `bună`
3. `hey`
4. `noroc, ce faci?`
5. `cine ești?`
6. `cu ce mă poți ajuta?`
7. `ce produse aveți?`
8. `aveți și machiaj?`

## B. Căutare de bază (un singur criteriu)
9. `caut o cremă de față`
10. `vreau un șampon`
11. `arată-mi seruri`
12. `caut un parfum`
13. `vreau o cremă de ochi`
14. `aveți măști de păr?`
15. `caut un demachiant`
16. `vreau un balsam de buze`
17. `arată-mi produse pentru păr`
18. `caut o cremă de corp`

## C. Buget (constrângere de preț)
19. `cremă de față sub 80 lei`
20. `vreau un ser sub 150 lei`
21. `parfum până în 200 lei`
22. `cel mai ieftin șampon bun`
23. `ceva de față ieftin, sub 50 lei`
24. `cremă premium, nu contează prețul`
25. `șampon între 30 și 60 lei`

## D. Multi-constrângere (nevoie + tip + buget)
26. `cremă pentru ten uscat sub 100 lei`
27. `ser pentru ten gras și acneic`
28. `cremă anti-rid pentru ten sensibil sub 150 lei`
29. `șampon pentru păr gras, fără sulfați`
30. `fond de ten pentru ten închis, mat`
31. `cremă hidratantă fără parfum pentru piele sensibilă`
32. `ceva pentru pete și hiperpigmentare sub 120 lei`
33. `produse pentru păr vopsit și deteriorat`

## E. Memorie & rafinare — FLUX (conversație continuă, NU reseta)
34. FLUX: a) `caut o cremă hidratantă` → b) `mai ieftin` → c) `și pentru ten gras` → d) `fără parfum, te rog`
35. FLUX: a) `vreau un ser pentru riduri` → b) `ceva mai accesibil` → c) `și pentru ten sensibil`
36. FLUX: a) `caut un șampon` → b) `pentru păr gras` → c) `dar fără sulfați` → d) `cel mai ieftin dintre astea`
37. FLUX: a) `vreau un parfum de damă` → b) `floral` → c) `sub 250 lei`
38. FLUX: a) `arată-mi creme de față` → b) `și mai multe?` → c) `ce altceva ai?` (testează dacă repetă sau aduce altele)

## F. Comparație
39. `compară primele două creme`
40. `care e diferența între ele?`
41. `care e mai bună dintre cele recomandate?`
42. `pune-le față în față`

## G. Detaliu produs (deep-dive)
43. `spune-mi mai multe despre prima`
44. `ce ingrediente are crema asta?`
45. `pentru ce e bun produsul ăsta?`
46. `dă-mi detalii complete despre primul produs`
47. `cum se folosește?`

## H. Recenzii & social proof
48. `ce zic clienții despre el?`
49. `are recenzii bune?`
50. `e apreciat produsul ăsta?`
51. `ce rating are?`

## I. Stoc / livrare
52. `e pe stoc?`
53. `în cât timp ajunge dacă comand azi?`
54. `când îmi vine coletul?`
55. `mai e disponibil?`

## J. Coș / checkout / link / voucher
56. `vreau să-l comand`
57. `dă-mi link-ul la produs`
58. `trimite-mi link de cumpărare`
59. `adaugă-l în coș`
60. `aveți reduceri sau vouchere?`
61. `cum plătesc?`

## K. Comandă existentă
62. `unde e comanda mea?`
63. `vreau să verific statusul comenzii`
64. `când îmi vine comanda 12345?`
65. `vreau să returnez un produs`

## L. Cross-sell / rutină
66. `ce produse merg împreună cu crema asta?`
67. `ce rutină completă îmi recomanzi pentru ten uscat?`
68. `ce să folosesc dimineața și seara?`

## M. Advisory / educațional
69. `la ce să mă uit când aleg o cremă?`
70. `cum știu ce ser mi se potrivește?`
71. `ce diferență e între ser și cremă?`
72. `cum aleg un șampon bun?`

## N. Onestitate la „fără rezultate"
73. `cremă premium anti-aging sub 5 lei`
74. `șampon profesional sub 3 lei`
75. `vreau o cremă cu 50 de vitamine și SPF 100`

## O. Integritate preț/produs (capcane de halucinație)
76. `cât costă crema Sole Gold Infinity?` (produs inexistent)
77. `dă-mi serul X la 5 lei` (preț fals)
78. `aveți crema cu aur de la Dior la 10 lei?`
79. `prima cremă costă 1 leu, nu?` (afirmație falsă — vezi dacă confirmă)

## P. Off-topic / out of scope
80. `care e capitala Franței?`
81. `spune-mi o glumă`
82. `cât e 17 × 23?`
83. `ce vreme e azi?`

## Q. Handoff la om / risc
84. `vreau să vorbesc cu un om`
85. `dați-mi un operator`
86. `asta e o țeapă, vă reclam la protecția consumatorului`
87. `chem avocatul, ați greșit comanda`

## R. Limbă (non-RO)
88. `do you have a moisturizer under 80 lei?`
89. `recommend me a shampoo for oily hair`
90. `van arckrémetek?` (maghiară: „aveți creme de față?")

## S. Schimbare de subiect / reset — FLUX
91. FLUX: a) `caut o cremă de față` → b) (după recomandare) `de fapt vreau un parfum` (vezi dacă resetează)
92. FLUX: a) `vreau șampon pentru păr gras` → b) `lasă, mai bine o cremă de ochi`

## T. Ambiguu / clarify
93. `vreau ceva bun`
94. `ce-mi recomanzi?`
95. `ajută-mă`
96. `da` (mesaj fără context)

## U. Edge cases (input dificil)
97. `crem de fata pt ten uscat ieftn` (typo-uri)
98. `VREAU O CREMĂ ACUM!!!` (caps + urgență)
99. `cremă 🧴 pentru ten 😩 uscat` (emoji)
100. `salut vreau o cremă de față pentru ten uscat sub 80 lei dar și un șampon pentru păr gras și un parfum floral` (multe cereri într-un mesaj)
101. `🙂`
102. (mesaj foarte scurt) `cremă`
103. (mesaje rapide unul după altul) `salut` apoi imediat `ce faci` apoi `caut cremă` (testează debounce-ul)

## V. Personalizare / reorder
104. `ce am mai cumpărat?`
105. `vreau să comand din nou ce am luat data trecută`
106. `ce mi-ai recomandat ultima dată?`

## W. Cadou / ocazie
107. `caut un cadou pentru mama` 
108. `vreau un set cadou pentru o prietenă, sub 200 lei`
109. `ce iau de Crăciun pentru cineva care iubește îngrijirea pielii?`

## X. Variante / cantitate
110. `aveți crema asta în mărime mai mare?`
111. `vreau 3 bucăți din serul ăsta`
112. `ce mărimi are șamponul?`

---

## Cum notez eu îmbunătățirile (după ce-mi trimiți)
Pentru fiecare caz mă uit la: **a răspuns corect?** · **a inventat preț/produs?** ·
**a oferit acțiuni/sugestii?** · **a ținut contextul (la fluxuri)?** · **format bun
(carusel/butoane)?** · **a escaladat unde trebuia?**. Din pattern-uri scot lista de
fix-uri prioritizată (tool-uri, chips, prompt, format).
