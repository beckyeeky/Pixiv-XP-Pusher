import unittest

from utils import format_xp_profile_lines


class XpProfileFormattingTests(unittest.TestCase):
    def test_format_xp_profile_lines_uses_scores_and_relative_intensity(self):
        lines = format_xp_profile_lines(
            [
                ("large_breasts", 7.6),
                ("breasts", 6.5),
                ("thighs", 6.1),
                ("smile", 5.4),
            ],
            "🎯 XP 画像 Top 15",
        )

        self.assertEqual(lines[0], "🎯 XP 画像 Top 15")
        self.assertEqual(lines[1], "🥇 large_breasts · 7.6 分 · 强度 100%")
        self.assertEqual(lines[2], "🥈 breasts · 6.5 分 · 强度 86%")
        self.assertEqual(lines[3], "🥉 thighs · 6.1 分 · 强度 80%")
        self.assertEqual(lines[4], "4. smile · 5.4 分 · 强度 71%")

    def test_format_xp_profile_lines_supports_markdown_escape(self):
        lines = format_xp_profile_lines(
            [("large_breasts[tag]", 5.0)],
            "🎯 *XP 画像 Top 15*",
            markdown=True,
        )

        self.assertEqual(lines[1], "🥇 large\\_breasts\\[tag] · 5.0 分 · 强度 100%")


if __name__ == "__main__":
    unittest.main()
