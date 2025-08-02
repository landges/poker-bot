# utils.py

from typing import Union
from sqlalchemy.orm import Session
from models import Player, PlayerResult, GroupPlayer, Group
from sqlalchemy import func


def get_global_player_stats(db: Session, group_tg_id: Union[int, None] = None) -> str:
    result_data = []

    if group_tg_id:
        group = db.query(Group).filter_by(tg_id=group_tg_id).first()
        if not group:
            return "Группа не найдена."

        group_players = (
            db.query(GroupPlayer)
            .filter_by(group_id=group.id)
            .join(GroupPlayer.player)
            .all()
        )

        for gp in group_players:
            player = gp.player

            total = (
                db.query(PlayerResult)
                .join(PlayerResult.session)
                .filter(
                    PlayerResult.player_id == player.id,
                    PlayerResult.session.has(group_id=group.id)
                )
                .with_entities(func.sum(PlayerResult.amount))
                .scalar() or 0
            )

            game_count = (
                db.query(func.count(PlayerResult.id))
                .join(PlayerResult.session)
                .filter(
                    PlayerResult.player_id == player.id,
                    PlayerResult.session.has(group_id=group.id)
                )
                .scalar()
            )

            name = player.username or player.full_name or "Без имени"
            result_data.append((name, total, game_count))

    else:
        players = db.query(Player).all()
        for player in players:
            total = sum(res.amount for res in player.results)
            game_count = len(player.results)
            name = player.username or player.full_name or "Без имени"
            result_data.append((name, total, game_count))

    # Сортировка по убыванию выигрыша
    result_data.sort(key=lambda x: -x[1])

    result_lines = [
        f"{name} {total:+} ({games} игр)"
        for name, total, games in result_data
    ]

    return "\n".join(result_lines) if result_lines else "Нет данных."

