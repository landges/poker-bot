import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

import recover


def message(i, text, sender="user1", reply=None):
    return {"id": i, "type": "message", "date": f"2026-01-01T00:{i // 60:02}:{i % 60:02}",
            "text": text, "from_id": sender, "reply_to_message_id": reply}


def chat(*messages):
    return {"id": 1234567890, "type": "private_supergroup", "name": "Test", "messages": list(messages)}


class RecoveryTests(unittest.TestCase):
    def test_formatted_text_unicode_minus_missing_space_and_note(self):
        m = message(1, [{"type": "bold", "text": "Результаты 01.01.2026:"},
                        "\nА−120\n", {"type": "mention", "text": "@user"},
                        " +120\n\nП.С. Важная заметка"])
        report = recover.parse_report(m)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["rows"], [{"name": "А", "amount": -120}, {"name": "@user", "amount": 120}])
        self.assertEqual(report["notes"], ["П.С. Важная заметка"])

    def test_invalid_row_blocks_entire_database(self):
        report = recover.analyze(chat(message(1, "Результаты 01.01.2026:\nА +100\nБ -100oops")), {})
        self.assertTrue(report["errors"])
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "restored.db"
            with self.assertRaises(ValueError):
                recover.create_database(report, target)
            self.assertFalse(target.exists())

    def test_invalid_date_is_reported(self):
        report = recover.analyze(chat(message(1, "Результаты 31.02.2026:\nА +10")), {})
        self.assertTrue(any("дата" in e for e in report["errors"]))

    def test_conversation_stats_and_date_photo_are_not_games(self):
        photo = message(3, "Итоги 01.01.2026")
        photo["photo"] = "photos/1.jpg"
        report = recover.analyze(chat(message(1, "Результаты 01.01.2026 в студию"),
                                     message(2, "Статистика игроков:\nА +10 (1 игр)"), photo), {})
        self.assertEqual(report["sessions"], [])
        self.assertEqual(len(report["photos"]), 1)

    def test_cancellation_replacement_and_untrusted_cancellation(self):
        data = chat(
            message(1, "Результаты 01.01.2026:\nА +100\nБ -100"),
            message(2, "Результаты сохранены ✅", "bot", 1),
            message(3, "/undo_last"),
            message(4, "Результаты за 01.01.2026 удалены 🗑", "bot", 3),
            message(5, "Результаты 01.01.2026:\nА +200\nБ -200"),
            message(6, "Результаты за 01.01.2026 удалены 🗑", "other"),
            message(7, "/undo_last"))
        report = recover.analyze(data, {})
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["sessions"][0]["message_id"], 5)
        self.assertEqual(report["all_text_reports"][0]["status"], "cancelled")
        self.assertEqual(len(report["cancellations"]), 1)

    def test_conflicting_same_date_requires_decision(self):
        data = chat(message(1, "Результаты 01.01.2026:\nА +10"),
                    message(2, "Результаты 01.01.2026:\nА +20"))
        self.assertTrue(recover.analyze(data, {})["errors"])
        result = recover.analyze(data, {"select_by_date": {"2026-01-01": 2}})
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["sessions"][0]["rows"][0]["amount"], 20)

    def test_exact_reposts_are_deduplicated(self):
        text = "Результаты 01.01.2026:\nА +10\nБ -10"
        report = recover.analyze(chat(message(1, text), message(2, text)), {})
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["summary"]["sessions"], 1)
        self.assertEqual(report["summary"]["results"], 2)

    def test_alias_merging_duplicate_player_is_blocked(self):
        data = chat(message(1, "Результаты 01.01.2026:\n@User +10\n@USER -10"))
        self.assertTrue(recover.analyze(data, {})["errors"])
        with self.assertRaises(ValueError):
            recover.canonical("А", {"А": "Б", "Б": "А"})

    def test_snapshot_verifies_both_amounts_and_counts(self):
        data = chat(message(1, "Результаты 01.01.2026:\nСтарое имя +10\nБ -10"),
                    message(2, "Статистика игроков:\nНовое имя +10 (1 игр)\nБ -10 (1 игр)", "bot"))
        config = {"aliases": {"Старое имя": "Новое имя"}, "validate_snapshots": [2]}
        self.assertEqual(recover.analyze(data, config)["errors"], [])
        data["messages"][1]["text"] = "Статистика игроков:\nНовое имя +10 (2 игр)\nБ -10 (1 игр)"
        self.assertTrue(recover.analyze(data, config)["errors"])

    def test_migrated_message_order_uses_date(self):
        original = message(1, "Результаты 01.01.2026:\nА +10")
        original["id"] = -9999
        report = recover.analyze(chat(original, message(2, "Результаты за 01.01.2026 удалены 🗑", "bot")),
                                 {"bot_sender_id": "bot"})
        self.assertEqual(report["sessions"], [])

    def test_supplement_requires_provenance_and_valid_rows(self):
        data = chat(message(1, "Результаты сохранены ✅", "bot", 99))
        extra = {"date": "2026-01-01", "recorded_at_message_id": 1,
                 "rows": [{"name": "А", "amount": 0}], "sources": [{"kind": "stats_delta"}]}
        config = {"supplemental_sessions": [extra]}
        self.assertEqual(recover.analyze(data, config)["summary"]["sessions"], 1)
        extra["rows"][0]["amount"] = "0"
        self.assertTrue(recover.analyze(data, config)["errors"])

    def test_existing_database_never_overwritten_and_schema_is_compatible(self):
        report = recover.analyze(chat(message(1, "Результаты 01.01.2026:\n@user +100\nИмя -100")), {})
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "restored.db"
            recover.create_database(report, target)
            original = target.read_bytes()
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db.execute("SELECT tg_id FROM groups").fetchone()[0], -1001234567890)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM player_results").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM group_players").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT SUM(amount) FROM player_results").fetchone()[0], 0)
            with self.assertRaises(FileExistsError):
                recover.create_database(report, target)
            self.assertEqual(original, target.read_bytes())
            self.assertEqual(list(Path(temp).glob(".recover-*")), [])

    def test_full_export_chat_selection_and_duplicate_message_guard(self):
        first = chat(message(1, "Обычное сообщение"))
        second = dict(first, id=42, type="private_group")
        with tempfile.TemporaryDirectory() as temp:
            export = Path(temp) / "result.json"
            export.write_text(json.dumps({"chats": {"list": [first, second]}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                recover.load_chat(export)
            self.assertEqual(recover.load_chat(export, -42)["id"], 42)
            first["messages"].append(first["messages"][0])
            export.write_text(json.dumps(first), encoding="utf-8")
            with self.assertRaises(ValueError):
                recover.load_chat(export)

    def test_ignored_autumn_report_not_imported(self):
        data = chat(message(1, "Результаты 01.01.2026:\nА +10"),
                    message(2, "9.10.2026\nА +20"))
        result = recover.analyze(data, {"ignore_message_ids": [2]})
        self.assertEqual([g["date"] for g in result["sessions"]], ["2026-01-01"])
        self.assertEqual(result["all_text_reports"][1]["status"], "ignored")


if __name__ == "__main__":
    unittest.main()
