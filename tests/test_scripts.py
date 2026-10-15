import importlib.util
import random
from pathlib import Path


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent.parent / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _slow_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j - 1] + (x != y), prev[j] + 1, cur[j - 1] + 1))
        prev = cur
    return prev[-1]


def test_word_edit_distance_matches_textbook_dp():
    agreement = _load("agreement")
    rng = random.Random(3)
    for _ in range(200):
        a = [rng.choice("abcd") for _ in range(rng.randint(0, 12))]
        b = [rng.choice("abcd") for _ in range(rng.randint(0, 12))]
        assert agreement.edit_distance(a, b) == _slow_distance(a, b)
    assert agreement.words("It's  the PLC, isn't it?") == ["it's", "the", "plc", "isn't", "it"]
