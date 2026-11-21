import logging

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message


logger = logging.getLogger(__name__)


async def require_game_editor(message: Message) -> bool:
    if message.sender_chat is not None:
        # Anonymous group administrators send messages on behalf of this group.
        allowed = message.sender_chat.id == message.chat.id
    elif message.from_user is None:
        allowed = False
    else:
        try:
            member = await message.bot.get_chat_member(
                chat_id=message.chat.id, user_id=message.from_user.id
            )
        except TelegramAPIError:
            logger.warning("Could not check administrator rights in chat %s", message.chat.id)
            await message.reply("Не удалось проверить права. Попробуйте ещё раз.")
            return False
        allowed = member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)

    if not allowed:
        await message.reply("Изменять результаты могут только владелец и администраторы группы.")
    return allowed
