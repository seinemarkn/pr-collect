import unittest

from lib.widgets.catalog import Catalog, Widget


class TestCatalog(unittest.TestCase):
    def test_names(self):
        self.assertEqual(Catalog().names(), ["bolt", "gear", "spring", "washer"])

    def test_largest(self):
        self.assertEqual(Catalog().largest().name, "gear")

    def test_total_size(self):
        self.assertEqual(Catalog().total_size(), 11)

    def test_empty(self):
        self.assertEqual(Catalog([]).names(), [])


if __name__ == "__main__":
    unittest.main()
