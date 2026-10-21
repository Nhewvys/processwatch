import hashlib
from pathlib import Path
import unittest

from processwatch.legal_catalog import DEFAULT_ROLES, load_roles
from processwatch.source_matrix import MATRIX, load_document


ROOT = Path(__file__).resolve().parents[1]


class DistributionResourceTests(unittest.TestCase):
    def test_runtime_catalogs_are_bundled_and_match_source_copies(self):
        pairs = (
            (ROOT / "data/legal/roles-2026-10-14.json",
             ROOT / "src/processwatch/_data/legal/roles-2026-10-14.json"),
            (ROOT / "data/sources/courts.json",
             ROOT / "src/processwatch/_data/sources/courts.json"),
        )
        for source, bundled in pairs:
            self.assertTrue(source.is_file())
            self.assertTrue(bundled.is_file())
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),
                             hashlib.sha256(bundled.read_bytes()).digest())

    def test_source_checkout_runtime_catalogs_remain_available(self):
        self.assertTrue(DEFAULT_ROLES.is_file())
        self.assertTrue(MATRIX.is_file())
        self.assertTrue(load_roles()["roles"])
        self.assertTrue(load_document()["tribunals"])


if __name__ == "__main__":
    unittest.main()
