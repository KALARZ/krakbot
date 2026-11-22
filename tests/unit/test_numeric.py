from decimal import Decimal

import pytest

from krakbot.numeric import D, ceil_to_step, floor_to_step, from_db, round_price_passive, step_from_decimals, to_db


def test_D_accepts_str_int_decimal():
    assert D("0.00006") == Decimal("0.00006")
    assert D(5) == Decimal(5)
    assert D(Decimal("1.5")) == Decimal("1.5")


@pytest.mark.parametrize("bad", [0.1, True, None, [1]])
def test_D_rejects_float_bool_and_others(bad):
    with pytest.raises(TypeError):
        D(bad)


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "Infinity", "-inf"])
def test_D_rejects_non_finite_and_garbage(bad):
    with pytest.raises(ValueError):
        D(bad)


def test_to_db_never_uses_exponent_and_no_negative_zero():
    assert to_db(Decimal("1E-8")) == "0.00000001"
    assert to_db(Decimal("54557.1")) == "54557.1"
    assert to_db(Decimal("-0.00")) == "0.00"
    assert to_db(None) is None
    with pytest.raises(TypeError):
        to_db(0.5)  # type: ignore[arg-type]


def test_roundtrip():
    for s in ["0", "0.00000001", "123456.78901234", "-12.5"]:
        assert to_db(from_db(s)) == s


def test_steps_and_rounding():
    lot = step_from_decimals(8)
    assert lot == Decimal("0.00000001")
    assert floor_to_step(Decimal("0.123456789"), lot) == Decimal("0.12345678")
    tick = Decimal("0.1")
    assert floor_to_step(Decimal("54557.19"), tick) == Decimal("54557.1")
    assert ceil_to_step(Decimal("54557.11"), tick) == Decimal("54557.2")
    assert ceil_to_step(Decimal("54557.1"), tick) == Decimal("54557.1")
    # passive rounding: buys down, sells up
    assert round_price_passive(Decimal("100.05"), tick, "buy") == Decimal("100.0")
    assert round_price_passive(Decimal("100.05"), tick, "sell") == Decimal("100.1")
    with pytest.raises(ValueError):
        floor_to_step(Decimal(1), Decimal(0))
    with pytest.raises(ValueError):
        step_from_decimals(19)
