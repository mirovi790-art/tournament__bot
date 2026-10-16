import asyncio
import html
import logging
import os
import uvicorn
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import (CommandStart, Command, StateFilter, BaseFilter)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton, WebAppInfo,
                            ReplyKeyboardMarkup, KeyboardButton, ErrorEvent)
from dotenv import load_dotenv
from sqlalchemy import select, func

# ===== Safe monkey-patch =====
_original_cb_answer = CallbackQuery.answer
async def _safe_cb_answer(self, text=None, show_alert=False, *args, **kwargs):
    try:
        return await _original_cb_answer(self, text, show_alert, *args, **kwargs)
    except TelegramBadRequest as e:
        s = str(e)
        if "query is too old" in s or "query ID is invalid" in s: return None
        if "message is not modified" in s: return None
        logging.warning("cb.answer: %s", e); return None
    except TelegramForbiddenError:
        return None
    except Exception as e:
        logging.warning("cb.answer: %s", e); return None
CallbackQuery.answer = _safe_cb_answer
# =============================

from database import (init_db, SessionLocal, User, Admin, Ban, Tournament,
                       Participant)
from webapp import create_app
import game as gm
from game import (WheelGame, Wheel1v1Game, BracketGame,
                  BRACKET_SERIES_TARGET, BOT_ID_BASE, is_bot)
from poker import PokerTournament

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "7883953756"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
SEP = "➖➖➖➖➖➖➖➖➖"

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s:%(name)s:%(message)s")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

router = Router()
_finalized_tids = set()
_bg_tasks: set[asyncio.Task] = set()
VALID_BRACKET_SIZES = [4, 8, 16, 32]
VALID_SERIES_TARGETS = [1, 2, 3]
MAX_ACTIVE_TOURNAMENTS_PER_ADMIN = 5

POKER_STACKS = [500, 1000, 1500, 2000, 3000]
POKER_BLINDS = [(5, 10), (10, 20), (25, 50), (50, 100)]
POKER_TIMES = [10, 15, 20, 30]
POKER_MAX_SEATS = [2, 3, 4, 5, 6, 7]


def _norm(s: str) -> str:
    if not s: return ""
    out = []
    for ch in s:
        if ch.isalnum() or ch.isspace():
            out.append(ch.lower())
    return "".join(out).strip()


_MENU_RAW = [
    "🏆 Турниры", "👤 Профиль", "🆕 Создать турнир", "ℹ️ Помощь",
    "👑 Админство", "🔍 Найти игрока", "⬅️ В главное меню",
    "🌐 Открытые турниры", "🎟 Мои турниры", "📂 Созданные мной",
    "📊 Все активные турниры", "📋 Список админов", "🚫 Черный список",
    "❌ Отмена", "меню",
]
_MENU_NORM = {_norm(x) for x in _MENU_RAW}


class TextEq(BaseFilter):
    def __init__(self, *variants):
        self.norm = {_norm(v) for v in variants}
    async def __call__(self, message: Message) -> bool:
        if not message.text: return False
        return _norm(message.text) in self.norm


class TextNotMenu(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.text: return True
        return _norm(message.text) not in _MENU_NORM


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(lambda x: _bg_tasks.discard(x))
    return t


def esc(s): return html.escape(s or "")


async def _safe_edit(cb: CallbackQuery, text: str, **kwargs):
    rm = kwargs.get("reply_markup")
    if rm is not None and isinstance(rm, ReplyKeyboardMarkup):
        try:
            if cb.message is not None:
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            await cb.bot.send_message(cb.from_user.id, text, **kwargs)
            return True
        except Exception as e:
            logging.warning("_safe_edit reply-kb fail: %s", e)
            return False
    if cb.message is None:
        try:
            await cb.bot.send_message(cb.from_user.id, text, **kwargs); return True
        except Exception as e:
            logging.warning("_safe_edit no-msg fail: %s", e); return False
    try:
        await cb.message.edit_text(text, **kwargs); return True
    except TelegramBadRequest as e:
        s = str(e)
        if "message is not modified" in s: return True
        try:
            if cb.message is not None:
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            await cb.bot.send_message(cb.from_user.id, text, **kwargs)
            return True
        except Exception as e2:
            logging.warning("_safe_edit edit→send fail: %s | %s", e, e2)
            return False
    except Exception as e:
        logging.warning("_safe_edit unknown fail: %s", e)
        try:
            await cb.bot.send_message(cb.from_user.id, text, **kwargs)
            return True
        except Exception:
            return False


async def _is_admin(user_id):
    if is_bot(user_id): return False
    if user_id == OWNER_ID: return True
    async with SessionLocal() as s:
        r = await s.execute(select(Admin).where(Admin.user_id == user_id))
        return r.scalar_one_or_none() is not None


async def _check_ban(user_id):
    if is_bot(user_id): return False
    async with SessionLocal() as s:
        return await s.get(Ban, user_id) is not None


async def _active_tournaments_count(user_id):
    async with SessionLocal() as s:
        r = await s.execute(select(func.count()).select_from(Tournament).where(
            Tournament.creator_id == user_id,
            Tournament.status.in_(("registration", "running", "paused"))))
        return r.scalar() or 0


async def _upsert_user(fu):
    async with SessionLocal() as s:
        u = await s.get(User, fu.id)
        if u is None:
            u = User(id=fu.id, username=fu.username, first_name=fu.first_name); s.add(u)
        else:
            u.username = fu.username; u.first_name = fu.first_name
        await s.commit()


def _display_name(u, uid):
    if u:
        if u.first_name and u.first_name.strip(): return u.first_name.strip()
        if u.username and u.username.strip(): return "@" + u.username.strip()
    return f"Игрок {uid}"


async def _enrich_names(bot, user_ids):
    result = {}
    async with SessionLocal() as s:
        for uid in user_ids:
            u = await s.get(User, uid)
            if u and u.first_name and u.first_name.strip():
                result[uid] = u.first_name.strip(); continue
            if is_bot(uid):
                result[uid] = _display_name(u, uid); continue
            try:
                chat = await asyncio.wait_for(bot.get_chat(uid), timeout=3.0)
                fn = (chat.first_name or "").strip(); un = (chat.username or "").strip()
                if fn: result[uid] = fn
                elif un: result[uid] = "@" + un
                else: result[uid] = f"Игрок {uid}"
                if u is None: s.add(User(id=uid, first_name=fn or None, username=un or None))
                else:
                    if fn: u.first_name = fn
                    if un: u.username = un
            except Exception:
                result[uid] = _display_name(u, uid)
        try: await s.commit()
        except: pass
    return result


async def _resolve_uid(raw: str):
    async with SessionLocal() as s:
        if raw.startswith("@"):
            r = await s.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            return u.id if u else None
        try: uid = int(raw)
        except ValueError: return None
        u = await s.get(User, uid)
        if not u:
            s.add(User(id=uid, first_name=f"Игрок {uid}")); await s.commit()
        return uid


async def _new_bot_id():
    async with SessionLocal() as s:
        r = await s.execute(select(func.max(User.id)).where(User.id >= BOT_ID_BASE))
        mx = r.scalar() or (BOT_ID_BASE - 1)
        return int(mx) + 1


async def _add_bot_to_tournament(tid):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: return False, None, "Турнир не найден"
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid,
            Participant.user_id >= BOT_ID_BASE))
        bot_number = len(r.scalars().all()) + 1
        if t.game_type == "bracket":
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.status.in_(("approved", "winner", "pending"))))
            if len(r.scalars().all()) >= t.bracket_size:
                return False, None, "Сетка уже заполнена"
        if t.game_type == "wheel_1v1":
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.status.in_(("approved", "winner", "pending"))))
            if len(r.scalars().all()) >= 2:
                return False, None, "Уже 2 участника"
        if t.game_type == "poker":
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.status.in_(("approved", "winner", "pending"))))
            if len(r.scalars().all()) >= 49:
                return False, None, "Максимум 49 игроков (7 столов по 7)"
        bot_id = await _new_bot_id()
        bot_name = f"🤖 Бот {bot_number}"
        s.add(User(id=bot_id, username=f"bot_{bot_id - BOT_ID_BASE}", first_name=bot_name))
        s.add(Participant(tournament_id=tid, user_id=bot_id,
                          status="approved", chips=t.start_stack))
        await s.commit()
    return True, bot_name, None


async def _remove_last_bot_from_tournament(tid):
    async with SessionLocal() as s:
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid,
            Participant.user_id >= BOT_ID_BASE).order_by(Participant.id.desc()).limit(1))
        p = r.scalar_one_or_none()
        if not p: return False, None, "В турнире нет ботов"
        bot_id = p.user_id
        u = await s.get(User, bot_id)
        name = u.first_name if u else f"Бот {bot_id}"
        await s.delete(p)
        if u: await s.delete(u)
        await s.commit()
    return True, name, None


def reply_main_menu(user_id, is_admin):
    rows = [[KeyboardButton(text="🏆 Турниры")], [KeyboardButton(text="👤 Профиль")]]
    if is_admin: rows.append([KeyboardButton(text="🆕 Создать турнир")])
    if user_id == OWNER_ID:
        rows.append([KeyboardButton(text="👑 Админство")])
        rows.append([KeyboardButton(text="🔍 Найти игрока")])
    rows.append([KeyboardButton(text="ℹ️ Помощь")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def reply_tournaments_menu(user_id, is_admin):
    rows = [[KeyboardButton(text="🌐 Открытые турниры")], [KeyboardButton(text="🎟 Мои турниры")]]
    if is_admin: rows.append([KeyboardButton(text="📂 Созданные мной")])
    if user_id == OWNER_ID: rows.append([KeyboardButton(text="📊 Все активные турниры")])
    rows.append([KeyboardButton(text="⬅️ В главное меню")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def owner_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="owner_add_admin")],
        [InlineKeyboardButton(text="➖ Удалить админа", callback_data="owner_del_admin")],
        [InlineKeyboardButton(text="📋 Список админов", callback_data="owner_list_admins")],
        [InlineKeyboardButton(text="🚫 Забанить игрока", callback_data="owner_ban")],
        [InlineKeyboardButton(text="✅ Разбанить игрока", callback_data="owner_unban")],
        [InlineKeyboardButton(text="📜 Черный список", callback_data="owner_blacklist")]])


def back_kb(t="back_main"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=t)]])


