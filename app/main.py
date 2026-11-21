import asyncio
import logging
import sys
from html import escape

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.exc import SQLAlchemyError

from app.access import require_game_editor
from app.config import settings
from app.db import SessionLocal, init_db
from app.models import Group
from app.reports import ReportError, parse_report, save_report, undo_last_game
from app.utils import get_global_player_stats


logger = logging.getLogger(__name__)
dp = Dispatcher()
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


@dp.my_chat_member()
async def handle_bot_added(event: ChatMemberUpdated):
    if event.chat.type not in GROUP_TYPES:
        return
    if event.new_chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        with SessionLocal.begin() as db:
            group = db.query(Group).filter_by(tg_id=event.chat.id).first()
            if group is None:
                db.add(Group(tg_id=event.chat.id, name=event.chat.title))
            else:
                group.name = event.chat.title


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.reply(
        "Привет! Владелец и администраторы группы могут отправлять отчёты в формате:\n\n"
        "<code>Результаты 26.07.2025:\n"
        "@user1 +100\n"
        "@user2 -300\n"
        "Имя Фамилия +200</code>\n\n"
        "/stats — статистика игроков\n"
        "/undo_last — удалить игру с самой поздней датой (для администраторов)"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    group_id = message.chat.id if message.chat.type in GROUP_TYPES else None
    with SessionLocal() as db:
        text = get_global_player_stats(db, group_tg_id=group_id)
    await message.reply(f"<b>Статистика игроков:</b>\n{text}")


@dp.message(Command("undo_last"))
async def cmd_undo_last(message: Message):
    if message.chat.type not in GROUP_TYPES:
        await message.reply("Команда доступна только в группах.")
        return
    if not await require_game_editor(message):
        return

    try:
        with SessionLocal.begin() as db:
            game_date, removed_names = undo_last_game(db, message.chat.id)
    except ReportError as exc:
        await message.reply(escape(str(exc)))
        return
    except SQLAlchemyError:
        logger.exception("Could not undo a game in chat %s", message.chat.id)
        await message.reply("Не удалось удалить игру. Изменения отменены, попробуйте ещё раз.")
        return

    text = f"Результаты за {game_date:%d.%m.%Y} удалены 🗑"
    if removed_names:
        text += "\nУдалены из группы: " + ", ".join(escape(name) for name in removed_names)
    await message.reply(text)


@dp.message()
async def handle_report_message(message: Message):
    if not message.text or message.chat.type not in GROUP_TYPES:
        return
    if not message.text.strip().startswith("Результаты "):
        return
    if not await require_game_editor(message):
        return

    try:
        game_date, entries = parse_report(message.text)
        with SessionLocal.begin() as db:
            save_report(db, message.chat.id, message.chat.title, game_date, entries)
    except ReportError as exc:
        await message.reply(escape(str(exc)))
        return
    except SQLAlchemyError:
        logger.exception("Could not save a report in chat %s", message.chat.id)
        await message.reply("Не удалось сохранить отчёт. Изменения отменены, попробуйте ещё раз.")
        return

    await message.reply("Результаты сохранены ✅")


async def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("Задайте BOT_TOKEN в переменных окружения или файле .env.")
    init_db()
    session = AiohttpSession(proxy=settings.proxy_url)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
