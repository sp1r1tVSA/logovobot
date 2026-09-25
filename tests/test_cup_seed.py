"""
tests/test_cup_seed.py

Сид сетки кубка: присланные пары против ростера лиги.

Владелец присылает пары живым текстом («МЮ», «Ман Сити», «Реад МАдрид»), а
резолвер OCR, линия и `users.team_name` работают с каноном из
`config.DIVISION_CLUBS`. Значит сид обязан проверить всё до первой записи и
выдать все находки сразу: сетка, где один клуб стоит в двух парах, даёт стадию,
в которой клуб проходит и выбывает одновременно.

Отдельная группа тестов — dry-run: скрипт запускается на боевой базе, и «по
умолчанию ничего не писать» в нём не вежливость, а защита от автопилота.
"""

import contextlib
import os
import tempfile
import unittest

import config
import database
import scripts.seed_cup_bracket as seeder


class CupSeedValidationTest(unittest.TestCase):
    @contextlib.contextmanager
    def _temporary_pairs(self, stage, pairs):
        """Подменить список стадии и вернуть как было — даже если тест упал."""
        had = stage in seeder.PAIRS
        original = seeder.PAIRS.get(stage)
        seeder.PAIRS[stage] = pairs
        try:
            yield
        finally:
            if had:
                seeder.PAIRS[stage] = original
            else:
                seeder.PAIRS.pop(stage, None)

    def test_01_real_grid_normalizes_to_sixteen_canonical_pairs(self):
        pairs = seeder.validate_pairs("1/64")
        self.assertEqual(len(pairs), 16)
        flat = [club for pair in pairs for club in pair]
        self.assertEqual(len(set(c.lower() for c in flat)), 32)

        expected = {
            "Ман Сити": "Манчестер Сити",
            "МЮ": "Манчестер Юнайтед",
            "Реад МАдрид": "Реал Мадрид",
            "Интер Майми": "Интер Майами",
        }
        raw_flat = [club for pair in seeder.PAIRS["1/64"] for club in pair]
        for raw, canon in expected.items():
            self.assertIn(raw, raw_flat, f"исходный список должен остаться как его прислали: {raw}")
            self.assertIn(canon, flat, f"«{raw}» обязан схлопнуться в «{canon}»")
        self.assertNotIn("Ман Сити", flat)

    def test_02_every_club_belongs_to_the_low_divisions(self):
        divisions = seeder._canonical_divisions()
        for pair in seeder.validate_pairs("1/64"):
            for club in pair:
                self.assertIn(divisions[club.lower()], {"DIV_4", "DIV_5"},
                              f"{club} не должен попасть в 1/64")

    def test_03_duplicate_club_is_reported(self):
        with self._temporary_pairs("1/32", [("Байер", "Милан"), ("Милан", "Рома")]):
            with self.assertRaises(ValueError) as ctx:
                seeder.validate_pairs("1/32")
            self.assertIn("дважды", str(ctx.exception))

    def test_04_wrong_division_is_reported(self):
        original = seeder.PAIRS["1/64"]
        try:
            # Клуб заведомо не из Д4/Д5: проверка стадии обязана его отклонить.
            top_club = next(c for code, clubs in config.DIVISION_CLUBS.items()
                            if code == "DIV_1" for c in clubs)
            seeder.PAIRS["1/64"] = [(top_club, "Байер")] + original[1:]
            with self.assertRaises(ValueError) as ctx:
                seeder.validate_pairs("1/64")
            self.assertIn(top_club, str(ctx.exception))
            self.assertIn("допущены", str(ctx.exception))
        finally:
            seeder.PAIRS["1/64"] = original

    def test_05_club_outside_the_registry_is_reported(self):
        # Резолвер отдаёт неизвестное имя самим собой (это безопасно), поэтому
        # сигналом служит отсутствие клуба в `config.DIVISION_CLUBS`.
        with self._temporary_pairs("1/16", [("Несуществующий Клуб", "Байер")]):
            with self.assertRaises(ValueError) as ctx:
                seeder.validate_pairs("1/16")
            self.assertIn("нет в config.DIVISION_CLUBS", str(ctx.exception))

    def test_06_problems_are_reported_all_at_once(self):
        with self._temporary_pairs("1/8", [("Такого Клуба Нет", "Байер"), ("Милан", "Милан")]):
            with self.assertRaises(ValueError) as ctx:
                seeder.validate_pairs("1/8")
            message = str(ctx.exception)
        self.assertIn("Такого Клуба Нет", message)
        self.assertIn("дважды", message)
        self.assertIn("- ", message)

    def test_07_stage_without_pairs_is_refused(self):
        with self.assertRaises(ValueError):
            seeder.validate_pairs("final")


