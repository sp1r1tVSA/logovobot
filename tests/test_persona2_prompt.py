"""
tests/test_persona2_prompt.py

The «Булли» (persona2) system prompt in `services/ai/ai_chat.py`.

Its archive used to be keyed by club — «ПСВ (@MatveyN) — аутсайдер» — but clubs
were handed out anew with the divisions, so a club's old record would land on its
new owner. The archive is now keyed by coach, participant styles are limited to
the people in the conversation, and one-word chat samples are filtered out.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ai import persona_base
from services.ai.ai_chat import _build_persona2_instruction

CONTEXT = (
    "Пользователь, который с тобой говорит: vtrrgyg (тренер команды 'ПСВ').\n"
    "1. ПСВ (@vtrrgyg) О12, 5-4-0-1, 14:5, WWLWW\n"
)


def _prompt(user_text="", samples=()):
    with patch("services.ai.ai_chat.database.get_style_samples", return_value=list(samples)):
        return _build_persona2_instruction(CONTEXT, user_text)["parts"][0]["text"]


class TestCoachArchive(unittest.TestCase):
    def test_every_archive_line_names_a_coach(self):
        for line in persona_base.COACH_ARCHIVE_TEXT.splitlines()[1:]:
            self.assertIn("@", line, line)

    def test_prompt_warns_that_clubs_changed_hands(self):
        text = _prompt()
        self.assertIn("БЫЛЫЕ ЗАСЛУГИ ТРЕНЕРОВ", text)
        self.assertIn("у ДРУГИХ тренеров", text)
        self.assertNotIn("АРХИВ СИЛЫ КЛУБОВ", text)


class TestParticipantStyles(unittest.TestCase):
    def test_only_people_in_the_conversation_are_described(self):
        styles = persona_base.get_participant_styles_text("а ты что скажешь, @LachesisQQQ?", CONTEXT)

        self.assertIn("@LachesisQQQ", styles)
        self.assertIn("@vtrrgyg", styles)  # the speaker, named without @ in the context
        self.assertNotIn("@crcsss", styles)

    def test_handles_match_as_whole_words(self):
        self.assertEqual(persona_base.get_participant_styles_text("epl_lover и sp1r1tVSAx"), "")

    def test_the_list_is_capped(self):
        everyone = " ".join(persona_base.PARTICIPANT_STYLES)
        lines = persona_base.get_participant_styles_text(everyone).splitlines()
        self.assertEqual(len(lines), persona_base.MAX_STYLES_IN_PROMPT)

    def test_prompt_carries_the_selected_styles(self):
        text = _prompt("@Snikers2121 опять ноет")
        self.assertIn("@Snikers2121 —", text)
        self.assertNotIn("@Belka809 —", text)


class TestStyleSamples(unittest.TestCase):
    def test_one_word_noise_is_dropped_and_the_rest_capped(self):
        samples = ["А", "Го", "…", "Проклятье 1/8 кубка"] + [f"длинная фраза номер {i}" for i in range(20)]
        text = _prompt(samples=samples)

        self.assertIn("- Проклятье 1/8 кубка", text)
        self.assertNotIn("\n- А\n", text)
        self.assertNotIn("\n- Го\n", text)
        self.assertNotIn("номер 9\n", text)  # only the freshest ten make it


if __name__ == "__main__":
    unittest.main()
