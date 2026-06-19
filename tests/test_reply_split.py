"""NX-90 — split_reply: spargerea reply-ului lung în max 2 mesaje. Funcție PURĂ (fără I/O)."""

from src.worker.reply_split import split_reply


def _words(parts):
    return " ".join(parts).split()


def test_short_text_single_fragment():
    assert split_reply("text scurt", limit=200) == ["text scurt"]


def test_exactly_limit_single_fragment():
    text = "a" * 200
    assert split_reply(text, limit=200) == [text]  # limita e inclusivă


def test_splits_at_sentence_boundary():
    a = "Prima propoziție despre cremă. "
    text = a * 10  # >200ch, multe „. "
    frags = split_reply(text, limit=200)
    assert len(frags) == 2
    assert len(frags[0]) <= 200
    assert frags[0].endswith(".")  # punctuația rămâne în primul fragment
    assert _words(frags) == text.split()  # niciun cuvânt pierdut/dublat


def test_splits_at_paragraph_boundary():
    text = "Paragraf unu, destul de lung ca să umple spațiu rezonabil aici.\n\n" + "Restul " * 40
    frags = split_reply(text, limit=120)
    assert len(frags) == 2
    assert "\n\n" not in frags[0] and len(frags[0]) <= 120
    assert frags[0].startswith("Paragraf unu")


def test_no_word_broken_at_space():
    text = "cuvant " * 60  # 420ch, fără punctuație, doar spații
    frags = split_reply(text, limit=200)
    assert len(frags) == 2
    assert len(frags[0]) <= 200
    assert not frags[0].endswith("cuva")  # nu rupe cuvântul la mijloc
    assert all(w == "cuvant" for w in _words(frags))


def test_hard_cut_when_no_boundary():
    text = "x" * 350  # un singur „cuvânt" lung, nicio graniță sub limit
    frags = split_reply(text, limit=200)
    assert len(frags) == 2
    assert len(frags[0]) == 200
    assert frags[0] + frags[1] == text  # tăiere dură → nimic pierdut


def test_second_fragment_may_exceed_limit():
    # regula e MAX 2 mesaje (nu N): al doilea fragment poate fi > limit
    text = "Scurt început. " + "y" * 400
    frags = split_reply(text, limit=200)
    assert len(frags) == 2
    assert len(frags[1]) > 0
