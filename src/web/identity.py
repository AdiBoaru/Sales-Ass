"""NX-129 — verificare JWT host-signed (HS256) pentru login passthrough pe web.

Site-ul gazdă, când randează widget-ul pentru un client AUTENTIFICAT, semnează identitatea lui cu
`identity_secret`-ul per-tenant (din `channels.settings`, separat de `session_secret`) și o pasează
widget-ului ca JWT. Aici îl verificăm la MARGINEA web (ca semnătura de sesiune, NX-20a) → `sub` =
`customer_ref` (id STABIL de client din eshop). Secretul stă pe backend-ul gazdei + control plane,
NICIODATĂ în browser sau pe stream.

Verificare cu STDLIB (hmac/hashlib/base64, ca `session.py`) — fără dependență nouă și cu control
TOTAL pe pinning-ul de algoritm: respingem `alg=none` și confuzia de algoritm (atac clasic JWT).
Funcția NU ridică niciodată pe input ostil → calea web tratează un eșec ca vizitator anonim (P6).
"""

from __future__ import annotations

from src.web.security import verify_hs256_claims


def verify_identity_token(
    token: str,
    secret: str,
    *,
    leeway_s: int = 30,
    issuer: str | None = None,
    audience: str | None = None,
) -> tuple[str | None, str | None]:
    """`(customer_ref, reject_reason)` dintr-un JWT HS256 semnat de gazdă.

    Succes → `(sub, None)`. Orice problemă → `(None, motiv)` cu motiv ∈ {`malformed`, `bad_alg`,
    `bad_signature`, `expired`, `bad_issuer`, `bad_audience`, `no_sub`}. Nu ridică niciodată pe
    input ostil → calea web tratează un eșec ca vizitator anonim (P6).

    NX-229: criptografia s-a mutat în `security.verify_hs256_claims`, primitiva pe care o folosește
    și poarta de acces demo. Două implementări de JWT în același repo înseamnă că într-o zi doar
    una primește fixul; aici e o singură bucată de cod cu pinning DUR pe HS256 (anti `alg=none` și
    confuzie de algoritm) și comparare în timp constant.

    `issuer`/`audience` sunt opționale și se verifică DOAR dacă tenantul le configurează — un host
    care nu le emite nu e penalizat, dar unul care le emite e ținut de cuvânt.
    """
    claims, reason = verify_hs256_claims(
        token, secret, leeway_s=leeway_s, issuer=issuer, audience=audience
    )
    if reason is not None:
        return None, reason
    assert claims is not None  # reason is None ⇒ claims valide (contractul primitivei)
    sub = claims.get("sub")
    # `sub` e ce transformă un token valid într-o IDENTITATE. Fără el, semnătura dovedește doar că
    # cineva cunoaște secretul — nu și despre cine vorbim.
    if not isinstance(sub, str) or not sub.strip():
        return None, "no_sub"
    return sub.strip(), None
