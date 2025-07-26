import asyncio
import logging
from os import getenv
import re
from datetime import datetime
import sys

from aiogram import F, Bot, Dispatcher, types, Router
from aiogram.types import Message, ChatMemberUpdated
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode

from db import init_db, SessionLocal
from models import Group, Player, GameSession, PlayerResult, GroupPlayer
from utils import get_global_player_stats
from aiogram.client.default import DefaultBotProperties
TOKEN = getenv("BOT_TOKEN")
# bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

@dp.my_chat_member()
async def handle_bot_added(event: ChatMemberUpdated):
    if event.new_chat_member.status in ("member", "administrator"):
        with SessionLocal() as db:
            group = db.query(Group).filter_by(tg_id=event.chat.id).first()
            if not group:
                group = Group(tg_id=event.chat.id, name=event.chat.title)
                db.add(group)
            else:
                group.name = event.chat.title
            db.commit()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.reply("Привет! Отправь отчёт в формате:\n\n"
                        "<code>Результаты 26.07.2025:\n"
                        "@user1 +100\n"
                        "@user2 -300\n"
                        "Имя Фамилия +200</code>")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    group_id = message.chat.id if message.chat.type.endswith("group") else None
    with SessionLocal() as db:
        text = get_global_player_stats(db, group_tg_id=group_id)
    await message.reply(f"<b>Статистика игроков:</b>\n{text}")


@dp.message()
async def handle_report_message(message: Message):
    if not message.text:
        return
    print(message.text)
    if not message.chat.type.endswith("group"):
        return
    text = message.text.strip()
    date_match = re.search(r"Результаты (\d{2}\.\d{2}\.\d{4}):", text)
    if not date_match:
        return

    try:
        game_date = datetime.strptime(date_match.group(1), "%d.%m.%Y").date()
    except ValueError:
        await message.reply("Неверный формат даты.")
        return

    lines = text.splitlines()[1:]

    with SessionLocal() as db:
        group = db.query(Group).filter_by(tg_id=message.chat.id).first()
        if not group:
            group = Group(tg_id=message.chat.id, name=message.chat.title)
            db.add(group)
            db.commit()
            db.refresh(group)

        existing_session = db.query(GameSession).filter_by(group_id=group.id, date=game_date).first()
        if existing_session:
            await message.reply(f"Сессия за {game_date.strftime('%d.%m.%Y')} уже сохранена.")
            return

        session_entry = GameSession(group_id=group.id, date=game_date)
        db.add(session_entry)
        db.commit()
        db.refresh(session_entry)

        for line in lines:
            match = re.match(r"(?P<name>@?\S.+?)\s+(?P<amount>[+-]?\d+)", line.strip())
            if not match:
                continue

            name = match.group("name").strip()
            amount = int(match.group("amount"))

            if name.startswith("@"):  # username
                player = db.query(Player).filter_by(username=name).first()
                if not player:
                    player = Player(username=name)
            else:
                player = db.query(Player).filter_by(full_name=name).first()
                if not player:
                    player = Player(full_name=name)

            db.add(player)
            db.commit()
            db.refresh(player)

            # Привязка игрока к группе
            gp_exists = db.query(GroupPlayer).filter_by(group_id=group.id, player_id=player.id).first()
            if not gp_exists:
                gp_link = GroupPlayer(group_id=group.id, player_id=player.id)
                db.add(gp_link)

            # Добавляем результат
            result = PlayerResult(
                session_id=session_entry.id,
                player_id=player.id,
                amount=amount
            )
            db.add(result)

        db.commit()
        await message.reply("Результаты сохранены ✅")


async def main() -> None:
    # Initialize Bot instance with default bot properties which will be passed to all API calls
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    init_db()
    # And the run events dispatching
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
