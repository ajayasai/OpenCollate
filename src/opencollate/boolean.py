"""Small, deterministic Boolean-expression support for collateral checks.

Liberty cell functions are intentionally kept independent from any particular
file parser.  The grammar accepted here is the common two-valued Liberty
subset: constants, pin names, grouping, inversion, AND, XOR, and OR.  Exact
equivalence is established with a bounded truth table; exceeding the bound is
reported as indeterminate instead of being guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import product


class BooleanSyntaxError(ValueError):
    """A source-aware error raised for unsupported or malformed expressions."""

    def __init__(self, message: str, expression: str, offset: int) -> None:
        self.message = message
        self.expression = expression
        self.offset = offset
        super().__init__(f"{message} at offset {offset}")


class BoolExpr:
    """Base class for the immutable Boolean IR."""

    def evaluate(self, values: Mapping[str, bool]) -> bool:
        raise NotImplementedError

    def variables(self) -> frozenset[str]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class BoolConst(BoolExpr):
    value: bool

    def evaluate(self, values: Mapping[str, bool]) -> bool:
        del values
        return self.value

    def variables(self) -> frozenset[str]:
        return frozenset()

    def __str__(self) -> str:
        return "1" if self.value else "0"


@dataclass(frozen=True, slots=True)
class BoolVar(BoolExpr):
    name: str

    def evaluate(self, values: Mapping[str, bool]) -> bool:
        try:
            return bool(values[self.name])
        except KeyError as error:
            raise KeyError(f"missing Boolean value for {self.name!r}") from error

    def variables(self) -> frozenset[str]:
        return frozenset((self.name,))

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class BoolNot(BoolExpr):
    operand: BoolExpr

    def evaluate(self, values: Mapping[str, bool]) -> bool:
        return not self.operand.evaluate(values)

    def variables(self) -> frozenset[str]:
        return self.operand.variables()

    def __str__(self) -> str:
        return f"!({self.operand})"


@dataclass(frozen=True, slots=True)
class BoolAnd(BoolExpr):
    operands: tuple[BoolExpr, ...]

    def evaluate(self, values: Mapping[str, bool]) -> bool:
        return all(operand.evaluate(values) for operand in self.operands)

    def variables(self) -> frozenset[str]:
        return _variables(self.operands)

    def __str__(self) -> str:
        return "(" + " & ".join(map(str, self.operands)) + ")"


@dataclass(frozen=True, slots=True)
class BoolOr(BoolExpr):
    operands: tuple[BoolExpr, ...]

    def evaluate(self, values: Mapping[str, bool]) -> bool:
        return any(operand.evaluate(values) for operand in self.operands)

    def variables(self) -> frozenset[str]:
        return _variables(self.operands)

    def __str__(self) -> str:
        return "(" + " | ".join(map(str, self.operands)) + ")"


@dataclass(frozen=True, slots=True)
class BoolXor(BoolExpr):
    operands: tuple[BoolExpr, ...]

    def evaluate(self, values: Mapping[str, bool]) -> bool:
        result = False
        for operand in self.operands:
            result ^= operand.evaluate(values)
        return result

    def variables(self) -> frozenset[str]:
        return _variables(self.operands)

    def __str__(self) -> str:
        return "(" + " ^ ".join(map(str, self.operands)) + ")"


def _variables(expressions: Iterable[BoolExpr]) -> frozenset[str]:
    result: set[str] = set()
    for expression in expressions:
        result.update(expression.variables())
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    offset: int


_OPERATORS = frozenset("()!~'&*^|+")
_SIZED_BOOLEAN = re.compile(r"1'[bBhH]([01])(?![0-9a-fA-F_xXzZ?])")
_SIZED_LITERAL = re.compile(r"\d+'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+")
_MAX_EXPRESSION_CHARACTERS = 65_536
_MAX_EXPRESSION_TOKENS = 4_096
_MAX_GROUP_NESTING = 128


def _strip_outer_quotes(source: str) -> tuple[str, int]:
    text = source.strip()
    base = len(source) - len(source.lstrip())
    if len(text) >= 2 and text[0] == text[-1] == '"':
        # Liberty strings use backslash continuation and C-like escaping.  Only
        # unescape a quote or backslash; pin names containing other backslashes
        # must remain intact.
        inner = text[1:-1]
        inner = inner.replace("\\\n", "").replace("\\\r\n", "")
        inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner, base + 1
    return text, base


def _tokenize(expression: str) -> tuple[list[_Token], str, int]:
    if len(expression) > _MAX_EXPRESSION_CHARACTERS:
        raise BooleanSyntaxError(
            f"expression exceeds {_MAX_EXPRESSION_CHARACTERS} character limit",
            expression,
            _MAX_EXPRESSION_CHARACTERS,
        )
    source, base = _strip_outer_quotes(expression)
    tokens: list[_Token] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        sized = _SIZED_BOOLEAN.match(source, index)
        if sized is not None:
            text = sized.group(0)
            tokens.append(_Token("CONST1" if sized.group(1) == "1" else "CONST0", text, index))
            index = sized.end()
            continue
        unsupported_literal = _SIZED_LITERAL.match(source, index)
        if unsupported_literal is not None:
            raise BooleanSyntaxError(
                "only one-bit constants 0 and 1 are supported",
                expression,
                base + index,
            )
        if char in _OPERATORS:
            tokens.append(_Token(char, char, index))
            index += 1
            continue
        if char == "\\":
            start = index
            index += 1
            while index < len(source) and not source[index].isspace():
                # An operator may legally be part of an escaped Verilog name.
                index += 1
            if index == start + 1:
                raise BooleanSyntaxError("empty escaped identifier", expression, base + start)
            tokens.append(_Token("NAME", source[start:index], start))
            continue
        if char.isalnum() or char in "_.$:/[]<>-":
            start = index
            index += 1
            while index < len(source):
                next_char = source[index]
                if next_char.isalnum() or next_char in "_.$:/[]<>-":
                    index += 1
                    continue
                break
            text = source[start:index]
            lowered = text.lower().replace("_", "")
            if lowered in {"0", "1'b0", "1'h0"}:
                tokens.append(_Token("CONST0", text, start))
            elif lowered in {"1", "1'b1", "1'h1"}:
                tokens.append(_Token("CONST1", text, start))
            elif text.isdecimal():
                raise BooleanSyntaxError(
                    "only Boolean constants 0 and 1 are supported",
                    expression,
                    base + start,
                )
            else:
                tokens.append(_Token("NAME", text, start))
            continue
        raise BooleanSyntaxError(f"unsupported character {char!r}", expression, base + index)
    tokens.append(_Token("EOF", "", len(source)))
    if len(tokens) - 1 > _MAX_EXPRESSION_TOKENS:
        raise BooleanSyntaxError(
            f"expression exceeds {_MAX_EXPRESSION_TOKENS} token limit",
            expression,
            base + tokens[_MAX_EXPRESSION_TOKENS].offset,
        )
    nesting = 0
    for token in tokens:
        if token.kind == "(":
            nesting += 1
            if nesting > _MAX_GROUP_NESTING:
                raise BooleanSyntaxError(
                    f"expression exceeds {_MAX_GROUP_NESTING} group nesting limit",
                    expression,
                    base + token.offset,
                )
        elif token.kind == ")":
            nesting = max(0, nesting - 1)
    return tokens, source, base


class _Parser:
    def __init__(self, original: str) -> None:
        self.original = original
        self.tokens, self.source, self.base = _tokenize(original)
        self.index = 0

    @property
    def token(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        token = self.token
        self.index += 1
        return token

    def error(self, message: str, token: _Token | None = None) -> BooleanSyntaxError:
        actual = token or self.token
        return BooleanSyntaxError(message, self.original, self.base + actual.offset)

    def parse(self) -> BoolExpr:
        if self.token.kind == "EOF":
            raise self.error("empty Boolean expression")
        expression = self.parse_or()
        if self.token.kind != "EOF":
            raise self.error(f"unexpected token {self.token.text!r}")
        return expression

    def parse_or(self) -> BoolExpr:
        operands = [self.parse_xor()]
        while self.token.kind in {"|", "+"}:
            self.advance()
            operands.append(self.parse_xor())
        return _combine(BoolOr, operands)

    def parse_xor(self) -> BoolExpr:
        operands = [self.parse_and()]
        while self.token.kind == "^":
            self.advance()
            operands.append(self.parse_and())
        return _combine(BoolXor, operands)

    def parse_and(self) -> BoolExpr:
        operands = [self.parse_unary()]
        while True:
            if self.token.kind in {"&", "*"}:
                self.advance()
                operands.append(self.parse_unary())
                continue
            # Liberty accepts adjacency as implicit AND, for example "A B'".
            if self.token.kind in {"NAME", "CONST0", "CONST1", "(", "!", "~"}:
                operands.append(self.parse_unary())
                continue
            break
        return _combine(BoolAnd, operands)

    def parse_unary(self) -> BoolExpr:
        inversions = 0
        while self.token.kind in {"!", "~"}:
            inversions += 1
            self.advance()
        expression = self.parse_primary()
        while self.token.kind == "'":
            inversions += 1
            self.advance()
        if inversions % 2:
            return BoolNot(expression)
        return expression

    def parse_primary(self) -> BoolExpr:
        token = self.advance()
        if token.kind == "CONST0":
            return BoolConst(False)
        if token.kind == "CONST1":
            return BoolConst(True)
        if token.kind == "NAME":
            return BoolVar(token.text)
        if token.kind == "(":
            expression = self.parse_or()
            if self.token.kind != ")":
                raise self.error("expected ')'")
            self.advance()
            return expression
        raise self.error(f"expected pin, constant, or '('; found {token.text!r}", token)


def _combine(
    kind: type[BoolAnd] | type[BoolOr] | type[BoolXor],
    operands: list[BoolExpr],
) -> BoolExpr:
    if len(operands) == 1:
        return operands[0]
    flattened: list[BoolExpr] = []
    for operand in operands:
        if isinstance(operand, kind):
            flattened.extend(operand.operands)
        else:
            flattened.append(operand)
    return kind(tuple(flattened))


def parse_boolean(expression: str) -> BoolExpr:
    """Parse a common two-valued Liberty/Verilog Boolean expression."""

    try:
        return _Parser(expression).parse()
    except RecursionError as error:  # defensive boundary for platform recursion limits
        raise BooleanSyntaxError(
            "expression exceeds parser nesting limit",
            expression,
            min(len(expression), _MAX_EXPRESSION_CHARACTERS),
        ) from error


@dataclass(frozen=True, slots=True)
class EquivalenceResult:
    """Exact bounded-equivalence result.

    ``equivalent`` is ``None`` when the configured variable limit is exceeded.
    A false result includes the first deterministic counterexample.
    """

    equivalent: bool | None
    variables: tuple[str, ...]
    checked_assignments: int
    counterexample: Mapping[str, bool] | None = None
    reason: str | None = None


def check_equivalence(
    left: BoolExpr | str,
    right: BoolExpr | str,
    *,
    max_variables: int = 12,
    aliases: Mapping[str, str] | None = None,
) -> EquivalenceResult:
    """Compare two functions exactly with a bounded truth table.

    ``aliases`` maps native variable names to canonical names before matching.
    It is useful when the same pin has view-specific spelling.
    """

    if max_variables < 0:
        raise ValueError("max_variables must be non-negative")
    left_expr = parse_boolean(left) if isinstance(left, str) else left
    right_expr = parse_boolean(right) if isinstance(right, str) else right
    alias_map = aliases or {}
    left_names = left_expr.variables()
    right_names = right_expr.variables()
    variables = tuple(sorted({alias_map.get(name, name) for name in left_names | right_names}))
    if len(variables) > max_variables:
        return EquivalenceResult(
            equivalent=None,
            variables=variables,
            checked_assignments=0,
            reason=(
                f"function has {len(variables)} variables; exact truth-table limit "
                f"is {max_variables}"
            ),
        )

    checked = 0
    for bits in product((False, True), repeat=len(variables)):
        canonical_values = dict(zip(variables, bits, strict=True))
        left_values = {name: canonical_values[alias_map.get(name, name)] for name in left_names}
        right_values = {name: canonical_values[alias_map.get(name, name)] for name in right_names}
        checked += 1
        if left_expr.evaluate(left_values) != right_expr.evaluate(right_values):
            return EquivalenceResult(
                equivalent=False,
                variables=variables,
                checked_assignments=checked,
                counterexample=canonical_values,
                reason="functions differ for the returned input assignment",
            )
    return EquivalenceResult(
        equivalent=True,
        variables=variables,
        checked_assignments=checked,
    )


__all__ = [
    "BoolAnd",
    "BoolConst",
    "BoolExpr",
    "BoolNot",
    "BoolOr",
    "BoolVar",
    "BoolXor",
    "BooleanSyntaxError",
    "EquivalenceResult",
    "check_equivalence",
    "parse_boolean",
]
