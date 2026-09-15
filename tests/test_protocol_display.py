"""Tests for the V4 display/Decimal boundary (T012, ADR-009).

The display helper lives in :mod:`robinhood_lp.presentation.prices`
and is the single function in V1 permitted to convert between the
integer protocol domain and the ``Decimal`` display domain.

These tests pin the boundary math (no ``float`` involved) and
guard against re-introducing ``float`` symbols in the protocol
package (ADR-009).
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from robinhood_lp.presentation.prices import (
    DISPLAY_PRICE_QUANTUM_EXPONENT,
    to_display_price,
)

# ---------------------------------------------------------------------------
# Display math: known closed-form values
# ---------------------------------------------------------------------------


def test_to_display_price_decimals_zero_zero() -> None:
    """sqrtPriceX96 = 2**96 corresponds to a raw price of 1.0.

    With ``decimals0 == decimals1 == 0`` the decimal shift is unity,
    so the display price must be exactly 1 (to within 1e-10 of the
    18-fractional-digit quantum).
    """
    sqrt_price_x96 = 1 << 96  # 79228162514264337593543950336
    result = to_display_price(sqrt_price_x96, 0, 0)
    expected = Decimal("1")
    assert isinstance(result, Decimal)
    assert abs(result - expected) < Decimal("1e-10"), (
        f"to_display_price(2**96, 0, 0) = {result}; expected ~1.0"
    )
    # And the quantisation quantum is 1e-18.
    assert result == expected.quantize(Decimal("1e-18"))


def test_to_display_price_decimals_six_eighteen() -> None:
    """6/18-decimals pair (USDC/WETH): display price of raw=1.0 is 10**12.

    Derivation: a raw price of 1.0 token1 per token0 with token0
    having 6 decimals (USDC) and token1 having 18 decimals (WETH)
    means 1 raw unit of token0 (= 1e-6 human USDC) is worth 1 raw
    unit of token1 (= 1e-18 human WETH), so the human ratio is
    1 human WETH per 1e-12 human USDC -> wait, that's the inverse.
    The V4 convention: ``price`` is units of token1 per unit of
    token0. With decimals1=18 and decimals0=6 the raw-to-human
    scale is ``10**(decimals1 - decimals0) = 10**12``.

    Closed form: raw=1.0 -> display = 1.0 * 10**12 = 1e12 human
    WETH per human USDC. This is the canonical USDC/WETH display
    when the *quote* is WETH.
    """
    sqrt_price_x96 = 1 << 96
    # Closed form: 1.0 raw ratio * 10**12 = 10**12 human WETH / USDC.
    expected = Decimal("1000000000000").quantize(Decimal(10) ** -DISPLAY_PRICE_QUANTUM_EXPONENT)
    result = to_display_price(sqrt_price_x96, 6, 18)
    assert isinstance(result, Decimal)
    assert result == expected, f"to_display_price(2**96, 6, 18) = {result}; expected {expected}"


def test_to_display_price_inverse_of_decimals_inverts_price() -> None:
    """Swapping decimals0 and decimals1 inverts the display price.

    For the same sqrt_price_x96 (raw price = 1.0),
    display(sqrt, 6, 18) * display(sqrt, 18, 6) must equal 1.0
    (within 18 fractional digits), because the two scale factors
    are reciprocals.
    """
    sqrt_price_x96 = 1 << 96
    forward = to_display_price(sqrt_price_x96, 6, 18)
    inverse = to_display_price(sqrt_price_x96, 18, 6)
    product = forward * inverse
    # Each is quantised to 1e-18; the product's precision is bounded
    # by 1e-18 * (max value). Use a relative-style absolute tolerance
    # against 1.0 - product, bounded by 1e-9.
    assert abs(product - Decimal("1")) < Decimal("1e-9"), (
        f"forward*inverse = {product}; expected ~1.0"
    )


def test_to_display_price_rejects_non_int_sqrt() -> None:
    """sqrt_price_x96 must be a real ``int`` (bool is rejected)."""
    with pytest.raises(TypeError):
        to_display_price(79228162514264337593543950336.0, 0, 0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_display_price("79228162514264337593543950336", 0, 0)  # type: ignore[arg-type]
    # mypy treats bool as a subclass of int, so the call below passes
    # mypy's type check; the runtime isinstance check is the binding
    # contract that rejects bool even though bool isinstance int.
    with pytest.raises(TypeError):
        to_display_price(True, 0, 0)


def test_to_display_price_rejects_negative_sqrt() -> None:
    with pytest.raises(ValueError):
        to_display_price(-1, 0, 0)


def test_to_display_price_rejects_uint160_overflow() -> None:
    """sqrt_price_x96 must fit in uint160 (V4 domain)."""
    with pytest.raises(ValueError):
        to_display_price(1 << 160, 0, 0)


def test_to_display_price_rejects_out_of_range_decimals() -> None:
    """decimals0 / decimals1 must be in [0, 36] (ERC-20 contract)."""
    with pytest.raises(ValueError):
        to_display_price(1 << 96, -1, 0)
    with pytest.raises(ValueError):
        to_display_price(1 << 96, 0, 37)
    with pytest.raises(TypeError):
        to_display_price(1 << 96, 6.0, 18)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Protocol-package float guard (ADR-009)
# ---------------------------------------------------------------------------


def _iter_python_sources(package_dir: Path) -> list[Path]:
    """Return all .py source files in ``package_dir``, excluding bytecode caches."""
    return sorted(p for p in package_dir.rglob("*.py") if "__pycache__" not in p.parts)


def _collect_float_symbols(package_dir: Path) -> list[str]:
    """Walk every .py source under ``package_dir`` and collect lines where
    ``float`` appears as a Python identifier in a load-bearing position
    (type annotation, default value, decorator, or reference in a
    function-call position).

    Plain textual mentions in docstrings or comments are *not* an
    ADR-009 violation: ADR-009 forbids ``float`` *symbols* (type hints
    and re-exports), not commentary. We accept the trade-off that the
    public API must be ``float``-typed-free; the test below enforces
    exactly that, not a blanket text grep.
    """
    offenders: list[str] = []
    for path in _iter_python_sources(package_dir):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            node_line = getattr(node, "lineno", 0)
            label: str | None = None
            if isinstance(node, ast.Name) and node.id == "float":
                label = f"Name 'float' at line {node_line}"
            elif isinstance(node, ast.Attribute) and node.attr == "float":
                label = f"Attribute '.float' at line {node_line}"
            elif isinstance(node, ast.arg) and node.arg == "float":
                label = f"Argument 'float' at line {node_line}"
            if label is not None:
                offenders.append(f"{path}:{node_line}: {label}")
    return offenders


def test_to_display_price_no_float_in_protocol_package() -> None:
    """ADR-009: ``src/robinhood_lp/protocol/`` has zero ``float`` symbols.

    This is a property test that fails if anyone re-adds a float
    helper. We walk every Python source file under the protocol
    package and reject any ``float`` *symbol* (type annotation,
    argument, attribute access, or import alias).
    """
    repo_root = Path(__file__).resolve().parents[1]
    protocol_dir = repo_root / "src" / "robinhood_lp" / "protocol"
    assert protocol_dir.is_dir(), f"protocol dir missing: {protocol_dir}"
    offenders = _collect_float_symbols(protocol_dir)
    if offenders:
        pytest.fail("Found `float` symbols in src/robinhood_lp/protocol/:\n" + "\n".join(offenders))
