# Fixture-uri multi-tur — NX-235

Fiecare fișier descrie o CONVERSAȚIE, nu un apel de funcție: o listă de tururi, fiecare cu
propunerile pe care le-ar produce stagiile, și ce trebuie să fie adevărat despre stare după
turul respectiv. Rulate de `tests/test_conversation_state_v2_fixtures.py`.

De ce fixture și nu teste scrise de mână: regulile care contează aici (o revocare nu revine, un
`hard` nu se relaxează, un topic switch nu atinge siguranța) se manifestă abia după mai multe
tururi. Un fixture face secvența citibilă și lasă un caz nou să fie un fișier, nu cod.

Schema:

```jsonc
{
  "name": "...",
  "locale": "ro",                  // limba conversației (D3: nucleul e locale-aware)
  "initial_state": {},             // jsonb v1 sau v2 de la care pornim (opțional)
  "turns": [
    {
      "utterance": "…",            // DOAR pentru citire — nu intră niciodată în stare
      "proposals": [{"op": "set_need", "key": "budget_max", "value": 150,
                     "source": "user_explicit"}],
      "expect": {
        "active": {"budget_max": 150},        // nevoi active + valoarea canonică
        "absent": ["brand"],                  // chei care NU au voie să fie active
        "revoked": ["budget_max"],            // chei cu tombstone valabil
        "strength": {"budget_max": "hard"},
        "topic": "seruri",
        "pending": "budget_max",              // sau null
        "rejected": ["hard_downgrade"]        // motivele așteptate de respingere
      }
    }
  ]
}
```
