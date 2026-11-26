"""Shared for every test: the hub sees a disk with room to spare, whatever machine the tests run on (a nearly
full disk on the test machine would otherwise show up as a Health problem in unrelated tests). Tests about
storage set their own."""
import pytest

from relay import media


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    monkeypatch.setattr(media, "disk", lambda path: (60 << 30, 100 << 30))
