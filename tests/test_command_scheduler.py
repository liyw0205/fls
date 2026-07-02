import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.dont_write_bytecode = True

_BASE_DIR_CTX = tempfile.TemporaryDirectory()
os.environ["FLS_BASE_DIR"] = _BASE_DIR_CTX.name

from fls_manager import command, config, paths, scheduler as scheduler_mod


class CommandSchedulerTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(paths.SCRIPT_DIR, ignore_errors=True)
        paths.SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)

        config.CONFIG_FILE.write_text(
            json.dumps(
                {
                    "timezone_offset_hours": 8,
                    "panel_time_offset_seconds": 3661,
                    "task_types": {
                        "py": True,
                        "js": True,
                    },
                }
            ),
            encoding="utf-8",
        )

    def script(self, relative_path, text=""):
        path = paths.SCRIPT_DIR / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path.resolve()

    def test_normalize_script_type_aliases(self):
        self.assertEqual(command.normalize_script_type(".PY"), "py")
        self.assertEqual(command.normalize_script_type("mjs"), "js")
        self.assertEqual(command.normalize_script_type("cjs"), "js")
        self.assertEqual(command.normalize_script_type("bash"), "sh")
        self.assertEqual(command.normalize_script_type("CMD"), "bat")
        self.assertEqual(command.normalize_script_type("unknown"), "unknown")

    def test_command_list_to_shell_quotes_arguments(self):
        cmd = ["python", "two words", "plain"]

        if os.name == "nt":
            expected = subprocess.list2cmdline(cmd)
        else:
            expected = "python 'two words' plain"

        self.assertEqual(command.command_list_to_shell(cmd), expected)

    def test_parse_and_build_python_task_command(self):
        script_path = self.script("hello.py")

        with mock.patch.object(command, "PYTHON_BIN", "/test/python"):
            parsed = command.parse_task_line_to_cmd(
                'task hello.py "two words" --flag'
            )
            built = command.build_command(
                {"command": 'task hello.py "two words" --flag'}
            )

        expected_cmd = [
            "/test/python",
            str(script_path),
            "two words",
            "--flag",
        ]
        self.assertEqual(parsed, expected_cmd)
        self.assertEqual(built["cmd"], expected_cmd)
        self.assertEqual(
            built["display_cmd"],
            command.command_list_to_shell(expected_cmd),
        )
        self.assertFalse(built["shell"])
        self.assertEqual(built["cwd"], str(script_path.parent))
        self.assertEqual(built["mode"], "task")

    def test_build_mixed_command_expands_task_lines(self):
        py_script = self.script("jobs/hello.py")
        js_script = self.script("demo.mjs")
        raw = "\n".join(
            [
                "echo start",
                "task jobs/hello.py one",
                "cd work",
                'task demo.mjs "two words"',
                "echo done",
            ]
        )

        with mock.patch.object(command, "PYTHON_BIN", "/test/python"), \
                mock.patch.object(command, "NODE_BIN", "/test/node"), \
                mock.patch.object(command, "_exists", return_value=True):
            built = command.build_command({"command": raw})

        expected = "\n".join(
            [
                "echo start",
                f"/test/python {py_script} one",
                "cd work",
                f"/test/node {js_script} 'two words'",
                "echo done",
            ]
        )
        self.assertTrue(built["shell"])
        self.assertEqual(built["cmd"], expected)
        self.assertEqual(built["display_cmd"], expected)
        self.assertEqual(built["cwd"], str(paths.SCRIPT_DIR))
        self.assertEqual(built["mode"], "mixed")

    def test_cron_to_trigger_supports_five_and_six_fields(self):
        tz = config.get_panel_timezone()
        now = datetime(2026, 1, 1, 6, 0, 0, tzinfo=tz)

        five_field = scheduler_mod.cron_to_trigger("30 6 * * *")
        six_field = scheduler_mod.cron_to_trigger("15 30 6 * * *")

        self.assertEqual(
            five_field.get_next_fire_time(None, now),
            datetime(2026, 1, 1, 6, 30, 0, tzinfo=tz),
        )
        self.assertEqual(
            six_field.get_next_fire_time(None, now),
            datetime(2026, 1, 1, 6, 30, 15, tzinfo=tz),
        )

    def test_virtual_and_real_time_are_inverse_with_offset(self):
        tz = config.get_panel_timezone()
        real = datetime(2026, 7, 3, 12, 0, 0, tzinfo=tz)
        virtual = datetime(2026, 7, 3, 14, 15, 0, tzinfo=tz)

        self.assertEqual(
            scheduler_mod.real_to_virtual_time(real),
            real + timedelta(seconds=3661),
        )
        self.assertEqual(
            scheduler_mod.virtual_to_real_time(virtual),
            virtual - timedelta(seconds=3661),
        )
        self.assertEqual(
            scheduler_mod.virtual_to_real_time(
                scheduler_mod.real_to_virtual_time(real)
            ),
            real,
        )
        self.assertEqual(
            scheduler_mod.real_to_virtual_time(
                scheduler_mod.virtual_to_real_time(virtual)
            ),
            virtual,
        )


def tearDownModule():
    try:
        scheduler_mod.scheduler.shutdown(wait=False)
    except Exception:
        pass
    _BASE_DIR_CTX.cleanup()


if __name__ == "__main__":
    unittest.main()