class CupSeedWriteTest(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    def _main(self, argv):
        import sys
        old = sys.argv
        sys.argv = ["seed_cup_bracket.py"] + argv
        try:
            return seeder.main()
        finally:
            sys.argv = old

    def test_08_dry_run_writes_nothing(self):
        self.assertEqual(self._main(["--stage", "1/64"]), 0)
        self.assertEqual(database.get_cup_bracket("1/64"), [])
        self.assertIsNone(database.get_cup_stage("1/64"))

    def test_09_apply_writes_the_grid_once(self):
        self.assertEqual(self._main(["--stage", "1/64", "--apply"]), 0)
        bracket = database.get_cup_bracket("1/64")
        self.assertEqual(len(bracket), 16)
        self.assertEqual(bracket[0]["team1_name"], "Интер Милан")
        self.assertEqual(bracket[1]["team2_name"], "Манчестер Сити")

        # Повторный сид не удваивает сетку и не ругается кодом ошибки.
        self.assertEqual(self._main(["--stage", "1/64", "--apply"]), 0)
        self.assertEqual(len(database.get_cup_bracket("1/64")), 16)

    def test_10_provision_after_seed_creates_games_and_headers(self):
        self._main(["--stage", "1/64", "--apply"])
        report = database.provision_cup_stage_line("1/64")
        self.assertEqual(report["series"], 16)
        self.assertEqual(report["created_games"], 48)
        self.assertEqual(report["created_headers"], 16)

    # --- 1/32: победители 1/64 + Д1–Д3, ссылка на серию ---------------------

    def _decide_64(self, overrides=None):
        """Решить 1/64 так, как её сыграли: победители — клубы из сетки 1/32."""
        overrides = overrides or {}
        winners = {"Интер Милан", "Манчестер Сити", "Ювентус", "Атлетико Мадрид", "Бетис",
                   "Брайтон", "Галатасарай", "Барселона", "Эвертон", "Аль-Хиляль", "ПСЖ",
                   "Арсенал", "Бавария", "Наполи", "Бешикташ", "Лейпциг"}
        with database.transaction() as conn:
            for s in database.get_cup_bracket("1/64"):
                winner = overrides.get(s["series_num"])
                if winner is None:
                    winner = s["team1_name"] if s["team1_name"] in winners else s["team2_name"]
                if winner is False:
                    continue
                conn.execute(
                    "UPDATE cup_series SET winner_name = ?, status = 'finished' WHERE id = ?",
                    (winner, s["id"]),
                )

    def test_11_round_of_32_takes_the_winner_of_the_open_slot(self):
        self._main(["--stage", "1/64", "--apply"])
        self._decide_64()
        pairs = seeder.validate_pairs("1/32")
        self.assertEqual(len(pairs), 32)
        self.assertEqual(len({c.lower() for p in pairs for c in p}), 64)
        self.assertEqual(pairs[-1], ("Борнмут", "Лейпциг"))

        self.assertEqual(self._main(["--stage", "1/32", "--apply"]), 0)
        bracket = database.get_cup_bracket("1/32")
        self.assertEqual(len(bracket), 32)
        self.assertEqual(bracket[31]["team2_name"], "Лейпциг")

    def test_12_undecided_series_blocks_the_round_of_32(self):
        self._main(["--stage", "1/64", "--apply"])
        self._decide_64({4: False})
        with self.assertRaises(ValueError) as ctx:
            seeder.validate_pairs("1/32")
        message = str(ctx.exception)
        self.assertIn("Милан — Лейпциг", message)
        self.assertIn("не решена", message)
        self.assertEqual(self._main(["--stage", "1/32", "--apply"]), 1)
        self.assertEqual(database.get_cup_bracket("1/32"), [])

    def test_13_eliminated_club_is_refused_in_the_round_of_32(self):
        self._main(["--stage", "1/64", "--apply"])
        self._decide_64({15: "Ньюкасл"})
        with self.assertRaises(ValueError) as ctx:
            seeder.validate_pairs("1/32")
        message = str(ctx.exception)
        self.assertIn("«Наполи» выбыл", message)
        self.assertIn("не хватает: Ньюкасл", message)


if __name__ == "__main__":
    unittest.main()
