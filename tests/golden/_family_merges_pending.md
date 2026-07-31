# NX-203 — fuziuni de familie propuse, de confirmat ÎNAINTE de etichetare

Fiecare rând spune: **aceste query-uri au același contract de adevăr**, deci aceeași `family_id`.
Contează pentru că scorul headline e macro pe familie: două familii pentru aceeași întrebare o fac
să cântărească dublu, fără ca nimic real să se fi schimbat.

Confirmă sau respinge fiecare rând separat. Un „respins" nu e o greșeală — înseamnă că
formularea *chiar* schimbă ce ar trebui returnat, iar atunci sunt două contracte diferite.

Id-urile sunt cele de după split-ul 5a/5b și scoaterea celor trei query-uri de abstenție.

| # | se contopește | cu | de ce | verdict |
|---|---|---|---|---|
| 1 | `lot5a-02`, `lot5a-03` | `lot5a-01` | „aveți seruri cu vitamina C?" / „cât costă un ser cu vitamina C?" vs „caut un ser cu vitamina C" — aceeași căutare, altă formulare. Prețul se schimbă în răspuns, nu în ce trebuie găsit | |
| 2 | `lot5a-04`, `lot5a-05` | `lot5a-01` | „ten fără strălucire" / „să-mi lumineze pielea" = efectul așteptat, nu un atribut de catalog. **Respinge dacă** consideri că sunt calificatori reali care restrâng gold-ul | |
| 3 | `lot5a-13` | `lot5a-12` | „caut o cremă pentru ten gras" vs „caut o cremă hidratantă pentru ten gras" — vocabular, nu intenție | |
| 4 | `lot5a-07` | `q-self-01` | „recomandă-mi un ser pentru ten gras" = „am tenul gras, ce ser îmi recomanzi?" — imperativ vs întrebare | |
| 5 | `lot5a-10` | `q-cat-01`, `q-cat-02` | „cremă de față pentru ten uscat" vs „cremă hidratantă pentru ten uscat" | |
| 6 | `lot6-02`, `lot6-03` | `lot6-01` | trei formulări ale „cremă cu SPF pentru ten sensibil". `lot6-03` adaugă „se înroșește / să nu irite" — simptom și așteptare, nu atribute | |
| 7 | `lot6-09` | `lot6-08` | „rujuri roșii ieftine" vs „rujuri roșii" — „ieftine" nu adaugă constrângere (toate cele 6 rujuri sunt sub 45 lei) | |
| 8 | `lot6-10` | `lot4-02`, `lot4-03` | „ruj mat sub 60" — pragul nu discriminează, toate rujurile mate sunt sub 60 | |
| 9 | `lot6-12` | `lot6-11`, `lot4-07` | trei formulări ale „șampon pentru păr uscat și deteriorat" | |
| 10 | `lot6-14` | `q-con-03`, `lot4-16` | „sal mi s eusuca mainele des" = cremă de mâini pentru mâini uscate. Testează robustețea la typos grele | |
| 11 | `lot6-05` | `q-con-01` | „aveți cremă SPF 50 disponibilă?" pe pool-ul SPF 50 — întrebare de disponibilitate peste aceeași căutare | |

## Ce se schimbă dacă se confirmă toate

79 de query-uri (18 confirmate + 61 din loturi) devin **~64 de familii distincte**.

Sub ținta de 100. Diferența se acoperă cu **intenții noi**, nu cu parafraze — o parafrază intră în
familia existentă și nu urcă numărătoarea.

## Cazuri de graniță pe care NU le-am propus ca fuziuni

- `lot7-01` („mai aveți pe stoc serul cu vitamina C?") vs `lot5a-01` — același pool, altă intenție.
  Le-am ținut separate pentru că blocul de disponibilitate se decide ca bloc (intră sau nu în
  corpus). Dacă intră, **atunci** e o fuziune de discutat.
- `lot6-06` („fond de ten acoperire medie pentru ten mixt") vs `q-con-02` („fond de ten cu acoperire
  medie") — al doilea calificator e real și canonic (`combination`), deci pool diferit. Familii
  distincte, dar **același `split_group`**: nu au voie în felii diferite.
- `lot5a-08` („ser hidratant sub 150") vs `lot3-10` („caut un ser pentru hidratare") — pragul chiar
  taie, deci contract diferit. Familii distincte, același `split_group`.
