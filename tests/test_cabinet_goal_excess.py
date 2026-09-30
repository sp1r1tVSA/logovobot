"""
tests/test_cabinet_goal_excess.py

Карточка подтверждения ИИ-результата. Голов меньше счёта — это прокрутка
таблицы, и карточка лишь предупреждает. Голов больше счёта не бывает: скрытые
строки только добавляют голы, значит ИИ перепутал колонки «Г»/«А» (МЮ 1:1
Барселона записали как два гола Барселоны). Такое сохранять нельзя.
"""

import unittest

from handlers import cabinet


class GoalExcessLineTest(unittest.TestCase):
    def test_more_goals_than_the_score_is_an_error(self):
        line = cabinet._goal_excess_line((
            ("Манчестер Юнайтед", {"Tielemans": 1}, 1),
            ("Барселона", {"Gordon": 1, "Bardghji": 1}, 1),
        ))
        self.assertIn("⛔", line)
        self.assertIn("Барселона — 2 при счёте 1", line)
        self.assertNotIn("Манчестер Юнайтед", line)

    def test_exact_or_short_goal_lists_are_not_an_excess(self):
        self.assertEqual(cabinet._goal_excess_line((
            ("Манчестер Юнайтед", {"Tielemans": 1}, 1),
            ("Барселона", {}, 1),
        )), "")

    def test_team_name_is_escaped(self):
        line = cabinet._goal_excess_line((("A<b>", {"X": 2}, 0),))
        self.assertIn("A&lt;b&gt;", line)


if __name__ == "__main__":
    unittest.main()
