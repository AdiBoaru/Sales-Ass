"""NX-248 — contractele de OPERARE: cine e procesul ăsta, ce schemă tolerează, e gata de trafic.

Trei module, trei întrebări diferite:

  • `build_info` — CE artefact rulează (release, digest, revizie de config, interval de schemă).
  • `health` — POATE servi acum (probe mărginite pe dependențele DECLARATE ale rolului).
  • `worker_health` — un proces fără HTTP e viu ȘI lucrează (heartbeat cu dovadă de proces).

Regula comună: nimic de aici nu apelează LLM, nu creează conversație, nu scrie date de business
și nu ține o conexiune peste deadline. O sondă care schimbă sistemul pe care îl măsoară nu e o
sondă, e un tur.
"""
