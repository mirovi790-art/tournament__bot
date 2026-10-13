import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select, func

from database import SessionLocal, User, Tournament, Participant, Ban, Admin
import game as gm

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
HERE = os.path.dirname(os.path.abspath(__file__))

_bracket_series_tasks: dict[int, asyncio.Task] = {}


def _esc(s): return html.escape(s or "")


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


def validate_init_data(init_data):
    if not init_data or not BOT_TOKEN: return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash: return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash): return None
        uj = parsed.get("user")
        if uj: parsed["user_obj"] = json.loads(uj)
        return parsed
    except Exception:
        return None


async def _waiting_state(bot, tid, t, user_id):
    async with SessionLocal() as s:
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid,
            Participant.status.in_(("approved", "winner"))))
        parts = r.scalars().all()
        uids = [p.user_id for p in parts]
    names = await _enrich_names(bot, uids)
    players_info = [{"user_id": uid, "name": names.get(uid, f"Игрок {uid}")} for uid in uids]

    if t.game_type == "poker":
        return {"type": "poker_state", "tid": tid, "table_id": 0, "table_count": 1,
                "phase": "waiting", "pot": 0, "community": [], "current_bet": 0,
                "min_raise": 20, "big_blind": 20, "small_blind": 10,
                "turn_idx": -1, "turn_deadline": 0, "turn_time": t.turn_time,
                "dealer_idx": -1, "hand_number": 0, "finished": False,
                "winner_info": None, "log": ["⏳ Ожидание старта турнира..."],
                "players": [{"user_id": p["user_id"], "name": p["name"],
                             "chips": t.start_stack, "bet": 0, "folded": False,
                             "all_in": False, "place": None, "cards": [], "is_turn": False}
                            for p in players_info]}
    if t.game_type == "bracket":
        return {"type": "bracket_state", "tid": tid, "size": t.bracket_size,
                "total_rounds": 0, "prize": t.prize or "🏆 Приз",
                "finished": False, "winner": None, "rounds": [],
                "waiting": True, "server_now": int(time.time() * 1000)}
    return {"type": "wheel_state", "tid": tid, "active": players_info,
            "eliminated": [], "spin_count": 0, "finished": False,
            "winner": None, "prize": t.prize or "🏆 Приз", "last_spin": None,
            "server_now": int(time.time() * 1000)}


