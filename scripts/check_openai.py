"""Verifică rapid că OPENAI_API_KEY + cele 3 modele din config răspund.

Rulează: python scripts/check_openai.py
Citește OPENAI_API_KEY (+ MODEL_* dacă le-ai suprascris) din .env. Bifează
pasul „Test 1 apel pe fiecare model" din T017. NU intră în CI (necesită cheie
reală, ca testele integration). Apeluri minuscule — cost ~$0.

Decuplat de DB intenționat: poți testa cheia OpenAI înainte de a avea
SUPABASE_DB_URL setat (de aceea citește din os.environ, ca db_check.py, nu
prin src.config care cere DB url-ul).
"""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

# Aceleași default-uri ca src/config.py (modelele pe care le folosește botul).
MODEL_TRIAGE = os.environ.get("MODEL_TRIAGE", "gpt-5.4-nano")
MODEL_AGENT = os.environ.get("MODEL_AGENT", "gpt-5.4-mini")
MODEL_EMBED = os.environ.get("MODEL_EMBED", "text-embedding-3-small")


def _check_chat(client: OpenAI, model: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Răspunde cu un singur cuvânt: OK"}],
    )
    return (resp.choices[0].message.content or "").strip() or "(răspuns gol)"


def _check_embed(client: OpenAI, model: str) -> str:
    resp = client.embeddings.create(model=model, input="ping")
    return f"{len(resp.data[0].embedding)} dimensiuni"


def main() -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("❌ OPENAI_API_KEY lipsește din .env — pune-o întâi (T017).")
        return 1

    client = OpenAI(api_key=key)
    checks = [
        ("triaj", MODEL_TRIAGE, _check_chat),
        ("agent", MODEL_AGENT, _check_chat),
        ("embed", MODEL_EMBED, _check_embed),
    ]

    print("Verific cheia + cele 3 modele (apeluri minuscule)...\n")
    failed = 0
    for label, model, fn in checks:
        try:
            result = fn(client, model)
            print(f"✅ {label:<6} {model:<26} → {result}")
        except Exception as e:  # noqa: BLE001 — raportăm orice eroare clar, nu crăpăm
            failed += 1
            print(f"❌ {label:<6} {model:<26} → {type(e).__name__}: {e}")

    print()
    if failed:
        print(
            f"{failed}/{len(checks)} au eșuat. Indicii: 401=cheie greșită, "
            "404=model inexistent, 429=fără credit / peste limită."
        )
        return 1
    print('Toate răspund. Bifează în T017 „test 1 apel pe fiecare model". ✅')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
