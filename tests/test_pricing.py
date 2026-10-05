from decimal import Decimal

import pytest

from game_server.pricing import PRICES_PER_MTOK, Price, cost_usd, price_for


@pytest.mark.parametrize(
    ("model", "price"),
    [
        ("claude-haiku-4-5", Price(Decimal(1), Decimal(5))),
        ("claude-haiku-4-5-20251001", Price(Decimal(1), Decimal(5))),  # a dated snapshot
        ("claude-sonnet-5", Price(Decimal(2), Decimal(10))),
        ("claude-sonnet-5-5", Price(Decimal(2), Decimal(10))),
        ("claude-opus-5", Price(Decimal(5), Decimal(25))),
        ("claude-opus-5-5", Price(Decimal(4), Decimal(20))),  # not a snapshot of claude-opus-5
        ("claude-opus-5-5-20260401", Price(Decimal(4), Decimal(20))),
    ],
)
def test_price_for_a_known_model(model: str, price: Price) -> None:
    assert price_for(model) == price


@pytest.mark.parametrize(
    "model",
    [
        "some-other-model",
        "claude-haiku-4-50",  # not a dated snapshot of haiku-4-5
        "claude-haiku-4-5-2025100",  # seven digits: not a date
        "claude-haiku-4-5-20251001-extra",
        "claude-opus-5-9",  # a model without a price is never priced as claude-opus-5
    ],
)
def test_no_price_for_an_unknown_model(model: str) -> None:
    assert price_for(model) is None


def test_prices_are_exact_decimals() -> None:
    for price in PRICES_PER_MTOK.values():
        assert all(isinstance(amount, Decimal) for amount in price)


def test_cost_from_tokens_is_exact() -> None:
    assert cost_usd("claude-haiku-4-5", 1_000_000, 200_000) == Decimal(2)
    assert cost_usd("claude-haiku-4-5", 1500, 120) == Decimal("0.0021")


def test_cost_of_a_dated_snapshot() -> None:
    assert cost_usd("claude-haiku-4-5-20251001", 1500, 120) == Decimal("0.0021")


def test_no_cost_for_an_unknown_model() -> None:
    assert cost_usd("unknown", 10, 10) is None
