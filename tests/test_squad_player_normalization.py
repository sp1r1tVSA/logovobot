"""
tests/test_squad_player_normalization.py

Unit tests verifying:
1. Normalization rules (Unicode, accents, hyphens, Scandinavian chars, Cyrillic).
2. Deduplication and normalized search before creating squad_players rows.
3. Database unique constraint preventing duplicate normalized players within the same club.
4. Retention of canonical squad_players.id across updates (upsert vs delete/recreate).
5. Strict isolation between clubs (same player name allowed across different clubs).
6. Consistency of match_events and matches.mvp_player links with canonical squad players.
"""

import sqlite3
import unittest
import uuid

import database
from club_registry import normalize_team_name, resolve_team_name
from services.player_names import (
    is_same_footballer,
    normalize_footballer_name,
    normalize_player_name_key,
)


class TestPlayerNameNormalization(unittest.TestCase):
    """Test player name normalization algorithm."""

    def test_unicode_and_diacritics_stripping(self):
        self.assertEqual(normalize_player_name_key("Éder Militão"), "eder militao")
        self.assertEqual(normalize_player_name_key("GÜLER"), "guler")
        self.assertEqual(normalize_player_name_key("Niakaté"), "niakate")
        self.assertEqual(normalize_player_name_key("João Pedro"), "joao pedro")
        self.assertEqual(normalize_player_name_key("Mbappé"), "mbappe")
        self.assertEqual(normalize_player_name_key("MBAPPÉ"), "mbappe")
        self.assertEqual(normalize_player_name_key(""), "")
        self.assertEqual(normalize_player_name_key(None), "")

    def test_hyphen_and_whitespace_standardization(self):
        # All variations of dashes/hyphens and spacing collapse identically
        self.assertEqual(normalize_player_name_key("Alexander-Arnold"), "alexander arnold")
        self.assertEqual(normalize_player_name_key("Alexander - Arnold"), "alexander arnold")
        self.assertEqual(normalize_player_name_key("Alexander—Arnold"), "alexander arnold")
        self.assertEqual(normalize_player_name_key("Milinković-Savić"), "milinkovic savic")
        self.assertEqual(normalize_player_name_key("MILINKOVIC-SAVIC"), "milinkovic savic")

    def test_scandinavian_and_special_european_characters(self):
        self.assertEqual(normalize_player_name_key("Grønbæk"), "gronbaek")
        self.assertEqual(normalize_player_name_key("ØDEGAARD"), "odegaard")
        self.assertEqual(normalize_player_name_key("Kæstner"), "kaestner")

    def test_turkish_dotless_and_dotted_i(self):
        # 'ı' has no decomposition, so it needs its own fold to meet the capital 'I'
        self.assertEqual(normalize_player_name_key("Yıldız"), "yildiz")
        self.assertEqual(normalize_player_name_key("YILDIZ"), "yildiz")
        self.assertEqual(normalize_player_name_key("İlkay Gündoğan"), "ilkay gundogan")
        self.assertTrue(is_same_footballer("Kenan Yıldız", "YILDIZ"))

    def test_word_breaks_do_not_split_a_player(self):
        self.assertTrue(is_same_footballer("ALDAWSARI", "Al Dawsari"))
        self.assertTrue(is_same_footballer("De Jong", "DEJONG"))

    def test_cyrillic_transliteration(self):
        self.assertEqual(normalize_player_name_key("Винисиус"), "vinisius")
        self.assertEqual(normalize_player_name_key("Холанд"), "holand")
        self.assertEqual(normalize_player_name_key("Мбаппе"), "mbappe")

    def test_alias_and_similarity_matching(self):
        self.assertTrue(is_same_footballer("Vini Jr", "Vinicius Junior"))
        self.assertTrue(is_same_footballer("VINÍCIUS JÚNIOR", "VINI JR."))
        self.assertTrue(is_same_footballer("Kylian Mbappé", "Mbappé"))
        self.assertTrue(is_same_footballer("Bellingham", "Jude Bellingham"))
        self.assertTrue(is_same_footballer("Alexander-Arnold", "Trent Alexander-Arnold"))

    def test_different_players_not_matched(self):
        self.assertFalse(is_same_footballer("Gabriel Jesus", "Gabriel Martinelli"))
        self.assertFalse(is_same_footballer("Lucas Hernandez", "Theo Hernandez"))
        self.assertFalse(is_same_footballer("Rodrygo Goes", "Rodrigo Silva"))


