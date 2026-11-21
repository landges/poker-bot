import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app import main
from app.access import require_game_editor
from app.models import Base, GameSession, Group, GroupPlayer, Player, PlayerResult


def message(text="Результаты 21.11.2026:\n@alice 100\n@bob -100", status=ChatMemberStatus.ADMINISTRATOR):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=-1001, title="Group", type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=123), sender_chat=None,
        bot=SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status=status))),
        reply=AsyncMock(),
    )


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_owner_and_admin_are_allowed(self):
        for status in ChatMemberStatus:
            with self.subTest(status=status):
                msg = message(status=status)
                allowed = await require_game_editor(msg)
                self.assertEqual(allowed, status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR))
                msg.bot.get_chat_member.assert_awaited_once_with(chat_id=-1001, user_id=123)

    async def test_anonymous_admin_of_own_group_is_allowed(self):
        msg = message()
        msg.sender_chat = SimpleNamespace(id=msg.chat.id)
        self.assertTrue(await require_game_editor(msg))
        msg.bot.get_chat_member.assert_not_awaited()

    async def test_foreign_sender_chat_and_missing_user_are_denied(self):
        for sender_chat in (SimpleNamespace(id=-9999), None):
            msg = message()
            msg.sender_chat = sender_chat
            msg.from_user = None
            self.assertFalse(await require_game_editor(msg))

    async def test_api_failure_denies_changes(self):
        msg = message()
        msg.bot.get_chat_member.side_effect = TelegramNetworkError(
            method=GetChatMember(chat_id=-1001, user_id=123), message="Unavailable",
        )
        self.assertFalse(await require_game_editor(msg))
        self.assertIn("Не удалось проверить права", msg.reply.call_args.args[0])

    async def test_demoted_admin_loses_access_immediately(self):
        msg = message()
        self.assertTrue(await require_game_editor(msg))
        msg.bot.get_chat_member.return_value.status = ChatMemberStatus.MEMBER
        self.assertFalse(await require_game_editor(msg))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)
        patcher = patch.object(main, "SessionLocal", self.sessions)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_member_cannot_save_or_delete_report(self):
        for handler in (main.handle_report_message, main.cmd_undo_last):
            msg = message(status=ChatMemberStatus.MEMBER)
            await handler(msg)
            self.assertIn("только владелец и администраторы", msg.reply.call_args.args[0])
        with self.sessions() as db:
            self.assertEqual(db.query(GameSession).count(), 0)

    async def test_owner_can_save_and_admin_can_undo(self):
        report = message(status=ChatMemberStatus.CREATOR)
        await main.handle_report_message(report)
        self.assertIn("сохранены", report.reply.call_args.args[0])
        await main.cmd_undo_last(message("/undo_last"))
        with self.sessions() as db:
            self.assertEqual(db.query(GameSession).count(), 0)
            self.assertEqual(db.query(PlayerResult).count(), 0)

    async def test_invalid_or_duplicate_reports_do_not_create_rows(self):
        for text in (
            "Результаты 21.11.2026:",
            "Результаты 21.11.2026:\n@alice 100\n@Alice -100",
            "Результаты 21.11.2026:\n@alice 100\n@bob -100.50",
        ):
            await main.handle_report_message(message(text))
        with self.sessions() as db:
            for model in (Group, Player, GroupPlayer, GameSession, PlayerResult):
                self.assertEqual(db.query(model).count(), 0)

    async def test_storage_error_returns_failure_and_rolls_back(self):
        def fail_results(conn, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO player_results"):
                raise SQLAlchemyError("Injected storage error")

        event.listen(self.engine, "before_cursor_execute", fail_results)
        msg = message()
        try:
            with self.assertLogs(main.logger, level="ERROR"):
                await main.handle_report_message(msg)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_results)
        self.assertIn("Изменения отменены", msg.reply.call_args.args[0])
        with self.sessions() as db:
            self.assertEqual(db.query(Group).count(), 0)
            self.assertEqual(db.query(GameSession).count(), 0)
        await main.handle_report_message(message())
        with self.sessions() as db:
            self.assertEqual(db.query(PlayerResult).count(), 2)

    async def test_html_is_escaped_in_stats_undo_and_errors(self):
        await main.handle_report_message(message("Результаты 21.11.2026:\n<A & B> 100\n@bob -100"))
        stats = message("/stats")
        await main.cmd_stats(stats)
        self.assertIn("&lt;A &amp; B&gt;", stats.reply.call_args.args[0])
        undo = message("/undo_last")
        await main.cmd_undo_last(undo)
        self.assertIn("&lt;A &amp; B&gt;", undo.reply.call_args.args[0])
        duplicate = message("Результаты 21.11.2026:\n<A> 100\n<A> -100")
        await main.handle_report_message(duplicate)
        self.assertIn("&lt;A&gt;", duplicate.reply.call_args.args[0])

    async def test_ordinary_messages_do_not_check_admin_rights(self):
        msg = message("Привет")
        await main.handle_report_message(msg)
        msg.bot.get_chat_member.assert_not_awaited()
        msg.reply.assert_not_awaited()

    async def test_private_reports_are_ignored_and_undo_is_denied(self):
        msg = message()
        msg.chat.type = ChatType.PRIVATE
        await main.handle_report_message(msg)
        msg.reply.assert_not_awaited()
        await main.cmd_undo_last(msg)
        self.assertIn("только в группах", msg.reply.call_args.args[0])


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_fails_before_database_initialization(self):
        with patch.object(main, "settings", SimpleNamespace(bot_token="")):
            with patch.object(main, "init_db") as initialize:
                with self.assertRaisesRegex(RuntimeError, "BOT_TOKEN"):
                    await main.main()
                initialize.assert_not_called()

    async def test_proxy_is_optional_and_passed_from_settings(self):
        for proxy_url in (None, "http://proxy:3128"):
            with self.subTest(proxy=proxy_url):
                settings = SimpleNamespace(bot_token="123:token", proxy_url=proxy_url)
                with patch.object(main, "settings", settings), patch.object(main, "init_db"):
                    with patch.object(main, "AiohttpSession") as session, patch.object(main, "Bot") as bot:
                        with patch.object(main.dp, "start_polling", new_callable=AsyncMock) as polling:
                            await main.main()
                            session.assert_called_once_with(proxy=proxy_url)
                            self.assertIs(bot.call_args.kwargs["session"], session.return_value)
                            polling.assert_awaited_once_with(bot.return_value)


if __name__ == "__main__":
    unittest.main()
