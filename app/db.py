from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import Base, PlayerResult


engine = create_engine(settings.database_url, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, future=True)

def init_db():
    Base.metadata.create_all(engine)
    # create_all does not add new indexes to tables in an existing database.
    try:
        for index in PlayerResult.__table__.indexes:
            index.create(engine, checkfirst=True)
    except IntegrityError as exc:
        raise RuntimeError(
            "В player_results есть повторы (session_id, player_id). "
            "Исправьте повторяющиеся результаты перед запуском бота."
        ) from exc
