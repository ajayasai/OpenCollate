from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from opencollate.boolean import check_equivalence, parse_boolean
from opencollate.model import IndexRange

_ATOM = st.sampled_from(("A", "B", "C", "0", "1"))
_EXPRESSION = st.recursive(
    _ATOM,
    lambda child: st.one_of(
        child.map(lambda value: f"!({value})"),
        st.tuples(child, child).map(lambda pair: f"({pair[0]} & {pair[1]})"),
        st.tuples(child, child).map(lambda pair: f"({pair[0]} | {pair[1]})"),
        st.tuples(child, child).map(lambda pair: f"({pair[0]} ^ {pair[1]})"),
    ),
    max_leaves=12,
)


@given(_EXPRESSION)
@settings(max_examples=100)
def test_boolean_equivalence_is_reflexive_for_generated_supported_expressions(
    expression: str,
) -> None:
    parsed = parse_boolean(expression)
    result = check_equivalence(parsed, parsed, max_variables=3)

    assert result.equivalent is True
    assert result.counterexample is None


@given(_EXPRESSION)
@settings(max_examples=100)
def test_boolean_prefix_negation_spellings_are_equivalent(expression: str) -> None:
    result = check_equivalence(f"!({expression})", f"~({expression})", max_variables=3)

    assert result.equivalent is True


@given(st.integers(-1024, 1024), st.integers(-1024, 1024))
def test_index_range_width_and_order_are_consistent(left: int, right: int) -> None:
    index_range = IndexRange(left, right)
    ordered = index_range.ordered_indices

    assert len(ordered) == index_range.width
    assert ordered[0] == left
    assert ordered[-1] == right
    assert len(set(ordered)) == len(ordered)
