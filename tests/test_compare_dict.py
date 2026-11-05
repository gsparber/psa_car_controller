import unittest

import numpy as np

from tests.utils import compare_dict


class TestCompareDict(unittest.TestCase):
    def test_same(self):
        self.assertTrue(compare_dict([{"a": 1.5, "b": [1, "x"]}], [{"a": 1.5, "b": [1, "x"]}]))

    def test_differences(self):
        for result, expected in (({"a": 1.5}, {"a": 1.6}), ({"a": 1}, {"a": 1, "b": 2}), ({"a": 1, "b": 2}, {"a": 1}),
                                 ([1, 2], [1]), ({"a": np.float64(1.5)}, {"a": 1.5}), ({"a": 1}, {"a": 1.0}),
                                 ({"a": float("nan")}, {"a": float("nan")})):
            with self.subTest(result=result, expected=expected), self.assertRaises(AssertionError):
                compare_dict(result, expected)
