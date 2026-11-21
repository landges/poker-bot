import unittest
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app import db as database
from app.models import Base, GameSession, Group, GroupPlayer, Player, PlayerResult
from app.reports import ReportEntry, ReportError, parse_report, save_report, undo_last_game
from app.utils import get_global_player_stats


class ReportParsingTests(unittest.TestCase):
    def test_integer_results_and_single_letter_name(self):
        game_date, entries = parse_report("Результаты 21.11.2026:\nЯ +100\n\n@bob -100\nНоль 0")
        self.assertEqual(game_date, date(2026, 11, 21))
        self.assertEqual(entries, [ReportEntry("Я", 100), ReportEntry("@bob", -100), ReportEntry("Ноль", 0)])

    def test_rejects_invalid_reports(self):
        for report in (
            "", "Результаты 31.02.2026:\n@alice 100", "Результаты 21.11.2026:",
            "Результаты 21.11.2026:\n@alice +100.50",
            "Результаты 21.11.2026:\n@alice 100abc",
            "Результаты 21.11.2026:\n@alice 100\n@bob минус100",
            "Результаты 21.11.2026:\n@alice 99999999999999999999999999",
        ):
            with self.subTest(report=report), self.assertRaises(ReportError):
                parse_report(report)

    def test_rejects_duplicate_names_including_case_and_whitespace(self):
        for first, second in (("@alice", "@Alice"), ("Иван Петров", "иван   петров")):
            with self.subTest(name=first), self.assertRaisesRegex(ReportError, "несколько раз"):
                parse_report(f"Результаты 21.11.2026:\n{first} 100\n{second} -100")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)

    def save(self, group=-1001, game_date=date(2026, 11, 21), entries=None):
        if entries is None:
            entries = [ReportEntry("@alice", 100), ReportEntry("@bob", -100)]
        with self.sessions.begin() as db:
            save_report(db, group, "Group", game_date, entries)

    def counts(self):
        with self.sessions() as db:
            return [db.query(model).count() for model in (Group, Player, GroupPlayer, GameSession, PlayerResult)]

    def test_report_is_committed_in_full(self):
        self.save()
        self.assertEqual(self.counts(), [1, 2, 2, 1, 2])

    def test_failure_rolls_back_all_rows_and_allows_retry(self):
        inserted = [0]

        def fail_second_result(conn, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO player_results"):
                inserted[0] += 1
                if inserted[0] == 2:
                    raise SQLAlchemyError("Injected failure after the first result")

        event.listen(self.engine, "before_cursor_execute", fail_second_result)
        try:
            with self.assertRaises(SQLAlchemyError):
                self.save()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_second_result)
        self.assertEqual(inserted[0], 2)
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        self.save()
        self.assertEqual(self.counts(), [1, 2, 2, 1, 2])

    def test_failed_duplicate_player_leaves_previous_game_intact(self):
        self.save()
        before = self.counts()
        with self.assertRaises(ReportError):
            self.save(game_date=date(2026, 11, 22), entries=[ReportEntry("@Alice", 10), ReportEntry("@alice", 20)])
        self.assertEqual(self.counts(), before)

    def test_existing_game_is_not_overwritten(self):
        self.save()
        with self.assertRaisesRegex(ReportError, "уже сохранена"):
            self.save()
        self.assertEqual(self.counts(), [1, 2, 2, 1, 2])

    def test_username_case_reuses_player_across_games(self):
        self.save()
        self.save(game_date=date(2026, 11, 22), entries=[ReportEntry("@ALICE", 50)])
        with self.sessions() as db:
            self.assertEqual(db.query(Player).count(), 2)
            self.assertIn("@alice +150 (2 игр)", get_global_player_stats(db, -1001))

    def test_database_rejects_duplicate_player_result(self):
        self.save()
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as db:
                result = db.query(PlayerResult).first()
                db.add(PlayerResult(session_id=result.session_id, player_id=result.player_id, amount=50))
        self.assertEqual(self.counts(), [1, 2, 2, 1, 2])

    def test_init_adds_index_to_existing_database_and_is_repeatable(self):
        index = next(iter(PlayerResult.__table__.indexes))
        index.drop(self.engine)
        self.save()
        with patch.object(database, "engine", self.engine):
            database.init_db()
            database.init_db()
        self.assertIn(index.name, [item["name"] for item in inspect(self.engine).get_indexes("player_results")])
        self.assertEqual(self.counts(), [1, 2, 2, 1, 2])

    def test_legacy_duplicates_are_reported_without_changing_results(self):
        next(iter(PlayerResult.__table__.indexes)).drop(self.engine)
        self.save()
        with self.sessions.begin() as db:
            result = db.query(PlayerResult).first()
            db.add(PlayerResult(session_id=result.session_id, player_id=result.player_id, amount=50))
        with patch.object(database, "engine", self.engine):
            with self.assertRaisesRegex(RuntimeError, "повторы"):
                database.init_db()
        self.assertEqual(self.counts(), [1, 2, 2, 1, 3])

    def test_undo_preserves_older_games_and_other_groups(self):
        self.save(game_date=date(2026, 11, 20), entries=[ReportEntry("@alice", 10)])
        self.save()
        self.save(group=-1002)
        with self.sessions.begin() as db:
            removed_date, names = undo_last_game(db, -1001)
        self.assertEqual(removed_date, date(2026, 11, 21))
        self.assertEqual(names, ["@bob"])
        self.assertEqual(self.counts(), [2, 2, 3, 2, 3])
        with self.sessions() as db:
            self.assertEqual(get_global_player_stats(db, -1001), "@alice +10 (1 игр)")
            self.assertIn("@bob -100 (1 игр)", get_global_player_stats(db, -1002))

    def test_undo_missing_group_or_empty_group(self):
        with self.assertRaisesRegex(ReportError, "не зарегистрирована"):
            with self.sessions.begin() as db:
                undo_last_game(db, -1001)
        with self.sessions.begin() as db:
            db.add(Group(tg_id=-1001, name="Empty"))
        with self.assertRaisesRegex(ReportError, "Нет сохранённых игр"):
            with self.sessions.begin() as db:
                undo_last_game(db, -1001)

    def test_stats_aggregate_sort_escape_and_isolate_groups_in_one_query(self):
        self.save(entries=[ReportEntry("<Alice & Bob>", 100), ReportEntry("@bob", -100)])
        self.save(game_date=date(2026, 11, 22), entries=[ReportEntry("<Alice & Bob>", -20), ReportEntry("@bob", 20)])
        self.save(group=-1002, entries=[ReportEntry("@bob", 1000)])
        query_count = [0]

        def count_queries(*args):
            query_count[0] += 1

        event.listen(self.engine, "before_cursor_execute", count_queries)
        try:
            with self.sessions() as db:
                group_stats = get_global_player_stats(db, -1001)
                self.assertEqual(query_count[0], 1)
                global_stats = get_global_player_stats(db)
                self.assertEqual(query_count[0], 2)
        finally:
            event.remove(self.engine, "before_cursor_execute", count_queries)
        self.assertEqual(group_stats, "&lt;Alice &amp; Bob&gt; +80 (2 игр)\n@bob -80 (2 игр)")
        self.assertEqual(global_stats, "@bob +920 (3 игр)\n&lt;Alice &amp; Bob&gt; +80 (2 игр)")

    def test_stats_distinguish_missing_group_and_empty_group(self):
        with self.sessions.begin() as db:
            db.add(Group(tg_id=-1001, name="Empty"))
        with self.sessions() as db:
            self.assertEqual(get_global_player_stats(db, -9999), "Группа не найдена.")
            self.assertEqual(get_global_player_stats(db, -1001), "Нет данных.")
            self.assertEqual(get_global_player_stats(db), "Нет данных.")


if __name__ == "__main__":
    unittest.main()
