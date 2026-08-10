# Diagramele arhitecturii — export per figură

Fiecare figură din [`docs/ARCHITECTURE-WORKFLOWS.md`](../ARCHITECTURE-WORKFLOWS.md) trăiește
aici și ca fișier de sine stătător, ca să poată fi descărcată, editată sau pusă într-o
prezentare fără să cari tot documentul.

| Format | Unde | La ce folosește |
| --- | --- | --- |
| `NN-nume.mmd` | acest folder | **Sursa editabilă** (text Mermaid) |
| `svg/NN-nume.svg` | `svg/` | Imagine vectorială — de descărcat, printat, pus în slide-uri |

## Cum editezi o diagramă

1. Deschide fișierul `.mmd` (e text simplu).
2. Alege unealta:
   - **[mermaid.live](https://mermaid.live)** — lipești conținutul, vezi diagrama live,
     editezi, apoi exporți PNG/SVG direct de acolo. Cel mai rapid drum.
   - **VS Code** — extensia *Mermaid Chart* randează `.mmd` direct în editor.
   - **draw.io / diagrams.net** — *Insert → Advanced → Mermaid*, lipești codul; de acolo
     poți muta chenarele cu mouse-ul (dar exportul înapoi în Mermaid se pierde — draw.io
     îl transformă în forme proprii).

## Regula de sincronizare (important)

**Documentul e sursa de adevăr; aceste fișiere sunt GENERATE din el.**

```
docs/ARCHITECTURE-WORKFLOWS.md  →  python scripts/export_diagrams.py  →  *.mmd (+ --svg)
```

- Ai editat un `.mmd` și vrei schimbarea permanentă? Ea trebuie să ajungă înapoi în
  document (copiezi blocul editat peste cel din `ARCHITECTURE-WORKFLOWS.md`, sau îi ceri
  lui Claude s-o facă). Altfel următorul export o suprascrie.
- CI-ul verifică sincronizarea (`scripts/verify_architecture_doc.py` rulează și
  `export_diagrams.py --check`): un `.mmd` divergent de document = build roșu. Divergența
  e o decizie, nu un accident.

## Regenerare

```bash
python scripts/export_diagrams.py         # rescrie .mmd din document
python scripts/export_diagrams.py --svg   # + randează SVG (local: node + mermaid-cli + Edge)
```

SVG-urile nu sunt verificate în CI (cer browser) — sunt produse derivate de vizualizare;
regenerează-le după orice schimbare de diagramă.

## Convenția etichetelor

Prima linie a unui chenar spune **ce se întâmplă, pe românește**; citarea `(fișier:linie)`
din paranteză e **dovada în cod**, nu mesajul. Citările sunt verificate de poarta CI
(fișierul există, linia e în interval).
