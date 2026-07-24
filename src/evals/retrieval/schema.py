"""NX-203 — schema qrels pentru benchmark-ul de retrieval (SCHELET, fără dataset masiv).

Un `QrelsQuery` = o interogare ro etichetată cu adevărul de relevanță, plus metadatele fără de
care benchmark-ul minte: relevanță GRADUALĂ (nu binar), constrângeri hard așteptate, produse
INTERZISE explicit, proveniența (real vs sintetic), și versiunea catalogului la etichetare
(etichetele expiră când catalogul se schimbă).

Truth-first, aliniat cu NX-202: adevărul de business (hard/soft/forbidden) NU depinde de contractul
de tool. Alimentat ulterior din etichetele NX-202 validate de Adi.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Provenance(str, Enum):
    """De unde vine query-ul — pentru a distinge datele reale de cele generate (Codex: minim de
    reale per categorie; toate variațiile generate verificate uman)."""

    real_sanitized = "real_sanitized"  # din trafic real, redactat PII
    synthetic = "synthetic"  # construit de la zero
    paraphrase = "paraphrase"  # reformulare a unui query real


class Relevance(int, Enum):
    """Relevanță GRADUALĂ (nu relevant/irelevant) — hrănește nDCG cu grade reale."""

    irrelevant = 0
    marginal = 1
    relevant = 2
    ideal = 3


class HardConstraint(BaseModel):
    """Constrângere inviolabilă (D7) așteptată a fi respectată de retrieval/selection."""

    facet: str
    op: str = "eq"  # eq | lte | gte | contains | ...
    value: object = None
    unit: str | None = None


class QrelJudgment(BaseModel):
    """Un produs + gradul lui de relevanță pentru query."""

    product_id: str
    relevance: Relevance


class QrelsQuery(BaseModel):
    """O interogare etichetată. Câmpurile de adevăr (judgments/forbidden/hard) vin din etichetarea
    Adi (NX-202); Claude propune structura + proveniența."""

    id: str
    query: str
    locale: str = "ro"
    provenance: Provenance
    category: str | None = None  # pentru stratificare pe categorii
    catalog_version: str  # versiunea catalogului la care s-au făcut etichetele
    judgments: list[QrelJudgment] = Field(default_factory=list)  # produse relevante, graduale
    forbidden_products: list[str] = Field(
        default_factory=list
    )  # NU trebuie să apară (off-constraint)
    hard_constraints: list[HardConstraint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_overlap(self) -> QrelsQuery:
        """Integritate: un produs nu poate fi și relevant, și interzis (contradicție)."""
        judged = {j.product_id for j in self.judgments}
        clash = judged & set(self.forbidden_products)
        if clash:
            raise ValueError(f"{self.id}: produse și relevante și interzise: {sorted(clash)}")
        dup = len(self.forbidden_products) != len(set(self.forbidden_products))
        if dup:
            raise ValueError(f"{self.id}: forbidden_products conține duplicate")
        return self


class QrelsSet(BaseModel):
    """Colecția + meta. Split-ul (tuning/holdout) trăiește separat (splits.py), ca aceleași qrels
    să poată fi re-partiționate fără a rescrie datele."""

    schema_version: int = 1
    business_id: str
    queries: list[QrelsQuery]

    @model_validator(mode="after")
    def _unique_ids(self) -> QrelsSet:
        ids = [q.id for q in self.queries]
        if len(ids) != len(set(ids)):
            raise ValueError("id-uri de query duplicate în qrels")
        return self
