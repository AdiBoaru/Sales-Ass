"""NX-244 — copy-ul de SHELL servit la bootstrap, înaintea primului tur.

Problema pe care o rezolvă: `chrome`/`composer`/`a11y` sunt server-owned și complete (NX-228),
dar călătoresc DOAR în interiorul unui `web-view.v2`, iar un view există abia după ce un tur s-a
încheiat. Între încărcarea paginii și primul răspuns, frontendul n-avea de unde lua eticheta
launcherului, titlul dialogului sau placeholderul composerului — deci le inventa
(`BRAND.assistant`, „Întreabă orice despre produse…"). Exact ce interzice boundary-ul NX-244:
copy-ul comercial nu se scrie în browser.

Soluția e cea mai mică posibilă: ACELEAȘI tabele de copy (`src/web/localization.py`), expuse mai
devreme. Zero text nou, zero al doilea vocabular care poate diverge de cel din view. Un `view_copy`
și `chrome`-ul unui view din același locale sunt, prin construcție, aceleași string-uri.

Două reguli pe care le impune modulul:

1. **Validare prin modelele de contract**, nu prin convenție. Copy-ul trece prin `ComposerView` /
   `ChromeView` / `A11yView` înainte să iasă pe sârmă: un label gol sau un câmp lipsă e o eroare
   aici, nu un shell fără nume în browser.
2. **Locale-aware, nu română hardcodată** (D3/P11). Limba vine de la tenant; `normalize_locale`
   decide fallbackul pilot pentru orice valoare necunoscută sau absentă.

Ce NU e: un ViewModel. `view_copy` nu are `messages`, `turn` sau `conversation` — nu e un răspuns
și nu se randează ca unul. E strict copy-ul ramei, disponibil înainte să existe conținut.
"""

from __future__ import annotations

from typing import Any, Final

from src.web.contracts_v2 import A11yView, ChromeView, ComposerView
from src.web.localization import copy_for, normalize_locale

# Cheia sub care apare în răspunsul `/web/bootstrap`. Numită ca atare — nu `view`, nu `bootstrap` —
# fiindcă e copy, nu un turn proiectat.
BOOTSTRAP_COPY_KEY: Final = "view_copy"


def shell_copy(locale: Any) -> dict[str, Any]:
    """Copy-ul ramei pentru un locale: `{composer, chrome, a11y}`.

    Formă IDENTICĂ celei din envelope-ul unui view (`src/channels/web/render_v2.py::_envelope`),
    ca frontendul să aibă UN singur cod de citire: `view?.chrome ?? bootstrap.view_copy.chrome`.

    `composer.enabled=True`: la bootstrap nu există turn activ, deci nimic nu blochează inputul.
    Din primul tur încolo adevărul e al view-ului, care poartă propriul `enabled`.
    """
    copy = copy_for(normalize_locale(locale))
    # Validarea nu e decor: `Label`/`Value` din NX-228 resping string-ul gol, iar `A11yView` cere
    # un anunț pentru FIECARE status. O gaură în tabelul de copy pică AICI, la un test, nu în
    # browserul unui client care vede un buton fără nume.
    return {
        "composer": ComposerView(enabled=True, **copy["composer"]).model_dump(),
        "chrome": ChromeView(**copy["chrome"]).model_dump(),
        "a11y": A11yView(announcements=copy["announcements"]).model_dump(),
    }
