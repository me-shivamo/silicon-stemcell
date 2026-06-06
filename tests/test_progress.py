import unittest

from core.progress import EXECUTING, progress_display_line, progress_event


class ProgressDisplayTest(unittest.TestCase):
    def test_executing_command_does_not_expose_command_or_output_while_running_or_successful(self):
        for status in ("started", "output", "completed"):
            with self.subTest(status=status):
                line = progress_display_line(
                    progress_event(
                        "codex",
                        EXECUTING,
                        status=status,
                        command="python manage.py migrate --database prod",
                        preview="/Users/codanium/Documents/silicon/private/path.py",
                        exit_code=0,
                    )
                )
                self.assertEqual(line, "executing command")

    def test_failed_executing_command_includes_failure_output_without_command(self):
        line = progress_display_line(
            progress_event(
                "codex",
                EXECUTING,
                status="completed",
                command="python manage.py migrate --database prod",
                preview="Traceback: target id not found",
                exit_code=1,
            )
        )

        self.assertEqual(line, "executing command failed: Traceback: target id not found")
        self.assertNotIn("manage.py", line)


if __name__ == "__main__":
    unittest.main()
