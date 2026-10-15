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
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import select, func

from database import SessionLocal, User, Tournament, Participant, Ban, Admin
import game as gm

HERE = os.path.dirname(os.path.abspath(__file__))

_bracket_spin_tasks = {}
_wheel1v1_spin_tasks = {}


def _get_bot_token():
    return os.getenv("BOT_TOKEN", "")


def _get_owner_id():
    try: return int(os.getenv("OWNER_ID", "0") or "0")
    except Exception: return 0


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
            if gm.is_bot(uid):
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


def validate_init_data(init_data):
    bot_token = _get_bot_token()
    if not init_data or not bot_token: return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash: return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
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

    if t.game_type == "bracket":
        return {"type": "bracket_state", "tid": tid, "size": t.bracket_size,
                "total_rounds": 0, "prize": t.prize or "🏆 Приз",
                "finished": False, "winner": None, "rounds": [],
                "waiting": True, "server_now": int(time.time() * 1000)}
    if t.game_type == "wheel_1v1":
        return {"type": "wheel_1v1_state", "tid": tid,
                "players": [], "ready": [], "spin_count": 0,
                "finished": False, "winner": None,
                "prize": t.prize or "🏆 Приз", "last_spin": None,
                "series_target": int(getattr(t, "series_target", 1) or 1),
                "p1_wins": 0, "p2_wins": 0, "pending_winner_idx": None,
                "waiting": True,
                "server_now": int(time.time() * 1000)}
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
        if gm.is_bot(loser.get("user_id")): return
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
        if gm.is_bot(player.get("user_id")): return
        await bot.send_message(
            player["user_id"],
            f"<b>🏆 Соперник выбыл</b>\n➖➖➖➖➖➖➖➖➖\n"
            f"🏆 <code>{_esc(tname)}</code>\n\n"
            "<blockquote>Вы автоматически прошли дальше по сетке!</blockquote>")
    except Exception as e:
        logging.warning("auto-advance notify fail: %s", e)


async def _run_bracket_series(bot, tid, match_id):
    logging.info("[BRACKET-SERIES start] tid=%s match=%s", tid, match_id)
    try:
        await asyncio.sleep(1.5)
        while True:
            g = gm.get_bracket(tid)
            if not g:
                logging.info("[BRACKET-SERIES end] game gone tid=%s", tid)
                return
            m = g.find_match(match_id)
            if not m:
                logging.info("[BRACKET-SERIES end] match gone tid=%s mid=%s", tid, match_id)
                return
            if m.finished:
                logging.info("[BRACKET-SERIES end] match finished tid=%s mid=%s", tid, match_id)
                return
            if len(m.room_ready) < 2:
                logging.info("[BRACKET-SERIES end] not ready tid=%s mid=%s rr=%s",
                             tid, match_id, list(m.room_ready))
                return
            # Защитный сброс: если pending_winner_idx залип — сбрасываем,
            # иначе spin_match вернёт None и цикл молча выйдет.
            if m.pending_winner_idx is not None:
                logging.warning("[BRACKET-SERIES] resetting stale pending_winner_idx "
                                "tid=%s mid=%s", tid, match_id)
                m.pending_winner_idx = None
            spun = g.spin_match(match_id)
            if not spun:
                logging.warning("[BRACKET-SERIES] spin_match returned None tid=%s mid=%s",
                                tid, match_id)
                return
            await gm.broadcast(tid, g.to_dict())
            logging.info("[BRACKET-SERIES] spin #%s broadcast tid=%s mid=%s",
                         spun.spin_count, tid, match_id)

            await asyncio.sleep(gm.BRACKET_SPIN_DURATION_MS / 1000.0 + 0.5)

            g = gm.get_bracket(tid)
            if not g:
                logging.info("[BRACKET-SERIES end] game gone after spin tid=%s", tid)
                return
            m2, advanced_list = g.finalize_spin(match_id)
            if not m2:
                logging.warning("[BRACKET-SERIES] finalize_spin returned None tid=%s mid=%s",
                                tid, match_id)
                return
            await gm.broadcast(tid, g.to_dict())
            logging.info("[BRACKET-SERIES] finalized tid=%s mid=%s p1w=%s p2w=%s finished=%s",
                         tid, match_id, m2.p1_wins, m2.p2_wins, m2.finished)

            if m2.finished:
                if m2.loser:
                    asyncio.create_task(_notify_loser_delayed(bot, tid, m2.loser, 1.0))
                for adv in advanced_list or []:
                    asyncio.create_task(_notify_auto_advanced(bot, tid, adv))
                return
            await asyncio.sleep(gm.BRACKET_NEXT_DELAY_S)
    except asyncio.CancelledError:
        logging.info("[BRACKET-SERIES cancelled] tid=%s mid=%s", tid, match_id)
        raise
    except Exception as e:
        logging.exception("[BRACKET-SERIES error] tid=%s mid=%s: %s", tid, match_id, e)
    finally:
        _bracket_spin_tasks.pop(match_id, None)


