import pytest

from billing import ledger


@pytest.fixture
def conn():
    c = ledger.connect(":memory:")
    yield c
    c.close()