async def _notify_loser_delayed(bot, tid, loser, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        async with SessionLocal() as s:
            t = await s.get(Tournament, tid)
            tname = t.name if t else "?"
        await bot.send_message(
            loser["user_id"],
            f"<b>❌ Вы выбыли из турнира</b>\n➖➖➖➖➖➖➖➖➖\n"
            f"🏆 <code>{_esc(tname)}</code>\n\n"
            "<blockquote>К сожалению, вы проиграли серию.</blockquote>")
    except Exception as e:
        logging.warning("loser notify fail: %s", e)


async def _notify_auto_advanced(bot, tid, player):
    try:
        async with SessionLocal() as s:
            t = await s.get(Tournament, tid)
            tname = t.name if t else "?"
        await bot.send_message(
            player["user_id"],
            f"<b>🏆 Соперник выбыл</b>\n➖➖➖➖➖➖➖➖➖\n"
            f"🏆 <code>{_esc(tname)}</code>\n\n"
            "<blockquote>Вы автоматически прошли дальше по сетке!</blockquote>")
    except Exception as e:
        logging.warning("auto-advance notify fail: %s", e)


async def _run_bracket_series(bot, tid, match_id):
    """
    Двухфазный цикл:
      1) spin_match (pending) → broadcast (анимация у всех)
      2) wait BRACKET_SPIN_DURATION_MS + 500ms
      3) finalize_spin → увеличиваем счёт, при победе в серии — advance
      4) broadcast обновлённого стейта
      5) если серия не закончена — пауза и следующий раунд
    """
    try:
        # Первый спин запускаем через 1.5 сек после обоих "Готов"
        await asyncio.sleep(1.5)
        while True:
            g = gm.get_bracket(tid)
            if not g: return
            m = g.find_match(match_id)
            if not m or m.finished: return
            if len(m.room_ready) < 2: return

            # Фаза 1: визуальный спин (без изменения счёта)
            if not g.spin_match(match_id):
                return
            await gm.broadcast(tid, g.to_dict())

            # Ждём окончания анимации + небольшой запас
            await asyncio.sleep(gm.BRACKET_SPIN_DURATION_MS / 1000.0 + 0.5)

            # Фаза 2: финализация — увеличиваем счёт
            g = gm.get_bracket(tid)
            if not g: return
            m2, advanced_list = g.finalize_spin(match_id)
            if not m2: return
            await gm.broadcast(tid, g.to_dict())

            # Уведомления о проигравшем + авто-проходах
            if m2.finished:
                if m2.loser:
                    asyncio.create_task(_notify_loser_delayed(bot, tid, m2.loser, 1.0))
                for adv in advanced_list or []:
                    asyncio.create_task(_notify_auto_advanced(bot, tid, adv))
                return

            # Пауза между раундами серии
            await asyncio.sleep(gm.BRACKET_NEXT_DELAY_S)
    except Exception as e:
        logging.exception("run_bracket_series error: %s", e)
    finally:
        _bracket_series_tasks.pop(match_id, None)


def _task_running(match_id):
    t = _bracket_series_tasks.get(match_id)
    return t is not None and not t.done()


def create_app(bot):
    app = FastAPI(title="Tournament Mini App")

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(HERE, "index.html"))

    @app.get("/api/health")
    async def health():
        return {"ok": True, "ts": time.time()}

    @app.get("/api/profile")
    async def api_profile(initData: str):
        parsed = validate_init_data(initData)
        if not parsed: raise HTTPException(status_code=401, detail="Invalid initData")
        u_obj = parsed.get("user_obj", {})
        user_id = u_obj.get("id")
        if not user_id: raise HTTPException(status_code=401, detail="No user")
        is_owner = (OWNER_ID and user_id == OWNER_ID)
        async with SessionLocal() as s:
            if await s.get(Ban, user_id):
                raise HTTPException(status_code=403, detail="Banned")
            u = await s.get(User, user_id)
            if not u:
                u = User(id=user_id, username=u_obj.get("username"),
                         first_name=u_obj.get("first_name"))
                s.add(u); await s.commit()
            else:
                if u_obj.get("first_name"): u.first_name = u_obj.get("first_name")
                if u_obj.get("username"): u.username = u_obj.get("username")
                await s.commit()
            wins = (await s.execute(select(func.count()).select_from(Participant)
                .where(Participant.user_id == user_id,
                       Participant.status == "winner"))).scalar() or 0
            is_admin_db = (await s.execute(select(Admin).where(
                Admin.user_id == user_id))).scalar_one_or_none() is not None
            is_admin = is_admin_db or is_owner

            seen_tids = set()
            my_tours = []

            r = await s.execute(select(Participant).where(
                Participant.user_id == user_id,
                Participant.status.in_(("approved", "winner", "pending", "kicked", "left"))))
            for p in r.scalars().all():
                t = await s.get(Tournament, p.tournament_id)
                if not t or t.id in seen_tids: continue
                if t.status in ("finished", "cancelled"): continue
                if p.status in ("kicked", "left") and t.game_type != "bracket":
                    continue
                seen_tids.add(t.id)
                status_to_show = p.status if t.game_type in ("wheel_1v1", "bracket") else t.status
                my_tours.append({"tid": t.id, "name": t.name, "status": t.status,
                                 "display_status": status_to_show,
                                 "game_type": t.game_type, "my_status": p.status,
                                 "prize": t.prize or ""})

            r = await s.execute(select(Tournament).where(
                Tournament.creator_id == user_id,
                Tournament.status.in_(("registration", "running", "paused"))))
            for t in r.scalars().all():
                if t.id in seen_tids: continue
                seen_tids.add(t.id)
                my_tours.append({"tid": t.id, "name": t.name, "status": t.status,
                                 "display_status": t.status,
                                 "game_type": t.game_type, "my_status": "creator",
                                 "prize": t.prize or ""})

            if is_admin:
                r = await s.execute(select(Tournament).where(
                    Tournament.game_type.in_(("wheel", "wheel_1v1", "bracket")),
                    Tournament.status.in_(("running", "paused"))))
                for t in r.scalars().all():
                    if t.id in seen_tids: continue
                    seen_tids.add(t.id)
                    my_tours.append({"tid": t.id, "name": t.name, "status": t.status,
                                     "display_status": t.status,
                                     "game_type": t.game_type, "my_status": "admin",
                                     "prize": t.prize or ""})

        return {"user_id": user_id, "username": u_obj.get("username"),
                "first_name": u_obj.get("first_name"),
                "wins": wins, "is_admin": is_admin, "tournaments": my_tours}

    @app.get("/api/tournament/{tid}")
    async def api_tournament(tid: int, initData: str):
        parsed = validate_init_data(initData)
        if not parsed: raise HTTPException(status_code=401, detail="Invalid initData")
        user_id = parsed.get("user_obj", {}).get("id")
        if not user_id: raise HTTPException(status_code=401, detail="No user")
        async with SessionLocal() as s:
            if await s.get(Ban, user_id):
                raise HTTPException(status_code=403, detail="Banned")
            t = await s.get(Tournament, tid)
            if not t: raise HTTPException(status_code=404, detail="Not found")
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid, Participant.user_id == user_id))
            p = r.scalar_one_or_none()
            is_creator = (t.creator_id == user_id)
            is_admin = is_creator or ((await s.execute(select(Admin).where(
                Admin.user_id == user_id))).scalar_one_or_none() is not None)
            is_participant = p is not None and p.status in ("approved", "winner")
            is_pending = p is not None and p.status == "pending"
            is_excluded = p is not None and p.status in ("kicked", "left")
            if not is_participant and not is_admin and not is_pending and not is_excluded:
                raise HTTPException(status_code=403, detail="Not a participant")
            if is_excluded and t.game_type != "bracket":
                raise HTTPException(status_code=403, detail="Not a participant")
            my_status = p.status if p else "creator"
        return {"id": t.id, "name": t.name, "game_type": t.game_type,
                "access_type": t.access_type, "status": t.status,
                "prize": t.prize, "user_id": user_id,
                "min_players": t.min_players, "bracket_size": t.bracket_size,
                "is_admin": is_admin, "is_participant": is_participant,
                "is_pending": is_pending, "is_excluded": is_excluded,
                "my_status": my_status}

    @app.websocket("/ws/{tid}")
    async def ws_endpoint(websocket: WebSocket, tid: int):
        init_data = websocket.query_params.get("initData", "")
        parsed = validate_init_data(init_data)
        if not parsed: await websocket.close(code=4001); return
        user_id = parsed.get("user_obj", {}).get("id")
        if not user_id: await websocket.close(code=4001); return
        async with SessionLocal() as s:
            u = await s.get(User, user_id)
            if not u:
                s.add(User(id=user_id,
                           username=parsed.get("user_obj", {}).get("username"),
                           first_name=parsed.get("user_obj", {}).get("first_name")))
                await s.commit()
            else:
                new_fn = parsed.get("user_obj", {}).get("first_name")
                new_un = parsed.get("user_obj", {}).get("username")
                if new_fn: u.first_name = new_fn
                if new_un: u.username = new_un
                await s.commit()
            t = await s.get(Tournament, tid)
            if not t: await websocket.close(code=4004); return
            r = await s.execute(select(Participant).where(
                Participant.tournament_id == tid, Participant.user_id == user_id))
            p = r.scalar_one_or_none()
            is_creator = (t.creator_id == user_id)
            is_admin = is_creator or ((await s.execute(select(Admin).where(
                Admin.user_id == user_id))).scalar_one_or_none() is not None)
            is_participant = p is not None and p.status in ("approved", "winner")
            is_excluded = p is not None and p.status in ("kicked", "left")
        allowed = is_admin or is_participant or (is_excluded and t.game_type == "bracket")
        if not allowed:
            await websocket.close(code=4003); return
        await websocket.accept()
        gm.connect(tid, websocket)
        state = None
        if t.game_type == "poker":
            g = gm.get_poker(tid)
            if g: state = g.to_dict(for_user_id=user_id)
        elif t.game_type == "wheel_1v1":
            g = gm.get_wheel_1v1(tid)
            if g: state = g.to_dict()
            else:
                state = {"type": "wheel_1v1_state", "tid": tid,
                         "players": [], "ready": [], "spin_count": 0,
                         "finished": False, "winner": None,
                         "prize": t.prize or "🏆 Приз", "last_spin": None,
                         "waiting": True,
                         "server_now": int(time.time() * 1000)}
        elif t.game_type == "bracket":
            g = gm.get_bracket(tid)
            if g: state = g.to_dict()
        else:
            g = gm.get_wheel(tid)
            if g: state = g.to_dict()
        if not state:
            state = await _waiting_state(bot, tid, t, user_id)
        try: await websocket.send_json(state)
        except Exception: pass
        try:
            while True:
                msg = await websocket.receive_json()
                await _handle(bot, websocket, tid, user_id, msg, t, is_admin)
        except WebSocketDisconnect:
            gm.disconnect(tid, websocket)
        except Exception:
            gm.disconnect(tid, websocket)

    return app


