"""
write_workbook.verify_conventions() -- the template drift guard.

No openpyxl workbook needed: verify_conventions() only ever reads
`ws[ref].value`, so a minimal fake sheet exercises it in isolation.
"""

import pytest

from write_workbook import CONVENTIONS, verify_conventions


class _FakeCell:
    def __init__(self, value):
        self.value = value


class _FakeSheet:
    def __init__(self, values: dict):
        self._cells = {ref: _FakeCell(v) for ref, v in values.items()}

    def __getitem__(self, ref):
        return self._cells.get(ref, _FakeCell(None))


def _valid_values() -> dict:
    """Every CONVENTIONS cell at its expected value -- a sheet that passes."""
    return dict(CONVENTIONS)


def test_a_conforming_sheet_passes():
    verify_conventions(_FakeSheet(_valid_values()))  # must not raise


def test_a_drifted_cell_raises_value_error():
    values = _valid_values()
    values["B65"] = 0.5  # expected 0.12
    with pytest.raises(ValueError, match="Template conventions have drifted"):
        verify_conventions(_FakeSheet(values))


def test_blank_b63_raises_value_error_not_type_error():
    """
    B63 missing must still reach the intended ValueError.

    The B63 + B64 == 10 check used to run unconditionally, so a blank B63
    (None) crashed on `None + int` with a raw TypeError before the
    ValueError this function exists to raise was ever reached.
    """
    values = _valid_values()
    values["B63"] = None
    with pytest.raises(ValueError, match="Template conventions have drifted"):
        verify_conventions(_FakeSheet(values))


def test_blank_b64_raises_value_error_not_type_error():
    values = _valid_values()
    values["B64"] = None
    with pytest.raises(ValueError, match="Template conventions have drifted"):
        verify_conventions(_FakeSheet(values))


def test_b63_plus_b64_not_ten_is_flagged():
    values = _valid_values()
    values["B63"], values["B64"] = 4, 5  # both individually "wrong" too,
    # but the point here is the sum check firing on its own message
    with pytest.raises(ValueError, match="B63 \\+ B64 must equal 10"):
        verify_conventions(_FakeSheet(values))
