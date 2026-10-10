import unittest
import database
from services.player_positions import (
    normalize_position,
    detect_player_position,
    known_position,
    KNOWN_PLAYER_POSITIONS,
    VALID_POSITIONS
)
from services.graphics import fc_card_generator


class TestPlayerPositions(unittest.TestCase):
    def setUp(self):
        database.init_db()

    def test_normalize_position(self):
        # Russian aliases
        self.assertEqual(normalize_position("ВРТ"), "GK")
        self.assertEqual(normalize_position("вратарь"), "GK")
        self.assertEqual(normalize_position("ЦЗ"), "CB")
        self.assertEqual(normalize_position("центральный защитник"), "CB")
        self.assertEqual(normalize_position("ЛЗ"), "LB")
        self.assertEqual(normalize_position("ПЗ"), "RB")
        self.assertEqual(normalize_position("ЦОП"), "CDM")
        self.assertEqual(normalize_position("опорник"), "CDM")
        self.assertEqual(normalize_position("ЦП"), "CM")
        self.assertEqual(normalize_position("ЦАП"), "CAM")
        self.assertEqual(normalize_position("плеймейкер"), "CAM")
        self.assertEqual(normalize_position("ЛВ"), "LW")
        self.assertEqual(normalize_position("левый вингер"), "LW")
        self.assertEqual(normalize_position("ПВ"), "RW")
        self.assertEqual(normalize_position("НАП"), "ST")
        self.assertEqual(normalize_position("форвард"), "ST")

        # English aliases
        self.assertEqual(normalize_position("Goalkeeper"), "GK")
        self.assertEqual(normalize_position("Striker"), "ST")
        self.assertEqual(normalize_position("Left Wing"), "LW")
        self.assertEqual(normalize_position("Right Wing"), "RW")
        self.assertEqual(normalize_position("Attacking Midfield"), "CAM")
        self.assertEqual(normalize_position("Centre-Back"), "CB")

        # Invalid fallback
        self.assertEqual(normalize_position("unknown_xyz"), "ST")
        self.assertEqual(normalize_position(None), "ST")

    def test_detect_known_players(self):
        self.assertEqual(detect_player_position("Vinicius Jr"), "LW")
        self.assertEqual(detect_player_position("Винисиус"), "LW")
        self.assertEqual(detect_player_position("Viktor Gyökeres"), "ST")
        self.assertEqual(detect_player_position("Виктор Дьёкереш"), "ST")
        self.assertEqual(detect_player_position("Thibaut Courtois"), "GK")
        self.assertEqual(detect_player_position("Тибо Куртуа"), "GK")
        self.assertEqual(detect_player_position("Virgil van Dijk"), "CB")
        self.assertEqual(detect_player_position("Jude Bellingham"), "CAM")
        self.assertEqual(detect_player_position("Rodri"), "CDM")
        self.assertEqual(detect_player_position("Roony Bardghji"), "RW")
        self.assertEqual(detect_player_position("Amoura"), "ST")
        self.assertEqual(detect_player_position("Mohamed Amoura"), "ST")
        self.assertEqual(detect_player_position("Francisco Moura"), "LB")
        self.assertEqual(known_position("Amoura"), "ST")
        self.assertEqual(known_position("Moura"), "LB")

    def test_database_squad_positions(self):
        club = "Тестовый Клуб Позиций"
        database.clear_squad(club)

        # 1. Add squad with automatic position resolution
        players = ["Vinicius Jr", "Thibaut Courtois", "Virgil van Dijk", "Неизвестный Игрок"]
        database.save_squad_players(club, players)

        squad = database.get_squad_with_positions(club)
        self.assertEqual(len(squad), 4)

        # Check detected positions
        pos_map = {p["player_name"]: p["position"] for p in squad}
        self.assertEqual(pos_map["Vinicius Jr"], "LW")
        self.assertEqual(pos_map["Thibaut Courtois"], "GK")
        self.assertEqual(pos_map["Virgil van Dijk"], "CB")
        self.assertEqual(pos_map["Неизвестный Игрок"], "ST")

        # 2. Update position explicitly
        database.set_player_position("Неизвестный Игрок", club, "ЦОП")
        updated_pos = database.get_player_position("Неизвестный Игрок", club)
        self.assertEqual(updated_pos, "CDM")

        # Cleanup
        database.clear_squad(club)

    def test_fc_card_generator_position_ovr_weights(self):
        # Defender OVR should prioritize DEF and PHY
        defender_data = {
            "player_name": "Virgil van Dijk",
            "position": "CB",
            "matches_played": 10,
            "total_goals": 1,
            "total_assists": 0
        }
        def_stats = fc_card_generator.calculate_fut_attributes(defender_data)
        self.assertEqual(def_stats["position"], "CB")
        self.assertGreaterEqual(def_stats["def"], 80)
        self.assertGreaterEqual(def_stats["phy"], 80)

        # Attacker OVR should prioritize SHO and PAC
        attacker_data = {
            "player_name": "Erling Haaland",
            "position": "ST",
            "matches_played": 10,
            "total_goals": 15,
            "total_assists": 2
        }
        att_stats = fc_card_generator.calculate_fut_attributes(attacker_data)
        self.assertEqual(att_stats["position"], "ST")
        self.assertGreaterEqual(att_stats["sho"], 85)
        self.assertGreaterEqual(att_stats["ovr"], 88)


if __name__ == "__main__":
    unittest.main()