async def _handle(bot, ws, tid, user_id, msg, t, is_admin):
    mtype = msg.get("type")
    if mtype == "ping":
        await ws.send_json({"type": "pong"}); return
    if mtype == "leave_tournament":
        await _handle_leave(bot, ws, tid, user_id, t); return
    if mtype == "ready" and t.game_type == "wheel_1v1":
        await _handle_1v1_ready(bot, tid, user_id); return
    if is_admin and mtype == "admin_end_tournament":
        await _admin_end(bot, tid); return
    if is_admin and mtype == "admin_kick_player":
        target = int(msg.get("user_id", 0) or 0)
        if target: await _admin_kick(bot, tid, t, target)
        return
    if t.game_type == "poker":
        g = gm.get_poker(tid)
        if not g: return
        if mtype == "poker_action":
            g.action(user_id, msg.get("action"), int(msg.get("amount", 0) or 0))
            await _broadcast_poker(tid, g)
        elif mtype == "start_hand":
            if getattr(g, "finished", False): return
            g.start_hand(); await _broadcast_poker(tid, g)
        elif mtype == "request_state":
            await ws.send_json(g.to_dict(for_user_id=user_id))
    elif t.game_type == "wheel_1v1":
        g = gm.get_wheel_1v1(tid)
        if not g: return
        if mtype == "spin":
            if not is_admin:
                try: await ws.send_json({"type": "spin_denied"})
                except: pass
                return
            if not g.both_ready():
                try: await ws.send_json({"type": "not_ready"})
                except: pass
                return
            if not g.finished:
                g.spin()
                await gm.broadcast(tid, g.to_dict())
        elif mtype == "request_state":
            await ws.send_json(g.to_dict())
    elif t.game_type == "bracket":
        g = gm.get_bracket(tid)
        if not g:
            try: await ws.send_json({"type": "bracket_not_started"})
            except: pass
            return
        if mtype == "bracket_enter":
            match_id = int(msg.get("match_id", 0))
            if g.enter_match(match_id, user_id, force=is_admin):
                await gm.broadcast(tid, g.to_dict())
                try: await ws.send_json(g.to_dict())
                except: pass

        elif mtype == "bracket_ready":
            match_id = int(msg.get("match_id", 0))
            if g.press_ready(match_id, user_id):
                await gm.broadcast(tid, g.to_dict())

        elif mtype == "bracket_room_ready":
            match_id = int(msg.get("match_id", 0))
            if g.press_room_ready(match_id, user_id):
                await gm.broadcast(tid, g.to_dict())
                m = g.find_match(match_id)
                if m and len(m.room_ready) >= 2 and not _task_running(match_id):
                    task = asyncio.create_task(_run_bracket_series(bot, tid, match_id))
                    _bracket_series_tasks[match_id] = task

        elif mtype == "request_state":
            await ws.send_json(g.to_dict())
    else:
        g = gm.get_wheel(tid)
        if not g: return
        if mtype == "spin":
            if not is_admin:
                try: await ws.send_json({"type": "spin_denied"})
                except: pass
                return
            g.spin(); await gm.broadcast(tid, g.to_dict())
        elif mtype == "request_state":
            await ws.send_json(g.to_dict())


