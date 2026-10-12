import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from kairos import workspaces


class NewWorkspaceTests(unittest.TestCase):
    def test_creates_timestamped_folder_and_suffixes_collisions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "kairos_workspace"
            fixed = datetime(2026, 10, 11, 14, 30, 5)
            with patch.object(workspaces, "WORKSPACE_ROOT", root), \
                    patch.object(workspaces, "datetime") as clock:
                clock.now.return_value = fixed
                first = workspaces.new_workspace()
                second = workspaces.new_workspace()
                third = workspaces.new_workspace()

            self.assertEqual(first, root / "2026-10-11_143005")
            self.assertEqual(second, root / "2026-10-11_143005_2")
            self.assertEqual(third, root / "2026-10-11_143005_3")
            self.assertTrue(all(p.is_dir() for p in (first, second, third)))

    def test_default_root(self):
        self.assertEqual(workspaces.WORKSPACE_ROOT, Path.home() / "Documents" / "kairos_workspace")

    def test_name_uses_local_time_format(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(workspaces, "WORKSPACE_ROOT", Path(temp)):
                path = workspaces.new_workspace()
        self.assertRegex(path.name, re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$"))


if __name__ == "__main__":
    unittest.main()
