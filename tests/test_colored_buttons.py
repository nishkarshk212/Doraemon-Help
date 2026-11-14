#!/usr/bin/env python3
import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import (
    colored_button,
    clean_button_url,
    parse_markdown_buttons,
    parse_line_buttons,
    normalize_buttons,
    build_colored_keyboard,
    format_buttons_for_display,
    extract_buttons_from_markup,
    BUTTON_COLOR_MAP
)
from moderation.filter import extract_trigger_and_reply

class TestColoredButtons(unittest.TestCase):
    def test_colored_button_prefixes(self):
        self.assertEqual(colored_button("Hello", "green"), "#g Hello")
        self.assertEqual(colored_button("Danger", "red"), "#r Danger")
        self.assertEqual(colored_button("Primary", "blue"), "#p Primary")
        self.assertEqual(colored_button("Normal", "default"), "Normal")

    def test_clean_button_url(self):
        self.assertEqual(clean_button_url("https://t.me/example"), "https://t.me/example")
        self.assertEqual(clean_button_url("http://example.com"), "http://example.com")
        self.assertEqual(clean_button_url("t.me/mychannel"), "https://t.me/mychannel")
        self.assertEqual(clean_button_url("buttonurl://https://t.me/test"), "https://t.me/test")
        self.assertEqual(clean_button_url("buttonurl:https://t.me/test"), "https://t.me/test")
        self.assertEqual(clean_button_url("tg://resolve?domain=test"), "tg://resolve?domain=test")

    def test_parse_markdown_buttons_pipe(self):
        raw_text = (
            "Welcome to our community!\n"
            "[#g Join Channel | https://t.me/channel]\n"
            "[#r Rules | https://t.me/rules] [#p Support | https://t.me/support :same]\n"
            "[Website | https://google.com | green]"
        )
        clean_text, buttons = parse_markdown_buttons(raw_text)
        self.assertEqual(clean_text, "Welcome to our community!")
        self.assertEqual(len(buttons), 3) # 3 rows
        
        # Row 1
        self.assertEqual(buttons[0][0]['text'], "Join Channel")
        self.assertEqual(buttons[0][0]['color'], "green")
        self.assertEqual(buttons[0][0]['url'], "https://t.me/channel")
        
        # Row 2 (2 buttons on same row)
        self.assertEqual(len(buttons[1]), 2)
        self.assertEqual(buttons[1][0]['text'], "Rules")
        self.assertEqual(buttons[1][0]['color'], "red")
        self.assertEqual(buttons[1][1]['text'], "Support")
        self.assertEqual(buttons[1][1]['color'], "blue")
        
        # Row 3
        self.assertEqual(buttons[2][0]['text'], "Website")
        self.assertEqual(buttons[2][0]['color'], "green")
        self.assertEqual(buttons[2][0]['url'], "https://google.com")

    def test_parse_markdown_buttons_url_syntax(self):
        raw_text = (
            "Hello there!\n"
            "[#g Channel](buttonurl:https://t.me/channel)\n"
            "[#p Group](buttonurl://https://t.me/group:same)"
        )
        clean_text, buttons = parse_markdown_buttons(raw_text)
        self.assertEqual(clean_text, "Hello there!")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(buttons[0][0]['color'], "green")
        self.assertEqual(buttons[0][1]['color'], "blue")

    def test_parse_line_buttons(self):
        text = (
            "#g Channel | https://t.me/channel\n"
            "#r Support | https://t.me/support | same\n"
            "#p Website | https://example.com\n"
        )
        btns = parse_line_buttons(text)
        self.assertEqual(len(btns), 2) # Row 1: 2 buttons, Row 2: 1 button
        self.assertEqual(len(btns[0]), 2)
        self.assertEqual(btns[0][0]['color'], "green")
        self.assertEqual(btns[0][1]['color'], "red")
        self.assertEqual(btns[1][0]['color'], "blue")

    def test_normalize_buttons(self):
        flat = [
            {"label": "Btn 1", "url": "https://a.com", "color": "green"},
            {"text": "Btn 2", "url": "https://b.com", "color": "red"}
        ]
        matrix = normalize_buttons(flat)
        self.assertEqual(len(matrix), 2)
        self.assertEqual(matrix[0][0]['text'], "Btn 1")
        self.assertEqual(matrix[1][0]['text'], "Btn 2")

    def test_format_buttons_for_display(self):
        matrix = [
            [{"text": "Btn 1", "url": "https://a.com", "color": "green"}, {"text": "Btn 2", "url": "https://b.com", "color": "red"}],
            [{"text": "Btn 3", "url": "https://c.com", "color": "blue"}]
        ]
        display = format_buttons_for_display(matrix)
        self.assertIn("🟢 [Btn 1](https://a.com)", display)
        self.assertIn("🔴 [Btn 2](https://b.com)", display)
        self.assertIn("🔵 [Btn 3](https://c.com)", display)
        self.assertIn("Row 1:", display)
        self.assertIn("Row 2:", display)

    def test_filter_trigger_extraction(self):
        # Single word trigger
        trig, rep = extract_trigger_and_reply("/filter hello Welcome! [#g Visit | https://t.me]")
        self.assertEqual(trig, "hello")
        self.assertEqual(rep, "Welcome! [#g Visit | https://t.me]")
        
        # Quoted multi-word trigger
        trig, rep = extract_trigger_and_reply('/filter "my welcome message" Hello all!')
        self.assertEqual(trig, "my welcome message")
        self.assertEqual(rep, "Hello all!")
        
        # Apostrophes in reply (should not crash!)
        trig, rep = extract_trigger_and_reply("/filter help Don't forget it's here [#p Support | https://t.me]")
        self.assertEqual(trig, "help")
        self.assertEqual(rep, "Don't forget it's here [#p Support | https://t.me]")

        # Bot username tag
        trig, rep = extract_trigger_and_reply("/filter@DoraemonHelpBot rules Read rules [#r Rules | https://t.me]")
        self.assertEqual(trig, "rules")
        self.assertEqual(rep, "Read rules [#r Rules | https://t.me]")

if __name__ == '__main__':
    unittest.main()