async def _handle_1v1_ready(bot, tid, user_id):
    g = gm.get_wheel_1v1(tid)
    if not g or g.finished: return
    if user_id not in g.user_ids(): return
    if g.press_ready(user_id):
        await gm.broadcast(tid, g.to_dict())
        if g.both_ready():
            async def _auto_spin():
                await asyncio.sleep(1.5)
                if g.finished: return
                g.spin()
                await gm.broadcast(tid, g.to_dict())
            asyncio.create_task(_auto_spin())


async def _admin_end(bot, tid):
    from main import _finalize_tournament, _finalized_tids
    for d in (gm._active_poker, gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket):
        d.pop(tid, None)
    _finalized_tids.discard(tid)
    try: await _finalize_tournament(bot, tid, manual=True)
    except Exception as e: logging.exception("admin_end fail: %s", e)


async def _admin_kick(bot, tid, t, target_uid):
    async with SessionLocal() as s:
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == target_uid))
        p = r.scalar_one_or_none()
        if p: p.status = "kicked"; await s.commit()

    tname = t.name
    if t.game_type == "poker":
        g = gm.get_poker(tid)
        if g:
            for tb in g.tables:
                for pl in tb.players:
                    if pl.user_id == target_uid and pl.chips > 0:
                        pl.folded = True; pl.chips = 0
                        if pl.place is None:
                            alive = [x for x in tb.players if x.chips > 0]
                            pl.place = len(alive) + 1
                        break
            await _broadcast_poker(tid, g)
    elif t.game_type == "wheel":
        g = gm.get_wheel(tid)
        if g:
            for a in list(g.active):
                if a["user_id"] == target_uid:
                    g.active.remove(a)
                    g.eliminated.append({**a, "place": len(g.active) + 1}); break
            if len(g.active) == 1 and not g.finished:
                g.winner = g.active[0]; g.winner["place"] = 1; g.finished = True
            await gm.broadcast(tid, g.to_dict())
    elif t.game_type == "bracket":
        g = gm.get_bracket(tid)
        if g:
            advanced_list = g.remove_player(target_uid)
            seen = set()
            for adv in advanced_list:
                if adv["user_id"] in seen: continue
                seen.add(adv["user_id"])
                asyncio.create_task(_notify_auto_advanced(bot, tid, adv))
            await gm.broadcast(tid, g.to_dict())

    try:
        await bot.send_message(target_uid,
            f"<b>🚫 Вас исключили из турнира</b>\n➖➖➖➖➖➖➖➖➖\n"
            f"🏆 <code>{_esc(tname)}</code>")
    except: pass

    await gm.broadcast(tid, {"type": "player_kicked", "tid": tid, "user_id": target_uid})


