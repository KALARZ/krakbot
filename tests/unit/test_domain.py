import pytest

from krakbot.domain import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    TradeState,
    assert_transition,
    can_transition,
)


def test_every_state_has_a_transition_entry():
    assert set(TRANSITIONS) == set(TradeState)


def test_terminal_states_have_no_exit():
    for s in TERMINAL_STATES:
        assert TRANSITIONS[s] == frozenset()


def test_non_terminal_states_can_reach_a_terminal_state():
    for s in set(TradeState) - TERMINAL_STATES:
        seen, frontier = {s}, [s]
        while frontier:
            for n in TRANSITIONS[frontier.pop()]:
                if n not in seen:
                    seen.add(n)
                    frontier.append(n)
        assert seen & TERMINAL_STATES, s


def test_position_states_can_always_be_flattened_or_reprotected():
    # any state holding a position must be able to go to FLATTENING (kill switch) or be re-protected
    for s in (TradeState.OPEN_PROTECTED, TradeState.UNPROTECTED, TradeState.EXITING, TradeState.ENTRY_WORKING):
        assert can_transition(s, TradeState.FLATTENING)
    assert can_transition(TradeState.UNPROTECTED, TradeState.OPEN_PROTECTED)


def test_illegal_transitions_raise():
    with pytest.raises(IllegalTransition):
        assert_transition(TradeState.CLOSED, TradeState.OPEN_PROTECTED)
    with pytest.raises(IllegalTransition):
        assert_transition(TradeState.SIGNALLED, TradeState.CLOSED)
    assert_transition(TradeState.ENTRY_WORKING, TradeState.OPEN_PROTECTED)
