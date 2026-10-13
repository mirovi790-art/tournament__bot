from datetime import datetime
from sqlalchemy import BigInteger, String, Integer, DateTime, ForeignKey, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

DB_URL = "sqlite+aiosqlite:///tournament.db"
engine = create_async_engine(DB_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chips: Mapped[int] = mapped_column(Integer, default=1000)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Ban(Base):
    __tablename__ = "bans"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    banned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Tournament(Base):
    __tablename__ = "tournaments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    access_type: Mapped[str] = mapped_column(String(16))
    game_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="registration")
    creator_id: Mapped[int] = mapped_column(BigInteger)
    start_stack: Mapped[int] = mapped_column(Integer, default=1000)
    turn_time: Mapped[int] = mapped_column(Integer, default=20)
    blind_interval: Mapped[int] = mapped_column(Integer, default=5)
    min_players: Mapped[int] = mapped_column(Integer, default=2)
    bracket_size: Mapped[int] = mapped_column(Integer, default=0)
    auto_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prize: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Participant(Base):
    __tablename__ = "participants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    password: Mapped[str | None] = mapped_column(String(16), nullable=True)
    chips: Mapped[int] = mapped_column(Integer, default=0)
    place: Mapped[int | None] = mapped_column(Integer, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with engine.begin() as conn:
        try:
            res = await conn.exec_driver_sql("PRAGMA table_info(tournaments)")
            cols = [row[1] for row in res.fetchall()]
            if "min_players" not in cols:
                await conn.exec_driver_sql("ALTER TABLE tournaments ADD COLUMN min_players INTEGER DEFAULT 2")
            if "bracket_size" not in cols:
                await conn.exec_driver_sql("ALTER TABLE tournaments ADD COLUMN bracket_size INTEGER DEFAULT 0")
            if "auto_start_at" not in cols:
                await conn.exec_driver_sql("ALTER TABLE tournaments ADD COLUMN auto_start_at DATETIME")
        except Exception:
            pass