async def _handle_leave(bot, ws, tid, user_id, t):
    async with SessionLocal() as s:
        r = await s.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == user_id))
        p = r.scalar_one_or_none()
        if p: p.status = "left"; await s.commit()

    if t.game_type == "poker":
        g = gm.get_poker(tid)
        if g:
            for tb in g.tables:
                for pl in tb.players:
                    if pl.user_id == user_id:
                        pl.folded = True; pl.chips = 0
                        if pl.place is None:
                            alive = [x for x in tb.players if x.chips > 0]
                            pl.place = len(alive) + 1
                        break
            await _broadcast_poker(tid, g)
    elif t.game_type == "wheel":
        g = gm.get_wheel(tid)
        if g:
            for a in list(g.active):
                if a["user_id"] == user_id:
                    g.active.remove(a)
                    g.eliminated.append({**a, "place": len(g.active) + 1}); break
            if len(g.active) == 1 and not g.finished:
                g.winner = g.active[0]; g.winner["place"] = 1; g.finished = True
            await gm.broadcast(tid, g.to_dict())
    elif t.game_type == "bracket":
        g = gm.get_bracket(tid)
        if g:
            advanced_list = g.remove_player(user_id)
            seen = set()
            for adv in advanced_list:
                if adv["user_id"] in seen: continue
                seen.add(adv["user_id"])
                asyncio.create_task(_notify_auto_advanced(bot, tid, adv))
            await gm.broadcast(tid, g.to_dict())

    try: await ws.send_json({"type": "left_tournament"})
    except: pass


async def _broadcast_poker(tid, g):
    if not gm._connections.get(tid): return
    for ws in gm._connections[tid]:
        try: await ws.send_json(g.to_dict())
        except Exception: gm.disconnect(tid, ws)
