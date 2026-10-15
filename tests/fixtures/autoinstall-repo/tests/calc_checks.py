import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.calc import add  # noqa: E402


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0