class TestSquadPlayerDatabaseRules(unittest.TestCase):
    """Test database rules: upsert, deduplication, canonical IDs, club isolation."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.club1 = f"Club Alpha {self.uid}"
        self.club2 = f"Club Beta {self.uid}"

    def tearDown(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM squad_players WHERE team_name IN (?, ?)", (self.club1, self.club2))
            conn.execute("DELETE FROM match_events WHERE team_name IN (?, ?)", (self.club1, self.club2))

    def test_add_squad_deduplicates_same_player_in_same_club(self):
        """Adding different spelling variants of the same player in one club must NOT create multiple rows."""
        # 1. Add first canonical entry
        added = database.add_squad(self.club1, ["Vinicius Jr"])
        self.assertEqual(added, 1)

        # Retrieve canonical player
        p1 = database.find_player_in_squad("Vinicius Jr", self.club1)
        self.assertIsNotNone(p1)
        canon_id = p1["id"]

        # 2. Add uppercase variant
        added2 = database.add_squad(self.club1, ["VINICIUS JR"])
        self.assertEqual(added2, 0)

        # 3. Add accented variant with position
        added3 = database.add_squad(self.club1, [("Vinícius Júnior", "LW")])
        self.assertEqual(added3, 0)

        # Verify club still has exactly 1 player with same canonical id
        squad = database.get_squad(self.club1)
        self.assertEqual(len(squad), 1)

        p_after = database.find_player_in_squad("Vinicius Junior", self.club1)
        self.assertIsNotNone(p_after)
        self.assertEqual(p_after["id"], canon_id)
        # Position should have been enriched
        self.assertEqual(p_after["position"], "LW")

    def test_sqlite_unique_constraint_enforces_one_player_per_club(self):
        """Direct SQL insert violating (norm_team_name, norm_name) must raise IntegrityError."""
        t_norm = normalize_team_name(resolve_team_name(self.club1) or self.club1)
        p_norm = normalize_player_name_key("Éder Militão")

        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO squad_players (team_name, player_name, norm_name, norm_team_name) VALUES (?, ?, ?, ?)",
                (self.club1, "Éder Militão", p_norm, t_norm)
            )

        # Attempting direct duplicate insert must fail at SQLite engine level
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    "INSERT INTO squad_players (team_name, player_name, norm_name, norm_team_name) VALUES (?, ?, ?, ?)",
                    (self.club1, "EDER MILITAO", p_norm, t_norm)
                )

    def test_different_clubs_keep_identical_player_names_separate(self):
        """Players with identical names in DIFFERENT clubs must remain strictly isolated."""
        # Add 'Rodrigo' to club 1
        added1 = database.add_squad(self.club1, ["Rodrigo"])
        self.assertEqual(added1, 1)

        # Add 'Rodrigo' to club 2
        added2 = database.add_squad(self.club2, ["Rodrigo"])
        self.assertEqual(added2, 1)

        p_club1 = database.find_player_in_squad("Rodrigo", self.club1)
        p_club2 = database.find_player_in_squad("Rodrigo", self.club2)

        self.assertIsNotNone(p_club1)
        self.assertIsNotNone(p_club2)
        self.assertNotEqual(p_club1["id"], p_club2["id"])
        self.assertEqual(p_club1["team_name"], self.club1)
        self.assertEqual(p_club2["team_name"], self.club2)

    def test_replace_squad_upsert_preserves_canonical_ids(self):
        """Replacing a squad via upsert must retain existing players' canonical IDs."""
        database.add_squad(self.club1, [
            {"player_name": "Star Player", "position": "ST"},
            {"player_name": "Old Bench", "position": "CM"},
        ])
        initial_star = database.find_player_in_squad("Star Player", self.club1)
        star_id = initial_star["id"]

        # Replace squad: keep 'Star Player' (with new pos 'CF'), drop 'Old Bench', add 'New Signing'
        deleted, added = database.replace_squad(self.club1, [
            {"player_name": "Star Player", "position": "CF"},
            {"player_name": "New Signing", "position": "CB"},
        ])

        self.assertEqual(deleted, 1)  # 'Old Bench' removed
        self.assertEqual(added, 1)    # 'New Signing' added

        # Star Player MUST still have the exact same canonical ID!
        updated_star = database.find_player_in_squad("Star Player", self.club1)
        self.assertEqual(updated_star["id"], star_id)
        self.assertEqual(updated_star["position"], "CF")

        # New Signing was added with a new ID
        new_signing = database.find_player_in_squad("New Signing", self.club1)
        self.assertIsNotNone(new_signing)
        self.assertNotEqual(new_signing["id"], star_id)

    def test_missing_players_detection_and_alignment(self):
        """get_missing_squad_players must not report existing players with spelling variants, and add_missing aligns spellings."""
        database.add_squad(self.club1, ["Milinković-Savić"])

        with database.transaction() as conn:
            # Create match row first to satisfy FOREIGN KEY(match_id) REFERENCES matches(id)
            conn.execute(
                "INSERT INTO matches (id, player1_team, player2_team, status) VALUES (99999, ?, 'Opponent FC', 'confirmed')",
                (self.club1,)
            )
            # Simulate a match event recorded with ASCII spelling without accent
            conn.execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) VALUES (?, ?, ?, 'goal', 1)",
                (99999, self.club1, "Milinkovic-Savic")
            )
            # Simulate another match event for a genuinely unregistered player
            conn.execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) VALUES (?, ?, ?, 'goal', 1)",
                (99999, self.club1, "Unknown Youth")
            )

        try:
            # get_missing_squad_players should ONLY return 'Unknown Youth', NOT 'Milinkovic-Savic'
            missing = database.get_missing_squad_players(self.club1)
            self.assertEqual(missing, ["Unknown Youth"])

            # Running add_missing_squad_players should add only 1 player (Unknown Youth)
            # and align 'Milinkovic-Savic' in match_events to 'Milinković-Savić'
            added = database.add_missing_squad_players(self.club1)
            self.assertEqual(added, 1)

            with database.transaction() as conn:
                event_players = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT player_name FROM match_events WHERE team_name = ?",
                        (self.club1,)
                    ).fetchall()
                ]
                self.assertIn("Milinković-Savić", event_players)
                self.assertNotIn("Milinkovic-Savic", event_players)
        finally:
            with database.transaction() as conn:
                conn.execute("DELETE FROM match_events WHERE match_id = 99999")
                conn.execute("DELETE FROM matches WHERE id = 99999")

    def _insert_match(self, match_id, mvp=None):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO matches (id, player1_team, player2_team, status, mvp_player) "
                "VALUES (?, ?, ?, 'confirmed', ?)",
                (match_id, self.club1, self.club2, mvp)
            )

    def _insert_event(self, match_id, player, count=1, team=None, event_type="goal"):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) "
                "VALUES (?, ?, ?, ?, ?)",
                (match_id, team or self.club1, player, event_type, count)
            )

    def _events(self, match_id):
        with database.transaction() as conn:
            return sorted(
                (r["team_name"], r["player_name"], r["event_type"], r["count"])
                for r in conn.execute(
                    "SELECT team_name, player_name, event_type, count FROM match_events WHERE match_id = ?",
                    (match_id,)
                ).fetchall()
            )

    def _mvp(self, match_id):
        with database.transaction() as conn:
            return conn.execute("SELECT mvp_player FROM matches WHERE id = ?", (match_id,)).fetchone()[0]

    def _drop_match(self, match_id):
        with database.transaction() as conn:
            conn.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
            conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))

    def test_squad_registered_after_match_repoints_events_and_mvp(self):
        """Stats confirmed before the squad existed are re-pointed to the squad spelling."""
        self._insert_match(77701, mvp="Yıldız")
        self._insert_event(77701, "Yıldız")
        self._insert_event(77701, "YILDIZ ")  # second OCR spelling in the same match
        self._insert_event(77701, "Yıldız", event_type="assist")
        self._insert_event(77701, "Mystery Man")
        try:
            database.add_squad(self.club1, ["YILDIZ"])

            self.assertEqual(self._events(77701), [
                (self.club1, "Mystery Man", "goal", 1),
                (self.club1, "YILDIZ", "assist", 1),
                (self.club1, "YILDIZ", "goal", 2),
            ])
            self.assertEqual(self._mvp(77701), "YILDIZ")
        finally:
            self._drop_match(77701)

    def test_ambiguous_surname_is_not_repointed(self):
        """A surname shared by two squad players stays as recorded."""
        self._insert_match(77702)
        self._insert_event(77702, "Hernandez")
        try:
            database.replace_squad(self.club1, ["Lucas Hernandez", "Theo Hernandez"])
            self.assertEqual(self._events(77702), [(self.club1, "Hernandez", "goal", 1)])
        finally:
            self._drop_match(77702)

    def test_mvp_matching_both_sides_is_not_repointed(self):
        """matches.mvp_player has no club: a name both sides answer to stays as recorded."""
        database.add_squad(self.club2, ["Rodrigo"])
        self._insert_match(77703, mvp="RODRIGO")
        try:
            database.add_squad(self.club1, ["Rodrigo"])
            self.assertEqual(self._mvp(77703), "RODRIGO")
        finally:
            self._drop_match(77703)

    def test_set_player_position_insert_repoints_events(self):
        self._insert_match(77704)
        self._insert_event(77704, "Aldawsari")
        try:
            database.set_player_position("Al Dawsari", self.club1, "LW")
            self.assertEqual(self._events(77704), [(self.club1, "Al Dawsari", "goal", 1)])
        finally:
            self._drop_match(77704)

    def test_other_club_events_are_untouched(self):
        self._insert_match(77705)
        self._insert_event(77705, "Yıldız", team=self.club2)
        try:
            database.add_squad(self.club1, ["YILDIZ"])
            self.assertEqual(self._events(77705), [(self.club2, "Yıldız", "goal", 1)])
        finally:
            self._drop_match(77705)

    def test_init_db_recomputes_stale_norm_names_and_merges(self):
        """Rows keyed by an older normalizer are re-keyed on startup and merged."""
        t_norm = normalize_team_name(resolve_team_name(self.club1) or self.club1)
        with database.transaction() as conn:
            # 'yldz' is what the key looked like before 'ı' was folded
            conn.execute(
                "INSERT INTO squad_players (team_name, player_name, position, norm_name, norm_team_name) "
                "VALUES (?, 'YILDIZ', 'LW', 'yildiz', ?)",
                (self.club1, t_norm)
            )
            conn.execute(
                "INSERT INTO squad_players (team_name, player_name, norm_name, norm_team_name) "
                "VALUES (?, 'Yıldız', 'yldz', ?)",
                (self.club1, t_norm)
            )

        database.init_db()

        with database.transaction() as conn:
            rows = conn.execute(
                "SELECT player_name, norm_name, position FROM squad_players WHERE team_name = ?",
                (self.club1,)
            ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("YILDIZ", "yildiz", "LW")])

    def test_rename_player_updates_squad_norm_events_and_mvp(self):
        """rename_player must update player_name, norm_name, match_events, and matches.mvp_player."""
        database.add_squad(self.club1, ["Kylian Mbappe"])

        with database.transaction() as conn:
            # Insert match first
            conn.execute(
                "INSERT INTO matches (id, season_id, division_id, player1_team, player2_team, status, mvp_player) "
                "VALUES (88888, 1, 1, ?, 'Opponent FC', 'confirmed', 'Kylian Mbappe')",
                (self.club1,)
            )
            # Then insert match_events referencing match
            conn.execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) VALUES (?, ?, ?, 'goal', 1)",
                (88888, self.club1, "Kylian Mbappe")
            )

        try:
            squad_cnt, event_cnt = database.rename_player("Kylian Mbappe", "Kylian Mbappé", self.club1)
            self.assertEqual(squad_cnt, 1)
            self.assertEqual(event_cnt, 1)

            # Check squad player norm_name is updated
            p = database.find_player_in_squad("Kylian Mbappé", self.club1)
            self.assertEqual(p["player_name"], "Kylian Mbappé")
            self.assertEqual(p["norm_name"], "kylian mbappe")

            # Check matches.mvp_player is updated
            with database.transaction() as conn:
                mvp = conn.execute("SELECT mvp_player FROM matches WHERE id = 88888").fetchone()[0]
                self.assertEqual(mvp, "Kylian Mbappé")
        finally:
            with database.transaction() as conn:
                conn.execute("DELETE FROM match_events WHERE match_id = 88888")
                conn.execute("DELETE FROM matches WHERE id = 88888")


if __name__ == "__main__":
    unittest.main()
