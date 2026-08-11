"""NX-230 — frontiera de privacy: textul brut nu trece de aici către niciun sink durabil.

Import public minim, intenționat: cine are nevoie de altceva decât de asta probabil face o
redactare ad-hoc, adică o a doua sursă de adevăr pentru politică.
"""

from src.privacy.boundary import apply_boundary, make_safe, safe_for_telemetry
from src.privacy.contracts import (
    CATEGORIES,
    RawInbound,
    RawText,
    SafeInbound,
    SensitiveTokenMap,
)
from src.privacy.detectors import detect, redact

__all__ = [
    "CATEGORIES",
    "RawInbound",
    "RawText",
    "SafeInbound",
    "SensitiveTokenMap",
    "apply_boundary",
    "detect",
    "make_safe",
    "redact",
    "safe_for_telemetry",
]
