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
    op: str = "eq"  # eq | lte | gte | in | contains | ...
    value: object = None
    unit: str | None = None
    #: Politică EXPLICITĂ pentru starea „necunoscut" (atribut absent). Implicit `False`: absenţa nu
    #: e incompatibilitate. `True` doar pentru praguri de SIGURANŢĂ — la o cerere de SPF 50, un
    #: produs cu SPF nedeclarat NU satisface cerinţa. Se scrie lângă constrângere, nu în cod.
    unknown_is_violation: bool = False
    note: str | None = None


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
    human_verified: bool = False  # obligatoriu la gate; explicit ca să nu presupunem verificarea
    # DOUĂ niveluri de grupare, EXPLICITE în date — nu derivate la runtime din text. Derivarea ar
    # însemna că o schimbare de normalizare rescrie tăcut ponderarea unui benchmark deja rulat.
    #
    # `family_id`     = același CONTRACT DE ADEVĂR (aceleași qrels). Variante de formă: diacritice,
    #                   majuscule, typo, formulare echivalentă. Metricile se mediază ÎN interiorul
    #                   familiei, apoi macro peste familii — altfel un query duplicat de două ori
    #                   cântărește dublu în scorul headline.
    # `split_group_id`= întrebări destul de apropiate încât una ar informa tuning-ul celeilalte.
    #                   NU au voie în felii diferite, chiar dacă au gold diferit. Superset al
    #                   familiei: aceeași familie ⇒ obligatoriu același split_group.
    family_id: str | None = None
    split_group_id: str | None = None
    catalog_version: str  # versiunea catalogului la care s-au făcut etichetele
    judgments: list[QrelJudgment] = Field(default_factory=list)  # produse relevante, graduale
    # EXCEPȚII explicite, nu lista derivabilă din constrângeri. Un produs ajunge aici doar dacă
    # incompatibilitatea e reală dar NU are reprezentare sigură în atributele catalogului.
    # Restul se evaluează din `hard_constraints`, independent de ce a returnat retrieval-ul —
    # altfel gold-ul ar depinde de sistemul testat.
    forbidden_products: list[str] = Field(default_factory=list)
    #: product_id → DE CE e o excepție. Obligatoriu pentru fiecare intrare din
    #: `forbidden_products`: o excepție fără justificare scrisă e indistinctă de un fals-pozitiv
    #: cules din retrieval, exact confuzia pe care politica o elimină.
    forbidden_rationale: dict[str, str] = Field(default_factory=dict)
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
        if missing := sorted(set(self.forbidden_products) - set(self.forbidden_rationale)):
            raise ValueError(
                f"{self.id}: excepţii fără raţiune scrisă: {missing[:3]} — o excepţie "
                f"nejustificată "
                f"e indistinctă de un fals-pozitiv cules din retrieval"
            )
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

    @model_validator(mode="after")
    def _grouping_is_all_or_nothing(self) -> QrelsSet:
        """Gruparea e ori completă, ori absentă — niciodată parţială.

        Varianta anterioară verifica `if q.family_id and q.split_group_id`, deci o intrare FĂRĂ
        id-uri SĂREA verificarea. Fail-open: exact query-urile negrupate scăpau şi de agregare, şi
        de protecţia contra contaminării, iar setul părea valid. O grupare parţială e mai rea decât
        niciuna, pentru că scuteşte tăcut tocmai ce n-a fost acoperit."""
        with_ids = [q for q in self.queries if q.family_id or q.split_group_id]
        if not with_ids:
            return self  # set negrupat: legitim (fixture-uri, seturi vechi)
        incomplete = sorted(q.id for q in self.queries if not (q.family_id and q.split_group_id))
        if incomplete:
            raise ValueError(
                f"grupare parţială: {len(incomplete)} query-uri fără ambele id-uri {incomplete[:5]}"
                f" — ar fi excluse tăcut din agregare şi din protecţia de contaminare"
            )
        return self

    @model_validator(mode="after")
    def _family_within_one_split_group(self) -> QrelsSet:
        """O familie NU poate traversa două `split_group`-uri.

        Invariantul e structural, nu o convenție: dacă două variante ale aceluiaşi contract de
        adevăr ajung în felii diferite, holdout-ul e contaminat şi nicio agregare ulterioară nu mai
        repară asta — metrica ar arăta corect şi ar fi greşită."""
        groups: dict[str, set[str]] = {}
        for q in self.queries:
            if q.family_id and q.split_group_id:
                groups.setdefault(q.family_id, set()).add(q.split_group_id)
        split = {f: sorted(g) for f, g in groups.items() if len(g) > 1}
        if split:
            raise ValueError(f"familii împărţite între split_group-uri: {split}")
        return self
