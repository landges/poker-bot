from html import escape
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models import GameSession, Group, GroupPlayer, Player, PlayerResult


def get_global_player_stats(db: Session, group_tg_id: Optional[int] = None) -> str:
    if group_tg_id is not None:
        totals = (
            db.query(
                PlayerResult.player_id,
                GameSession.group_id,
                func.sum(PlayerResult.amount).label("total"),
                func.count(func.distinct(PlayerResult.session_id)).label("games"),
            )
            .join(PlayerResult.session)
            .join(GameSession.group)
            .filter(Group.tg_id == group_tg_id)
            .group_by(PlayerResult.player_id, GameSession.group_id)
            .subquery()
        )
        total = func.coalesce(totals.c.total, 0)
        rows = (
            db.query(Player.id, Player.username, Player.full_name, total,
                     func.coalesce(totals.c.games, 0))
            .select_from(Group)
            .outerjoin(GroupPlayer, GroupPlayer.group_id == Group.id)
            .outerjoin(Player, Player.id == GroupPlayer.player_id)
            .outerjoin(totals, and_(totals.c.player_id == Player.id, totals.c.group_id == Group.id))
            .filter(Group.tg_id == group_tg_id)
            .order_by(total.desc(), Player.id)
            .all()
        )
        if not rows:
            return "Группа не найдена."
    else:
        total = func.coalesce(func.sum(PlayerResult.amount), 0)
        rows = (
            db.query(Player.id, Player.username, Player.full_name, total,
                     func.count(func.distinct(PlayerResult.session_id)))
            .outerjoin(PlayerResult, PlayerResult.player_id == Player.id)
            .group_by(Player.id, Player.username, Player.full_name)
            .order_by(total.desc(), Player.id)
            .all()
        )

    lines = [
        f"{escape(username or full_name or 'Без имени')} {amount:+} ({games} игр)"
        for player_id, username, full_name, amount, games in rows
        if player_id is not None
    ]
    return "\n".join(lines) if lines else "Нет данных."
