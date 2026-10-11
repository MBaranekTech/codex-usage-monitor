import unittest

from codex_statusline import PRESETS, uninstall_config, update_config


class ConfigEditingTests(unittest.TestCase):
    def test_adds_tui_section(self):
        actual = update_config('model = "gpt-test"\n', PRESETS["compact"], True)
        self.assertIn("[tui]", actual)
        self.assertIn('status_line = ["model-with-reasoning"', actual)
        self.assertIn("status_line_use_colors = true", actual)

    def test_preserves_existing_tui_keys_and_other_sections(self):
        source = "[tui]\nanimations = false\n\n[mcp_servers.demo]\ncommand = \"demo\"\n"
        actual = update_config(source, PRESETS["git"], False)
        self.assertIn("animations = false", actual)
        self.assertIn("[mcp_servers.demo]", actual)
        self.assertIn("status_line_use_colors = false", actual)

    def test_replaces_multiline_array_without_leaving_fragments(self):
        source = '[tui]\nstatus_line = [\n  "model",\n  "current-dir",\n]\nanimations = true\n'
        actual = update_config(source, PRESETS["compact"], True)
        self.assertNotIn('  "current-dir",', actual)
        self.assertEqual(actual.count("status_line ="), 1)
        self.assertIn("animations = true", actual)

    def test_uninstall_only_removes_managed_keys(self):
        source = update_config("[tui]\nanimations = false\n", PRESETS["full"], True)
        actual = uninstall_config(source)
        self.assertNotIn("status_line", actual)
        self.assertIn("animations = false", actual)


if __name__ == "__main__":
    unittest.main()
