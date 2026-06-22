"""NX-120 — cap de mărime a corpului cererii, ÎNAINTE de orice muncă scumpă (pur, fără I/O extern).

Pe un VPS mic fără swap, bufferizarea unui POST de mulți MB înainte de verificarea semnăturii
poate OOM-ui procesul (și pică TOȚI tenanții). `enforce_body_cap` respinge cu 413:
  • Content-Length lipsă → 413 (nu bufferizăm nelimitat un chunked anonim);
  • Content-Length > cap → 413 imediat (fără a citi corpul);
  • Content-Length mincinos (declară mic, trimite mai mult) → stream-limit prinde la depășire → 413.
Întoarce bytes-ii citiți, ca apelantul (webhook, fără body Pydantic) să nu citească de două ori.
"""

from __future__ import annotations

from fastapi import HTTPException, Request


async def enforce_body_cap(request: Request, max_bytes: int) -> bytes:
    """Respinge corpuri peste `max_bytes` (413) înainte de a le bufferiza integral. Întoarce
    bytes-ii citiți (folosiți de webhook în loc de un al doilea `request.body()`)."""
    content_length = request.headers.get("content-length")
    if content_length is None:
        # Fără Content-Length (ex. chunked anonim) → refuzăm; clienții legitimi îl trimit mereu.
        raise HTTPException(status_code=413, detail="length required")
    try:
        declared = int(content_length)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="bad content-length") from e
    if declared < 0:
        # Content-Length negativ = invalid (RFC 7230) → poate desincroniza un proxy → 400.
        raise HTTPException(status_code=400, detail="bad content-length")
    if declared > max_bytes:
        raise HTTPException(status_code=413, detail="payload too large")

    # Stream-limit: prinde Content-Length mincinos (declarat mic, corp mai mare / chunked).
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail="payload too large")
    return bytes(body)