def access_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Открытый", callback_data="tt_access_open")],
        [InlineKeyboardButton(text="🔴 Закрытый", callback_data="tt_access_closed")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


def game_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎡 Колесо Фортуны", callback_data="tt_game_wheel")],
        [InlineKeyboardButton(text="🏆 Турнир", callback_data="tt_game_bracket")],
        [InlineKeyboardButton(text="🃏 Покер (Texas Hold'em)", callback_data="tt_game_poker")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


def wheel_subtype_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎡 Колесо (общее)", callback_data="tt_wheel_general")],
        [InlineKeyboardButton(text="⚔️ Колесо 1×1", callback_data="tt_wheel_1v1")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


def bracket_size_kb():
    rows = [[InlineKeyboardButton(text=f"{n} игроков", callback_data=f"tt_bracket_{n}")]
            for n in VALID_BRACKET_SIZES]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def series_kb(prefix="tt_series"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥇 До 1 победы", callback_data=f"{prefix}_1")],
        [InlineKeyboardButton(text="🥈 До 2 побед", callback_data=f"{prefix}_2")],
        [InlineKeyboardButton(text="🥉 До 3 побед", callback_data=f"{prefix}_3")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


def poker_stack_kb():
    rows = []
    line = []
    for st in POKER_STACKS:
        line.append(InlineKeyboardButton(text=f"💰 {st}", callback_data=f"tt_pk_stack_{st}"))
        if len(line) == 2:
            rows.append(line); line = []
    if line: rows.append(line)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def poker_blinds_kb():
    rows = []
    for sb, bb in POKER_BLINDS:
        rows.append([InlineKeyboardButton(text=f"🎯 {sb} / {bb}", callback_data=f"tt_pk_blind_{sb}_{bb}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def poker_time_kb():
    rows = []
    line = []
    for t in POKER_TIMES:
        line.append(InlineKeyboardButton(text=f"⏱ {t} сек", callback_data=f"tt_pk_time_{t}"))
        if len(line) == 2:
            rows.append(line); line = []
    if line: rows.append(line)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def poker_seats_kb():
    rows = []
    line = []
    for s in POKER_MAX_SEATS:
        line.append(InlineKeyboardButton(text=f"🪑 {s}", callback_data=f"tt_pk_seats_{s}"))
        if len(line) == 3:
            rows.append(line); line = []
    if line: rows.append(line)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="tt_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


def cancel_text_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)


def join_request_kb(pid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{pid}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{pid}")]])


def invite_response_kb(tid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_invite_{tid}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_invite_{tid}")]])


def enter_webapp_kb(tid, url):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Открыть турнир (Mini App)",
                              web_app=WebAppInfo(url=f"{url}/?tid={tid}"))]])


def manage_tournament_kb(tid, status, game_type=None, access_type="closed"):
    rows = [[InlineKeyboardButton(text="🎮 Открыть Mini App",
                                  web_app=WebAppInfo(url=f"{WEBAPP_URL}/?tid={tid}"))]]
    if game_type == "wheel" and status == "running":
        rows.append([InlineKeyboardButton(text="🎡 Крутить колесо", callback_data=f"tt_spin_{tid}")])
    rows.append([
        InlineKeyboardButton(text="🤖 Добавить бота", callback_data=f"tt_addbot_{tid}"),
        InlineKeyboardButton(text="🗑 Убрать бота", callback_data=f"tt_delbot_{tid}"),
    ])
    if status == "registration":
        can_join = (game_type not in ("wheel_1v1", "bracket")) or (game_type == "bracket" and access_type == "open")
        if game_type == "poker":
            can_join = True
        if can_join:
            rows.append([InlineKeyboardButton(text="🖐 Вступить в турнир", callback_data=f"tt_join_{tid}")])
        if game_type != "bracket":
            rows.append([InlineKeyboardButton(text="▶️ Начать турнир", callback_data=f"tt_start_{tid}")])
        if game_type != "wheel_1v1":
            rows.append([InlineKeyboardButton(text="✉️ Пригласить игрока", callback_data=f"tt_invite_{tid}")])
        rows.append([InlineKeyboardButton(text="🚫 Кикнуть игрока", callback_data=f"tt_kick_{tid}")])
    if status == "running":
        if game_type not in ("wheel_1v1", "bracket", "poker"):
            rows.append([InlineKeyboardButton(text="🖐 Вступить в турнир", callback_data=f"tt_join_{tid}")])
        if game_type not in ("wheel_1v1", "bracket", "poker"):
            rows.append([InlineKeyboardButton(text="✉️ Пригласить игрока", callback_data=f"tt_invite_{tid}")])
        rows.append([InlineKeyboardButton(text="🏁 Закончить турнир", callback_data=f"tt_end_{tid}")])
        rows.append([InlineKeyboardButton(text="🚫 Кикнуть игрока", callback_data=f"tt_kick_{tid}")])
        rows.append([InlineKeyboardButton(text="🛑 Отменить", callback_data=f"tt_cancel_{tid}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить турнир", callback_data=f"tt_delete_{tid}")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_manage_tournaments")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def player_tournament_kb(tid, status):
    rows = []
    if status in ("running", "paused"):
        rows.append([InlineKeyboardButton(text="🎮 Открыть турнир (Mini App)",
                                          web_app=WebAppInfo(url=f"{WEBAPP_URL}/?tid={tid}"))])
    rows.append([InlineKeyboardButton(text="⬅️ К моим турнирам", callback_data="my_tournaments")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class OwnerForm(StatesGroup):
    add_admin = State(); ban_id = State(); unban_id = State()


class TournamentFSM(StatesGroup):
    access_type = State()
    game_type = State()
    wheel_subtype = State()
    name = State()
    prize = State()
    w1v1_p1 = State()
    w1v1_p2 = State()
    w1v1_series = State()
    bracket_size = State()
    bracket_series = State()
    bracket_players = State()
    pk_stack = State()
    pk_blinds = State()
    pk_time = State()
    pk_seats = State()
    confirm = State()


class InviteFSM(StatesGroup):
    user_id = State()


class FindPlayerFSM(StatesGroup):
    query = State()


def _help_text():
    return ("<b>ℹ️ ПОМОЩЬ И ПРАВИЛА</b>\n" f"{SEP}\n"
            "<b>🎡 Колесо Фортуны:</b>\n"
            "▫️ Общий режим — крутит админ, выбывает по одному.\n"
            "▫️ 1×1 — дуэль двух игроков, серия побед настраивается.\n\n"
            "<b>🏆 Турнир:</b>\n"
            "▫️ 4/8/16/32 игрока в плей-офф.\n"
            "▫️ Серия побед настраивается (1/2/3).\n\n"
            "<b>🃏 Покер (NLHE):</b>\n"
            "▫️ 2–49 игроков, столы до 7 мест.\n"
            "▫️ Настраиваются: стек, блайнды, время хода, размер стола.\n"
            "▫️ Игрок сам выбирает стол в лобби (3 мин на выбор).\n"
            "▫️ Автослияние столов, лобби с рейтингом.\n\n"
            "<b>🤖 Боты:</b> в меню турнира есть «Добавить бота».\n\n"
            "<blockquote>🎯 Команды: /start /menu /cancel /help</blockquote>")


# ============ 1. КОМАНДЫ ============
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if await _check_ban(message.from_user.id):
        await message.answer("<blockquote>⛔ Заблокированы!</blockquote>"); return
    await _upsert_user(message.from_user)
    is_adm = await _is_admin(message.from_user.id)
    await message.answer(
        f"<b>👋 Привет, {esc(message.from_user.first_name)}!</b>\n{SEP}\n"
        "Это <b>турнирная экосистема</b>.\n\n"
        "<blockquote>🎯 Участвуй и побеждай!</blockquote>",
        reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    if await _check_ban(message.from_user.id):
        await message.answer("<blockquote>⛔ Заблокированы!</blockquote>"); return
    await _upsert_user(message.from_user)
    is_adm = await _is_admin(message.from_user.id)
    await message.answer("🏠 <b>Главное меню</b>",
                         reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(Command("cancel"))
@router.message(Command("reset"))
async def cmd_cancel(message: Message, state: FSMContext):
    cur = await state.get_state()
    await state.clear()
    if await _check_ban(message.from_user.id):
        await message.answer("<blockquote>⛔ Заблокированы!</blockquote>"); return
    is_adm = await _is_admin(message.from_user.id)
    prefix = "❌ Действие отменено.\n\n" if cur else ""
    await message.answer(prefix + "🏠 <b>Главное меню</b>",
                         reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(Command("help"))
async def cmd_help(message: Message): await message.answer(_help_text())


@router.message(Command("owner"))
async def cmd_owner(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != OWNER_ID:
        await message.answer("<blockquote>⛔ Владелец.</blockquote>"); return
    await message.answer(f"<b>👑 ВЛАДЕЛЕЦ</b>\n{SEP}", reply_markup=owner_menu_kb())


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if not await _is_admin(message.from_user.id):
        await message.answer("<blockquote>⛔ Админы.</blockquote>"); return
    await message.answer(f"<b>🛠 АДМИН</b>\n{SEP}", reply_markup=owner_menu_kb())


# ============ 2. МЕНЮ-КНОПКИ ============
@router.message(TextEq("меню"))
async def txt_menu_btn(message: Message, state: FSMContext):
    await state.clear()
    if await _check_ban(message.from_user.id):
        await message.answer("<blockquote>⛔ Заблокированы!</blockquote>"); return
    await _upsert_user(message.from_user)
    is_adm = await _is_admin(message.from_user.id)
    await message.answer("🏠 <b>Главное меню</b>",
                         reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(TextEq("🏆 Турниры"))
async def txt_tournaments(message: Message, state: FSMContext):
    await state.clear()
    is_adm = await _is_admin(message.from_user.id)
    await message.answer("🏆 <b>Раздел турниров</b>\n\n⬇️",
                         reply_markup=reply_tournaments_menu(message.from_user.id, is_adm))


@router.message(TextEq("👤 Профиль"))
async def txt_profile(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        u = await s.get(User, message.from_user.id)
        if u is None:
            u = User(id=message.from_user.id, username=message.from_user.username,
                     first_name=message.from_user.first_name)
            s.add(u); await s.commit()
        r = await s.execute(select(Participant).where(
            Participant.user_id == message.from_user.id, Participant.status == "winner"))
        wins = len(r.scalars().all())
        r = await s.execute(select(Admin).where(Admin.user_id == message.from_user.id))
        is_adm_db = r.scalar_one_or_none() is not None
    role = ("👑 Создатель" if message.from_user.id == OWNER_ID else
            ("🛠 Админ" if is_adm_db else "🎮 Игрок"))
    await message.answer(
        f"<b>👤 ПРОФИЛЬ</b>\n{SEP}\n🆔 <code>{message.from_user.id}</code>\n"
        f"👤 {esc(message.from_user.first_name)}\n🌐 @{message.from_user.username or '—'}\n"
        f"🎭 Роль: {role}\n🏆 Побед: <code>{wins}</code>")


@router.message(TextEq("ℹ️ Помощь"))
async def txt_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(_help_text())


@router.message(TextEq("⬅️ В главное меню"))
async def txt_back_main(message: Message, state: FSMContext):
    await state.clear()
    is_adm = await _is_admin(message.from_user.id)
    await message.answer("🏠 <b>Главное меню</b>",
                         reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(TextEq("❌ Отмена"))
async def txt_cancel_any(message: Message, state: FSMContext):
    cur = await state.get_state()
    await state.clear()
    is_adm = await _is_admin(message.from_user.id)
    prefix = "❌ Действие отменено.\n\n" if cur else ""
    await message.answer(prefix + "🏠 <b>Главное меню</b>",
                         reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(TextEq("👑 Админство"))
async def txt_adm_owner(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Владелец."); return
    await message.answer(f"<b>👑 АДМИНСТВО</b>\n{SEP}", reply_markup=owner_menu_kb())


@router.message(TextEq("🔍 Найти игрока"))
async def txt_find_player(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Только для владельца."); return
    await state.set_state(FindPlayerFSM.query)
    await message.answer(
        f"<b>🔍 ПОИСК ИГРОКА</b>\n{SEP}\n"
        "Отправь <b>Telegram ID</b> или <b>@username</b> игрока.\n\n"
        "<blockquote>Чтобы выйти — /cancel или ❌ Отмена.</blockquote>",
        reply_markup=cancel_text_kb())


@router.message(TextEq("🌐 Открытые турниры"))
async def txt_open(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as s:
        r = await s.execute(select(Tournament).where(
            Tournament.access_type == "open", Tournament.status == "registration",
            Tournament.game_type != "wheel_1v1"))
        tours = r.scalars().all()
    if not tours:
        await message.answer(f"<b>🌐 Открытые</b>\n{SEP}\n<blockquote>Нет активных.</blockquote>"); return
    lines = [f"<b>🌐 Открытые турниры</b>\n{SEP}"]; rows = []
    for t in tours:
        emoji = {"bracket": "🏆", "poker": "🃏"}.get(t.game_type, "🎡")
        extra = f" · {t.bracket_size} игр." if t.game_type == "bracket" else ""
        lines.append(f"{emoji} <b>{esc(t.name)}</b> · <i>{esc(t.prize or '—')}</i>{extra}")
        rows.append([InlineKeyboardButton(text=f"📩 Заявка · {t.name[:20]}",
                                          callback_data=f"apply_{t.id}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(TextEq("🎟 Мои турниры"))
async def txt_my(message: Message, state: FSMContext):
    await state.clear()
    rows = []; lines = [f"<b>🎟 Мои турниры</b>\n{SEP}"]; has = False; shown = set()
    async with SessionLocal() as s:
        r = await s.execute(select(Participant).where(
            Participant.user_id == message.from_user.id,
            Participant.status.in_(("approved", "winner", "pending"))))
        for p in r.scalars().all():
            t = await s.get(Tournament, p.tournament_id)
            if not t or t.id in shown: continue
            if t.status in ("finished", "cancelled"): continue
            shown.add(t.id)
            emoji = {"wheel":"🎡","wheel_1v1":"⚔️","bracket":"🏆","poker":"🃏"}.get(t.game_type, "🎡")
            sl = {"pending": "⏳ заявка", "approved": "✅ принят",
                  "winner": "🏆 победа"}.get(p.status, t.status)
            lines.append(f"  {emoji} <b>{esc(t.name)}</b> · {sl}")
            rows.append([InlineKeyboardButton(text=f"📋 {t.name[:22]}",
                                              callback_data=f"tt_player_{t.id}")])
            has = True
    if not has: lines.append("<blockquote>ℹ️ Нет активных.</blockquote>")
    await message.answer("\n".join(lines),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


@router.message(TextEq("📂 Созданные мной"))
async def txt_created(message: Message, state: FSMContext):
    await state.clear()
    if not await _is_admin(message.from_user.id):
        await message.answer("⛔ Админы."); return
    async with SessionLocal() as s:
        r = await s.execute(select(Tournament).where(
            Tournament.creator_id == message.from_user.id,
            Tournament.status.in_(("registration", "running", "paused"))))
        tours = r.scalars().all()
    if not tours:
        await message.answer(f"<b>📂 Созданные</b>\n{SEP}\n<blockquote>Нет активных.</blockquote>"); return
    lines = [f"<b>📂 Созданные мной</b>\n{SEP}"]; rows = []
    for t in tours:
        emoji = {"wheel":"🎡","wheel_1v1":"⚔️","bracket":"🏆","poker":"🃏"}.get(t.game_type, "🎡")
        lines.append(f"{emoji} <b>{esc(t.name)}</b> · <code>{t.status}</code>")
        rows.append([InlineKeyboardButton(text=f"⚙️ {t.name[:22]}",
                                          callback_data=f"tt_manage_{t.id}")])
    lines.append(f"\n<i>Активных: {len(tours)}/{MAX_ACTIVE_TOURNAMENTS_PER_ADMIN}</i>")
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(TextEq("📊 Все активные турниры"))
async def txt_all_active(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Владелец."); return
    async with SessionLocal() as s:
        r = await s.execute(select(Tournament).where(
            Tournament.status.in_(("registration", "running", "paused"))))
        tours = r.scalars().all()
    if not tours:
        await message.answer(f"<b>📊 Все активные</b>\n{SEP}\n<blockquote>Нет.</blockquote>"); return
    lines = [f"<b>📊 Все активные</b>\n{SEP}"]; rows = []
    for t in tours:
        emoji = {"wheel":"🎡","wheel_1v1":"⚔️","bracket":"🏆","poker":"🃏"}.get(t.game_type, "🎡")
        lock = "🟢" if t.access_type == "open" else "🔴"
        lines.append(f"{lock}{emoji} <b>{esc(t.name)}</b> · <code>{t.status}</code>")
        rows.append([InlineKeyboardButton(text=f"⚙️ {t.name[:22]}",
                                          callback_data=f"tt_manage_{t.id}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(TextEq("📋 Список админов"))
async def txt_list_admins(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Владелец."); return
    async with SessionLocal() as s:
        r = await s.execute(select(Admin)); admins = r.scalars().all()
    if not admins:
        await message.answer(f"<b>📋 Админы</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"); return
    lines = [f"<b>📋 Админы</b>\n{SEP}"]
    for a in admins: lines.append(f"🔹 <code>{a.user_id}</code> · {a.added_at:%d.%m.%Y}")
    await message.answer("\n".join(lines))


@router.message(TextEq("🚫 Черный список"))
async def txt_blacklist(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Владелец."); return
    async with SessionLocal() as s:
        r = await s.execute(select(Ban)); bans = r.scalars().all()
    if not bans:
        await message.answer(f"<b>📜 Чёрный список</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"); return
    lines = [f"<b>📜 Чёрный список</b>\n{SEP}"]
    for b in bans: lines.append(f"🚫 <code>{b.user_id}</code> · {b.banned_at:%d.%m.%Y}")
    await message.answer("\n".join(lines))


# ============ 3. OWNER CALLBACKS ============
@router.callback_query(F.data == "owner_back")
async def cb_owner_back(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    await state.clear()
    await _safe_edit(cb, f"<b>👑 ВЛАДЕЛЕЦ</b>\n{SEP}", reply_markup=owner_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "owner_add_admin")
async def cb_add_admin(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    await state.set_state(OwnerForm.add_admin)
    await _safe_edit(cb, f"<b>➕ Админ</b>\n{SEP}\nID или @username:",
                     reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.add_admin, TextNotMenu())
async def msg_add_admin(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    raw = message.text.strip(); target_id = None
    async with SessionLocal() as s:
        if raw.startswith("@"):
            r = await s.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if u: target_id = u.id
        else:
            try: target_id = int(raw)
            except ValueError: pass
        if target_id is None:
            await message.answer("<blockquote>❌ Не найден.</blockquote>"); return
        r = await s.execute(select(Admin).where(Admin.user_id == target_id))
        if r.scalar_one_or_none():
            await message.answer("<blockquote>ℹ️ Уже админ.</blockquote>"); return
        s.add(Admin(user_id=target_id)); await s.commit()
    await state.clear()
    await message.answer(f"<b>✅ Добавлен</b>\n🆔 <code>{target_id}</code>", reply_markup=owner_menu_kb())


@router.callback_query(F.data == "owner_del_admin")
async def cb_del_admin(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    async with SessionLocal() as s:
        r = await s.execute(select(Admin)); admins = r.scalars().all()
    if not admins:
        await _safe_edit(cb, "<blockquote>Пусто.</blockquote>", reply_markup=back_kb("owner_back"))
        await cb.answer(); return
    rows = [[InlineKeyboardButton(text=f"❌ {a.user_id}", callback_data=f"owner_rm_{a.user_id}")] for a in admins]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="owner_back")])
    await _safe_edit(cb, f"<b>➖ Удалить</b>\n{SEP}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("owner_rm_"))
async def cb_rm_admin(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    uid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        r = await s.execute(select(Admin).where(Admin.user_id == uid))
        a = r.scalar_one_or_none()
        if a: await s.delete(a); await s.commit()
    await cb.answer("Удалён"); await cb_del_admin(cb)


@router.callback_query(F.data == "owner_list_admins")
async def cb_list_admins(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    async with SessionLocal() as s:
        r = await s.execute(select(Admin)); admins = r.scalars().all()
    if not admins: text = f"<b>📋</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"
    else:
        lines = [f"<b>📋 Админы</b>\n{SEP}"]
        for a in admins: lines.append(f"🔹 <code>{a.user_id}</code> · {a.added_at:%d.%m.%Y}")
        text = "\n".join(lines)
    await _safe_edit(cb, text, reply_markup=back_kb("owner_back")); await cb.answer()


@router.callback_query(F.data == "owner_ban")
async def cb_ban(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    await state.set_state(OwnerForm.ban_id)
    await _safe_edit(cb, f"<b>🚫 Забанить</b>\n{SEP}\nID:", reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.ban_id, TextNotMenu())
async def msg_ban(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    try: uid = int(message.text.strip())
    except ValueError: await message.answer("<blockquote>❌ ID.</blockquote>"); return
    async with SessionLocal() as s:
        if await s.get(Ban, uid) is None:
            s.add(Ban(user_id=uid)); await s.commit()
    await state.clear()
    try: await message.bot.send_message(uid, "<blockquote>⛔ Заблокирован!</blockquote>")
    except Exception: pass
    await message.answer(f"<b>✅ Забанен</b>\n🆔 <code>{uid}</code>", reply_markup=owner_menu_kb())


@router.callback_query(F.data == "owner_unban")
async def cb_unban(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    await state.set_state(OwnerForm.unban_id)
    await _safe_edit(cb, f"<b>✅ Разбанить</b>\n{SEP}\nID:", reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.unban_id, TextNotMenu())
async def msg_unban(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    try: uid = int(message.text.strip())
    except ValueError: await message.answer("<blockquote>❌ ID.</blockquote>"); return
    async with SessionLocal() as s:
        b = await s.get(Ban, uid)
        if b: await s.delete(b); await s.commit()
    await state.clear()
    await message.answer(f"<b>✅ Разбанен</b>\n🆔 <code>{uid}</code>", reply_markup=owner_menu_kb())


@router.callback_query(F.data == "owner_blacklist")
async def cb_blacklist(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    async with SessionLocal() as s:
        r = await s.execute(select(Ban)); bans = r.scalars().all()
    if not bans: text = f"<b>📜</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"
    else:
        lines = [f"<b>📜 Чёрный список</b>\n{SEP}"]
        for b in bans: lines.append(f"🚫 <code>{b.user_id}</code> · {b.banned_at:%d.%m.%Y}")
        text = "\n".join(lines)
    await _safe_edit(cb, text, reply_markup=back_kb("owner_back")); await cb.answer()


@router.callback_query(F.data == "admin_cancel_fsm")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    try: await state.clear()
    except: pass
    is_adm = await _is_admin(cb.from_user.id)
    await _safe_edit(cb, "❌ Отменено.\n\n<i>Возврат в меню...</i>")
    try:
        await cb.bot.send_message(cb.from_user.id, "🏠 <b>Главное меню</b>",
                                  reply_markup=reply_main_menu(cb.from_user.id, is_adm))
    except: pass
    await cb.answer()


# ============ 4. FIND PLAYER FSM ============
@router.message(FindPlayerFSM.query, TextNotMenu())
async def msg_find_player(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    raw = message.text.strip()
    target_id = None
    async with SessionLocal() as s:
        if raw.startswith("@"):
            r = await s.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if u: target_id = u.id
        else:
            try: target_id = int(raw)
            except ValueError: target_id = None
        if target_id is None:
            await message.answer("<blockquote>❌ Не найден.</blockquote>"); return
        u = await s.get(User, target_id)
        if not u:
            await message.answer("<blockquote>❌ Нет в базе.</blockquote>"); return
        wins = (await s.execute(select(func.count()).select_from(Participant)
            .where(Participant.user_id == target_id,
                   Participant.status == "winner"))).scalar() or 0
        total_participated = (await s.execute(select(func.count()).select_from(Participant)
            .where(Participant.user_id == target_id,
                   Participant.status.in_(("approved", "winner"))))).scalar() or 0
        is_adm_db = (await s.execute(select(Admin).where(
            Admin.user_id == target_id))).scalar_one_or_none() is not None
        is_banned = (await s.get(Ban, target_id)) is not None
    role = ("👑 Создатель" if target_id == OWNER_ID else
            ("🛠 Администратор" if is_adm_db else "🎮 Игрок"))
    ban_label = "🚫 ЗАБАНЕН" if is_banned else "✅ Активен"
    uname = f"@{u.username}" if u.username else "—"
    lines = ["<b>🔍 ПРОФИЛЬ ИГРОКА</b>", SEP,
             f"🆔 <b>ID:</b> <code>{target_id}</code>",
             f"👤 <b>Имя:</b> {esc(u.first_name or '—')}",
             f"🌐 <b>Username:</b> {uname}",
             f"🎭 <b>Роль:</b> {role}",
             f"📊 <b>Статус:</b> {ban_label}",
             SEP, f"🏆 <b>Побед:</b> <code>{wins}</code>",
             f"🎮 <b>Участий:</b> <code>{total_participated}</code>",
             f"📅 <b>В боте с:</b> {u.created_at:%d.%m.%Y}",
             "", "<blockquote>Введи ещё или /cancel</blockquote>"]
    await message.answer("\n".join(lines))


# ============ 5. FSM: СОЗДАНИЕ ТУРНИРА ============
@router.message(TextEq("🆕 Создать турнир"))
async def txt_create(message: Message, state: FSMContext):
    await state.clear()
    if not await _is_admin(message.from_user.id):
        await message.answer("⛔ Только админ — @Dimo0nchik"); return
    active_count = await _active_tournaments_count(message.from_user.id)
    if active_count >= MAX_ACTIVE_TOURNAMENTS_PER_ADMIN:
        await message.answer(
            f"<blockquote>⛔ У тебя уже <b>{active_count}</b> активных турниров "
            f"(максимум {MAX_ACTIVE_TOURNAMENTS_PER_ADMIN}).</blockquote>")
        return
    await state.set_state(TournamentFSM.access_type)
    await message.answer(
        f"<b>🆕 Шаг 1</b>\n{SEP}\nДоступ:\n"
        f"<i>(Активных турниров: {active_count}/{MAX_ACTIVE_TOURNAMENTS_PER_ADMIN})</i>\n\n"
        "<blockquote>Нажми «❌ Отмена» в любой момент, чтобы прервать.</blockquote>",
        reply_markup=access_type_kb())


@router.callback_query(F.data == "tt_access_open")
async def tt_open(cb: CallbackQuery, state: FSMContext):
    await state.update_data(access_type="open"); await state.set_state(TournamentFSM.game_type)
    await _safe_edit(cb, f"<b>🆕 Шаг 2</b>\n{SEP}\nИгра:", reply_markup=game_type_kb()); await cb.answer()


@router.callback_query(F.data == "tt_access_closed")
async def tt_closed(cb: CallbackQuery, state: FSMContext):
    await state.update_data(access_type="closed"); await state.set_state(TournamentFSM.game_type)
    await _safe_edit(cb, f"<b>🆕 Шаг 2</b>\n{SEP}\nИгра:", reply_markup=game_type_kb()); await cb.answer()


@router.callback_query(F.data == "tt_game_wheel")
async def tt_game_wheel(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TournamentFSM.wheel_subtype)
    await _safe_edit(cb,
        f"<b>🎡 Колесо Фортуны · Шаг 3</b>\n{SEP}\nВыбери <b>подтип</b>:",
        reply_markup=wheel_subtype_kb())
    await cb.answer()


@router.callback_query(F.data == "tt_wheel_general")
async def tt_wheel_general(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="wheel")
    await state.set_state(TournamentFSM.name)
    await _safe_edit(cb, f"<b>🎡 Колесо (общее) · Шаг 4</b>\n{SEP}\n"
                         "Отправь <b>название турнира</b>:\n\n"
                         "<blockquote>❌ Отмена — отменить.</blockquote>",
                     reply_markup=cancel_text_kb())
    await cb.answer()


@router.callback_query(F.data == "tt_wheel_1v1")
async def tt_wheel_1v1(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="wheel_1v1", access_type="closed")
    await state.set_state(TournamentFSM.name)
    await _safe_edit(cb, f"<b>⚔️ Колесо 1×1 · Шаг 4</b>\n{SEP}\n"
                         "Отправь <b>название турнира</b>:\n\n"
                         "<blockquote>❌ Отмена — отменить.</blockquote>",
                     reply_markup=cancel_text_kb())
    await cb.answer()


@router.callback_query(F.data == "tt_game_bracket")
async def tt_bracket(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="bracket")
    await state.set_state(TournamentFSM.name)
    await _safe_edit(cb, f"<b>🏆 Турнир · Шаг 3</b>\n{SEP}\n"
                         "Отправь <b>название турнира</b>:\n\n"
                         "<blockquote>❌ Отмена — отменить.</blockquote>",
                     reply_markup=cancel_text_kb())
    await cb.answer()


@router.callback_query(F.data == "tt_game_poker")
async def tt_poker(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="poker")
    await state.set_state(TournamentFSM.name)
    await _safe_edit(cb, f"<b>🃏 Покер · Шаг 3</b>\n{SEP}\n"
                         "Отправь <b>название турнира</b>:\n\n"
                         "<blockquote>❌ Отмена — отменить.</blockquote>",
                     reply_markup=cancel_text_kb())
    await cb.answer()


@router.message(TournamentFSM.name, TextNotMenu())
async def tt_name(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    await state.update_data(name=message.text.strip()[:100])
    await state.set_state(TournamentFSM.prize)
    await message.answer(f"<b>🆕 Шаг 4</b>\n{SEP}\nОтправь <b>приз</b>:\n\n"
                         "<blockquote>❌ Отмена — отменить.</blockquote>",
                         reply_markup=cancel_text_kb())


@router.message(TournamentFSM.prize, TextNotMenu())
async def tt_prize(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    await state.update_data(prize=message.text.strip()[:200])
    data = await state.get_data()
    gt = data["game_type"]
    if gt == "wheel_1v1":
        await state.set_state(TournamentFSM.w1v1_p1)
        await message.answer(f"<b>⚔️ Колесо 1×1 · Шаг 5</b>\n{SEP}\n"
                             "Введи <b>@username</b> или ID <b>первого</b> игрока:\n\n"
                             "<blockquote>❌ Отмена — отменить.</blockquote>",
                             reply_markup=cancel_text_kb())
    elif gt == "bracket":
        await state.set_state(TournamentFSM.bracket_size)
        await message.answer(f"<b>🏆 Турнир · Шаг 5</b>\n{SEP}\n"
                             "Выбери <b>размер сетки</b>:",
                             reply_markup=bracket_size_kb())
    elif gt == "poker":
        await state.set_state(TournamentFSM.pk_stack)
        await message.answer(f"<b>🃏 Покер · Шаг 5/8</b>\n{SEP}\n"
                             "Выбери <b>стартовый стек</b> фишек:",
                             reply_markup=poker_stack_kb())
    else:
        await _show_confirm(message, state)


# ---------- POKER FSM ----------
@router.callback_query(F.data.startswith("tt_pk_stack_"))
async def tt_pk_stack(cb: CallbackQuery, state: FSMContext):
    try: stack = int(cb.data.split("_")[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if stack not in POKER_STACKS:
        await cb.answer("Неверный стек", show_alert=True); return
    await state.update_data(pk_stack=stack)
    await state.set_state(TournamentFSM.pk_blinds)
    await _safe_edit(cb, f"<b>🃏 Покер · Шаг 6/8</b>\n{SEP}\n"
                         f"Стек: <b>{stack}</b>\n\nВыбери <b>блайнды</b> (SB / BB):",
                     reply_markup=poker_blinds_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("tt_pk_blind_"))
async def tt_pk_blinds(cb: CallbackQuery, state: FSMContext):
    try:
        parts = cb.data.split("_")
        sb = int(parts[-2]); bb = int(parts[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if (sb, bb) not in POKER_BLINDS:
        await cb.answer("Неверные блайнды", show_alert=True); return
    await state.update_data(pk_sb=sb, pk_bb=bb)
    await state.set_state(TournamentFSM.pk_time)
    await _safe_edit(cb, f"<b>🃏 Покер · Шаг 7/8</b>\n{SEP}\n"
                         f"Блайнды: <b>{sb} / {bb}</b>\n\n"
                         "Выбери <b>время на ход</b>:",
                     reply_markup=poker_time_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("tt_pk_time_"))
async def tt_pk_time(cb: CallbackQuery, state: FSMContext):
    try: t = int(cb.data.split("_")[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if t not in POKER_TIMES:
        await cb.answer("Неверное время", show_alert=True); return
    await state.update_data(pk_time=t)
    await state.set_state(TournamentFSM.pk_seats)
    await _safe_edit(cb, f"<b>🃏 Покер · Шаг 8/8</b>\n{SEP}\n"
                         f"Время на ход: <b>{t} сек</b>\n\n"
                         "Выбери <b>макс. мест за столом</b>:",
                     reply_markup=poker_seats_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("tt_pk_seats_"))
async def tt_pk_seats(cb: CallbackQuery, state: FSMContext):
    try: n = int(cb.data.split("_")[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if n not in POKER_MAX_SEATS:
        await cb.answer("Неверное значение", show_alert=True); return
    await state.update_data(pk_seats=n)
    await state.set_state(TournamentFSM.confirm)
    await _show_confirm_from_cb(cb, state)
    await cb.answer()


# ---------- BRACKET / WHEEL 1v1 ----------
@router.message(TournamentFSM.w1v1_p1, TextNotMenu())
async def tt_w1v1_p1(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    uid = await _resolve_uid(message.text.strip())
    if not uid:
        await message.answer("<blockquote>❌ ID или @username.</blockquote>"); return
    await state.update_data(w1v1_p1_id=uid)
    await state.set_state(TournamentFSM.w1v1_p2)
    await message.answer(f"<b>⚔️ Шаг 6</b>\n{SEP}\nВведи ID/username <b>второго</b> игрока:\n\n"
                         "<blockquote>❌ Отмена — отменить.</blockquote>",
                         reply_markup=cancel_text_kb())


@router.message(TournamentFSM.w1v1_p2, TextNotMenu())
async def tt_w1v1_p2(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    uid = await _resolve_uid(message.text.strip())
    if not uid:
        await message.answer("<blockquote>❌ ID или @username.</blockquote>"); return
    data = await state.get_data()
    if uid == data.get("w1v1_p1_id"):
        await message.answer("<blockquote>❌ Игрок должен отличаться.</blockquote>"); return
    await state.update_data(w1v1_p2_id=uid)
    await state.set_state(TournamentFSM.w1v1_series)
    await message.answer(
        f"<b>⚔️ Шаг 7</b>\n{SEP}\n"
        "До скольких <b>побед</b> играем серию?\n\n"
        "<blockquote>Кто первым наберёт N побед, тот и победил в дуэли.</blockquote>",
        reply_markup=series_kb("tt_w1v1_series"))


@router.callback_query(F.data.startswith("tt_w1v1_series_"))
async def tt_w1v1_series(cb: CallbackQuery, state: FSMContext):
    try: n = int(cb.data.split("_")[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if n not in VALID_SERIES_TARGETS:
        await cb.answer("Неверное значение", show_alert=True); return
    await state.update_data(w1v1_series=n)
    await state.set_state(TournamentFSM.confirm)
    await _show_confirm_from_cb(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("tt_bracket_"))
async def tt_bracket_size(cb: CallbackQuery, state: FSMContext):
    try: n = int(cb.data.split("_")[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if n not in VALID_BRACKET_SIZES:
        await cb.answer("Неверный размер", show_alert=True); return
    await state.update_data(bracket_size=n, bracket_players=[], bracket_step=0)
    await state.set_state(TournamentFSM.bracket_series)
    await _safe_edit(cb,
        f"<b>🏆 Шаг 6</b>\n{SEP}\n"
        "До скольких <b>побед</b> играем каждый бой?\n\n"
        "<blockquote>Кто первым наберёт N побед в серии, тот проходит дальше.</blockquote>",
        reply_markup=series_kb("tt_series"))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_series_"))
async def tt_bracket_series(cb: CallbackQuery, state: FSMContext):
    try: n = int(cb.data.split("_")[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if n not in VALID_SERIES_TARGETS:
        await cb.answer("Неверное значение", show_alert=True); return
    await state.update_data(bracket_series=n)
    data = await state.get_data()
    if data.get("access_type") == "open":
        await state.set_state(TournamentFSM.confirm)
        await _show_confirm_from_cb(cb, state)
    else:
        size = data.get("bracket_size", 4)
        await state.set_state(TournamentFSM.bracket_players)
        await _safe_edit(cb,
            f"<b>🏆 Шаг 7</b>\n{SEP}\n"
            f"Введи <b>@username</b> или ID <b>игрока 1 из {size}</b>:\n\n"
            "<blockquote>❌ Отмена — отменить.</blockquote>")
        try:
            await cb.bot.send_message(cb.from_user.id, "↓ Введи игрока в чат ↓",
                                      reply_markup=cancel_text_kb())
        except: pass
    await cb.answer()


@router.message(TournamentFSM.bracket_players, TextNotMenu())
async def tt_bracket_player(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    uid = await _resolve_uid(message.text.strip())
    if not uid:
        await message.answer("<blockquote>❌ ID или @username.</blockquote>"); return
    data = await state.get_data()
    players = data.get("bracket_players", [])
    size = data.get("bracket_size", 4)
    if uid in players:
        await message.answer("<blockquote>❌ Уже добавлен.</blockquote>"); return
    players.append(uid)
    await state.update_data(bracket_players=players)
    if len(players) < size:
        await message.answer(f"<b>🏆 Шаг 7</b>\n{SEP}\n"
                             f"Введи ID/username <b>игрока {len(players) + 1} из {size}</b>:\n\n"
                             "<blockquote>❌ Отмена — отменить.</blockquote>",
                             reply_markup=cancel_text_kb())
    else:
        await message.answer("✅ Все игроки добавлены.")
        await _show_confirm(message, state)


async def _build_confirm_text(state: FSMContext) -> str:
    data = await state.get_data()
    game_label = {
        "wheel": "🎡 Колесо Фортуны (общее)",
        "wheel_1v1": "⚔️ Колесо 1×1",
        "bracket": "🏆 Турнир",
        "poker": "🃏 Покер (Texas Hold'em)",
    }.get(data["game_type"], "?")
    lines = ["<b>✅ ПОДТВЕРЖДЕНИЕ</b>", SEP,
             f"<b>🏆 Название:</b> <code>{esc(data['name'])}</code>",
             f"<b>🎁 Приз:</b> <i>{esc(data.get('prize','—'))}</i>",
             f"<b>Игра:</b> {game_label}"]
    if data["game_type"] != "wheel_1v1":
        acc = "🟢 Открытый" if data.get("access_type") == "open" else "🔴 Закрытый"
        lines.append(f"<b>Доступ:</b> {acc}")
    if data["game_type"] == "wheel_1v1":
        async with SessionLocal() as s:
            u1 = await s.get(User, data["w1v1_p1_id"])
            u2 = await s.get(User, data["w1v1_p2_id"])
        lines += [f"🔹 Игрок 1: <code>{esc(_display_name(u1, data['w1v1_p1_id']))}</code>",
                  f"🔹 Игрок 2: <code>{esc(_display_name(u2, data['w1v1_p2_id']))}</code>",
                  f"🔹 Серия: <b>до {data.get('w1v1_series', 1)} побед</b>"]
    elif data["game_type"] == "bracket":
        size = data.get("bracket_size", 4)
        series = data.get("bracket_series", 3)
        players = data.get("bracket_players", [])
        is_open = data.get("access_type") == "open"
        lines += [f"🔹 Размер сетки: <code>{size}</code>",
                  f"🔹 Формат боя: <b>до {series} побед</b>"]
        if is_open:
            lines.append("🔹 Игроки: <i>набираются через «Открытые турниры»</i>")
        else:
            async with SessionLocal() as s:
                names = []
                for uid in players:
                    u = await s.get(User, uid)
                    names.append(_display_name(u, uid))
            lines.append(f"🔹 Игроки: {', '.join(esc(n) for n in names)}")
    elif data["game_type"] == "poker":
        lines += [
            "🔹 Формат: <b>No-Limit Texas Hold'em</b>",
            f"🔹 Стартовый стек: <b>{data.get('pk_stack', 1000)}</b>",
            f"🔹 Блайнды: <b>{data.get('pk_sb', 10)} / {data.get('pk_bb', 20)}</b>",
            f"🔹 Время на ход: <b>{data.get('pk_time', 20)} сек</b>",
            f"🔹 Макс. мест за столом: <b>{data.get('pk_seats', 7)}</b>",
            f"🔹 Игроков всего: <b>2 – 49</b> (несколько столов + автослияние)",
            "\n<blockquote>Игроки сами выбирают стол в лобби (3 мин на выбор).</blockquote>",
        ]
    else:
        lines.append("\n<blockquote>Нажми ✅ для создания.</blockquote>")
    return "\n".join(lines)


async def _show_confirm(message: Message, state: FSMContext):
    await state.set_state(TournamentFSM.confirm)
    text = await _build_confirm_text(state)
    await message.answer(text, reply_markup=confirm_kb())


async def _show_confirm_from_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TournamentFSM.confirm)
    text = await _build_confirm_text(state)
    if not await _safe_edit(cb, text, reply_markup=confirm_kb()):
        try: await cb.bot.send_message(cb.from_user.id, text, reply_markup=confirm_kb())
        except Exception: pass


@router.callback_query(F.data == "tt_confirm")
async def tt_confirm(cb: CallbackQuery, state: FSMContext):
    active_count = await _active_tournaments_count(cb.from_user.id)
    if active_count >= MAX_ACTIVE_TOURNAMENTS_PER_ADMIN:
        await state.clear()
        await cb.answer("Лимит активных турниров", show_alert=True)
        await _safe_edit(cb,
            f"<blockquote>⛔ У тебя уже {active_count} активных турниров. "
            f"Максимум — {MAX_ACTIVE_TOURNAMENTS_PER_ADMIN}.</blockquote>")
        return

    data = await state.get_data()
    gt = data["game_type"]

    if gt == "wheel_1v1":
        p1 = data["w1v1_p1_id"]; p2 = data["w1v1_p2_id"]
        series = int(data.get("w1v1_series", 1))
        async with SessionLocal() as s:
            t = Tournament(name=data["name"], access_type="closed",
                           game_type="wheel_1v1", creator_id=cb.from_user.id,
                           start_stack=0, turn_time=0, blind_interval=0,
                           series_target=series, prize=data.get("prize"), status="registration")
            s.add(t); await s.commit(); await s.refresh(t); tid = t.id
            s.add(Participant(tournament_id=tid, user_id=p1, status="pending"))
            s.add(Participant(tournament_id=tid, user_id=p2, status="pending"))
            await s.commit()
        await state.clear(); await cb.answer(f"⚔️ Приглашения (до {series} побед)")
        for uid in (p1, p2):
            if is_bot(uid): continue
            try:
                await cb.bot.send_message(uid,
                    f"<b>⚔️ Колесо 1×1 — вызов!</b>\n{SEP}\n"
                    f"🏆 <code>{esc(data['name'])}</code>\n"
                    f"🎁 Приз: <i>{esc(data.get('prize') or '—')}</i>\n"
                    f"🥇 Серия: <b>до {series} побед</b>\n\n"
                    "<blockquote>Принять?</blockquote>",
                    reply_markup=invite_response_kb(tid))
            except Exception as e:
                logging.warning("1v1 invite fail uid=%s: %s", uid, e)
        await _show_tournament_menu(cb, tid); return

    if gt == "bracket":
        size = data.get("bracket_size", 4)
        series = int(data.get("bracket_series", 3))
        access = data.get("access_type", "closed")
        players = data.get("bracket_players", [])
        if access == "closed" and len(players) != size:
            await cb.answer("Не хватает игроков", show_alert=True); return
        async with SessionLocal() as s:
            t = Tournament(name=data["name"], access_type=access,
                           game_type="bracket", creator_id=cb.from_user.id,
                           start_stack=0, turn_time=0, blind_interval=0,
                           bracket_size=size, series_target=series,
                           prize=data.get("prize"), status="registration")
            s.add(t); await s.commit(); await s.refresh(t); tid = t.id
            if access == "closed":
                for uid in players:
                    s.add(Participant(tournament_id=tid, user_id=uid, status="pending"))
            await s.commit()
        await state.clear()
        if access == "closed":
            await cb.answer(f"🏆 Приглашения ({size}, до {series})")
            for uid in players:
                if is_bot(uid): continue
                try:
                    await cb.bot.send_message(uid,
                        f"<b>🏆 Турнир — приглашение!</b>\n{SEP}\n"
                        f"🏆 <code>{esc(data['name'])}</code>\n"
                        f"🎁 Приз: <i>{esc(data.get('prize') or '—')}</i>\n\n"
                        f"<blockquote>Сетка: {size}. Серия до {series} побед.\n"
                        "Принять?</blockquote>",
                        reply_markup=invite_response_kb(tid))
                except Exception as e:
                    logging.warning("bracket invite fail uid=%s: %s", uid, e)
        else:
            await cb.answer(f"🏆 Открытый ({size}, до {series})")
        await _show_tournament_menu(cb, tid); return

    if gt == "poker":
        access = data.get("access_type", "open")
        stack = int(data.get("pk_stack", 1000))
        sb = int(data.get("pk_sb", 10))
        bb = int(data.get("pk_bb", 20))
        ttime = int(data.get("pk_time", 20))
        seats = int(data.get("pk_seats", 7))
        async with SessionLocal() as s:
            t = Tournament(name=data["name"], access_type=access,
                           game_type="poker", creator_id=cb.from_user.id,
                           start_stack=stack, turn_time=ttime, blind_interval=sb,
                           series_target=seats,
                           prize=data.get("prize"), status="registration")
            s.add(t); await s.commit(); await s.refresh(t); tid = t.id
        await state.clear()
        await cb.answer("🃏 Покер создан!")
        await _show_tournament_menu(cb, tid); return

    async with SessionLocal() as s:
        t = Tournament(name=data["name"], access_type=data["access_type"],
                       game_type=gt, creator_id=cb.from_user.id,
                       start_stack=1000, turn_time=20, blind_interval=5,
                       series_target=3, prize=data.get("prize"))
        s.add(t); await s.commit(); await s.refresh(t); tid = t.id
    await state.clear(); await cb.answer("Создан!")
    await _show_tournament_menu(cb, tid)


# ============ 6. УПРАВЛЕНИЕ ТУРНИРОМ ============
async def _show_tournament_menu(cb: CallbackQuery, tid: int):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
        parts = r.scalars().all()
    approved = [p for p in parts if p.status in ("approved", "winner")]
    pending_count = len([p for p in parts if p.status == "pending"])
    bot_count = len([p for p in parts if is_bot(p.user_id)])
    gt = {"wheel": "🎡 Колесо", "wheel_1v1": "⚔️ Колесо 1×1",
          "bracket": "🏆 Турнир", "poker": "🃏 Покер"}.get(t.game_type, "?")
    status_label = t.status
    if t.game_type == "wheel_1v1" and t.status == "registration":
        status_label = f"ожидание приглашений ({len(approved)}/2)"
    if t.game_type == "bracket" and t.status == "registration":
        status_label = f"ожидание игроков ({len(approved)}/{t.bracket_size})"
    if t.game_type == "poker" and t.status == "registration":
        status_label = f"ожидание игроков ({len(approved)}/49)"
    total_needed = t.bracket_size if t.game_type == "bracket" else \
                   (2 if t.game_type == "wheel_1v1" else (49 if t.game_type == "poker" else None))
    total_label = f" / {total_needed}" if total_needed else ""
    text = (f"<b>🏆 ТУРНИР:</b> <code>{esc(t.name)}</code>\n{SEP}\n"
            f"<b>ID:</b> <code>{t.id}</code>\n"
            f"<b>Игра:</b> {gt}\n"
            f"<b>Доступ:</b> {'🟢 Открытый' if t.access_type=='open' else '🔴 Закрытый'}\n"
            f"<b>Статус:</b> <code>{status_label}</code>\n"
            f"<b>Участников:</b> <code>{len(approved)}{total_label}</code>")
    if t.game_type in ("bracket", "wheel_1v1"):
        text += f"\n<b>Формат боя:</b> до <code>{getattr(t, 'series_target', 1)}</code> побед"
    if t.game_type == "poker":
        text += (f"\n<b>Стек:</b> <code>{t.start_stack}</code>"
                 f" · <b>Время:</b> <code>{t.turn_time}с</code>"
                 f" · <b>Мест за столом:</b> <code>{t.series_target}</code>"
                 f" · <b>Блайнды:</b> <code>{t.blind_interval}/{t.blind_interval*2}</code>")
    if bot_count > 0:
        text += f"\n<b>🤖 Ботов:</b> <code>{bot_count}</code>"
    if pending_count > 0:
        text += f"\n<b>⏳ Ожидают подтверждения:</b> <code>{pending_count}</code>"
    text += f"\n<b>🎁 Приз:</b> <i>{esc(t.prize or '—')}</i>"
    kb = manage_tournament_kb(t.id, t.status, t.game_type, t.access_type)
    if not await _safe_edit(cb, text, reply_markup=kb):
        try: await cb.bot.send_message(cb.from_user.id, text, reply_markup=kb)
        except Exception: pass


async def _show_player_tournament_menu(cb: CallbackQuery, tid: int):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
        parts = r.scalars().all()
    approved = [p for p in parts if p.status in ("approved", "winner")]
    gt = {"wheel": "🎡 Колесо", "wheel_1v1": "⚔️ Колесо 1×1",
          "bracket": "🏆 Турнир", "poker": "🃏 Покер"}.get(t.game_type, "?")
    text = (f"<b>🏆 ТУРНИР:</b> <code>{esc(t.name)}</code>\n{SEP}\n"
            f"<b>Игра:</b> {gt}\n"
            f"<b>Статус:</b> <code>{t.status}</code>\n"
            f"<b>Участников:</b> <code>{len(approved)}</code>\n"
            f"<b>🎁 Приз:</b> <i>{esc(t.prize or '—')}</i>")
    kb = player_tournament_kb(t.id, t.status)
    if not await _safe_edit(cb, text, reply_markup=kb):
        try: await cb.bot.send_message(cb.from_user.id, text, reply_markup=kb)
        except Exception: pass


@router.callback_query(F.data.startswith("tt_addbot_"))
async def cb_addbot(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    ok, name, err = await _add_bot_to_tournament(tid)
    if not ok:
        await cb.answer(err or "Ошибка", show_alert=True); return
    await cb.answer(f"🤖 {name} добавлен", show_alert=True)
    await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_delbot_"))
async def cb_delbot(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    ok, name, err = await _remove_last_bot_from_tournament(tid)
    if not ok:
        await cb.answer(err or "Ошибка", show_alert=True); return
    await cb.answer(f"🗑 {name} удалён", show_alert=True)
    await _show_tournament_menu(cb, tid)


@router.callback_query(F.data == "admin_manage_tournaments")
async def cb_manage(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    async with SessionLocal() as s:
        r = await s.execute(select(Tournament).where(
            Tournament.status.in_(("registration", "running", "paused"))
        ).order_by(Tournament.id.desc()).limit(20))
        tours = r.scalars().all()
    if not tours: await cb.answer("Нет активных", show_alert=True); return
    rows = []
    for t in tours:
        emoji = {"wheel":"🎡","wheel_1v1":"⚔️","bracket":"🏆","poker":"🃏"}.get(t.game_type, "🎡")
        lock = "🟢" if t.access_type == "open" else "🔴"
        rows.append([InlineKeyboardButton(text=f"{emoji}{lock} {t.name} · {t.status}",
                                          callback_data=f"tt_manage_{t.id}")])
    await _safe_edit(cb, f"<b>📂</b>\n{SEP}",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_manage_"))
async def cb_manage_one(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    if not await _is_admin(cb.from_user.id):
        await _show_player_tournament_menu(cb, tid)
    else:
        await _show_tournament_menu(cb, tid)
    await cb.answer()


@router.callback_query(F.data.startswith("tt_player_"))
async def cb_player_view(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    await _show_player_tournament_menu(cb, tid); await cb.answer()


@router.callback_query(F.data.startswith("tt_spin_"))
async def cb_spin_wheel(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        gtype = t.game_type
    if gtype == "wheel_1v1":
        await cb.answer("Серия стартует автоматически", show_alert=True); return
    g = gm.get_wheel(tid)
    if not g: await cb.answer("Колесо не активно", show_alert=True); return
    if g.finished: await cb.answer("Завершён", show_alert=True); return
    if len(g.active) < 2: await cb.answer("Мало игроков", show_alert=True); return
    g.spin(); await gm.broadcast(tid, g.to_dict())
    await cb.answer("🎡 Крутится!")


@router.callback_query(F.data.startswith("tt_join_"))
async def cb_join_tournament(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        if t.game_type == "wheel_1v1":
            await cb.answer("Закрытый турнир", show_alert=True); return
        if t.game_type == "bracket" and t.access_type != "open":
            await cb.answer("Закрытый турнир", show_alert=True); return
        if t.game_type == "bracket":
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.status.in_(("approved", "winner"))))
            if len(r.scalars().all()) >= t.bracket_size:
                await cb.answer("Мест нет", show_alert=True); return
        if t.game_type == "poker":
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.status.in_(("approved", "winner"))))
            if len(r.scalars().all()) >= 49:
                await cb.answer("Стол полон (49/49)", show_alert=True); return
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        existing = r.scalar_one_or_none()
        if existing and existing.status in ("approved", "winner"):
            await cb.answer("Уже в турнире", show_alert=True); return
        if existing:
            existing.status = "approved"; existing.chips = t.start_stack
        else:
            s.add(Participant(tournament_id=tid, user_id=cb.from_user.id,
                              status="approved", chips=t.start_stack))
        u = await s.get(User, cb.from_user.id)
        if u is None:
            s.add(User(id=cb.from_user.id, username=cb.from_user.username,
                       first_name=cb.from_user.first_name))
        else:
            u.username = cb.from_user.username; u.first_name = cb.from_user.first_name
        await s.commit()
    await cb.answer("✅ Вступили!", show_alert=True)
    async with SessionLocal() as s:
        t2 = await s.get(Tournament, tid)
        if t2 and t2.game_type == "bracket":
            await _auto_start_bracket(cb.bot, tid)
    await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_delete_confirm_"))
async def cb_delete_confirm(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
        for p in r.scalars().all(): await s.delete(p)
        t = await s.get(Tournament, tid)
        if t: await s.delete(t)
        await s.commit()
    for d in (gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket, gm._active_poker):
        d.pop(tid, None)
    _finalized_tids.discard(tid)
    await gm.broadcast(tid, {"type": "tournament_cancelled", "tid": tid})
    await cb.answer("Удалено", show_alert=True)
    await _safe_edit(cb, f"<b>🗑 Удалён</b>\nID: <code>{tid}</code>")


@router.callback_query(F.data.startswith("tt_delete_"))
async def cb_delete_prompt(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await _safe_edit(cb,
        f"<b>⚠️ УДАЛИТЬ?</b>\n{SEP}\nID: <code>{tid}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да", callback_data=f"tt_delete_confirm_{tid}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"tt_manage_{tid}")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_end_confirm_"))
async def cb_end_confirm(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    for d in (gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket, gm._active_poker):
        d.pop(tid, None)
    _finalized_tids.discard(tid)
    await _finalize_tournament(cb.bot, tid, manual=True)
    await cb.answer("Завершено", show_alert=True)
    await _safe_edit(cb, f"<b>🏁 Завершён вручную</b>\n{SEP}\nID: <code>{tid}</code>")


@router.callback_query(F.data.startswith("tt_end_"))
async def cb_end_prompt(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await _safe_edit(cb,
        f"<b>⚠️ ЗАКОНЧИТЬ?</b>\n{SEP}\nID: <code>{tid}</code>\n\n"
        "<blockquote>Победитель НЕ будет засчитан.</blockquote>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏁 Да", callback_data=f"tt_end_confirm_{tid}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"tt_manage_{tid}")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_start_"))
async def cb_start(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        gtype = t.game_type
        if gtype == "bracket":
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.status.in_(("approved", "winner"))))
            approved = r.scalars().all()
            if len(approved) < t.bracket_size:
                await cb.answer(f"Нужно {t.bracket_size}, есть {len(approved)}", show_alert=True); return
            await _auto_start_bracket(cb.bot, tid)
            await cb.answer("Запущено!")
            await _show_tournament_menu(cb, tid)
            return
    ok = await _start_tournament(cb.bot, tid, notify=True)
    if not ok: await cb.answer("Недостаточно игроков (нужно ≥2)", show_alert=True); return
    await cb.answer("Запущено!"); await _show_tournament_menu(cb, tid)


async def _start_tournament(bot, tid, notify=False):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: return False
        if t.game_type in ("wheel_1v1", "bracket"):
            return False
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.status == "approved"))
        parts = r.scalars().all()
        if len(parts) < 2: return False
        uids = [p.user_id for p in parts]
    names = await _enrich_names(bot, uids)
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: return False
        players = []
        for p in parts:
            players.append({"user_id": p.user_id, "name": names.get(p.user_id, f"Игрок {p.user_id}")})
            pp = await s.get(Participant, p.id)
            if pp: pp.chips = t.start_stack
        t.status = "running"; await s.commit()
        tname, tgame, tprize = t.name, t.game_type, t.prize
        start_stack = t.start_stack or 1000
        turn_time = t.turn_time or 20
        max_seats = int(getattr(t, "series_target", 7) or 7)
        if max_seats < 2 or max_seats > 7: max_seats = 7
        sb = 10; bb = 20
        if tgame == "poker":
            sb = t.blind_interval or 10
            bb = sb * 2

    if tgame == "poker":
        pk = PokerTournament(tid, players, tprize or "🏆 Приз",
                             sb=sb, bb=bb, starting_stack=start_stack,
                             turn_time=turn_time, max_seats=max_seats)
        gm.set_poker(tid, pk)
        pk.auto_seat_bots()
        from webapp import _broadcast_poker_all, _broadcast_poker_lobby
        await _broadcast_poker_lobby(tid, pk)
        await _broadcast_poker_all(tid, pk)
    else:
        g = WheelGame(tid, players, tprize or "🏆 Приз")
        gm.set_wheel(tid, g)
        await gm.broadcast(tid, g.to_dict())

    if notify:
        for p in parts:
            if is_bot(p.user_id): continue
            try:
                await bot.send_message(p.user_id,
                    f"<b>🚀 Турнир начался!</b>\n{SEP}\n"
                    f"🏆 <code>{esc(tname)}</code>\n\n"
                    "<blockquote>Открой Mini App.</blockquote>",
                    reply_markup=enter_webapp_kb(tid, WEBAPP_URL))
            except: pass
    return True


async def _auto_start_bracket(bot, tid):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t or t.game_type != "bracket" or t.status != "registration":
            return
        r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
        parts = r.scalars().all()
        approved = [p for p in parts if p.status in ("approved", "winner")]
        if len(approved) < t.bracket_size:
            return
        uids = [p.user_id for p in approved[:t.bracket_size]]
        tname = t.name; tprize = t.prize
        series = int(getattr(t, "series_target", 3) or 3)
    names = await _enrich_names(bot, uids)
    players = [{"user_id": uid, "name": names.get(uid, f"Игрок {uid}")} for uid in uids]
    g = BracketGame(tid, players, tprize or "🏆 Приз", series_target=series)
    gm.set_bracket(tid, g)
    async with SessionLocal() as s:
        tt = await s.get(Tournament, tid)
        if tt: tt.status = "running"; await s.commit()
    await gm.broadcast(tid, g.to_dict())
    for p in players:
        if is_bot(p["user_id"]): continue
        try:
            await bot.send_message(p["user_id"],
                f"<b>🏆 Турнир стартовал!</b>\n{SEP}\n"
                f"🏆 <code>{esc(tname)}</code>\n"
                f"🥇 Формат боя: <b>до {series} побед</b>\n\n"
                "<blockquote>Открой Mini App → вкладка «Сетка».</blockquote>",
                reply_markup=enter_webapp_kb(tid, WEBAPP_URL))
        except: pass


@router.callback_query(F.data.startswith("tt_pause_"))
async def cb_pause(cb: CallbackQuery): await cb.answer("Пауза отключена", show_alert=True)


@router.callback_query(F.data.startswith("tt_resume_"))
async def cb_resume(cb: CallbackQuery): await cb.answer("Пауза отключена", show_alert=True)


@router.callback_query(F.data.startswith("tt_cancel_"))
async def cb_cancel_tour(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if t: t.status = "cancelled"; await s.commit()
    for d in (gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket, gm._active_poker):
        d.pop(tid, None)
    await gm.broadcast(tid, {"type": "tournament_cancelled", "tid": tid})
    await cb.answer("Отменён"); await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_invite_"))
async def cb_invite(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await state.update_data(invite_tid=tid, is_kick=False); await state.set_state(InviteFSM.user_id)
    await _safe_edit(cb, f"<b>✉️ Приглашение</b>\n{SEP}\nID или @username:",
                     reply_markup=back_kb(f"tt_manage_{tid}"))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_kick_"))
async def cb_kick(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await state.update_data(invite_tid=tid, is_kick=True); await state.set_state(InviteFSM.user_id)
    await _safe_edit(cb, f"<b>🚫 Кик</b>\n{SEP}\nID или @username:",
                     reply_markup=back_kb(f"tt_manage_{tid}"))
    await cb.answer()


@router.message(InviteFSM.user_id, TextNotMenu())
async def msg_invite(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    raw = message.text.strip(); uid = None
    async with SessionLocal() as s:
        if raw.startswith("@"):
            r = await s.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if not u:
                await message.answer("<blockquote>❌ Не найден.</blockquote>"); return
            uid = u.id
        else:
            try: uid = int(raw)
            except ValueError:
                await message.answer("<blockquote>❌ ID или @.</blockquote>"); return
        data = await state.get_data()
        tid = data.get("invite_tid"); is_kick = data.get("is_kick")
        t = await s.get(Tournament, tid)
        if not t:
            await message.answer("<blockquote>❌ Нет турнира.</blockquote>"); await state.clear(); return
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == uid))
        p = r.scalar_one_or_none()
        if is_kick:
            await _do_kick_player(message.bot, tid, t, uid, p)
        else:
            if p:
                if p.status in ("approved", "winner"):
                    await message.answer(f"<b>ℹ️ Уже принял</b>\n🆔 <code>{uid}</code>")
                    await state.clear(); return
                p.status = "pending"
            else:
                s.add(Participant(tournament_id=tid, user_id=uid, status="pending"))
            await s.commit()
            tname = t.name
            if not is_bot(uid):
                try:
                    await message.bot.send_message(uid,
                        f"<b>✉️ Приглашение!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
                        "<blockquote>Примешь?</blockquote>",
                        reply_markup=invite_response_kb(tid))
                except: pass
            await message.answer(f"<b>✅ Отправлено</b>\n🆔 <code>{uid}</code>")
    await state.clear()


async def _do_kick_player(bot, tid, t, uid, participant_row):
    async with SessionLocal() as s:
        p = await s.get(Participant, participant_row.id) if participant_row else None
        if p: p.status = "kicked"; await s.commit()
    tgame = t.game_type
    tname = t.name
    if tgame == "wheel":
        g = gm.get_wheel(tid)
        if g:
            for a in list(g.active):
                if a["user_id"] == uid:
                    g.active.remove(a)
                    g.eliminated.append({**a, "place": len(g.active) + 1}); break
            if len(g.active) == 1 and not g.finished:
                g.winner = g.active[0]; g.winner["place"] = 1; g.finished = True
            await gm.broadcast(tid, g.to_dict())
    elif tgame == "bracket":
        g = gm.get_bracket(tid)
        if g:
            advanced_list = g.remove_player(uid)
            seen = set()
            for adv in advanced_list:
                if adv["user_id"] in seen: continue
                seen.add(adv["user_id"])
                if is_bot(adv["user_id"]): continue
                try:
                    await bot.send_message(adv["user_id"],
                        f"<b>🏆 Соперник выбыл</b>\n{SEP}\n"
                        f"🏆 <code>{esc(tname)}</code>\n\n"
                        "<blockquote>Вы автоматически прошли дальше!</blockquote>")
                except Exception as e:
                    logging.warning("auto-advance notify fail: %s", e)
            await gm.broadcast(tid, g.to_dict())
    elif tgame == "poker":
        pk = gm.get_poker(tid)
        if pk:
            pk.kick_player(uid)
            from webapp import _broadcast_poker_all, _broadcast_poker_lobby
            await _broadcast_poker_all(tid, pk)
            await _broadcast_poker_lobby(tid, pk)
    if not is_bot(uid):
        try:
            await bot.send_message(uid,
                f"<b>🚫 Вас исключили</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>")
        except: pass


@router.callback_query(F.data == "my_tournaments")
async def cb_my(cb: CallbackQuery):
    rows = []; lines = [f"<b>🎟 Мои турниры</b>\n{SEP}"]; has = False; shown = set()
    async with SessionLocal() as s:
        r = await s.execute(select(Participant).where(
            Participant.user_id == cb.from_user.id,
            Participant.status.in_(("approved", "winner", "pending"))))
        for p in r.scalars().all():
            t = await s.get(Tournament, p.tournament_id)
            if not t or t.id in shown: continue
            if t.status in ("finished", "cancelled"): continue
            shown.add(t.id)
            emoji = {"wheel":"🎡","wheel_1v1":"⚔️","bracket":"🏆","poker":"🃏"}.get(t.game_type, "🎡")
            sl = {"pending": "⏳", "approved": "✅", "winner": "🏆"}.get(p.status, t.status)
            lines.append(f"  {emoji} <b>{esc(t.name)}</b> · {sl}")
            rows.append([InlineKeyboardButton(text=f"📋 {t.name[:22]}",
                                              callback_data=f"tt_player_{t.id}")])
            has = True
    if not has: lines.append("<blockquote>ℹ️ Нет активных.</blockquote>")
    text = "\n".join(lines)
    if rows: await _safe_edit(cb, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else: await _safe_edit(cb, text)
    await cb.answer()


@router.callback_query(F.data == "open_tournaments")
async def cb_open(cb: CallbackQuery):
    async with SessionLocal() as s:
        r = await s.execute(select(Tournament).where(
            Tournament.access_type == "open", Tournament.status == "registration",
            Tournament.game_type != "wheel_1v1"))
        tours = r.scalars().all()
    if not tours:
        await _safe_edit(cb, f"<b>🌐 Открытые</b>\n{SEP}\n<blockquote>Нет.</blockquote>")
        await cb.answer(); return
    lines = [f"<b>🌐 Открытые</b>\n{SEP}"]; rows = []
    for t in tours:
        emoji = {"bracket":"🏆","poker":"🃏"}.get(t.game_type, "🎡")
        extra = f" · {t.bracket_size} игр." if t.game_type == "bracket" else ""
        lines.append(f"{emoji} <b>{esc(t.name)}</b> · <i>{esc(t.prize or '—')}</i>{extra}")
        rows.append([InlineKeyboardButton(text=f"📩 Заявка · {t.name[:20]}",
                                          callback_data=f"apply_{t.id}")])
    await _safe_edit(cb, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("apply_"))
async def cb_apply(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        u = await s.get(User, cb.from_user.id)
        if u is None:
            s.add(User(id=cb.from_user.id, username=cb.from_user.username,
                       first_name=cb.from_user.first_name))
        else:
            u.username = cb.from_user.username; u.first_name = cb.from_user.first_name
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        existing = r.scalar_one_or_none()
        if existing and existing.status in ("pending", "approved"):
            await cb.answer("Уже подана", show_alert=True); return
        if existing: existing.status = "pending"; await s.commit(); pid = existing.id
        else:
            p = Participant(tournament_id=tid, user_id=cb.from_user.id, status="pending")
            s.add(p); await s.commit(); await s.refresh(p); pid = p.id
        creator_id = t.creator_id; tname = t.name
    try:
        await cb.bot.send_message(creator_id,
            f"<b>📩 Новая заявка!</b>\n{SEP}\n"
            f"👤 {esc(cb.from_user.first_name)} "
            f"(@{cb.from_user.username or '—'}, <code>{cb.from_user.id}</code>)\n"
            f"🏆 <b>{esc(tname)}</b>",
            reply_markup=join_request_kb(pid))
    except: pass
    await cb.answer("Отправлена!", show_alert=True)


@router.callback_query(F.data.startswith("approve_"))
async def cb_approve(cb: CallbackQuery):
    pid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        p = await s.get(Participant, pid)
        if not p: await cb.answer("Нет", show_alert=True); return
        p.status = "approved"
        t = await s.get(Tournament, p.tournament_id)
        await s.commit()
        tname = t.name if t else "?"; uid = p.user_id; tid = p.tournament_id
        gtype = t.game_type if t else None
    if not is_bot(uid):
        try:
            await cb.bot.send_message(uid,
                f"<b>✅ Одобрено!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
                "<blockquote>Жди старт.</blockquote>")
        except: pass
    await _safe_edit(cb, f"<b>✅ Принят</b>"); await cb.answer("Принято")
    if gtype == "bracket":
        try: await _auto_start_bracket(cb.bot, tid)
        except Exception as e: logging.warning("auto start: %s", e)


@router.callback_query(F.data.startswith("reject_"))
async def cb_reject(cb: CallbackQuery):
    pid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        p = await s.get(Participant, pid)
        if not p: await cb.answer("Нет", show_alert=True); return
        p.status = "rejected"; t = await s.get(Tournament, p.tournament_id); await s.commit()
        tname = t.name if t else "?"; uid = p.user_id
    if not is_bot(uid):
        try:
            await cb.bot.send_message(uid, f"<b>❌ Отклонена</b>\n🏆 <code>{esc(tname)}</code>")
        except: pass
    await _safe_edit(cb, f"<b>❌ Отклонён</b>"); await cb.answer("Отклонено")


@router.callback_query(F.data.startswith("accept_invite_"))
async def cb_accept_inv(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    start_1v1 = False; players_1v1 = []
    tname = ""; tprize = ""
    series_target = 1
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t:
            await cb.answer("Не найден", show_alert=True); return
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        p = r.scalar_one_or_none()
        if not p:
            await cb.answer("Вы не участник", show_alert=True); return
        if p.status == "approved":
            await cb.answer("Уже принято", show_alert=True); return
        p.status = "approved"
        u = await s.get(User, cb.from_user.id)
        if u is None:
            s.add(User(id=cb.from_user.id, username=cb.from_user.username,
                       first_name=cb.from_user.first_name))
        else:
            u.username = cb.from_user.username; u.first_name = cb.from_user.first_name
        await s.commit()
        tname = t.name; tprize = t.prize
        series_target = int(getattr(t, "series_target", 1) or 1)
        if t.game_type == "wheel_1v1":
            r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
            all_p = r.scalars().all()
            approved = [x for x in all_p if x.status == "approved"]
            if len(approved) >= 2:
                start_1v1 = True
                for pp in approved[:2]:
                    uu = await s.get(User, pp.user_id)
                    players_1v1.append({"user_id": pp.user_id, "name": _display_name(uu, pp.user_id)})
                t.status = "running"; await s.commit()
    try:
        await _safe_edit(cb,
            f"<b>✅ Принято!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
            "<blockquote>Ждём остальных…</blockquote>")
    except: pass
    await cb.answer("Принято!")
    if start_1v1 and len(players_1v1) == 2:
        g = Wheel1v1Game(tid, players_1v1[0], players_1v1[1],
                         tprize or "🏆 Приз", series_target=series_target)
        gm.set_wheel_1v1(tid, g)
        await gm.broadcast(tid, {"type": "wheel_1v1_ready"})
        for pl in players_1v1:
            if is_bot(pl["user_id"]): continue
            try:
                await cb.bot.send_message(pl["user_id"],
                    f"<b>⚔️ Оба приняли вызов!</b>\n{SEP}\n"
                    f"🏆 <code>{esc(tname)}</code>\n"
                    f"🥇 Серия: <b>до {series_target} побед</b>\n\n"
                    "<blockquote>Зайди в Mini App → «Готов».</blockquote>",
                    reply_markup=enter_webapp_kb(tid, WEBAPP_URL))
            except: pass


@router.callback_query(F.data.startswith("decline_invite_"))
async def cb_decline_inv(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    cancel_tour = False; tname = ""
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Не найдено", show_alert=True); return
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        p = r.scalar_one_or_none()
        if p: p.status = "rejected"
        if t.game_type in ("wheel_1v1", "bracket"):
            cancel_tour = True
            t.status = "cancelled"
        await s.commit()
        tname = t.name
    try:
        await _safe_edit(cb, "<blockquote>❌ Приглашение отклонено.</blockquote>")
    except: pass
    await cb.answer("Отклонено")
    if cancel_tour:
        async with SessionLocal() as s:
            r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
            for pp in r.scalars().all():
                if pp.user_id == cb.from_user.id: continue
                if is_bot(pp.user_id): continue
                try:
                    await cb.bot.send_message(pp.user_id,
                        f"<b>❌ Турнир отменён</b>\n{SEP}\n"
                        f"🏆 <code>{esc(tname)}</code>\n\n"
                        "<blockquote>Игрок отклонил приглашение.</blockquote>")
                except: pass
        await gm.broadcast(tid, {"type": "tournament_cancelled", "tid": tid})


# ============ 7. ФИНАЛИЗАЦИЯ ============
async def _notify_creator_and_users(bot, tname, tprize, winner_id, winner_name, winner_uname,
                                     manual, user_ids, total, creator_id):
    try:
        await asyncio.sleep(7)
        if creator_id and not is_bot(creator_id):
            try:
                if manual:
                    ct = (f"<b>🏁 Турнир завершён</b>\n{SEP}\n"
                          f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                          "<b>Остановлен админом.</b>\n"
                          f"👥 Участников: <code>{total}</code>")
                elif winner_id:
                    up = f" (@{winner_uname})" if winner_uname else ""
                    ct = (f"<b>🏁 Турнир завершён!</b>\n{SEP}\n"
                          f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                          f"🥇 Победитель: <code>{esc(winner_name or '—')}</code>{up}\n"
                          f"🎁 Приз: <i>{esc(tprize)}</i>\n"
                          f"👥 Участников: <code>{total}</code>")
                else:
                    ct = (f"<b>🏁 Турнир завершён</b>\n{SEP}\n"
                          f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                          f"👥 Участников: <code>{total}</code>")
                await bot.send_message(creator_id, ct)
            except Exception as e:
                logging.warning("creator notify fail: %s", e)
        for uid in user_ids:
            if is_bot(uid): continue
            try:
                if manual:
                    text = (f"<b>🏁 ТУРНИР ЗАВЕРШЁН</b>\n{SEP}\n"
                            f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                            "<b>Остановлен админом.</b>\n"
                            f"👥 <b>Участников:</b> <code>{total}</code>")
                elif winner_id and winner_id == uid:
                    text = (f"<b>🏁 ТУРНИР ЗАВЕРШЁН!</b>\n{SEP}\n"
                            f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                            f"🥇 <b>Победитель:</b> <code>{esc(winner_name or '—')}</code>\n"
                            f"🎁 <b>Приз:</b> <i>{esc(tprize)}</i>\n"
                            f"👥 <b>Участников:</b> <code>{total}</code>\n\n"
                            "<blockquote>🎉 Поздравляем!</blockquote>")
                else:
                    text = (f"<b>🏁 ТУРНИР ЗАВЕРШЁН!</b>\n{SEP}\n"
                            f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                            f"🥇 <b>Победитель:</b> <code>{esc(winner_name or '—')}</code>\n"
                            f"🎁 <b>Приз:</b> <i>{esc(tprize)}</i>\n"
                            f"👥 <b>Участников:</b> <code>{total}</code>\n\n"
                            "<blockquote>Спасибо за участие!</blockquote>")
                await bot.send_message(uid, text)
            except Exception as e:
                logging.warning("notify fail uid=%s: %s", uid, e)
    except Exception as e:
        logging.exception("notify task crashed: %s", e)


async def _finalize_tournament(bot, tid, winner_id=None, winner_name=None, manual=False):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t or t.status == "finished": return
        t.status = "finished"
        tname = t.name; tprize = t.prize or "—"
        creator_id = t.creator_id
        winner_uname = None
        if not manual and winner_id:
            u = await s.get(User, winner_id)
            if u and not is_bot(winner_id):
                u.wins = (u.wins or 0) + 1
                winner_name = winner_name or u.first_name or str(winner_id)
                winner_uname = u.username
            elif is_bot(winner_id):
                winner_name = winner_name or f"🤖 {winner_id}"
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid, Participant.user_id == winner_id))
            p = r.scalar_one_or_none()
            if p: p.status = "winner"
        r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
        parts = r.scalars().all()
        approved_users = [p.user_id for p in parts if p.status in ("approved", "winner")]
        total = len(approved_users)
        await s.commit()
    try:
        await gm.broadcast(tid, {"type": "tournament_ended", "tid": tid,
                                 "winner_id": winner_id if not manual else None,
                                 "winner_name": winner_name if not manual else None,
                                 "manual": manual})
    except Exception as e:
        logging.exception("broadcast final fail: %s", e)
    _spawn(_notify_creator_and_users(bot, tname, tprize, winner_id, winner_name,
                                     winner_uname, manual, approved_users, total, creator_id))


async def _check_1v1_finish(bot, tid):
    g = gm.get_wheel_1v1(tid)
    if not g or not g.finished: return
    if tid in _finalized_tids: return
    _finalized_tids.add(tid)
    wid = g.winner.get("user_id") if g.winner else None
    wname = g.winner.get("name") if g.winner else None
    await _finalize_tournament(bot, tid, wid, wname, manual=False)


async def _check_bracket_finish(bot, tid):
    g = gm.get_bracket(tid)
    if not g or not g.finished: return
    if tid in _finalized_tids: return
    _finalized_tids.add(tid)
    wid = g.winner.get("user_id") if g.winner else None
    wname = g.winner.get("name") if g.winner else None
    await _finalize_tournament(bot, tid, wid, wname, manual=False)


async def _check_poker_finish(bot, tid):
    pk = gm.get_poker(tid)
    if not pk or not pk.finished: return
    if tid in _finalized_tids: return
    _finalized_tids.add(tid)
    wid = pk.winner.get("user_id") if pk.winner else None
    wname = pk.winner.get("name") if pk.winner else None
    await _finalize_tournament(bot, tid, wid, wname, manual=False)


async def game_ticker(bot):
    while True:
        try:
            for tid, g in list(gm._active_wheel.items()):
                if g.finished and tid not in _finalized_tids:
                    _finalized_tids.add(tid)
                    wid = g.winner.get("user_id") if g.winner else None
                    wname = g.winner.get("name") if g.winner else None
                    await _finalize_tournament(bot, tid, wid, wname, manual=False)
            for tid, g in list(gm._active_wheel_1v1.items()):
                if g.finished and tid not in _finalized_tids:
                    await _check_1v1_finish(bot, tid)
            for tid, g in list(gm._active_bracket.items()):
                if g.finished and tid not in _finalized_tids:
                    await _check_bracket_finish(bot, tid)
            for tid, pk in list(gm._active_poker.items()):
                try:
                    events = pk.check_tick()
                    # Авто-кик тех, кто не сел за стол за 3 мин
                    timed_out = pk.check_unseated_timeout()
                    if timed_out:
                        async with SessionLocal() as s:
                            for uid in timed_out:
                                r = await s.execute(select(Participant).where(
                                    Participant.tournament_id == tid,
                                    Participant.user_id == uid))
                                p = r.scalar_one_or_none()
                                if p: p.status = "kicked"
                            await s.commit()
                        for uid in timed_out:
                            pk.kick_unseated(uid)
                            if not is_bot(uid):
                                try:
                                    await bot.send_message(uid,
                                        "<b>⏰ Вы не сели за стол</b>\n\n"
                                        "<blockquote>За 3 минуты после начала турнира "
                                        "нужно было выбрать стол. Вы исключены.</blockquote>")
                                except: pass
                        events.append(("unseated_timeout", None))
                    if events:
                        from webapp import (_broadcast_poker_all,
                                             _broadcast_poker_lobby,
                                             _broadcast_poker_event)
                        await _broadcast_poker_event(tid, pk, events)
                        await _broadcast_poker_lobby(tid, pk)
                except Exception as e:
                    logging.exception("poker tick error tid=%s: %s", tid, e)
                if pk.finished and tid not in _finalized_tids:
                    await _check_poker_finish(bot, tid)
        except Exception as e:
            logging.exception("ticker error: %s", e)
        await asyncio.sleep(1)


async def _periodic_cleanup():
    while True:
        try:
            await asyncio.sleep(60)
            for tid in list(gm._connections.keys()):
                if not gm._connections[tid]: gm._connections.pop(tid, None)
            for t in list(_bg_tasks):
                if t.done(): _bg_tasks.discard(t)
        except Exception as e:
            logging.exception("cleanup error: %s", e)


# ============ 8. FALLBACK ============
@router.callback_query()
async def fallback_cb(cb: CallbackQuery):
    logging.warning("Unhandled callback: %s", cb.data)
    try:
        await cb.answer("⚠ Действие недоступно", show_alert=False)
    except Exception:
        pass


@router.message()
async def fallback_handler(message: Message, state: FSMContext):
    cur = await state.get_state()
    logging.info("FALLBACK triggered: text=%r state=%s", message.text, cur)
    await state.clear()
    if cur:
        is_adm = await _is_admin(message.from_user.id)
        await message.answer(
            "🤖 <i>Предыдущий диалог сброшен.</i>\n\n🏠 <b>Главное меню</b>",
            reply_markup=reply_main_menu(message.from_user.id, is_adm))
    else:
        await message.answer(
            "🤖 <i>Я не понял команду. Используй кнопки внизу.</i>\n\n"
            "<blockquote>Команды: /start /menu /help /cancel</blockquote>")


async def main():
    if not BOT_TOKEN: raise SystemExit("❌ BOT_TOKEN не задан")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent):
        e = event.exception
        if isinstance(e, TelegramBadRequest):
            s = str(e)
            if ("query is too old" in s or "query ID is invalid" in s
                or "message is not modified" in s):
                return True
        logging.exception("Handler error: %s", e)
        return True

    app = create_app(bot)
    cfg = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", loop="asyncio")
    server = uvicorn.Server(cfg)
    logging.info("🚀 Бот и Mini App на %s:%s", HOST, PORT)

    async def safe_polling():
        while True:
            try:
                await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
                break
            except Exception as e:
                logging.exception("Polling crashed, restart in 5s: %s", e)
                await asyncio.sleep(5)

    await asyncio.gather(
        safe_polling(),
        server.serve(),
        game_ticker(bot),
        _periodic_cleanup(),
    )


if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logging.info("Стоп")
