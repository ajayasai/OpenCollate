from __future__ import annotations

import pytest

from opencollate.model import (
    BusShape,
    ComponentKind,
    ContractComponent,
    ContractPort,
    ContractRegister,
    ContractRegisterField,
    DesignContract,
    Direction,
    IndexRange,
    PortRole,
    Provenance,
    SourceSpan,
    ViewId,
    choose_provenance,
    decoded_identifier,
)


def test_view_id_parse_and_match() -> None:
    view = ViewId.parse("liberty.tt_0p80v")
    assert view.kind == "liberty"
    assert view.name == "tt_0p80v"
    assert str(view) == "liberty.tt_0p80v"
    assert view.matches("liberty")
    assert view.matches("LIBERTY.TT_0P80V")
    assert view.matches("*")
    assert ViewId.parse("rtl") == ViewId("rtl", "default")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("in", Direction.INPUT),
        ("OUT", Direction.OUTPUT),
        ("bidirectional", Direction.INOUT),
        ("FEEDTHRU", Direction.FEEDTHROUGH),
        ("mystery", Direction.UNKNOWN),
    ],
)
def test_direction_aliases(value: str, expected: Direction) -> None:
    assert Direction.parse(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("primary_power", PortRole.POWER),
        ("internal_ground", PortRole.GROUND),
        ("clk", PortRole.CLOCK),
        ("tie_high", PortRole.TIE),
        ("mystery", PortRole.UNKNOWN),
    ],
)
def test_role_aliases(value: str, expected: PortRole) -> None:
    assert PortRole.parse(value) == expected


def test_bus_shape_preserves_width_and_order() -> None:
    descending = BusShape(packed=(IndexRange(7, 0),))
    ascending = BusShape(left=0, right=7)
    assert descending.width == ascending.width == 8
    assert descending.ascending is False
    assert ascending.ascending is True
    assert descending.ordered_indices == tuple(range(7, -1, -1))
    assert descending.signature() != ascending.signature()


def test_multidimensional_shape_width() -> None:
    shape = BusShape(packed=(IndexRange(1, 0), IndexRange(3, 0)))
    assert shape.width == 8
    assert shape.ordered_indices is None


def test_exploded_bits_retain_duplicates_and_gaps() -> None:
    shape = BusShape(bit_indices=(3, 1, 1, 0))
    assert shape.width == 3
    assert shape.has_duplicate_bits
    assert shape.has_bit_gap


def test_scalar_and_single_bit_vector_are_distinct() -> None:
    scalar = BusShape.scalar()
    vector = BusShape(packed=(IndexRange(0, 0),))
    assert scalar.width == vector.width == 1
    assert scalar.signature() != vector.signature()


def test_invalid_width_and_positions_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        BusShape(width=0)
    with pytest.raises(ValueError, match="one-based"):
        SourceSpan("bad.sv", line=0)
    with pytest.raises(ValueError, match="precede"):
        SourceSpan("bad.sv", line=2, end_line=1)


def test_contract_round_trip_is_lossless() -> None:
    contract = DesignContract(
        components=(
            ContractComponent(
                canonical_name="uart",
                kind=ComponentKind.MODULE,
                names={"rtl.default": "uart", "liberty.tt": "UART"},
                required_views=("rtl.default", "liberty.tt"),
                ports=(
                    ContractPort(
                        canonical_name="irq",
                        names={"rtl.default": "irq_o"},
                        direction=Direction.OUTPUT,
                        role=PortRole.SIGNAL,
                        shape=BusShape.scalar(),
                    ),
                ),
            ),
        ),
        registers=(
            ContractRegister(
                canonical_name="CTRL",
                component="uart",
                names={"ipxact.default": "ctrl", "header.default": "UART_CTRL"},
                memory_map="regs",
                address_block="uart_regs",
                address_offset=0,
                size_bits=32,
                access="read-write",
                fields=(
                    ContractRegisterField(
                        canonical_name="ENABLE",
                        names={"ipxact.default": "enable"},
                        bit_offset=0,
                        bit_width=1,
                        reset_value=0,
                    ),
                ),
            ),
        ),
    )
    restored = DesignContract.from_dict(contract.to_dict())
    assert restored.to_dict() == contract.to_dict()


def test_identifier_decoding_does_not_treat_punctuation_as_hierarchy() -> None:
    assert decoded_identifier(r"\foo.bar ") == "foo.bar"
    assert decoded_identifier("ordinary") == "ordinary"


def test_choose_provenance_is_stable() -> None:
    later = Provenance("z.sv", 1, view=ViewId("rtl"))
    earlier = Provenance("a.sv", 20, view=ViewId("rtl"))
    assert choose_provenance((later, None, earlier)) == earlier
    assert choose_provenance((None,)) is None
