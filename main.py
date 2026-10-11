import asyncio
import html
import logging
import os
import uvicorn
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                            InlineKeyboardButton, WebAppInfo)
from dotenv import load_dotenv
from sqlalchemy import select

from database import (init_db, SessionLocal, User, Admin, Ban, Tournament,
                       Participant)
from webapp import create_app
import game as gm
from game import PokerGame, WheelGame

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


# ============ УТИЛИТЫ ============
def esc(s):
    return html.escape(s or "")


async def _is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    async with SessionLocal() as session:
        r = await session.execute(select(Admin).where(Admin.user_id == user_id))
        return r.scalar_one_or_none() is not None


async def _check_ban(user_id):
    async with SessionLocal() as session:
        return await session.get(Ban, user_id) is not None


async def _upsert_user(from_user):
    async with SessionLocal() as session:
        u = await session.get(User, from_user.id)
        if u is None:
            u = User(id=from_user.id, username=from_user.username,
                     first_name=from_user.first_name)
            session.add(u)
        else:
            u.username = from_user.username
            u.first_name = from_user.first_name
        await session.commit()


# ============ КЛАВИАТУРЫ ============
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Открытые турниры", callback_data="open_tournaments")],
        [InlineKeyboardButton(text="🎟 Мои турниры", callback_data="my_tournaments")],
        [InlineKeyboardButton(text="🆕 Создать турнир", callback_data="create_tournament_btn")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])


def owner_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="owner_add_admin")],
        [InlineKeyboardButton(text="➖ Удалить админа", callback_data="owner_del_admin")],
        [InlineKeyboardButton(text="📋 Список админов", callback_data="owner_list_admins")],
        [InlineKeyboardButton(text="🚫 Забанить игрока", callback_data="owner_ban")],
        [InlineKeyboardButton(text="✅ Разбанить игрока", callback_data="owner_unban")],
        [InlineKeyboardButton(text="📜 Черный список", callback_data="owner_blacklist")],
    ])


def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Создать турнир", callback_data="admin_create_tournament")],
        [InlineKeyboardButton(text="📂 Управление турнирами", callback_data="admin_manage_tournaments")],
    ])


def back_kb(target="back_main"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
    ])


def access_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Открытый", callback_data="tt_access_open")],
        [InlineKeyboardButton(text="🔴 Закрытый", callback_data="tt_access_closed")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")],
    ])


def game_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🃏 Покер", callback_data="tt_game_poker")],
        [InlineKeyboardButton(text="🎡 Колесо Фортуны", callback_data="tt_game_wheel")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")],
    ])


def confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="tt_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_fsm")],
    ])


def join_request_kb(pid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{pid}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{pid}")]
    ])


def invite_response_kb(tid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_invite_{tid}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_invite_{tid}")]
    ])


def enter_webapp_kb(tid, url):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Войти в турнир (Mini App)",
                              web_app=WebAppInfo(url=f"{url}/?tid={tid}"))]
    ])


