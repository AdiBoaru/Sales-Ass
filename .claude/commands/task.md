Implementează taskul $ARGUMENTS din acest proiect.

Pași obligatorii, în ordine:

1. Citește `CLAUDE.md` (arhitectură + cele 12 principii) și `CONTRIBUTING.md` (reguli de lucru).
2. Citește `tasks/$ARGUMENTS.md` — cardul complet al taskului.
3. Verifică secțiunea **Dependencies** din card: confirmă că taskurile listate sunt deja în main
   (uită-te în cod că există fișierele/funcțiile de care depinzi). Dacă lipsește o dependență,
   OPREȘTE-TE și spune-mi — nu improviza în jurul ei.
4. Verifică branch-ul curent: trebuie să fie cel din card (`git branch --show-current`).
   Dacă suntem pe main, oprește-te și spune-mi să creez branch-ul.
5. Implementează STRICT ce cere cardul. Secțiunea **Out of Scope** e lege:
   ce e acolo NU se face, oricât de tentant ar fi.
6. Scrie testele din secțiunea **Test Cases** (happy + edge + failure) — ele sunt
   asserturile minime, poți adăuga peste.
7. Rulează și arată-mi output-ul complet:
   ruff check . && ruff format . && pytest -x -q
8. Parcurge **Definition of Done** punct cu punct și arată-mi statusul fiecărui punct.
9. NU face commit până nu confirm eu. După confirmare: commit cu mesaj conventional
   (feat:/fix:/test:/chore:/docs:) care include ID-ul taskului.

Reguli permanente (pe scurt, detaliile în CLAUDE.md):
- Orice query SQL are `business_id = $1`. Lookup-urile în faq/cache/templates includ `language`.
- Telefoanele/PII nu apar niciodată în loguri sau analytics.
- LLM se apelează DOAR din triaj și agent, prin adapterul comun.
- Un singur scriitor per câmp din TurnContext — respectă proprietarii din docstring.
