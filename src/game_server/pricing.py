"""Claude API list prices, and what a call cost.

Shared by the referee's traces and the eval report, so the two can't disagree.
"""

import re
from decimal import Decimal
from typing import NamedTuple


class Price(NamedTuple):
    """USD per million tokens."""

    input: Decimal
    output: Decimal


# First-party Claude API list prices, from https://platform.claude.com/docs/en/about-claude/pricing
# Last checked: 2026-10-05. Unknown models report no cost rather than a guess. Prices for
# cache reads and writes are added when the referee uses prompt caching.
PRICES_PER_MTOK: dict[str, Price] = {
    "claude-haiku-4-5": Price(Decimal("1.00"), Decimal("5.00")),
    "claude-sonnet-4-6": Price(Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-5": Price(Decimal("2.00"), Decimal("10.00")),
    "claude-sonnet-5-5": Price(Decimal("2.00"), Decimal("10.00")),
    "claude-opus-4-8": Price(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-5": Price(Decimal("5.00"), Decimal("25.00")),
    "claude-opus-5-5": Price(Decimal("4.00"), Decimal("20.00")),
}
MILLION = Decimal(1_000_000)
# A dated snapshot of a model: its id, then `-YYYYMMDD`.
_SNAPSHOT = re.compile(r"(?P<model>.+)-\d{8}")


def price_for(model: str) -> Price | None:
    """Prices for a model id, or for a dated snapshot of it (e.g. `...-20251001`).

    Only a date suffix matches: `claude-opus-5-5` is not a snapshot of `claude-opus-5`.
    """
    snapshot = _SNAPSHOT.fullmatch(model)
    return PRICES_PER_MTOK.get(snapshot["model"] if snapshot else model)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """What the tokens cost at list price; None when the model's price isn't known."""
    price = price_for(model)
    if price is None:
        return None
    return (input_tokens * price.input + output_tokens * price.output) / MILLION
