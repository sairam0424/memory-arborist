"""Found by a third verification pass: the count options took int() at its word, so +3, 1_0, a number with spaces and digits of
other scripts (which int() reads as numbers) were accepted. A count here is ASCII digits and nothing else, above zero."""

import argparse
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

sys.path.insert(0, th.SCRIPTS)
import memory_common as mc  # noqa: E402


class PositiveIntStrictTests(unittest.TestCase):
    def test_only_ascii_digits_above_zero_are_counts(self):
        for good, value in (("1", 1), ("3", 3), ("20", 20), ("007", 7)):
            with self.subTest(good):
                self.assertEqual(mc.positive_int(good), value)
        for bad in ("+3", "1_0", " 3", "3 ", "3\n", "٣", "３", "²", "0", "00", "-1", "2.5", "", "nan", "1e3", "0x10"):
            with self.subTest(repr(bad)), self.assertRaises(argparse.ArgumentTypeError):
                mc.positive_int(bad)

    def test_more_digits_than_int_will_read_is_a_refusal_or_a_number_never_a_crash(self):
        try:  # Python 3.11 and later refuse to read 4,300 or more digits; older versions read them
            value = mc.positive_int("9" * 5000)
        except argparse.ArgumentTypeError:
            return
        self.assertGreater(value, 0)


if __name__ == "__main__":
    unittest.main()
