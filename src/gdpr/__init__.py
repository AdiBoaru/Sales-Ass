"""Strat GDPR (NX-72) — drept de ștergere / portabilitate / acces.

API public: `erase_contact`, `export_contact`, `access_contact`. Fiecare creează o
cerere în `gdpr_requests`, execută pe `admin_conn` (control plane) și lasă urmă în
`audit_log`. Vezi `src/gdpr/erase.py`.
"""

from src.gdpr.erase import access_contact, erase_contact, export_contact

__all__ = ["access_contact", "erase_contact", "export_contact"]
