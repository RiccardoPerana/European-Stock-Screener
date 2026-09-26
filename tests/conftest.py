"""
Pytest fixtures. Imports resolve against core/ and gui/ via pytest.ini.

`db` is module-scoped on purpose: the Cache tests in test_cache.py build on
each other's writes, in file order.
"""

import tempfile
from pathlib import Path

import pytest

from cache import Cache


@pytest.fixture(scope="module")
def db():
    """One shared Cache for the whole module, on a throwaway file."""
    with tempfile.TemporaryDirectory() as tmp:
        with Cache(Path(tmp) / "test.db") as cache:
            yield cache
