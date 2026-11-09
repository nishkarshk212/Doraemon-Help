import unittest
from features.ranking import (
    get_level_info,
    render_progress_bar,
    get_leaderboard_keyboard,
    render_ranking_panel_text,
    get_ranking_panel_keyboard,
    get_ranking_handlers,
    check_daily_leaderboard_job
)

class TestRankingFeature(unittest.TestCase):
    def test_level_progression(self):
        # 0 msgs -> Level 1 Novice Chatter
        lvl, title, badge, cur_req, next_req, next_title, next_badge = get_level_info(0)
        self.assertEqual(lvl, 1)
        self.assertEqual(badge, "🌱")
        self.assertEqual(cur_req, 0)
        self.assertEqual(next_req, 25)

        # 25 msgs -> Level 2 Active Chatter
        lvl, title, badge, cur_req, next_req, next_title, next_badge = get_level_info(25)
        self.assertEqual(lvl, 2)
        self.assertEqual(badge, "💬")
        self.assertEqual(next_req, 60)

        # 60 msgs -> Level 3 Bronze Speaker
        lvl, title, badge, cur_req, next_req, next_title, next_badge = get_level_info(60)
        self.assertEqual(lvl, 3)
        self.assertEqual(badge, "🥉")
        self.assertEqual(title, "Bronze Speaker")

        # 1200 msgs -> Level 9 Mythic Voice
        lvl, title, badge, cur_req, next_req, next_title, next_badge = get_level_info(1250)
        self.assertEqual(lvl, 9)
        self.assertEqual(badge, "🔥")
        self.assertEqual(title, "Mythic Voice")

        # 10,000 msgs -> Level 15 God of Chat
        lvl, title, badge, cur_req, next_req, next_title, next_badge = get_level_info(10000)
        self.assertEqual(lvl, 15)
        self.assertEqual(badge, "⚜️")

        # 16,000 msgs -> Level 17 God of Chat Tier 3
        lvl, title, badge, cur_req, next_req, next_title, next_badge = get_level_info(16000)
        self.assertEqual(lvl, 17)
        self.assertEqual(badge, "⚜️")
        self.assertIn("Tier 3", title)

    def test_progress_bar(self):
        bar_0 = render_progress_bar(0, 10)
        self.assertIn("[░░░░░░░░░░] 0%", bar_0)

        bar_50 = render_progress_bar(50, 10)
        self.assertIn("[█████░░░░░] 50%", bar_50)

        bar_100 = render_progress_bar(100, 10)
        self.assertIn("[██████████] 100%", bar_100)

    def test_keyboards_and_handlers(self):
        kb_daily = get_leaderboard_keyboard(-100123456, "daily")
        self.assertIsNotNone(kb_daily)
        self.assertEqual(len(kb_daily.inline_keyboard), 3)

        settings = {"ranking_enabled": True, "daily_leaderboard_auto_post": True, "level_up_alerts": True}
        text = render_ranking_panel_text("Test Chat", settings)
        self.assertIn("Test Chat", text)
        self.assertIn("ON", text)

        panel_kb = get_ranking_panel_keyboard(-100123456, settings)
        self.assertIsNotNone(panel_kb)

        handlers = get_ranking_handlers()
        self.assertGreater(len(handlers), 0)

if __name__ == "__main__":
    unittest.main()
