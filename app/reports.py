import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import GameSession, Group, GroupPlayer, Player, PlayerResult


class ReportError(ValueError):
    pass


@dataclass(frozen=True)
class ReportEntry:
    name: str
    amount: int


def parse_report(text: str) -> Tuple[date, List[ReportEntry]]:
    lines = text.strip().splitlines()
    header = re.fullmatch(r"Результаты (\d{2}\.\d{2}\.\d{4}):", lines[0].strip()) if lines else None
    if header is None:
        raise ReportError("Начните отчёт со строки «Результаты ДД.ММ.ГГГГ:».")
    try:
        game_date = datetime.strptime(header.group(1), "%d.%m.%Y").date()
    except ValueError as exc:
        raise ReportError("Неверная дата игры.") from exc

    entries = []
    seen_names = set()
    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        match = re.fullmatch(r"(?P<name>\S.*?)\s+(?P<amount>[+-]?[0-9]+)", line.strip())
        if match is None:
            raise ReportError(f"Ошибка в строке {line_number}: укажите имя и целое число.")
        name = " ".join(match.group("name").split())
        identity = name.casefold()
        if identity in seen_names:
            raise ReportError(f"Игрок {name} указан несколько раз. Оставьте одну строку на игрока.")
        seen_names.add(identity)
        amount = int(match.group("amount"))
        if not -(2 ** 63) <= amount < 2 ** 63:
            raise ReportError(f"Слишком большое число в строке {line_number}.")
        entries.append(ReportEntry(name, amount))

    if not entries:
        raise ReportError("В отчёте нет результатов игроков.")
    return game_date, entries


def save_report(
    db: Session, group_tg_id: int, group_name: str, game_date: date,
    entries: List[ReportEntry],
) -> None:
    """Save within the caller's transaction; never commit individual rows."""
    if not entries:
        raise ReportError("В отчёте нет результатов игроков.")
    group = db.query(Group).filter_by(tg_id=group_tg_id).first()
    if group is None:
        group = Group(tg_id=group_tg_id, name=group_name)
        db.add(group)
        db.flush()

    existing = db.query(GameSession).filter_by(group_id=group.id, date=game_date).first()
    if existing is not None:
        raise ReportError(f"Сессия за {game_date:%d.%m.%Y} уже сохранена.")

    game = GameSession(group_id=group.id, date=game_date)
    db.add(game)
    db.flush()
    seen_players = set()
    for entry in entries:
        if entry.name.startswith("@"):
            player = db.query(Player).filter(func.lower(Player.username) == entry.name.lower()).first()
            if player is None:
                player = Player(username=entry.name)
        else:
            player = db.query(Player).filter_by(full_name=entry.name).first()
            if player is None:
                player = Player(full_name=entry.name)
        db.add(player)
        db.flush()
        if player.id in seen_players:
            raise ReportError(f"Игрок {entry.name} указан несколько раз.")
        seen_players.add(player.id)

        link = db.query(GroupPlayer).filter_by(group_id=group.id, player_id=player.id).first()
        if link is None:
            db.add(GroupPlayer(group_id=group.id, player_id=player.id))
        db.add(PlayerResult(session_id=game.id, player_id=player.id, amount=entry.amount))
    db.flush()


def undo_last_game(db: Session, group_tg_id: int) -> Tuple[date, List[str]]:
    group = db.query(Group).filter_by(tg_id=group_tg_id).first()
    if group is None:
        raise ReportError("Группа не зарегистрирована.")
    game = (
        db.query(GameSession).filter_by(group_id=group.id)
        .order_by(GameSession.date.desc()).first()
    )
    if game is None:
        raise ReportError("Нет сохранённых игр для удаления.")

    game_date = game.date
    player_ids = [row[0] for row in db.query(PlayerResult.player_id).filter_by(session_id=game.id).distinct()]
    db.query(PlayerResult).filter_by(session_id=game.id).delete(synchronize_session=False)
    db.delete(game)
    db.flush()

    removed_names = []
    for player_id in player_ids:
        remaining = (
            db.query(PlayerResult.id).join(PlayerResult.session)
            .filter(PlayerResult.player_id == player_id, GameSession.group_id == group.id)
            .first()
        )
        if remaining is None:
            link = db.query(GroupPlayer).filter_by(group_id=group.id, player_id=player_id).first()
            if link is not None:
                removed_names.append(link.player.username or link.player.full_name or f"id {player_id}")
                db.delete(link)
    return game_date, removed_names
