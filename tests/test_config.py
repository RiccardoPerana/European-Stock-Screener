"""
config.fiscal_years() -- the screen_config.json "fiscal_years" override.

The model requires exactly five CONSECUTIVE years. Checking length and
ascending order alone would let a window with a gap in it (e.g. skipping
one year) pass silently, and every multi-year calculation downstream would
quietly use a non-consecutive window.
"""

import pytest

import config


def _with_years(monkeypatch, years):
    monkeypatch.setattr(config, "_load_config",
                        lambda path=config.CONFIG_FILE: {"fiscal_years": years})


def test_five_consecutive_years_pass(monkeypatch):
    _with_years(monkeypatch, [2021, 2022, 2023, 2024, 2025])
    assert config.fiscal_years() == [2021, 2022, 2023, 2024, 2025]


def test_a_gap_in_the_window_is_rejected(monkeypatch):
    _with_years(monkeypatch, [2020, 2021, 2023, 2024, 2025])  # skips 2022
    with pytest.raises(SystemExit):
        config.fiscal_years()


def test_wrong_length_is_rejected(monkeypatch):
    _with_years(monkeypatch, [2021, 2022, 2023])
    with pytest.raises(SystemExit):
        config.fiscal_years()


def test_descending_years_are_rejected(monkeypatch):
    _with_years(monkeypatch, [2025, 2024, 2023, 2022, 2021])
    with pytest.raises(SystemExit):
        config.fiscal_years()


def test_duplicate_years_are_rejected(monkeypatch):
    _with_years(monkeypatch, [2021, 2022, 2022, 2024, 2025])
    with pytest.raises(SystemExit):
        config.fiscal_years()


def test_explicit_fy0_bypasses_the_config_file():
    """An explicit fy0 skips screen_config.json entirely."""
    assert config.fiscal_years(fy0=2025) == [2021, 2022, 2023, 2024, 2025]
