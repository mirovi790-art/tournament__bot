import asyncio
import html
import logging
import os
import uvicorn
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton, WebAppInfo,
                            ReplyKeyboardMarkup, KeyboardButton)
from dotenv import load_dotenv
from sqlalchemy import select, func

from database import (init_db, SessionLocal, User, Admin, Ban, Tournament,
                       Participant)
from webapp import create_app
import game as gm
from game import (PokerGame, WheelGame, Wheel1v1Game, BracketGame,
                  MAX_TABLE, BRACKET_SERIES_TARGET)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "7883953756"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
SEP = "➖➖➖➖➖➖➖➖➖"
logging.basicConfig(level=logging.INFO)
router = Router()
_finalized_tids = set()
_bg_tasks: set[asyncio.Task] = set()
VALID_BRACKET_SIZES = [4, 8, 16]
VALID_SERIES_TARGETS = [1, 2, 3]


def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(lambda x: _bg_tasks.discard(x))
    return t


def esc(s): return html.escape(s or "")


async def _is_admin(user_id):
    if user_id == OWNER_ID: return True
    async with SessionLocal() as s:
        r = await s.execute(select(Admin).where(Admin.user_id == user_id))
        return r.scalar_one_or_none() is not None


async def _check_ban(user_id):
    async with SessionLocal() as s:
        return await s.get(Ban, user_id) is not None


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
        [InlineKeyboardButton(text="🃏 Покер", callback_data="tt_game_poker")],
        [InlineKeyboardButton(text="🎡 Колесо Фортуны", callback_data="tt_game_wheel")],
        [InlineKeyboardButton(text="⚔️ Колесо 1×1", callback_data="tt_game_wheel1v1")],
        [InlineKeyboardButton(text="🏆 Турнир с сеткой", callback_data="tt_game_bracket")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


def bracket_size_kb():
    rows = [[InlineKeyboardButton(text=f"{n} игроков", callback_data=f"tt_bracket_{n}")]
            for n in VALID_BRACKET_SIZES]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bracket_series_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥇 До 1 победы", callback_data="tt_series_1")],
        [InlineKeyboardButton(text="🥈 До 2 побед", callback_data="tt_series_2")],
        [InlineKeyboardButton(text="🥉 До 3 побед", callback_data="tt_series_3")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


def confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="tt_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")]])


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
    if game_type in ("wheel", "wheel_1v1") and status == "running":
        rows.append([InlineKeyboardButton(text="🎡 Крутить колесо", callback_data=f"tt_spin_{tid}")])
    if status == "registration":
        can_join = (game_type != "wheel_1v1") and (game_type != "bracket" or access_type == "open")
        if can_join:
            rows.append([InlineKeyboardButton(text="🖐 Вступить в турнир", callback_data=f"tt_join_{tid}")])
        if game_type != "bracket":
            rows.append([InlineKeyboardButton(text="▶️ Начать турнир", callback_data=f"tt_start_{tid}")])
        if game_type != "wheel_1v1":
            rows.append([InlineKeyboardButton(text="✉️ Пригласить игрока", callback_data=f"tt_invite_{tid}")])
        rows.append([InlineKeyboardButton(text="🚫 Кикнуть игрока", callback_data=f"tt_kick_{tid}")])
    if status == "running":
        if game_type not in ("wheel_1v1", "bracket"):
            rows.append([InlineKeyboardButton(text="🖐 Вступить в турнир", callback_data=f"tt_join_{tid}")])
        if game_type not in ("wheel_1v1", "bracket"):
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
    name = State()
    prize = State()
    w1v1_p1 = State()
    w1v1_p2 = State()
    bracket_size = State()
    bracket_series = State()
    bracket_players = State()
    poker_stack = State()
    poker_turn = State()
    poker_blind = State()
    confirm = State()


class InviteFSM(StatesGroup):
    user_id = State()


class FindPlayerFSM(StatesGroup):
    query = State()


