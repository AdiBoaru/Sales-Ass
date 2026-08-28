"""Cine e tenantul, pentru testele care ating baza REALĂ.

Până la proiectul v3, UUID-ul demo era și cuiul de care testele își agățau rândurile, și tenantul
care avea catalog. Sunt două roluri diferite, iar mutarea pe baza nouă le-a despărțit: catalogul
real trăiește sub `sole-ro`, iar rândul demo e doar un cui.

- `CATALOG_BIZ` — tenantul care CHIAR are produse. Îl folosesc testele care interoghează catalogul
  (căutare, preț, variante, diacritice). Fără el, ele nu cad pe o regresie, ci pe un tabel gol.
- `FIXTURE_BIZ` — cuiul pentru testele care își creează singure datele (canal, conversație,
  evenimente) și le curăță după. N-are nevoie de catalog; îl creează `scripts/seed_test_tenant.py`.

Ambele se pot suprascrie din mediu, ca suita să poată rula pe alt proiect fără să atingă cod.
"""

import os

#: Tenantul cu catalog real (proiect v3: `sole-ro`, 2.758 produse).
CATALOG_BIZ = os.environ.get("TEST_CATALOG_BUSINESS_ID", "99fe1292-f9ed-469e-8183-f994ea5b59c0")

#: Tenantul-cui al testelor care își aduc propriile date.
FIXTURE_BIZ = os.environ.get("TEST_FIXTURE_BUSINESS_ID", "6098812a-50fc-44bd-a1ba-bc77e6399158")
