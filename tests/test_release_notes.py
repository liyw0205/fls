import unittest

from tools.release_notes import extract_release_notes


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_requested_version_section(self):
        changelog = """# Changelog

## 2.0

- current change

## 1.0

- previous change
"""

        self.assertEqual(
            extract_release_notes(changelog, "2.0"),
            "## 2.0\n\n- current change\n",
        )
        self.assertNotIn("previous change", extract_release_notes(changelog, "2.0"))

    def test_missing_version_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_release_notes("# Changelog\n\n## 1.0\n- old\n", "2.0")


if __name__ == "__main__":
    unittest.main()
