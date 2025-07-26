from datetime import date
from typing import Optional, List

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(nullable=True)

    sessions: Mapped[List["GameSession"]] = relationship(back_populates="group")
    group_players: Mapped[List["GroupPlayer"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(unique=True, nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(nullable=True)

    results: Mapped[List["PlayerResult"]] = relationship(back_populates="player")
    group_players: Mapped[List["GroupPlayer"]] = relationship(back_populates="player", cascade="all, delete-orphan")


class GroupPlayer(Base):
    __tablename__ = "group_players"
    __table_args__ = (UniqueConstraint("group_id", "player_id", name="_group_player_uc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)

    group: Mapped[Group] = relationship(back_populates="group_players")
    player: Mapped[Player] = relationship(back_populates="group_players")


class GameSession(Base):
    __tablename__ = "game_sessions"
    __table_args__ = (UniqueConstraint("group_id", "date", name="_group_date_uc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), nullable=False)
    date: Mapped[date]

    group: Mapped[Group] = relationship(back_populates="sessions")
    results: Mapped[List["PlayerResult"]] = relationship(back_populates="session")


class PlayerResult(Base):
    __tablename__ = "player_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("game_sessions.id"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    amount: Mapped[int]

    session: Mapped[GameSession] = relationship(back_populates="results")
    player: Mapped[Player] = relationship(back_populates="results")