def _help_text():
    return ("<b>ℹ️ ПОМОЩЬ И ПРАВИЛА</b>\n" f"{SEP}\n"
            "<b>🃏 Покер:</b>\n▫️ До 7 игроков за столом.\n\n"
            "<b>🎡 Колесо Фортуны:</b>\n▫️ Крутит админ.\n\n"
            "<b>⚔️ Колесо 1×1:</b>\n▫️ Два игрока по нику.\n\n"
            "<b>🏆 Турнир с сеткой:</b>\n"
            "▫️ 4/8/16 игроков в плей-офф.\n"
            "▫️ Серия побед настраивается (1/2/3).\n"
            "▫️ Победитель матча проходит дальше.\n\n"
            "<blockquote>🎯 Кнопки внизу.</blockquote>")


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
@router.message(StateFilter(None), F.text.lower() == "меню")
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    if await _check_ban(message.from_user.id):
        await message.answer("<blockquote>⛔ Заблокированы!</blockquote>"); return
    await _upsert_user(message.from_user)
    is_adm = await _is_admin(message.from_user.id)
    await message.answer("🏠 <b>Главное меню</b>",
                         reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(Command("help"))
async def cmd_help(message: Message): await message.answer(_help_text())


@router.message(Command("owner"))
async def cmd_owner(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("<blockquote>⛔ Владелец.</blockquote>"); return
    await message.answer(f"<b>👑 ВЛАДЕЛЕЦ</b>\n{SEP}", reply_markup=owner_menu_kb())


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await _is_admin(message.from_user.id):
        await message.answer("<blockquote>⛔ Админы.</blockquote>"); return
    await message.answer(f"<b>🛠 АДМИН</b>\n{SEP}", reply_markup=owner_menu_kb())


@router.message(StateFilter(None), F.text == "🏆 Турниры")
async def txt_tournaments(message: Message):
    is_adm = await _is_admin(message.from_user.id)
    await message.answer("🏆 <b>Раздел турниров</b>\n\n⬇️",
                         reply_markup=reply_tournaments_menu(message.from_user.id, is_adm))


@router.message(StateFilter(None), F.text == "👤 Профиль")
async def txt_profile(message: Message):
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


@router.message(StateFilter(None), F.text == "🆕 Создать турнир")
async def txt_create(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        await message.answer("⛔ Только админ — @Dimo0nchik"); return
    await state.clear(); await state.set_state(TournamentFSM.access_type)
    await message.answer(f"<b>🆕 Шаг 1</b>\n{SEP}\nДоступ:", reply_markup=access_type_kb())


@router.message(StateFilter(None), F.text == "👑 Админство")
async def txt_adm_owner(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Владелец."); return
    await message.answer(f"<b>👑 АДМИНСТВО</b>\n{SEP}", reply_markup=owner_menu_kb())


@router.message(StateFilter(None), F.text == "🔍 Найти игрока")
async def txt_find_player(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Только для владельца."); return
    await state.clear(); await state.set_state(FindPlayerFSM.query)
    await message.answer(
        f"<b>🔍 ПОИСК ИГРОКА</b>\n{SEP}\n"
        "Отправь <b>Telegram ID</b> или <b>@username</b> игрока.\n\n"
        "<blockquote>Чтобы выйти — нажми ⬅️ В главное меню.</blockquote>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ В главное меню")]], resize_keyboard=True))


@router.message(FindPlayerFSM.query)
async def msg_find_player(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    raw = message.text.strip()
    if raw == "⬅️ В главное меню":
        await state.clear()
        is_adm = await _is_admin(message.from_user.id)
        await message.answer("🏠 <b>Главное меню</b>",
                             reply_markup=reply_main_menu(message.from_user.id, is_adm))
        return
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
        active_parts = (await s.execute(select(Participant).where(
            Participant.user_id == target_id,
            Participant.status.in_(("approved", "winner"))))).scalars().all()
        active_tours = []
        for p in active_parts:
            t = await s.get(Tournament, p.tournament_id)
            if t and t.status in ("registration", "running", "paused"):
                active_tours.append({"name": t.name, "status": t.status, "game": t.game_type})
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
             f"📅 <b>В боте с:</b> {u.created_at:%d.%m.%Y}"]
    if active_tours:
        lines.append(SEP); lines.append("<b>⚡ Активные турниры:</b>")
        for t in active_tours:
            em = "🃏" if t["game"] == "poker" else ("🏆" if t["game"] == "bracket" else "🎡")
            lines.append(f"  {em} {esc(t['name'])} · <code>{t['status']}</code>")
    lines.append(""); lines.append("<blockquote>Можешь ввести ещё игрока или нажать ⬅️</blockquote>")
    await message.answer("\n".join(lines))


@router.message(StateFilter(None), F.text == "ℹ️ Помощь")
async def txt_help(message: Message): await message.answer(_help_text())


@router.message(StateFilter(None), F.text == "⬅️ В главное меню")
async def txt_back_main(message: Message):
    is_adm = await _is_admin(message.from_user.id)
    await message.answer("🏠 <b>Главное меню</b>",
                         reply_markup=reply_main_menu(message.from_user.id, is_adm))


@router.message(StateFilter(None), F.text == "🌐 Открытые турниры")
async def txt_open(message: Message):
    async with SessionLocal() as s:
        r = await s.execute(select(Tournament).where(
            Tournament.access_type == "open", Tournament.status == "registration",
            Tournament.game_type != "wheel_1v1"))
        tours = r.scalars().all()
    if not tours:
        await message.answer(f"<b>🌐 Открытые</b>\n{SEP}\n<blockquote>Нет активных.</blockquote>"); return
    lines = [f"<b>🌐 Открытые турниры</b>\n{SEP}"]; rows = []
    for t in tours:
        emoji = "🃏" if t.game_type == "poker" else ("🏆" if t.game_type == "bracket" else "🎡")
        extra = f" · {t.bracket_size} игр. · до {getattr(t, 'series_target', 3)} поб." if t.game_type == "bracket" else ""
        lines.append(f"{emoji} <b>{esc(t.name)}</b> · <i>{esc(t.prize or '—')}</i>{extra}")
        rows.append([InlineKeyboardButton(text=f"📩 Заявка · {t.name[:20]}",
                                          callback_data=f"apply_{t.id}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(StateFilter(None), F.text == "🎟 Мои турниры")
async def txt_my(message: Message):
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
            emoji = "🃏" if t.game_type == "poker" else ("⚔️" if t.game_type == "wheel_1v1" else ("🏆" if t.game_type == "bracket" else "🎡"))
            sl = {"pending": "⏳ заявка", "approved": "✅ принят",
                  "winner": "🏆 победа"}.get(p.status, t.status)
            lines.append(f"  {emoji} <b>{esc(t.name)}</b> · {sl}")
            rows.append([InlineKeyboardButton(text=f"📋 {t.name[:22]}",
                                              callback_data=f"tt_player_{t.id}")])
            has = True
    if not has: lines.append("<blockquote>ℹ️ Нет активных.</blockquote>")
    await message.answer("\n".join(lines),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)


@router.message(StateFilter(None), F.text == "📂 Созданные мной")
async def txt_created(message: Message):
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
        emoji = "🃏" if t.game_type == "poker" else ("⚔️" if t.game_type == "wheel_1v1" else ("🏆" if t.game_type == "bracket" else "🎡"))
        lines.append(f"{emoji} <b>{esc(t.name)}</b> · <code>{t.status}</code>")
        rows.append([InlineKeyboardButton(text=f"⚙️ {t.name[:22]}",
                                          callback_data=f"tt_manage_{t.id}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(StateFilter(None), F.text == "📊 Все активные турниры")
async def txt_all_active(message: Message):
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
        emoji = "🃏" if t.game_type == "poker" else ("⚔️" if t.game_type == "wheel_1v1" else ("🏆" if t.game_type == "bracket" else "🎡"))
        lock = "🟢" if t.access_type == "open" else "🔴"
        lines.append(f"{lock}{emoji} <b>{esc(t.name)}</b> · <code>{t.status}</code>")
        rows.append([InlineKeyboardButton(text=f"⚙️ {t.name[:22]}",
                                          callback_data=f"tt_manage_{t.id}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(StateFilter(None), F.text == "📋 Список админов")
async def txt_list_admins(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Владелец."); return
    async with SessionLocal() as s:
        r = await s.execute(select(Admin)); admins = r.scalars().all()
    if not admins:
        await message.answer(f"<b>📋 Админы</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"); return
    lines = [f"<b>📋 Админы</b>\n{SEP}"]
    for a in admins: lines.append(f"🔹 <code>{a.user_id}</code> · {a.added_at:%d.%m.%Y}")
    await message.answer("\n".join(lines))


@router.message(StateFilter(None), F.text == "🚫 Черный список")
async def txt_blacklist(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("⛔ Владелец."); return
    async with SessionLocal() as s:
        r = await s.execute(select(Ban)); bans = r.scalars().all()
    if not bans:
        await message.answer(f"<b>📜 Чёрный список</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"); return
    lines = [f"<b>📜 Чёрный список</b>\n{SEP}"]
    for b in bans: lines.append(f"🚫 <code>{b.user_id}</code> · {b.banned_at:%d.%m.%Y}")
    await message.answer("\n".join(lines))


@router.callback_query(F.data == "owner_back")
async def cb_owner_back(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    await state.clear()
    await cb.message.edit_text(f"<b>👑 ВЛАДЕЛЕЦ</b>\n{SEP}", reply_markup=owner_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "owner_add_admin")
async def cb_add_admin(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    await state.set_state(OwnerForm.add_admin)
    await cb.message.edit_text(f"<b>➕ Админ</b>\n{SEP}\nID или @username:",
                               reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.add_admin)
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
        await cb.message.edit_text("<blockquote>Пусто.</blockquote>", reply_markup=back_kb("owner_back"))
        await cb.answer(); return
    rows = [[InlineKeyboardButton(text=f"❌ {a.user_id}", callback_data=f"owner_rm_{a.user_id}")] for a in admins]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="owner_back")])
    await cb.message.edit_text(f"<b>➖ Удалить</b>\n{SEP}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
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
    await cb.message.edit_text(text, reply_markup=back_kb("owner_back")); await cb.answer()


@router.callback_query(F.data == "owner_ban")
async def cb_ban(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer("Нет", show_alert=True); return
    await state.set_state(OwnerForm.ban_id)
    await cb.message.edit_text(f"<b>🚫 Забанить</b>\n{SEP}\nID:", reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.ban_id)
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
    await cb.message.edit_text(f"<b>✅ Разбанить</b>\n{SEP}\nID:", reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.unban_id)
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
    await cb.message.edit_text(text, reply_markup=back_kb("owner_back")); await cb.answer()


@router.callback_query(F.data == "admin_cancel_fsm")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Отменено."); await cb.answer()


# ========== FSM: Создание турнира ==========
@router.callback_query(F.data == "tt_access_open")
async def tt_open(cb: CallbackQuery, state: FSMContext):
    await state.update_data(access_type="open"); await state.set_state(TournamentFSM.game_type)
    await cb.message.edit_text(f"<b>🆕 Шаг 2</b>\n{SEP}\nИгра:", reply_markup=game_type_kb()); await cb.answer()


@router.callback_query(F.data == "tt_access_closed")
async def tt_closed(cb: CallbackQuery, state: FSMContext):
    await state.update_data(access_type="closed"); await state.set_state(TournamentFSM.game_type)
    await cb.message.edit_text(f"<b>🆕 Шаг 2</b>\n{SEP}\nИгра:", reply_markup=game_type_kb()); await cb.answer()


@router.callback_query(F.data == "tt_game_poker")
async def tt_poker(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="poker"); await state.set_state(TournamentFSM.name)
    await cb.message.edit_text(f"<b>🆕 Шаг 3</b>\n{SEP}\nНазвание:"); await cb.answer()


@router.callback_query(F.data == "tt_game_wheel")
async def tt_wheel(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="wheel"); await state.set_state(TournamentFSM.name)
    await cb.message.edit_text(f"<b>🆕 Шаг 3</b>\n{SEP}\nНазвание:"); await cb.answer()


@router.callback_query(F.data == "tt_game_wheel1v1")
async def tt_wheel1v1(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="wheel_1v1", access_type="closed")
    await state.set_state(TournamentFSM.name)
    await cb.message.edit_text(f"<b>⚔️ Колесо 1×1 · Шаг 3</b>\n{SEP}\nНазвание турнира:")
    await cb.answer()


@router.callback_query(F.data == "tt_game_bracket")
async def tt_bracket(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="bracket")
    await state.set_state(TournamentFSM.name)
    await cb.message.edit_text(f"<b>🏆 Турнир с сеткой · Шаг 3</b>\n{SEP}\nНазвание турнира:")
    await cb.answer()


@router.message(TournamentFSM.name)
async def tt_name(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    await state.update_data(name=message.text.strip()[:100])
    await state.set_state(TournamentFSM.prize)
    await message.answer(f"<b>🆕 Шаг 4</b>\n{SEP}\nПриз:")


@router.message(TournamentFSM.prize)
async def tt_prize(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    await state.update_data(prize=message.text.strip()[:200])
    data = await state.get_data()
    gt = data["game_type"]
    if gt == "poker":
        await state.set_state(TournamentFSM.poker_stack)
        await message.answer(f"<b>🆕 Покер</b>\n{SEP}\nСтартовый стек:")
    elif gt == "wheel_1v1":
        await state.set_state(TournamentFSM.w1v1_p1)
        await message.answer(f"<b>⚔️ Колесо 1×1 · Шаг 5</b>\n{SEP}\n"
                             "Введи <b>@username</b> или ID <b>первого</b> игрока:")
    elif gt == "bracket":
        await state.set_state(TournamentFSM.bracket_size)
        await message.answer(f"<b>🏆 Турнир с сеткой · Шаг 5</b>\n{SEP}\n"
                             "Выбери <b>размер сетки</b>:", reply_markup=bracket_size_kb())
    else:
        await _show_confirm(message, state)


@router.message(TournamentFSM.w1v1_p1)
async def tt_w1v1_p1(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    raw = message.text.strip()
    async with SessionLocal() as s:
        if raw.startswith("@"):
            r = await s.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if not u:
                await message.answer("<blockquote>❌ @username не найден.</blockquote>"); return
            uid = u.id
        else:
            try: uid = int(raw)
            except ValueError:
                await message.answer("<blockquote>❌ ID или @username.</blockquote>"); return
            u = await s.get(User, uid)
            if not u:
                s.add(User(id=uid, first_name=f"Игрок {uid}")); await s.commit()
    await state.update_data(w1v1_p1_id=uid)
    await state.set_state(TournamentFSM.w1v1_p2)
    await message.answer(f"<b>⚔️ Шаг 6</b>\n{SEP}\n"
                         "Введи <b>@username</b> или ID <b>второго</b> игрока:")


@router.message(TournamentFSM.w1v1_p2)
async def tt_w1v1_p2(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    raw = message.text.strip()
    async with SessionLocal() as s:
        if raw.startswith("@"):
            r = await s.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if not u:
                await message.answer("<blockquote>❌ @username не найден.</blockquote>"); return
            uid = u.id
        else:
            try: uid = int(raw)
            except ValueError:
                await message.answer("<blockquote>❌ ID или @username.</blockquote>"); return
            u = await s.get(User, uid)
            if not u:
                s.add(User(id=uid, first_name=f"Игрок {uid}")); await s.commit()
    data = await state.get_data()
    if uid == data.get("w1v1_p1_id"):
        await message.answer("<blockquote>❌ Игрок должен отличаться от первого.</blockquote>"); return
    await state.update_data(w1v1_p2_id=uid)
    await _show_confirm(message, state)


@router.callback_query(F.data.startswith("tt_bracket_"))
async def tt_bracket_size(cb: CallbackQuery, state: FSMContext):
    try: n = int(cb.data.split("_")[-1])
    except: await cb.answer("Ошибка", show_alert=True); return
    if n not in VALID_BRACKET_SIZES:
        await cb.answer("Неверный размер", show_alert=True); return
    await state.update_data(bracket_size=n, bracket_players=[], bracket_step=0)
    await state.set_state(TournamentFSM.bracket_series)
    await cb.message.edit_text(
        f"<b>🏆 Шаг 6</b>\n{SEP}\n"
        "До скольких <b>побед</b> играем каждый бой?\n\n"
        "<blockquote>Каждый матч сетки — серия. Кто первым наберёт N побед, "
        "тот проходит дальше.</blockquote>",
        reply_markup=bracket_series_kb())
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
        # Открытый — не спрашиваем игроков, они сами зайдут
        await state.set_state(TournamentFSM.confirm)
        await _show_confirm_from_cb(cb, state)
    else:
        size = data.get("bracket_size", 4)
        await state.set_state(TournamentFSM.bracket_players)
        await cb.message.edit_text(
            f"<b>🏆 Шаг 7</b>\n{SEP}\n"
            f"Введи <b>@username</b> или ID <b>игрока 1 из {size}</b>:")
    await cb.answer()


@router.message(TournamentFSM.bracket_players)
async def tt_bracket_player(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    raw = message.text.strip()
    async with SessionLocal() as s:
        if raw.startswith("@"):
            r = await s.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if not u:
                await message.answer("<blockquote>❌ @username не найден.</blockquote>"); return
            uid = u.id
        else:
            try: uid = int(raw)
            except ValueError:
                await message.answer("<blockquote>❌ ID или @username.</blockquote>"); return
            u = await s.get(User, uid)
            if not u:
                s.add(User(id=uid, first_name=f"Игрок {uid}")); await s.commit()
    data = await state.get_data()
    players = data.get("bracket_players", [])
    size = data.get("bracket_size", 4)
    if uid in players:
        await message.answer("<blockquote>❌ Этот игрок уже добавлен.</blockquote>"); return
    players.append(uid)
    await state.update_data(bracket_players=players)
    if len(players) < size:
        await message.answer(f"<b>🏆 Шаг 7</b>\n{SEP}\n"
                             f"Введи <b>@username</b> или ID <b>игрока {len(players) + 1} из {size}</b>:")
    else:
        await message.answer("✅ Все игроки добавлены.")
        await _show_confirm(message, state)


@router.message(TournamentFSM.poker_stack)
async def tt_stack(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    try: s = int(message.text.strip()); assert 10 <= s <= 1_000_000
    except: await message.answer("<blockquote>❌ 10..1M.</blockquote>"); return
    await state.update_data(poker_stack=s); await state.set_state(TournamentFSM.poker_turn)
    await message.answer(f"<b>🆕 Покер</b>\n{SEP}\nВремя на ход (сек):")


@router.message(TournamentFSM.poker_turn)
async def tt_turn(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    try: t = int(message.text.strip()); assert 5 <= t <= 300
    except: await message.answer("<blockquote>❌ 5..300.</blockquote>"); return
    await state.update_data(poker_turn=t); await state.set_state(TournamentFSM.poker_blind)
    await message.answer(f"<b>🆕 Покер</b>\n{SEP}\nРост блайндов (мин):")


@router.message(TournamentFSM.poker_blind)
async def tt_blind(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id): return
    try: b = int(message.text.strip()); assert 1 <= b <= 120
    except: await message.answer("<blockquote>❌ 1..120.</blockquote>"); return
    await state.update_data(poker_blind=b); await _show_confirm(message, state)


async def _build_confirm_text(state: FSMContext) -> str:
    data = await state.get_data()
    game_label = {"poker": "🃏 Покер", "wheel": "🎡 Колесо", "wheel_1v1": "⚔️ Колесо 1×1",
                  "bracket": "🏆 Турнир с сеткой"}.get(data["game_type"], "?")
    lines = ["<b>✅ ПОДТВЕРЖДЕНИЕ</b>", SEP,
             f"<b>🏆 Название:</b> <code>{esc(data['name'])}</code>",
             f"<b>🎁 Приз:</b> <i>{esc(data.get('prize','—'))}</i>",
             f"<b>Игра:</b> {game_label}"]
    if data["game_type"] != "wheel_1v1":
        acc = "🟢 Открытый" if data.get("access_type") == "open" else "🔴 Закрытый"
        lines.append(f"<b>Доступ:</b> {acc}")
    if data["game_type"] == "poker":
        lines += [f"🔹 Стек: <code>{data['poker_stack']}</code>",
                  f"🔹 Ход: <code>{data['poker_turn']} сек</code>",
                  f"🔹 Блайнды: <code>{data['poker_blind']} мин</code>"]
    elif data["game_type"] == "wheel_1v1":
        async with SessionLocal() as s:
            u1 = await s.get(User, data["w1v1_p1_id"])
            u2 = await s.get(User, data["w1v1_p2_id"])
        lines += [f"🔹 Игрок 1: <code>{esc(_display_name(u1, data['w1v1_p1_id']))}</code>",
                  f"🔹 Игрок 2: <code>{esc(_display_name(u2, data['w1v1_p2_id']))}</code>",
                  "\n<blockquote>После создания оба игрока получат приглашение.</blockquote>"]
    elif data["game_type"] == "bracket":
        size = data.get("bracket_size", 4)
        series = data.get("bracket_series", 3)
        players = data.get("bracket_players", [])
        is_open = data.get("access_type") == "open"
        lines += [f"🔹 Размер сетки: <code>{size}</code>",
                  f"🔹 Формат боя: <b>до {series} побед</b>"]
        if is_open:
            lines.append("🔹 Игроки: <i>набираются через «Открытые турниры»</i>")
            lines.append(f"\n<blockquote>Открытый турнир — игроки сами подают заявки. "
                         f"Старт, когда наберётся {size} принятых.</blockquote>")
        else:
            async with SessionLocal() as s:
                names = []
                for uid in players:
                    u = await s.get(User, uid)
                    names.append(_display_name(u, uid))
            lines.append(f"🔹 Игроки: {', '.join(esc(n) for n in names)}")
            lines.append(f"\n<blockquote>Все {size} игроков получат приглашение. "
                         "Турнир стартует, когда все примут.</blockquote>")
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
    try: await cb.message.edit_text(text, reply_markup=confirm_kb())
    except: await cb.message.answer(text, reply_markup=confirm_kb())


@router.callback_query(F.data == "tt_confirm")
async def tt_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    gt = data["game_type"]

    if gt == "wheel_1v1":
        p1 = data["w1v1_p1_id"]; p2 = data["w1v1_p2_id"]
        async with SessionLocal() as s:
            u1 = await s.get(User, p1); u2 = await s.get(User, p2)
            if not u1: u1 = User(id=p1, first_name=f"Игрок {p1}"); s.add(u1)
            if not u2: u2 = User(id=p2, first_name=f"Игрок {p2}"); s.add(u2)
            t = Tournament(name=data["name"], access_type="closed",
                           game_type="wheel_1v1", creator_id=cb.from_user.id,
                           start_stack=0, turn_time=0, blind_interval=0,
                           prize=data.get("prize"), status="registration")
            s.add(t); await s.commit(); await s.refresh(t); tid = t.id
            s.add(Participant(tournament_id=tid, user_id=p1, status="pending"))
            s.add(Participant(tournament_id=tid, user_id=p2, status="pending"))
            await s.commit()
        await state.clear(); await cb.answer("⚔️ Приглашения отправлены")
        for uid in (p1, p2):
            try:
                await cb.bot.send_message(uid,
                    f"<b>⚔️ Вас вызвали на дуэль!</b>\n{SEP}\n"
                    f"🏆 <code>{esc(data['name'])}</code>\n"
                    f"🎁 Приз: <i>{esc(data.get('prize') or '—')}</i>\n\n"
                    "<blockquote>Принять приглашение?</blockquote>",
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
                           prize=data.get("prize"),
                           status="registration")
            s.add(t); await s.commit(); await s.refresh(t); tid = t.id
            if access == "closed":
                for uid in players:
                    s.add(Participant(tournament_id=tid, user_id=uid, status="pending"))
            await s.commit()
        await state.clear()
        if access == "closed":
            await cb.answer(f"🏆 Приглашения отправлены ({size})")
            for uid in players:
                try:
                    await cb.bot.send_message(uid,
                        f"<b>🏆 Вас пригласили в турнир с сеткой!</b>\n{SEP}\n"
                        f"🏆 <code>{esc(data['name'])}</code>\n"
                        f"🎁 Приз: <i>{esc(data.get('prize') or '—')}</i>\n\n"
                        f"<blockquote>Размер сетки: {size}. Формат боя: до {series} побед. "
                        "Принять приглашение?</blockquote>",
                        reply_markup=invite_response_kb(tid))
                except Exception as e:
                    logging.warning("bracket invite fail uid=%s: %s", uid, e)
        else:
            await cb.answer(f"🏆 Открытый турнир ({size} мест, до {series} побед)")
        await _show_tournament_menu(cb, tid); return

    async with SessionLocal() as s:
        t = Tournament(name=data["name"], access_type=data["access_type"],
                       game_type=gt, creator_id=cb.from_user.id,
                       start_stack=data.get("poker_stack", 1000),
                       turn_time=data.get("poker_turn", 20),
                       blind_interval=data.get("poker_blind", 5),
                       prize=data.get("prize"))
        s.add(t); await s.commit(); await s.refresh(t); tid = t.id
    await state.clear(); await cb.answer("Создан!"); await _show_tournament_menu(cb, tid)


async def _show_tournament_menu(cb: CallbackQuery, tid: int):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
        parts = r.scalars().all()
    approved = [p for p in parts if p.status in ("approved", "winner")]
    pending_count = len([p for p in parts if p.status == "pending"])
    gt = {"poker": "🃏 Покер", "wheel": "🎡 Колесо", "wheel_1v1": "⚔️ Колесо 1×1",
          "bracket": "🏆 Турнир с сеткой"}.get(t.game_type, "?")
    status_label = t.status
    if t.game_type == "wheel_1v1" and t.status == "registration":
        status_label = f"ожидание приглашений ({len(approved)}/2)"
    if t.game_type == "bracket" and t.status == "registration":
        status_label = f"ожидание игроков ({len(approved)}/{t.bracket_size})"
    total_needed = t.bracket_size if t.game_type == "bracket" else (2 if t.game_type == "wheel_1v1" else None)
    total_label = f" / {total_needed}" if total_needed else ""
    text = (f"<b>🏆 ТУРНИР:</b> <code>{esc(t.name)}</code>\n{SEP}\n"
            f"<b>ID:</b> <code>{t.id}</code>\n"
            f"<b>Игра:</b> {gt}\n"
            f"<b>Доступ:</b> {'🟢 Открытый' if t.access_type=='open' else '🔴 Закрытый'}\n"
            f"<b>Статус:</b> <code>{status_label}</code>\n"
            f"<b>Участников:</b> <code>{len(approved)}{total_label}</code>")
    if t.game_type == "bracket":
        text += f"\n<b>Формат боя:</b> до <code>{getattr(t, 'series_target', 3)}</code> побед"
    if pending_count > 0:
        text += f"\n<b>⏳ Ожидают подтверждения:</b> <code>{pending_count}</code>"
    text += f"\n<b>🎁 Приз:</b> <i>{esc(t.prize or '—')}</i>"
    kb = manage_tournament_kb(t.id, t.status, t.game_type, t.access_type)
    try: await cb.message.edit_text(text, reply_markup=kb)
    except: await cb.message.answer(text, reply_markup=kb)


async def _show_player_tournament_menu(cb: CallbackQuery, tid: int):
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
        parts = r.scalars().all()
    approved = [p for p in parts if p.status in ("approved", "winner")]
    gt = {"poker": "🃏 Покер", "wheel": "🎡 Колесо", "wheel_1v1": "⚔️ Колесо 1×1",
          "bracket": "🏆 Турнир с сеткой"}.get(t.game_type, "?")
    text = (f"<b>🏆 ТУРНИР:</b> <code>{esc(t.name)}</code>\n{SEP}\n"
            f"<b>Игра:</b> {gt}\n"
            f"<b>Статус:</b> <code>{t.status}</code>\n"
            f"<b>Участников:</b> <code>{len(approved)}</code>\n"
            f"<b>🎁 Приз:</b> <i>{esc(t.prize or '—')}</i>")
    try: await cb.message.edit_text(text, reply_markup=player_tournament_kb(t.id, t.status))
    except: await cb.message.answer(text, reply_markup=player_tournament_kb(t.id, t.status))


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
        emoji = "🃏" if t.game_type == "poker" else ("⚔️" if t.game_type == "wheel_1v1" else ("🏆" if t.game_type == "bracket" else "🎡"))
        lock = "🟢" if t.access_type == "open" else "🔴"
        rows.append([InlineKeyboardButton(text=f"{emoji}{lock} {t.name} · {t.status}",
                                          callback_data=f"tt_manage_{t.id}")])
    await cb.message.edit_text(f"<b>📂</b>\n{SEP}",
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
        g = gm.get_wheel_1v1(tid)
        if not g: await cb.answer("Игра не начата", show_alert=True); return
        if g.finished: await cb.answer("Уже завершено", show_alert=True); return
        if not g.both_ready(): await cb.answer("Ждём готовности игроков", show_alert=True); return
        g.spin(); await gm.broadcast(tid, g.to_dict())
        await cb.answer("🎡 Колесо крутится!"); return

    g = gm.get_wheel(tid)
    if not g: await cb.answer("Колесо не активно", show_alert=True); return
    if g.finished: await cb.answer("Турнир завершён", show_alert=True); return
    if len(g.active) < 2: await cb.answer("Мало игроков", show_alert=True); return
    g.spin(); await gm.broadcast(tid, g.to_dict())
    await cb.answer("🎡 Колесо крутится!")


@router.callback_query(F.data.startswith("tt_join_"))
async def cb_join_tournament(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t: await cb.answer("Нет", show_alert=True); return
        if t.game_type == "wheel_1v1":
            await cb.answer("Это закрытый турнир", show_alert=True); return
        if t.game_type == "bracket" and t.access_type != "open":
            await cb.answer("Это закрытый турнир", show_alert=True); return
        if t.game_type == "bracket":
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.status.in_(("approved", "winner"))))
            approved_count = len(r.scalars().all())
            if approved_count >= t.bracket_size:
                await cb.answer("Мест нет", show_alert=True); return
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        existing = r.scalar_one_or_none()
        if existing and existing.status in ("approved", "winner"):
            await cb.answer("Вы уже в турнире", show_alert=True); return
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
    for d in (gm._active_poker, gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket):
        d.pop(tid, None)
    _finalized_tids.discard(tid)
    await gm.broadcast(tid, {"type": "tournament_cancelled", "tid": tid})
    await cb.answer("Удалено", show_alert=True)
    await cb.message.edit_text(f"<b>🗑 Удалён</b>\nID: <code>{tid}</code>")


@router.callback_query(F.data.startswith("tt_delete_"))
async def cb_delete_prompt(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await cb.message.edit_text(
        f"<b>⚠️ УДАЛИТЬ?</b>\n{SEP}\nID: <code>{tid}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да", callback_data=f"tt_delete_confirm_{tid}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"tt_manage_{tid}")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_end_confirm_"))
async def cb_end_confirm(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    for d in (gm._active_poker, gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket):
        d.pop(tid, None)
    _finalized_tids.discard(tid)
    await _finalize_tournament(cb.bot, tid, manual=True)
    await cb.answer("Завершено", show_alert=True)
    await cb.message.edit_text(f"<b>🏁 Завершён вручную</b>\n{SEP}\nID: <code>{tid}</code>")


@router.callback_query(F.data.startswith("tt_end_"))
async def cb_end_prompt(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await cb.message.edit_text(
        f"<b>⚠️ ЗАКОНЧИТЬ ТУРНИР?</b>\n{SEP}\nID: <code>{tid}</code>\n\n"
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
    if not ok: await cb.answer("Недостаточно игроков", show_alert=True); return
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
        tstack, tturn, tblind = t.start_stack, t.turn_time, t.blind_interval
    if tgame == "poker":
        g = PokerGame(tid, players, start_stack=tstack, turn_time=tturn, blind_interval=tblind)
        gm.set_poker(tid, g); g.start_hand()
        await gm.broadcast(tid, g.to_dict())
    else:
        g = WheelGame(tid, players, tprize or "🏆 Приз")
        gm.set_wheel(tid, g); await gm.broadcast(tid, g.to_dict())
    if notify:
        for p in parts:
            try:
                await bot.send_message(p.user_id,
                    f"<b>🚀 Турнир начался!</b>\n{SEP}\n"
                    f"🏆 <code>{esc(tname)}</code>\n👥 <code>{len(parts)}</code>\n\n"
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
        try:
            await bot.send_message(p["user_id"],
                f"<b>🏆 Турнир с сеткой стартовал!</b>\n{SEP}\n"
                f"🏆 <code>{esc(tname)}</code>\n"
                f"🥇 Формат боя: <b>до {series} побед</b>\n\n"
                "<blockquote>Открой Mini App → вкладка «Сетка турнира».</blockquote>",
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
    for d in (gm._active_poker, gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket):
        d.pop(tid, None)
    await gm.broadcast(tid, {"type": "tournament_cancelled", "tid": tid})
    await cb.answer("Отменён"); await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_invite_"))
async def cb_invite(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await state.update_data(invite_tid=tid, is_kick=False); await state.set_state(InviteFSM.user_id)
    await cb.message.edit_text(f"<b>✉️ Приглашение</b>\n{SEP}\nID или @username:",
                               reply_markup=back_kb(f"tt_manage_{tid}"))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_kick_"))
async def cb_kick(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id): await cb.answer("Нет", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await state.update_data(invite_tid=tid, is_kick=True); await state.set_state(InviteFSM.user_id)
    await cb.message.edit_text(f"<b>🚫 Кик</b>\n{SEP}\nID или @username:",
                               reply_markup=back_kb(f"tt_manage_{tid}"))
    await cb.answer()


@router.message(InviteFSM.user_id)
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
                    await message.answer(f"<b>ℹ️ Игрок уже принял приглашение</b>\n🆔 <code>{uid}</code>")
                    await state.clear(); return
                p.status = "pending"
            else:
                s.add(Participant(tournament_id=tid, user_id=uid, status="pending"))
            await s.commit()
            tname = t.name
            try:
                await message.bot.send_message(uid,
                    f"<b>✉️ Приглашение!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
                    "<blockquote>Примешь?</blockquote>",
                    reply_markup=invite_response_kb(tid))
            except: pass
            await message.answer(f"<b>✅ Приглашение отправлено</b>\n🆔 <code>{uid}</code>\n⏳ Ожидает подтверждения")
    await state.clear()


async def _do_kick_player(bot, tid, t, uid, participant_row):
    async with SessionLocal() as s:
        p = await s.get(Participant, participant_row.id) if participant_row else None
        if p: p.status = "kicked"; await s.commit()

    tgame = t.game_type
    tname = t.name

    if tgame == "poker":
        g = gm.get_poker(tid)
        if g:
            for tb in g.tables:
                for pl in tb.players:
                    if pl.user_id == uid and pl.chips > 0:
                        pl.folded = True; pl.chips = 0
                        if pl.place is None:
                            alive = [x for x in tb.players if x.chips > 0]
                            pl.place = len(alive) + 1
                        break
            await _broadcast_poker(tid, g)
    elif tgame == "wheel":
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
                try:
                    await bot.send_message(adv["user_id"],
                        f"<b>🏆 Соперник выбыл</b>\n{SEP}\n"
                        f"🏆 <code>{esc(tname)}</code>\n\n"
                        "<blockquote>Вы автоматически прошли дальше по сетке!</blockquote>")
                except Exception as e:
                    logging.warning("auto-advance notify fail: %s", e)
            await gm.broadcast(tid, g.to_dict())

    try:
        await bot.send_message(uid,
            f"<b>🚫 Вас исключили из турнира</b>\n{SEP}\n"
            f"🏆 <code>{esc(tname)}</code>")
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
            emoji = "🃏" if t.game_type == "poker" else ("⚔️" if t.game_type == "wheel_1v1" else ("🏆" if t.game_type == "bracket" else "🎡"))
            sl = {"pending": "⏳", "approved": "✅", "winner": "🏆"}.get(p.status, t.status)
            lines.append(f"  {emoji} <b>{esc(t.name)}</b> · {sl}")
            rows.append([InlineKeyboardButton(text=f"📋 {t.name[:22]}",
                                              callback_data=f"tt_player_{t.id}")])
            has = True
    if not has: lines.append("<blockquote>ℹ️ Нет активных.</blockquote>")
    if rows: await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else: await cb.message.edit_text("\n".join(lines))
    await cb.answer()


@router.callback_query(F.data == "open_tournaments")
async def cb_open(cb: CallbackQuery):
    async with SessionLocal() as s:
        r = await s.execute(select(Tournament).where(
            Tournament.access_type == "open", Tournament.status == "registration",
            Tournament.game_type != "wheel_1v1"))
        tours = r.scalars().all()
    if not tours:
        await cb.message.edit_text(f"<b>🌐 Открытые</b>\n{SEP}\n<blockquote>Нет.</blockquote>")
        await cb.answer(); return
    lines = [f"<b>🌐 Открытые</b>\n{SEP}"]; rows = []
    for t in tours:
        emoji = "🃏" if t.game_type == "poker" else ("🏆" if t.game_type == "bracket" else "🎡")
        extra = f" · {t.bracket_size} игр. · до {getattr(t, 'series_target', 3)} поб." if t.game_type == "bracket" else ""
        lines.append(f"{emoji} <b>{esc(t.name)}</b> · <i>{esc(t.prize or '—')}</i>{extra}")
        rows.append([InlineKeyboardButton(text=f"📩 Заявка · {t.name[:20]}",
                                          callback_data=f"apply_{t.id}")])
    await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
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
    try:
        await cb.bot.send_message(uid,
            f"<b>✅ Одобрено!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
            "<blockquote>Жди старт.</blockquote>")
    except: pass
    await cb.message.edit_text(f"<b>✅ Принят</b>"); await cb.answer("Принято")
    if gtype == "bracket":
        try: await _auto_start_bracket(cb.bot, tid)
        except Exception as e: logging.warning("auto start after approve: %s", e)


@router.callback_query(F.data.startswith("reject_"))
async def cb_reject(cb: CallbackQuery):
    pid = int(cb.data.split("_")[-1])
    async with SessionLocal() as s:
        p = await s.get(Participant, pid)
        if not p: await cb.answer("Нет", show_alert=True); return
        p.status = "rejected"; t = await s.get(Tournament, p.tournament_id); await s.commit()
        tname = t.name if t else "?"; uid = p.user_id
    try:
        await cb.bot.send_message(uid, f"<b>❌ Отклонена</b>\n🏆 <code>{esc(tname)}</code>")
    except: pass
    await cb.message.edit_text(f"<b>❌ Отклонён</b>"); await cb.answer("Отклонено")


@router.callback_query(F.data.startswith("accept_invite_"))
async def cb_accept_inv(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    start_1v1 = False; players_1v1 = []
    tname = ""; tprize = ""
    is_bracket = False
    async with SessionLocal() as s:
        t = await s.get(Tournament, tid)
        if not t:
            await cb.answer("Турнир не найден", show_alert=True); return
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        p = r.scalar_one_or_none()
        if not p:
            await cb.answer("Вы не участник", show_alert=True); return
        if p.status == "approved":
            await cb.answer("Вы уже приняли приглашение", show_alert=True); return
        p.status = "approved"
        u = await s.get(User, cb.from_user.id)
        if u is None:
            s.add(User(id=cb.from_user.id, username=cb.from_user.username,
                       first_name=cb.from_user.first_name))
        else:
            u.username = cb.from_user.username; u.first_name = cb.from_user.first_name
        await s.commit()
        tname = t.name; tprize = t.prize
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
        elif t.game_type == "bracket":
            is_bracket = True
    try:
        if is_bracket:
            await cb.message.edit_text(
                f"<b>✅ Вы приняли приглашение!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
                "<blockquote>Ждём остальных участников…</blockquote>")
        else:
            await cb.message.edit_text(
                f"<b>✅ Вы приняли приглашение!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
                "<blockquote>Ждём второго игрока...</blockquote>")
    except: pass
    await cb.answer("Принято!")
    if start_1v1 and len(players_1v1) == 2:
        g = Wheel1v1Game(tid, players_1v1[0], players_1v1[1], tprize or "🏆 Приз")
        gm.set_wheel_1v1(tid, g)
        await gm.broadcast(tid, {"type": "wheel_1v1_ready"})
        for pl in players_1v1:
            try:
                await cb.bot.send_message(pl["user_id"],
                    f"<b>⚔️ Оба игрока приняли вызов!</b>\n{SEP}\n"
                    f"🏆 <code>{esc(tname)}</code>\n\n"
                    "<blockquote>Зайди в Mini App, нажми «Готов».</blockquote>",
                    reply_markup=enter_webapp_kb(tid, WEBAPP_URL))
            except: pass
    if is_bracket:
        await _auto_start_bracket(cb.bot, tid)


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
        await cb.message.edit_text("<blockquote>❌ Вы отклонили приглашение.</blockquote>")
    except: pass
    await cb.answer("Отклонено")
    if cancel_tour:
        async with SessionLocal() as s:
            r = await s.execute(select(Participant).where(Participant.tournament_id == tid))
            for pp in r.scalars().all():
                if pp.user_id != cb.from_user.id:
                    try:
                        await cb.bot.send_message(pp.user_id,
                            f"<b>❌ Турнир отменён</b>\n{SEP}\n"
                            f"🏆 <code>{esc(tname)}</code>\n\n"
                            "<blockquote>Один из игроков отклонил приглашение.</blockquote>")
                    except: pass
        await gm.broadcast(tid, {"type": "tournament_cancelled", "tid": tid})


# ============ ФИНАЛИЗАЦИЯ ============
async def _notify_creator_and_users(bot, tname, tprize, winner_id, winner_name, winner_uname,
                                     manual, user_ids, total, creator_id):
    try:
        await asyncio.sleep(7)
        if creator_id:
            try:
                if manual:
                    creator_text = (f"<b>🏁 Турнир завершён</b>\n{SEP}\n"
                                    f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                                    "<b>Остановлен администратором.</b>\n"
                                    f"👥 Участников: <code>{total}</code>")
                elif winner_id:
                    uname_part = f" (@{winner_uname})" if winner_uname else ""
                    creator_text = (f"<b>🏁 Турнир завершён!</b>\n{SEP}\n"
                                    f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                                    f"🥇 Победитель: <code>{esc(winner_name or '—')}</code>{uname_part}\n"
                                    f"🎁 Приз: <i>{esc(tprize)}</i>\n"
                                    f"👥 Участников: <code>{total}</code>")
                else:
                    creator_text = (f"<b>🏁 Турнир завершён</b>\n{SEP}\n"
                                    f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                                    f"👥 Участников: <code>{total}</code>")
                await bot.send_message(creator_id, creator_text)
            except Exception as e:
                logging.warning("creator notify fail: %s", e)
        for uid in user_ids:
            try:
                if manual:
                    text = (f"<b>🏁 ТУРНИР ЗАВЕРШЁН</b>\n{SEP}\n"
                            f"🏆 <b>{esc(tname)}</b>\n{SEP}\n"
                            "<b>Турнир остановлен администратором.</b>\n"
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
            if u:
                u.wins = (u.wins or 0) + 1
                winner_name = winner_name or u.first_name or str(winner_id)
                winner_uname = u.username
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


async def _broadcast_poker(tid, g):
    if not gm._connections.get(tid): return
    for ws in gm._connections[tid]:
        try: await ws.send_json(g.to_dict())
        except Exception: gm.disconnect(tid, ws)


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


async def game_ticker(bot):
    while True:
        try:
            for tid, g in list(gm._active_poker.items()):
                if getattr(g, "finished", False):
                    if tid not in _finalized_tids:
                        _finalized_tids.add(tid)
                        wid = g.winner_info.get("user_id") if getattr(g, "winner_info", None) else None
                        wname = g.winner_info.get("name") if getattr(g, "winner_info", None) else None
                        await _finalize_tournament(bot, tid, wid, wname, manual=False)
                    continue
                changed = False
                if g._rebalance(): changed = True
                if g.check_timeout(): changed = True
                for tb in g.tables:
                    if tb.phase == "showdown":
                        if getattr(tb, "_showdown_at", None):
                            if asyncio.get_event_loop().time() - tb._showdown_at > 4:
                                tb.start_hand(); changed = True
                        else: tb._showdown_at = asyncio.get_event_loop().time()
                if changed: await _broadcast_poker(tid, g)
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


async def main():
    if not BOT_TOKEN: raise SystemExit("❌ BOT_TOKEN не задан")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(); dp.include_router(router)
    app = create_app(bot)
    cfg = uvicorn.Config(app, host=HOST, port=PORT, log_level="info", loop="asyncio")
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
