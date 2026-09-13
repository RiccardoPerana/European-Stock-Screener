"""
Pytest fixtures.

test_cache.py was written with a hand-rolled runner in `__main__` that
threaded one Cache through six tests in order. Under pytest the parameter
was simply an unknown fixture name, so all six ERRORed and only the four
that take no argument ran -- the suite looked like it passed while the
entire persistence layer went untested.

The `db` fixture below restores them. It is module-scoped on purpose: the
six tests build on each other's writes, exactly as the original runner
intended, and making them independent would mean rewriting the assertions
rather than fixing the plumbing.
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
