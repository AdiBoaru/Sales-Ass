"""NX-251 — `corroborated_by`: poarta prin care o valoare propusă de model devine afirmația
CLIENTULUI.

Miza e mai mare decât pare. Cât timp triajul extrage sloturi, `user_explicit` vine de la nano și
faptele clientului pot fi `hard`. Când triajul iese de pe drumul sincron, coroborarea rămâne
SINGURA sursă de `user_explicit` — deci dacă funcția asta e prea permisivă, o inferență devine
constrângere inviolabilă; dacă e prea strictă, nu mai există constrângeri inviolabile deloc.

Testele sunt scrise pe cele două direcții de eroare, nu pe implementare.
"""

from src.conversation.needs import corroborated_by

# ── Numere: valoarea trebuie ROSTITĂ ─────────────────────────────────────────


def test_a_number_the_client_actually_said_is_corroborated():
    assert corroborated_by("as vrea ceva sub 200 de lei", 200)


def test_a_number_the_client_never_said_is_not_corroborated():
    # Cazul care contează: modelul „deduce" un buget pe care nimeni nu l-a rostit.
    assert not corroborated_by("as vrea ceva ieftin", 200)


def test_the_romanian_thousands_separator_is_read_both_ways():
    # „1.500" e o mie cinci sute în scriere românească și unu virgulă cinci în scriere zecimală.
    # Nu putem ști care — dar în ambele lecturi numărul A FOST rostit, și asta confirmăm.
    assert corroborated_by("am buget 1.500 lei", 1500)
    assert corroborated_by("vreau sub 1,5 kg", 1.5)


def test_a_different_number_in_the_message_does_not_corroborate():
    assert not corroborated_by("vreau sub 150 lei", 200)


# ── Text: tokenul canonic vs. cuvintele clientului ───────────────────────────


def test_an_inflected_romanian_word_still_corroborates_its_canonical_token():
    # Vocabularul ține „ten gras"; clientul scrie „tenul gras". Fără potrivire pe prefix,
    # exact cazul NORMAL în română ar rămâne necoroborat.
    assert corroborated_by("am tenul gras si vreau ceva usor", "ten_gras")


def test_diacritics_do_not_decide_whether_a_fact_is_the_clients():
    assert corroborated_by("vreau ceva pentru păr vopsit", "par_vopsit")


def test_a_token_absent_from_the_message_is_not_corroborated():
    assert not corroborated_by("vreau un ser hidratant", "ten_gras")


def test_a_brand_the_client_named_is_corroborated():
    assert corroborated_by("de fapt accept si Sony", "sony")


def test_partial_overlap_is_not_enough():
    # „ten uscat" nu e coroborat de un mesaj care conține doar „ten": toți tokenii trebuie să apară,
    # altfel o jumătate de potrivire ar promova o inferență la fapt al clientului.
    assert not corroborated_by("ceva pentru ten", "ten_uscat")


def test_a_single_letter_in_the_message_cannot_corroborate_a_word():
    # Prefixul e permis doar peste pragul de lungime; altfel un „g" ar corobora „gras".
    assert not corroborated_by("vreau g", "gras")


# ── Conservatorism: fără dovadă, fără promovare ──────────────────────────────


def test_missing_or_non_factual_values_are_never_corroborated():
    assert not corroborated_by("orice mesaj", None)
    assert not corroborated_by("", "sony")
    # Booleanul nu are cum să fie „rostit": nu există dovadă textuală pentru True.
    assert not corroborated_by("da, vreau", True)
