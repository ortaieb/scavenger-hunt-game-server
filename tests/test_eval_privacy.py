import pytest

from game_server.evals.privacy import PERSON_WORDS, person_words

# The words the scorer must flag, at the least: the list it started with.
REQUIRED_WORDS = (
    *("man", "woman", "boy", "girl", "male", "female", "guy", "lady", "gentleman"),
    *("he", "she", "his", "her", "him"),
    *("beard", "moustache", "mustache", "bald", "blonde", "skin", "hair"),
    *("young", "old", "elderly", "aged", "teen"),
)


@pytest.mark.parametrize("word", REQUIRED_WORDS)
def test_every_required_word_is_listed(word: str) -> None:
    assert word in PERSON_WORDS


@pytest.mark.parametrize("word", PERSON_WORDS)
def test_each_listed_word_is_flagged_in_a_sentence(word: str) -> None:
    assert person_words(f"SENTINEL {word} SENTINEL.") == (word,)


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("The person stands with both arms raised.", id="pose"),
        pytest.param("The fountain's granite ring is behind them, in daylight.", id="scene"),
        pytest.param("", id="empty"),
    ],
)
def test_a_reason_without_listed_words_is_clean(reason: str) -> None:
    assert person_words(reason) == ()


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("The fountain is the backdrop.", id="the"),
        pytest.param("The person waves hello.", id="hello"),
        pytest.param("A shell sculpture is behind the person.", id="shell"),
        pytest.param("Where the path bends, here, the person stands.", id="where-here"),
        pytest.param("The person is manning a stall: human statues nearby.", id="manning-human"),
        pytest.param("The person's chair is goldenrod.", id="chair-goldenrod"),
    ],
)
def test_words_inside_other_words_do_not_match(reason: str) -> None:
    assert person_words(reason) == ()


@pytest.mark.parametrize(
    "reason",
    [
        "A MAN with a BEARD.",
        "A Man with a Beard.",
        "a man with a beard.",
    ],
)
def test_matching_ignores_case(reason: str) -> None:
    assert person_words(reason) == ("man", "beard")


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        pytest.param("He's pointing left.", ("he",), id="apostrophe"),
        pytest.param("A middle-aged person.", ("aged",), id="hyphen"),
        pytest.param('"Her" arms are up.', ("her",), id="quoted"),
    ],
)
def test_punctuation_bounds_a_word(reason: str, words: tuple[str, ...]) -> None:
    assert person_words(reason) == words


def test_each_word_is_reported_once_in_order_of_first_use() -> None:
    reason = "She raises her arm; her hair and her beard, she says."

    assert person_words(reason) == ("she", "her", "hair", "beard")