async def _run_wheel1v1_series(bot, tid):
    logging.info("[WHEEL1V1-SERIES start] tid=%s", tid)
    try:
        await asyncio.sleep(1.5)
        while True:
            g = gm.get_wheel_1v1(tid)
            if not g: return
            if g.finished: return
            if not g.both_ready(): return
            winner = g.spin()
            if winner is None: return
            await gm.broadcast(tid, g.to_dict())
            await asyncio.sleep(gm.WHEEL_1V1_SPIN_DURATION_MS / 1000.0 + 0.5)
            g = gm.get_wheel_1v1(tid)
            if not g: return
            result = g.finalize_spin()
            if not result: return
            series_over, _ = result
            await gm.broadcast(tid, g.to_dict())
            if series_over: return
            await asyncio.sleep(gm.WHEEL_1V1_NEXT_DELAY_S)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.exception("[WHEEL1V1-SERIES error] tid=%s: %s", tid, e)
    finally:
        _wheel1v1_spin_tasks.pop(tid, None)


def create_app(bot):
    app = FastAPI(title="Tournament Mini App")

    @app.get("/")
    async def index():
        path = os.path.join(HERE, "index.html")
        if not os.path.exists(path):
            return HTMLResponse("<h1>index.html not found</h1>", status_code=500)
        return FileResponse(path)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "ts": time.time(), "bot_token_set": bool(_get_bot_token())}

    @app.get("/api/profile")
    async def api_profile(initData: str):
        parsed = validate_init_data(initData)
        if not parsed: raise HTTPException(status_code=401, detail="Invalid initData")
        u_obj = parsed.get("user_obj", {})
        user_id = u_obj.get("id")
        if not user_id: raise HTTPException(status_code=401, detail="No user")
        is_owner = (_get_owner_id() and user_id == _get_owner_id())
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
                "series_target": int(getattr(t, "series_target", 3) or 3),
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
        if t.game_type == "wheel_1v1":
            g = gm.get_wheel_1v1(tid)
            if g: state = g.to_dict()
            else: state = await _waiting_state(bot, tid, t, user_id)
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
    if t.game_type == "wheel_1v1":
        g = gm.get_wheel_1v1(tid)
        if not g: return
        if mtype == "request_state":
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
                if m and len(m.room_ready) >= 2 and not _bracket_spin_tasks.get(match_id):
                    task = asyncio.create_task(_run_bracket_series(bot, tid, match_id))
                    _bracket_spin_tasks[match_id] = task
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
        if g.both_ready() and not _wheel1v1_spin_tasks.get(tid):
            task = asyncio.create_task(_run_wheel1v1_series(bot, tid))
            _wheel1v1_spin_tasks[tid] = task


async def _admin_end(bot, tid):
    from main import _finalize_tournament, _finalized_tids
    for d in (gm._active_wheel, gm._active_wheel_1v1, gm._active_bracket):
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
    if t.game_type == "wheel":
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

    if not gm.is_bot(target_uid):
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

    if t.game_type == "wheel":
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
