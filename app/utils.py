# utils.py

from typing import Union
from sqlalchemy.orm import Session
from models import Player, PlayerResult, GroupPlayer, Group


def get_global_player_stats(db: Session, group_tg_id: Union[int, None] = None) -> str:
    query = db.query(Player)

    if group_tg_id:
        query = (
            query.join(GroupPlayer)
            .join(GroupPlayer.group)
            .filter(Group.tg_id == group_tg_id)
        )

    players = query.all()

    result_lines = []

    for player in players:
        total = sum(res.amount for res in player.results)
        name = player.username or player.full_name or "Без имени"
        result_lines.append(f"{name} {total:+}")

    # Сортируем по убыванию выигрыша
    result_lines.sort(key=lambda x: -int(x.split()[-1]))

    return "\n".join(result_lines) if result_lines else "Нет данных."
