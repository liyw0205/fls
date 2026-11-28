import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_bulk_workflows, test_ui_route_components


class FixturePathTests(unittest.TestCase):
    def test_isolated_roots_match_backend_canonical_paths(self):
        temporary_directory = tempfile.TemporaryDirectory
        for fixture in (test_bulk_workflows, test_ui_route_components):
            with self.subTest(fixture=fixture.__name__), temporary_directory() as directory:
                parent = Path(directory)
                if os.name != "nt":
                    target = parent / "canonical"
                    target.mkdir()
                    alias = parent / "alias"
                    alias.symlink_to(target, target_is_directory=True)
                    parent = alias

                def aliased_temporary_directory(*args, **kwargs):
                    return temporary_directory(*args, **dict(kwargs, dir=parent))

                with patch.object(tempfile, "TemporaryDirectory", side_effect=aliased_temporary_directory):
                    with fixture.isolated_app() as (_app, root):
                        from fls_manager import paths

                        self.assertEqual(root, paths.BASE_DIR)
                        self.assertEqual(root, root.resolve())
