"""Sonda de producție interoghează o rută pe care aplicația chiar o montează?

Defectul pe care îl previne, măsurat pe 2026-09-16: `prod-reachability.yml` cerea
`${BASE_URL}/health`, rută care nu mai există din NX-248 (health-ul e `live` / `startup` /
`ready`). Cu producția
DEMONSTRABIL sănătoasă, `/health` întorcea 404, deci sonda nu putea ieși verde NICIODATĂ, iar
issue-ul „🔴 Producția nu răspunde" nu se putea închide singur.

De ce n-a prins-o nimeni: producția a fost efectiv jos pe toată durata de viață a sondei, deci
verdictul `down` părea corect. **O alarmă adevărată nu validează un monitor** — un monitor care
raportează mereu „jos" e indistinct de unul stricat, exact cât timp chiar e jos.

Testul ăsta e ieftin și mecanic: extrage calea din workflow și o confruntă cu rutele pe care le
declară routerul de health. Nu pornește serverul și nu atinge rețeaua.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "prod-reachability.yml"

#: Forma din workflow: `"${BASE_URL%/}/health/ready"`. Prindem calea, nu întreaga linie de curl —
#: dacă cineva schimbă opțiunile lui `curl`, testul trebuie să rămână despre CALE.
PROBE_PATH_RE = re.compile(r'"\$\{BASE_URL%/\}(?P<path>/[A-Za-z0-9/_\-]*)"')


def _probe_paths() -> list[str]:
    return [m.group("path") for m in PROBE_PATH_RE.finditer(WORKFLOW.read_text(encoding="utf-8"))]


def _health_routes() -> set[str]:
    """Căile REALE, citite din routerul montat în `src/webhook/app.py`, nu o listă scrisă de mână.

    O listă scrisă de mână ar diverge exact în ziua în care cineva redenumește o rută — adică fix
    ziua în care testul ar trebui să vorbească.
    """
    from src.webhook.health import router

    # `r.path` poartă DEJA prefixul routerului (`/health`); concatenarea ar da `/health/health/…`.
    return {r.path for r in router.routes}  # type: ignore[attr-defined]


def test_sonda_interogheaza_o_cale_care_exista() -> None:
    paths = _probe_paths()
    assert paths, f"nicio cale de sondă găsită în {WORKFLOW.name} — s-a schimbat forma comenzii?"

    routes = _health_routes()
    for path in paths:
        assert path in routes, (
            f"sonda cere {path}, dar aplicația montează doar {sorted(routes)}. "
            "O sondă pe o rută inexistentă raportează jos la infinit."
        )


def test_sonda_nu_cere_radacina_health_care_nu_mai_exista() -> None:
    """Gardă explicită pe regresia concretă: `/health` gol a fost valid ÎNAINTE de NX-248."""
    assert "/health" not in _probe_paths(), (
        "`/health` nu mai e o rută din NX-248; folosește `/health/ready`"
    )


@pytest.mark.parametrize("must_exist", ["/health/live", "/health/startup", "/health/ready"])
def test_rutele_de_health_sunt_cele_asteptate(must_exist: str) -> None:
    """Dacă una dintre ele dispare, testul de mai sus ar putea trece pe o mulțime goală."""
    assert must_exist in _health_routes()
