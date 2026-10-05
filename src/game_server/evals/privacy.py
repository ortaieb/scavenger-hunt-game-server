"""The privacy scorer: flags a referee reason that describes the person, not the pose.

The prompt tells the referee never to say what the person looks like; this measures whether
it holds. Production doesn't filter reasons: the scorer measures the prompt, it doesn't hide
the problem.
"""

import re

# Words that describe a person, matched as whole words, ignoring case. Blunt on purpose: a
# hit in a scene reason ("the old bridge") still counts, and the report says which word hit.
PERSON_WORDS: tuple[str, ...] = (
    # Who the person is.
    "man",
    "woman",
    "boy",
    "girl",
    "male",
    "female",
    "guy",
    "lady",
    "gentleman",
    # The same, plural: a reason can describe several people.
    "men",
    "women",
    "boys",
    "girls",
    "males",
    "females",
    "guys",
    "ladies",
    "gentlemen",
    # Gendered pronouns.
    "he",
    "she",
    "his",
    "her",
    "hers",
    "him",
    "himself",
    "herself",
    # What they look like.
    "beard",
    "bearded",
    "moustache",
    "mustache",
    "bald",
    "blond",
    "blonde",
    "skin",
    "hair",
    # How old they look.
    "young",
    "old",
    "elderly",
    "aged",
    "teen",
)

_PERSON_WORD = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in PERSON_WORDS) + r")\b", re.IGNORECASE
)


def person_words(reason: str) -> tuple[str, ...]:
    """The listed words in `reason`, lowercased, each once, in order of first appearance."""
    return tuple(dict.fromkeys(word.lower() for word in _PERSON_WORD.findall(reason)))