def manage_tournament_kb(tid, status):
    rows = []
    rows.append([InlineKeyboardButton(
        text="🎮 Открыть Mini App",
        web_app=WebAppInfo(url=f"{WEBAPP_URL}/?tid={tid}"))])
    if status == "registration":
        rows.append([InlineKeyboardButton(text="▶️ Начать турнир", callback_data=f"tt_start_{tid}")])
        rows.append([InlineKeyboardButton(text="✉️ Пригласить игрока", callback_data=f"tt_invite_{tid}")])
    if status == "running":
        rows.append([InlineKeyboardButton(text="⏸ Пауза", callback_data=f"tt_pause_{tid}")])
        rows.append([InlineKeyboardButton(text="✉️ Пригласить игрока", callback_data=f"tt_invite_{tid}")])
        rows.append([InlineKeyboardButton(text="🚫 Кик игрока", callback_data=f"tt_kick_{tid}")])
        rows.append([InlineKeyboardButton(text="🛑 Отменить", callback_data=f"tt_cancel_{tid}")])
    if status == "paused":
        rows.append([InlineKeyboardButton(text="▶️ Продолжить", callback_data=f"tt_resume_{tid}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_manage_tournaments")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============ FSM ============
class OwnerForm(StatesGroup):
    add_admin = State()
    ban_id = State()
    unban_id = State()


class TournamentFSM(StatesGroup):
    access_type = State()
    game_type = State()
    name = State()
    prize = State()
    poker_stack = State()
    poker_turn = State()
    poker_blind = State()
    confirm = State()


class InviteFSM(StatesGroup):
    user_id = State()


# ============ ТЕКСТ ПОМОЩИ ============
def _help_text():
    return (
        "<b>ℹ️ ПОМОЩЬ И ПРАВИЛА</b>\n"
        f"{SEP}\n"
        "<b>🃏 Покер (Техасский Холдем):</b>\n"
        "▫️ Каждому игроку раздаётся 2 карты.\n"
        "▫️ На столе — 5 общих карт (флоп/тёрн/ривер).\n"
        "▫️ Собирается лучшая комбинация из 7 карт.\n"
        "▫️ Блайнды растут каждые N минут.\n\n"
        "<b>🎡 Колесо Фортуны:</b>\n"
        "▫️ На секторах колеса — имена участников.\n"
        "▫️ Крутится колесо — выпавший игрок выбывает.\n"
        "▫️ Последний оставшийся — победитель 🏆.\n\n"
        "<blockquote>🎯 /start — меню, /owner — владелец, /admin — админ.\n"
        "💬 Напиши «меню» в чат — откроется главное меню.</blockquote>"
    )


def _main_menu_text():
    return f"<b>🏠 Главное меню</b>\n{SEP}\nВыбери раздел:"


# ============ СТАРТ И МЕНЮ ============
@router.message(CommandStart())
async def cmd_start(message: Message):
    if await _check_ban(message.from_user.id):
        await message.answer("<blockquote>⛔ Ваша учетная запись заблокирована!</blockquote>")
        return
    await _upsert_user(message.from_user)
    await message.answer(
        f"<b>👋 Добро пожаловать, {esc(message.from_user.first_name)}!</b>\n"
        f"{SEP}\nЭто <b>турнирная экосистема</b> в Telegram.\n\n"
        "<blockquote>🎯 Участвуй в турнирах и побеждай!</blockquote>",
        reply_markup=main_menu_kb())


@router.message(Command("menu"))
@router.message(StateFilter(None), F.text.lower() == "меню")
async def cmd_menu_text(message: Message):
    if await _check_ban(message.from_user.id):
        await message.answer("<blockquote>⛔ Ваша учетная запись заблокирована!</blockquote>")
        return
    await _upsert_user(message.from_user)
    await message.answer(_main_menu_text(), reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(_help_text())


@router.message(Command("owner"))
async def cmd_owner(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("<blockquote>⛔ Доступ только для владельца.</blockquote>")
        return
    await message.answer(
        f"<b>👑 ПАНЕЛЬ ВЛАДЕЛЬЦА</b>\n{SEP}\nУправление:",
        reply_markup=owner_menu_kb())


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await _is_admin(message.from_user.id):
        await message.answer("<blockquote>⛔ Доступ только для админов.</blockquote>")
        return
    await message.answer(
        f"<b>🛠 ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n{SEP}\nСоздание и управление турнирами:",
        reply_markup=admin_menu_kb())


# ============ ПАНЕЛЬ ВЛАДЕЛЬЦА ============
@router.callback_query(F.data == "owner_back")
async def cb_owner_back(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    await state.clear()
    await cb.message.edit_text(
        f"<b>👑 ПАНЕЛЬ ВЛАДЕЛЬЦА</b>\n{SEP}", reply_markup=owner_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "owner_add_admin")
async def cb_add_admin(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    await state.set_state(OwnerForm.add_admin)
    await cb.message.edit_text(
        f"<b>➕ Добавить админа</b>\n{SEP}\nОтправь Telegram ID или @username:",
        reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.add_admin)
async def msg_add_admin(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    raw = message.text.strip()
    target_id = None
    async with SessionLocal() as session:
        if raw.startswith("@"):
            r = await session.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if u:
                target_id = u.id
        else:
            try:
                target_id = int(raw)
            except ValueError:
                target_id = None
        if target_id is None:
            await message.answer("<blockquote>❌ Пользователь не найден.</blockquote>")
            return
        r = await session.execute(select(Admin).where(Admin.user_id == target_id))
        if r.scalar_one_or_none():
            await message.answer("<blockquote>ℹ️ Уже админ.</blockquote>")
            return
        session.add(Admin(user_id=target_id))
        await session.commit()
    await state.clear()
    await message.answer(
        f"<b>✅ Админ добавлен</b>\n{SEP}\n🆔 <code>{target_id}</code>",
        reply_markup=owner_menu_kb())


@router.callback_query(F.data == "owner_del_admin")
async def cb_del_admin(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    async with SessionLocal() as session:
        r = await session.execute(select(Admin))
        admins = r.scalars().all()
    if not admins:
        await cb.message.edit_text(
            "<blockquote>ℹ️ Нет админов.</blockquote>",
            reply_markup=back_kb("owner_back"))
        await cb.answer(); return
    rows = [[InlineKeyboardButton(text=f"❌ {a.user_id}", callback_data=f"owner_rm_{a.user_id}")]
            for a in admins]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="owner_back")])
    await cb.message.edit_text(
        f"<b>➖ Удалить админа</b>\n{SEP}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("owner_rm_"))
async def cb_rm_admin(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    uid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        r = await session.execute(select(Admin).where(Admin.user_id == uid))
        a = r.scalar_one_or_none()
        if a:
            await session.delete(a)
            await session.commit()
    await cb.answer("Удалён")
    await cb_del_admin(cb)


@router.callback_query(F.data == "owner_list_admins")
async def cb_list_admins(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    async with SessionLocal() as session:
        r = await session.execute(select(Admin))
        admins = r.scalars().all()
    if not admins:
        text = f"<b>📋 Админы</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"
    else:
        lines = [f"<b>📋 Админы</b>\n{SEP}"]
        for a in admins:
            lines.append(f"🔹 <code>{a.user_id}</code> · {a.added_at:%d.%m.%Y}")
        text = "\n".join(lines)
    await cb.message.edit_text(text, reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.callback_query(F.data == "owner_ban")
async def cb_ban(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    await state.set_state(OwnerForm.ban_id)
    await cb.message.edit_text(
        f"<b>🚫 Забанить</b>\n{SEP}\nОтправь ID:",
        reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.ban_id)
async def msg_ban(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("<blockquote>❌ Нужен числовой ID.</blockquote>")
        return
    async with SessionLocal() as session:
        if await session.get(Ban, uid) is None:
            session.add(Ban(user_id=uid))
            await session.commit()
    await state.clear()
    try:
        await message.bot.send_message(
            uid, "<blockquote>⛔ Вы заблокированы!</blockquote>")
    except Exception:
        pass
    await message.answer(
        f"<b>✅ Забанен</b>\n{SEP}\n🆔 <code>{uid}</code>",
        reply_markup=owner_menu_kb())


@router.callback_query(F.data == "owner_unban")
async def cb_unban(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    await state.set_state(OwnerForm.unban_id)
    await cb.message.edit_text(
        f"<b>✅ Разбанить</b>\n{SEP}\nОтправь ID:",
        reply_markup=back_kb("owner_back"))
    await cb.answer()


@router.message(OwnerForm.unban_id)
async def msg_unban(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("<blockquote>❌ Нужен числовой ID.</blockquote>")
        return
    async with SessionLocal() as session:
        b = await session.get(Ban, uid)
        if b:
            await session.delete(b)
            await session.commit()
    await state.clear()
    await message.answer(
        f"<b>✅ Разбанен</b>\n{SEP}\n🆔 <code>{uid}</code>",
        reply_markup=owner_menu_kb())


@router.callback_query(F.data == "owner_blacklist")
async def cb_blacklist(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("Нет доступа", show_alert=True); return
    async with SessionLocal() as session:
        r = await session.execute(select(Ban))
        bans = r.scalars().all()
    if not bans:
        text = f"<b>📜 Чёрный список</b>\n{SEP}\n<blockquote>Пусто.</blockquote>"
    else:
        lines = [f"<b>📜 Чёрный список</b>\n{SEP}"]
        for b in bans:
            lines.append(f"🚫 <code>{b.user_id}</code> · {b.banned_at:%d.%m.%Y}")
        text = "\n".join(lines)
    await cb.message.edit_text(text, reply_markup=back_kb("owner_back"))
    await cb.answer()


# ============ ПАНЕЛЬ АДМИНА ============
@router.callback_query(F.data == "admin_back")
async def cb_admin_back(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    await state.clear()
    await cb.message.edit_text(
        f"<b>🛠 ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n{SEP}",
        reply_markup=admin_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "admin_cancel_fsm")
async def cb_cancel_fsm(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        f"<b>🛠 ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n{SEP}",
        reply_markup=admin_menu_kb())
    await cb.answer("Отменено")


@router.callback_query(F.data == "create_tournament_btn")
async def cb_create_from_menu(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        await cb.answer(
            "⛔ Турнир может создать только админ — обращаться к @Dimo0nchik",
            show_alert=True)
        return
    await state.clear()
    await state.set_state(TournamentFSM.access_type)
    await cb.message.edit_text(
        f"<b>🆕 Создание турнира</b>\n{SEP}\nВыбери <b>формат доступа</b>:",
        reply_markup=access_type_kb())
    await cb.answer()


@router.callback_query(F.data == "admin_create_tournament")
async def cb_create_tour(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    await state.clear()
    await state.set_state(TournamentFSM.access_type)
    await cb.message.edit_text(
        f"<b>🆕 Создание турнира</b>\n{SEP}\nВыбери <b>формат доступа</b>:",
        reply_markup=access_type_kb())
    await cb.answer()


@router.callback_query(F.data == "tt_access_open")
async def tt_open(cb: CallbackQuery, state: FSMContext):
    await state.update_data(access_type="open")
    await state.set_state(TournamentFSM.game_type)
    await cb.message.edit_text(
        f"<b>🆕 Создание турнира</b>\n{SEP}\nВыбери <b>игру</b>:",
        reply_markup=game_type_kb())
    await cb.answer()


@router.callback_query(F.data == "tt_access_closed")
async def tt_closed(cb: CallbackQuery, state: FSMContext):
    await state.update_data(access_type="closed")
    await state.set_state(TournamentFSM.game_type)
    await cb.message.edit_text(
        f"<b>🆕 Создание турнира</b>\n{SEP}\nВыбери <b>игру</b>:",
        reply_markup=game_type_kb())
    await cb.answer()


@router.callback_query(F.data == "tt_game_poker")
async def tt_poker(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="poker")
    await state.set_state(TournamentFSM.name)
    await cb.message.edit_text(
        f"<b>🆕 Создание турнира</b>\n{SEP}\nВведи <b>название турнира</b>:")
    await cb.answer()


@router.callback_query(F.data == "tt_game_wheel")
async def tt_wheel(cb: CallbackQuery, state: FSMContext):
    await state.update_data(game_type="wheel")
    await state.set_state(TournamentFSM.name)
    await cb.message.edit_text(
        f"<b>🆕 Создание турнира</b>\n{SEP}\nВведи <b>название турнира</b>:")
    await cb.answer()


@router.message(TournamentFSM.name)
async def tt_name(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    name = message.text.strip()[:100]
    await state.update_data(name=name)
    await state.set_state(TournamentFSM.prize)
    await message.answer(
        f"<b>🆕 Создание турнира</b>\n{SEP}\nВведи <b>описание приза</b>:")


@router.message(TournamentFSM.prize)
async def tt_prize(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    prize = message.text.strip()[:200]
    await state.update_data(prize=prize)
    data = await state.get_data()
    if data["game_type"] == "poker":
        await state.set_state(TournamentFSM.poker_stack)
        await message.answer(
            f"<b>🆕 Параметры покера</b>\n{SEP}\nВведи <b>стартовый стек фишек</b>:")
    else:
        await _show_confirm(message, state)


@router.message(TournamentFSM.poker_stack)
async def tt_stack(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    try:
        stack = int(message.text.strip())
        assert 10 <= stack <= 1_000_000
    except Exception:
        await message.answer("<blockquote>❌ Целое от 10 до 1 000 000.</blockquote>")
        return
    await state.update_data(poker_stack=stack)
    await state.set_state(TournamentFSM.poker_turn)
    await message.answer(
        f"<b>🆕 Параметры покера</b>\n{SEP}\nВведи <b>время на ход (сек)</b>:")


@router.message(TournamentFSM.poker_turn)
async def tt_turn(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    try:
        t = int(message.text.strip())
        assert 5 <= t <= 300
    except Exception:
        await message.answer("<blockquote>❌ Целое от 5 до 300.</blockquote>")
        return
    await state.update_data(poker_turn=t)
    await state.set_state(TournamentFSM.poker_blind)
    await message.answer(
        f"<b>🆕 Параметры покера</b>\n{SEP}\nВведи <b>рост блайндов (мин)</b>:")


@router.message(TournamentFSM.poker_blind)
async def tt_blind(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    try:
        b = int(message.text.strip())
        assert 1 <= b <= 120
    except Exception:
        await message.answer("<blockquote>❌ Целое от 1 до 120.</blockquote>")
        return
    await state.update_data(poker_blind=b)
    await _show_confirm(message, state)


async def _show_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(TournamentFSM.confirm)
    lines = [
        "<b>✅ ПОДТВЕРЖДЕНИЕ</b>", SEP,
        f"<b>🏆 Название:</b> <code>{esc(data['name'])}</code>",
        f"<b>🎁 Приз:</b> <i>{esc(data.get('prize', '—'))}</i>",
        f"<b>Доступ:</b> {'🟢 Открытый' if data['access_type']=='open' else '🔴 Закрытый'}",
        f"<b>Игра:</b> {'🃏 Покер' if data['game_type']=='poker' else '🎡 Колесо'}",
    ]
    if data["game_type"] == "poker":
        lines += [
            f"🔹 Стек: <code>{data['poker_stack']}</code>",
            f"🔹 Ход: <code>{data['poker_turn']} сек</code>",
            f"🔹 Блайнды: каждые <code>{data['poker_blind']} мин</code>",
        ]
    lines.append("\n<blockquote>Нажми ✅ для создания.</blockquote>")
    await message.answer("\n".join(lines), reply_markup=confirm_kb())


@router.callback_query(F.data == "tt_confirm")
async def tt_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with SessionLocal() as session:
        t = Tournament(
            name=data["name"], access_type=data["access_type"],
            game_type=data["game_type"], creator_id=cb.from_user.id,
            start_stack=data.get("poker_stack", 1000),
            turn_time=data.get("poker_turn", 20),
            blind_interval=data.get("poker_blind", 5),
            prize=data.get("prize"),
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        tid = t.id
    await state.clear()
    await cb.answer("Турнир создан!")
    await _show_tournament_menu(cb, tid)


async def _show_tournament_menu(cb: CallbackQuery, tid: int):
    async with SessionLocal() as session:
        t = await session.get(Tournament, tid)
        if not t:
            await cb.answer("Нет турнира", show_alert=True); return
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid,
            Participant.status.in_(("approved", "winner"))))
        parts = r.scalars().all()
    text = (
        f"<b>🏆 ТУРНИР:</b> <code>{esc(t.name)}</code>\n{SEP}\n"
        f"<b>ID:</b> <code>{t.id}</code>\n"
        f"<b>Игра:</b> {'🃏 Покер' if t.game_type=='poker' else '🎡 Колесо'}\n"
        f"<b>Доступ:</b> {'🟢 Открытый' if t.access_type=='open' else '🔴 Закрытый'}\n"
        f"<b>Статус:</b> <code>{t.status}</code>\n"
        f"<b>Участников:</b> <code>{len(parts)}</code>\n"
        f"<b>🎁 Приз:</b> <i>{esc(t.prize or '—')}</i>"
    )
    await cb.message.edit_text(text, reply_markup=manage_tournament_kb(t.id, t.status))


@router.callback_query(F.data == "admin_manage_tournaments")
async def cb_manage(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    async with SessionLocal() as session:
        r = await session.execute(select(Tournament).order_by(Tournament.id.desc()).limit(20))
        tours = r.scalars().all()
    rows = []
    for t in tours:
        emoji = "🃏" if t.game_type == "poker" else "🎡"
        lock = "🟢" if t.access_type == "open" else "🔴"
        rows.append([InlineKeyboardButton(
            text=f"{emoji}{lock} {t.name} · {t.status}",
            callback_data=f"tt_manage_{t.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    await cb.message.edit_text(
        f"<b>📂 Турниры</b>\n{SEP}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_manage_"))
async def cb_manage_one(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    await _show_tournament_menu(cb, tid)
    await cb.answer()


@router.callback_query(F.data.startswith("tt_start_"))
async def cb_start(cb: CallbackQuery):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        t = await session.get(Tournament, tid)
        if not t:
            await cb.answer("Нет", show_alert=True); return
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.status == "approved"))
        parts = r.scalars().all()
        if len(parts) < 2:
            await cb.answer("Нужно минимум 2 участника", show_alert=True); return
        players = []
        for p in parts:
            u = await session.get(User, p.user_id)
            players.append({
                "user_id": p.user_id,
                "name": (u.first_name if u and u.first_name else str(p.user_id)),
            })
            p.chips = t.start_stack
        t.status = "running"
        await session.commit()
        tname = t.name
        tgame = t.game_type
        tprize = t.prize
        tstack = t.start_stack
        tturn = t.turn_time
        tblind = t.blind_interval
    if tgame == "poker":
        g = PokerGame(tid, players, start_stack=tstack,
                      turn_time=tturn, blind_interval=tblind)
        gm.set_poker(tid, g)
        g.start_hand()
        await gm.broadcast(tid, g.to_dict())
    else:
        g = WheelGame(tid, players, tprize or "🏆 Приз")
        gm.set_wheel(tid, g)
        await gm.broadcast(tid, g.to_dict())
    for p in parts:
        try:
            await cb.bot.send_message(
                p.user_id,
                f"<b>🚀 Турнир запущен!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>\n\n"
                "<blockquote>Нажми кнопку для входа.</blockquote>",
                reply_markup=enter_webapp_kb(tid, WEBAPP_URL))
        except Exception:
            pass
    await cb.answer("Запущено!")
    await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_pause_"))
async def cb_pause(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        t = await session.get(Tournament, tid)
        if t:
            t.status = "paused"
            await session.commit()
    await cb.answer("Пауза")
    await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_resume_"))
async def cb_resume(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        t = await session.get(Tournament, tid)
        if t:
            t.status = "running"
            await session.commit()
    await cb.answer("Продолжено")
    await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_cancel_"))
async def cb_cancel_tour(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        t = await session.get(Tournament, tid)
        if t:
            t.status = "cancelled"
            await session.commit()
    await gm.broadcast(tid, {"type": "tournament_cancelled", "tid": tid})
    await cb.answer("Отменён")
    await _show_tournament_menu(cb, tid)


@router.callback_query(F.data.startswith("tt_invite_"))
async def cb_invite(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await state.update_data(invite_tid=tid, is_kick=False)
    await state.set_state(InviteFSM.user_id)
    await cb.message.edit_text(
        f"<b>✉️ Приглашение игрока</b>\n{SEP}\n"
        "Отправь <b>Telegram User ID</b> или <b>@username</b> игрока:",
        reply_markup=back_kb(f"tt_manage_{tid}"))
    await cb.answer()


@router.callback_query(F.data.startswith("tt_kick_"))
async def cb_kick(cb: CallbackQuery, state: FSMContext):
    if not await _is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True); return
    tid = int(cb.data.split("_")[-1])
    await state.update_data(invite_tid=tid, is_kick=True)
    await state.set_state(InviteFSM.user_id)
    await cb.message.edit_text(
        f"<b>🚫 Кик игрока</b>\n{SEP}\n"
        "Отправь <b>Telegram User ID</b> или <b>@username</b> игрока:",
        reply_markup=back_kb(f"tt_manage_{tid}"))
    await cb.answer()


@router.message(InviteFSM.user_id)
async def msg_invite(message: Message, state: FSMContext):
    if not await _is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    uid = None
    async with SessionLocal() as session:
        if raw.startswith("@"):
            r = await session.execute(select(User).where(User.username == raw[1:]))
            u = r.scalar_one_or_none()
            if not u:
                await message.answer(
                    "<blockquote>❌ Пользователь с таким @username не найден. "
                    "Он должен хотя бы раз запустить бота командой /start.</blockquote>")
                return
            uid = u.id
        else:
            try:
                uid = int(raw)
            except ValueError:
                await message.answer(
                    "<blockquote>❌ Введи числовой Telegram ID или @username.</blockquote>")
                return
        data = await state.get_data()
        tid = data.get("invite_tid")
        is_kick = data.get("is_kick")
        t = await session.get(Tournament, tid)
        if not t:
            await message.answer("<blockquote>❌ Нет турнира.</blockquote>")
            await state.clear(); return
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == uid))
        p = r.scalar_one_or_none()
        if is_kick:
            if p:
                await session.delete(p)
                await session.commit()
            await message.answer(
                f"<b>🚫 Игрок удалён</b>\n{SEP}\n🆔 <code>{uid}</code>")
        else:
            if p:
                p.status = "approved"
            else:
                session.add(Participant(tournament_id=tid, user_id=uid, status="approved"))
            await session.commit()
            tname = t.name
            try:
                await message.bot.send_message(
                    uid,
                    f"<b>✉️ Вас пригласили в турнир!</b>\n{SEP}\n"
                    f"🏆 <code>{esc(tname)}</code>\n\n"
                    "<blockquote>Примешь?</blockquote>",
                    reply_markup=invite_response_kb(tid))
            except Exception:
                pass
            await message.answer(
                f"<b>✅ Приглашение отправлено</b>\n{SEP}\n🆔 <code>{uid}</code>")
    await state.clear()


# ============ МЕНЮ ИГРОКА ============
@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery):
    await cb.message.edit_text(_main_menu_text(), reply_markup=main_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.message.edit_text(_help_text(), reply_markup=back_kb("back_main"))
    await cb.answer()


@router.callback_query(F.data == "open_tournaments")
async def cb_open(cb: CallbackQuery):
    async with SessionLocal() as session:
        r = await session.execute(select(Tournament).where(
            Tournament.access_type == "open",
            Tournament.status == "registration"))
        tours = r.scalars().all()
    if not tours:
        await cb.message.edit_text(
            f"<b>🏆 Открытые турниры</b>\n{SEP}\n"
            "<blockquote>Пока нет активных.</blockquote>",
            reply_markup=back_kb("back_main"))
        await cb.answer(); return
    lines = [f"<b>🏆 Открытые турниры</b>\n{SEP}"]
    rows = []
    for t in tours:
        emoji = "🃏" if t.game_type == "poker" else "🎡"
        lines.append(f"{emoji} <b>{esc(t.name)}</b> · ID <code>{t.id}</code>")
        rows.append([InlineKeyboardButton(
            text=f"📩 Подать заявку · {t.name[:20]}",
            callback_data=f"apply_{t.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    await cb.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data.startswith("apply_"))
async def cb_apply(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        t = await session.get(Tournament, tid)
        if not t:
            await cb.answer("Нет турнира", show_alert=True); return
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        existing = r.scalar_one_or_none()
        if existing and existing.status in ("pending", "approved"):
            await cb.answer("Заявка уже подана", show_alert=True); return
        if existing:
            existing.status = "pending"
            await session.commit()
            pid = existing.id
        else:
            p = Participant(tournament_id=tid, user_id=cb.from_user.id, status="pending")
            session.add(p)
            await session.commit()
            await session.refresh(p)
            pid = p.id
        creator_id = t.creator_id
        tname = t.name
    try:
        await cb.bot.send_message(
            creator_id,
            f"<b>📩 Новая заявка!</b>\n{SEP}\n"
            f"👤 @{cb.from_user.username or cb.from_user.first_name} "
            f"(<code>{cb.from_user.id}</code>)\n"
            f"🏆 <b>{esc(tname)}</b>",
            reply_markup=join_request_kb(pid))
    except Exception:
        pass
    await cb.answer("Заявка отправлена!", show_alert=True)


@router.callback_query(F.data == "my_tournaments")
async def cb_my(cb: CallbackQuery):
    rows = []
    lines = [f"<b>🎟 Мои турниры</b>\n{SEP}"]
    has_items = False
    shown_tids = set()

    async with SessionLocal() as session:
        r = await session.execute(select(Participant).where(
            Participant.user_id == cb.from_user.id,
            Participant.status.in_(("approved", "winner"))))
        parts = r.scalars().all()

        if parts:
            lines.append("<b>🎮 Как участник:</b>")
            for p in parts:
                t = await session.get(Tournament, p.tournament_id)
                if not t or t.id in shown_tids:
                    continue
                shown_tids.add(t.id)
                emoji = "🃏" if t.game_type == "poker" else "🎡"
                lines.append(f"  {emoji} <b>{esc(t.name)}</b> · <code>{t.status}</code>")
                if t.status in ("running", "paused"):
                    rows.append([InlineKeyboardButton(
                        text=f"🎮 Войти: {t.name[:20]}",
                        web_app=WebAppInfo(url=f"{WEBAPP_URL}/?tid={t.id}"))])
                has_items = True

        r = await session.execute(select(Tournament).where(
            Tournament.creator_id == cb.from_user.id,
            Tournament.status.in_(("registration", "running", "paused"))))
        created = r.scalars().all()

        if created:
            if has_items:
                lines.append("")
            lines.append("<b>👑 Как создатель:</b>")
            for t in created:
                if t.id in shown_tids:
                    continue
                shown_tids.add(t.id)
                emoji = "🃏" if t.game_type == "poker" else "🎡"
                lock = "🟢" if t.access_type == "open" else "🔴"
                lines.append(f"  {lock}{emoji} <b>{esc(t.name)}</b> · <code>{t.status}</code>")
                rows.append([InlineKeyboardButton(
                    text=f"⚙️ Управление: {t.name[:20]}",
                    callback_data=f"tt_manage_{t.id}")])
                has_items = True

    if not has_items:
        lines.append("<blockquote>ℹ️ Пока нет турниров.</blockquote>")

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    await cb.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    async with SessionLocal() as session:
        u = await session.get(User, cb.from_user.id)
        if u is None:
            u = User(id=cb.from_user.id, username=cb.from_user.username,
                     first_name=cb.from_user.first_name)
            session.add(u)
            await session.commit()
        r = await session.execute(select(Participant).where(
            Participant.user_id == cb.from_user.id,
            Participant.status == "winner"))
        wins = len(r.scalars().all())
        r = await session.execute(select(Admin).where(Admin.user_id == cb.from_user.id))
        is_admin_db = r.scalar_one_or_none() is not None

    if cb.from_user.id == OWNER_ID:
        role = "👑 Создатель"
    elif is_admin_db:
        role = "🛠 Администратор"
    else:
        role = "🎮 Игрок"

    await cb.message.edit_text(
        f"<b>👤 ПРОФИЛЬ</b>\n{SEP}\n"
        f"🆔 <code>{cb.from_user.id}</code>\n"
        f"👤 {esc(cb.from_user.first_name)}\n"
        f"🌐 @{cb.from_user.username or '—'}\n"
        f"🎭 Роль: {role}\n"
        f"🏆 Побед: <code>{wins}</code>",
        reply_markup=back_kb("back_main"))
    await cb.answer()


@router.callback_query(F.data.startswith("approve_"))
async def cb_approve(cb: CallbackQuery):
    pid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        p = await session.get(Participant, pid)
        if not p:
            await cb.answer("Нет", show_alert=True); return
        p.status = "approved"
        t = await session.get(Tournament, p.tournament_id)
        await session.commit()
        tname = t.name if t else "?"
        uid = p.user_id
    try:
        await cb.bot.send_message(
            uid,
            f"<b>✅ Заявка одобрена!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>")
    except Exception:
        pass
    await cb.message.edit_text(f"<b>✅ Принят</b>\n{SEP}")
    await cb.answer("Принято")


@router.callback_query(F.data.startswith("reject_"))
async def cb_reject(cb: CallbackQuery):
    pid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        p = await session.get(Participant, pid)
        if not p:
            await cb.answer("Нет", show_alert=True); return
        p.status = "rejected"
        t = await session.get(Tournament, p.tournament_id)
        await session.commit()
        tname = t.name if t else "?"
        uid = p.user_id
    try:
        await cb.bot.send_message(
            uid, f"<b>❌ Заявка отклонена</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>")
    except Exception:
        pass
    await cb.message.edit_text(f"<b>❌ Отклонён</b>\n{SEP}")
    await cb.answer("Отклонено")


@router.callback_query(F.data.startswith("accept_invite_"))
async def cb_accept_inv(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        p = r.scalar_one_or_none()
        t = await session.get(Tournament, tid)
        if not p or not t:
            await cb.answer("Не найдено", show_alert=True); return
        p.status = "approved"
        await session.commit()
        tname = t.name
    await cb.message.edit_text(
        f"<b>✅ Вы приняли приглашение!</b>\n{SEP}\n🏆 <code>{esc(tname)}</code>")
    await cb.answer("Принято")


@router.callback_query(F.data.startswith("decline_invite_"))
async def cb_decline_inv(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    async with SessionLocal() as session:
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == cb.from_user.id))
        p = r.scalar_one_or_none()
        if p:
            p.status = "rejected"
            await session.commit()
    await cb.message.edit_text("<blockquote>❌ Вы отклонили приглашение.</blockquote>")
    await cb.answer()


# ============ ЗАВЕРШЕНИЕ ТУРНИРА ============
async def _finalize_tournament(bot, tid, winner_id):
    async with SessionLocal() as session:
        t = await session.get(Tournament, tid)
        if not t or t.status == "finished":
            return
        t.status = "finished"
        tname = t.name
        if winner_id:
            u = await session.get(User, winner_id)
            if u:
                u.wins = (u.wins or 0) + 1
            r = await session.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.user_id == winner_id))
            p = r.scalar_one_or_none()
            if p:
                p.status = "winner"
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid))
        parts = r.scalars().all()
        await session.commit()
    for p in parts:
        try:
            await bot.send_message(
                p.user_id,
                f"<b>🏁 Турнир завершён!</b>\n{SEP}\n"
                f"🏆 <code>{esc(tname)}</code>\n\n"
                "<blockquote>Победитель определён. Спасибо за участие!</blockquote>")
        except Exception:
            pass


async def game_ticker(bot):
    while True:
        try:
            for tid, g in list(gm._active_poker.items()):
                if g.finished:
                    if tid not in _finalized_tids:
                        _finalized_tids.add(tid)
                        wid = g.winner_info.get("user_id") if g.winner_info else None
                        await _finalize_tournament(bot, tid, wid)
                    continue
                changed = False
                if g.check_timeout():
                    changed = True
                if g.phase == "showdown" and hasattr(g, "_showdown_at"):
                    if asyncio.get_event_loop().time() - g._showdown_at > 4:
                        g.start_hand()
                        changed = True
                elif g.phase == "showdown":
                    g._showdown_at = asyncio.get_event_loop().time()
                if changed:
                    await gm.broadcast(tid, g.to_dict())
            for tid, g in list(gm._active_wheel.items()):
                if g.finished and tid not in _finalized_tids:
                    _finalized_tids.add(tid)
                    wid = g.winner.get("user_id") if g.winner else None
                    await _finalize_tournament(bot, tid, wid)
        except Exception as e:
            logging.exception("ticker error: %s", e)
        await asyncio.sleep(1)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN не задан в .env")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    app = create_app(bot)
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    logging.info("🚀 Бот и Mini App запущены на %s:%s", HOST, PORT)
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        server.serve(),
        game_ticker(bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Остановлено")