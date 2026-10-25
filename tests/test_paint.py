"""Tests for paint.py. Run with: python -m unittest discover tests"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import paint  # noqa: E402


def random_tree(rng: random.Random, depth: int = 0) -> list[tuple[str, int]]:
    """A random TriangleSelector tree as ('split', n) / ('side', s) / ('leaf', state) tokens."""
    if depth < 3 and rng.random() < 0.4:
        split = rng.randint(1, 3)
        tokens = [('split', split), ('side', rng.randint(0, 3))]
        for _ in range(split + 1):
            tokens += random_tree(rng, depth + 1)
        return tokens
    return [('split', 0), ('leaf', rng.randint(0, paint.MAX_STATE))]


def encode(tokens: list[tuple[str, int]]) -> str:
    """Write tokens the way Bambu Studio does: bits from bit 0 up, nibbles last first."""
    v, pos = 0, 0
    for kind, value in tokens:
        if kind == 'leaf':
            bits, n = (value, 2) if value < 3 else (3 | ((value - 3) << 2), 6)
        else:
            bits, n = value, 2
        v |= bits << pos
        pos += n
    return format(v, 'X').zfill((pos + 3) // 4)


class KnownStrings(unittest.TestCase):
    # Worked by hand from the format in paint.py's docstring.
    def test_whole_triangle_leaves(self):
        self.assertEqual(paint.states('4'), {1: 1})    # split 00, state 01
        self.assertEqual(paint.states('8'), {2: 1})    # split 00, state 10
        self.assertEqual(paint.states('7C'), {10: 1})  # split 00, state 11, ext 0111 -> 3 + 7

    def test_split_triangle(self):
        # split 01 (two children), side 00, leaf 1, leaf 0
        self.assertEqual(paint.states('041'), {1: 1, 0: 1})

    def test_remap_changes_only_states(self):
        self.assertEqual(paint.remap('7C', {10: 2}), '8')    # 6 bits -> 2 bits
        self.assertEqual(paint.remap('4', {1: 10}), '7C')    # 2 bits -> 6 bits
        self.assertEqual(paint.remap('041', {1: 3}), '00C1')  # the leaf grows, the tree stays

    def test_state_zero_never_moves(self):
        self.assertEqual(paint.remap('041', {1: 1, 0: 4}), '041')


class Refusals(unittest.TestCase):
    def test_unmapped_state_raises(self):
        with self.assertRaises(KeyError):
            paint.remap('7C', {1: 1})

    def test_out_of_range_state_raises(self):
        with self.assertRaises(ValueError):
            paint.remap('4', {1: paint.MAX_STATE + 1})

    def test_truncated_string_raises(self):
        with self.assertRaises(ValueError):
            paint.states('1')  # split 01 promises two children that aren't there


class RandomTrees(unittest.TestCase):
    def test_round_trip_and_remap(self):
        rng = random.Random(20261025)
        for _ in range(2000):
            tokens = random_tree(rng)
            code = encode(tokens)
            leaves = [v for k, v in tokens if k == 'leaf']
            self.assertEqual(paint.remap(code, {s: s for s in range(1, paint.MAX_STATE + 1)}), code)
            self.assertEqual(sum(paint.states(code).values()), len(leaves))
            mapping = {s: rng.randint(1, paint.MAX_STATE) for s in range(1, paint.MAX_STATE + 1)}
            want = encode([(k, mapping[v] if k == 'leaf' and v else v) for k, v in tokens])
            self.assertEqual(paint.remap(code, mapping), want)


if __name__ == '__main__':
    unittest.main()
