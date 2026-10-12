import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from database import SessionLocal, User, Tournament, Participant, Ban
import game as gm

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))


def validate_init_data(init_data):
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        user_json = parsed.get("user")
        if user_json:
            parsed["user_obj"] = json.loads(user_json)
        return parsed
    except Exception:
        return None


async def _waiting_state(tid, t, user_id):
    async with SessionLocal() as session:
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid,
            Participant.status.in_(("approved", "winner"))))
        parts = r.scalars().all()
        players_info = []
        for p in parts:
            u = await session.get(User, p.user_id)
            name = (u.first_name if u and u.first_name else str(p.user_id))
            players_info.append({"user_id": p.user_id, "name": name})
    if t.game_type == "poker":
        return {
            "type": "poker_state", "tid": tid, "table_id": 0, "table_count": 1,
            "phase": "waiting", "pot": 0, "community": [], "current_bet": 0,
            "min_raise": 20, "big_blind": 20, "small_blind": 10,
            "turn_idx": -1, "turn_deadline": 0, "turn_time": t.turn_time,
            "dealer_idx": -1, "hand_number": 0, "finished": False,
            "winner_info": None,
            "log": ["⏳ Ожидание старта турнира..."],
            "players": [
                {"user_id": p["user_id"], "name": p["name"],
                 "chips": t.start_stack, "bet": 0, "folded": False,
                 "all_in": False, "place": None,
                 "cards": [], "is_turn": False}
                for p in players_info
            ],
        }
    return {
        "type": "wheel_state", "tid": tid,
        "active": players_info, "eliminated": [], "spin_count": 0,
        "finished": False, "winner": None,
        "prize": t.prize or "🏆 Приз", "last_spin": None,
    }


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
        if not parsed:
            raise HTTPException(status_code=401, detail="Invalid initData")
        u_obj = parsed.get("user_obj", {})
        user_id = u_obj.get("id")
        if not user_id:
            raise HTTPException(status_code=401, detail="No user")
        from sqlalchemy import func
        async with SessionLocal() as session:
            if await session.get(Ban, user_id):
                raise HTTPException(status_code=403, detail="Banned")
            u = await session.get(User, user_id)
            if not u:
                u = User(id=user_id, username=u_obj.get("username"),
                         first_name=u_obj.get("first_name"))
                session.add(u)
                await session.commit()
            wins = (await session.execute(select(func.count()).select_from(Participant)
                    .where(Participant.user_id == user_id, Participant.status == "winner"))).scalar() or 0
            # активные турниры, где юзер участник
            r = await session.execute(select(Participant).where(
                Participant.user_id == user_id,
                Participant.status.in_(("approved", "winner", "pending"))))
            parts = r.scalars().all()
            my_tours = []
            for p in parts:
                t = await session.get(Tournament, p.tournament_id)
                if not t:
                    continue
                my_tours.append({
                    "tid": t.id, "name": t.name, "status": t.status,
                    "game_type": t.game_type, "my_status": p.status,
                })
            # роль
            from database import Admin as AdminM
            adm = await session.get(AdminM, 1)  # not used, placeholder
        from config_check import OWNER_ID as OID  # noqa
        return {
            "user_id": user_id,
            "username": u_obj.get("username"),
            "first_name": u_obj.get("first_name"),
            "wins": wins,
            "tournaments": my_tours,
        }

    @app.get("/api/join/{tid}")
    async def api_join(tid: int, initData: str, password: str):
        parsed = validate_init_data(initData)
        if not parsed:
            raise HTTPException(status_code=401, detail="Invalid initData")
        user_id = parsed.get("user_obj", {}).get("id")
        if not user_id:
            raise HTTPException(status_code=401, detail="No user")
        password = (password or "").strip().upper()
        async with SessionLocal() as session:
            t = await session.get(Tournament, tid)
            if not t:
                raise HTTPException(status_code=404, detail="Турнир не найден")
            r = await session.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.user_id == user_id))
            p = r.scalar_one_or_none()
            if not p:
                # может, создатель — ему можно
                if t.creator_id == user_id:
                    return {"ok": True, "tid": tid}
                raise HTTPException(status_code=403, detail="Вы не участник турнира")
            if p.password and p.password.upper() == password:
                return {"ok": True, "tid": tid}
            raise HTTPException(status_code=403, detail="Неверный пароль")

    @app.websocket("/ws/{tid}")
    async def ws_endpoint(websocket: WebSocket, tid: int):
        init_data = websocket.query_params.get("initData", "")
        parsed = validate_init_data(init_data)
        if not parsed:
            await websocket.close(code=4001); return
        user_id = parsed.get("user_obj", {}).get("id")
        if not user_id:
            await websocket.close(code=4001); return
        async with SessionLocal() as session:
            u = await session.get(User, user_id)
            if not u:
                session.add(User(id=user_id,
                                 username=parsed.get("user_obj", {}).get("username"),
                                 first_name=parsed.get("user_obj", {}).get("first_name")))
                await session.commit()
            t = await session.get(Tournament, tid)
            if not t:
                await websocket.close(code=4004); return
            r = await session.execute(select(Participant).where(
                Participant.tournament_id == tid, Participant.user_id == user_id,
                Participant.status.in_(("approved", "winner"))))
            p = r.scalar_one_or_none()
            is_creator = (t.creator_id == user_id)
        if not p and not is_creator:
            await websocket.close(code=4003); return
        await websocket.accept()
        gm.connect(tid, websocket)
        state = None
        if t.game_type == "poker":
            g = gm.get_poker(tid)
            if g:
                state = g.to_dict(for_user_id=user_id)
        else:
            g = gm.get_wheel(tid)
            if g:
                state = g.to_dict()
        if not state:
            state = await _waiting_state(tid, t, user_id)
        try:
            await websocket.send_json(state)
        except Exception:
            pass
        try:
            while True:
                msg = await websocket.receive_json()
                await _handle(websocket, tid, user_id, msg, t, is_creator)
        except WebSocketDisconnect:
            gm.disconnect(tid, websocket)
        except Exception:
            gm.disconnect(tid, websocket)

    return app


async def _handle(ws, tid, user_id, msg, t, is_creator):
    mtype = msg.get("type")
    if mtype == "ping":
        await ws.send_json({"type": "pong"}); return
    if mtype == "leave_tournament":
        await _handle_leave(ws, tid, user_id, t); return
    if mtype in ("admin_end_tournament", "admin_kick_player"):
        if not is_creator:
            return
        if mtype == "admin_end_tournament":
            await _admin_end(tid, t); return
        if mtype == "admin_kick_player":
            target = int(msg.get("user_id", 0) or 0)
            if target:
                await _admin_kick(tid, t, target)
            return
    if t.game_type == "poker":
        g = gm.get_poker(tid)
        if not g: return
        if mtype == "poker_action":
            g.action(user_id, msg.get("action"), int(msg.get("amount", 0) or 0))
            await _broadcast_poker(tid, g)
        elif mtype == "start_hand":
            if getattr(g, "finished", False):
                return
            g.start_hand()
            await _broadcast_poker(tid, g)
        elif mtype == "request_state":
            await ws.send_json(g.to_dict(for_user_id=user_id))
    else:
        g = gm.get_wheel(tid)
        if not g: return
        if mtype == "spin":
            g.spin()
            await gm.broadcast(tid, g.to_dict())
        elif mtype == "request_state":
            await ws.send_json(g.to_dict())


async def _admin_end(tid, t):
    from database import User as U
    async with SessionLocal() as session:
        tt = await session.get(Tournament, tid)
        if not tt or tt.status == "finished":
            return
        tt.status = "finished"
        # если игра идёт — финализируем
        g_p = gm.get_poker(tid)
        g_w = gm.get_wheel(tid)
        winner_id = None
        if g_p and getattr(g_p, "winner_info", None):
            winner_id = g_p.winner_info.get("user_id")
        if g_w and getattr(g_w, "winner", None):
            winner_id = g_w.winner.get("user_id")
        r = await session.execute(select(Participant).where(Participant.tournament_id == tid))
        parts = r.scalars().all()
        if winner_id:
            u = await session.get(U, winner_id)
            if u:
                u.wins = (u.wins or 0) + 1
        await session.commit()
        tname = tt.name
    gm._active_poker.pop(tid, None)
    gm._active_wheel.pop(tid, None)
    await gm.broadcast(tid, {"type": "tournament_ended", "tid": tid, "winner_id": winner_id})


async def _admin_kick(tid, t, target_uid):
    async with SessionLocal() as session:
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == target_uid))
        p = r.scalar_one_or_none()
        if p:
            p.status = "kicked"
            await session.commit()
    if t.game_type == "poker":
        g = gm.get_poker(tid)
        if g:
            for tb in g.tables:
                for pl in tb.players:
                    if pl.user_id == target_uid and pl.chips > 0:
                        pl.folded = True
                        pl.chips = 0
                        alive = [x for x in tb.players if x.chips > 0]
                        if pl.place is None:
                            pl.place = len(alive) + 1
                        break
            await _broadcast_poker(tid, g)
    else:
        g = gm.get_wheel(tid)
        if g:
            for a in list(g.active):
                if a["user_id"] == target_uid:
                    g.active.remove(a)
                    g.eliminated.append({**a, "place": len(g.active) + 1})
                    break
            if len(g.active) == 1 and not g.finished:
                g.winner = g.active[0]; g.winner["place"] = 1; g.finished = True
            await gm.broadcast(tid, g.to_dict())
    await gm.broadcast(tid, {"type": "player_kicked", "tid": tid, "user_id": target_uid})


async def _handle_leave(ws, tid, user_id, t):
    async with SessionLocal() as session:
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid, Participant.user_id == user_id))
        p = r.scalar_one_or_none()
        if p:
            p.status = "left"
            await session.commit()
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
    else:
        g = gm.get_wheel(tid)
        if g:
            for a in list(g.active):
                if a["user_id"] == user_id:
                    g.active.remove(a)
                    g.eliminated.append({**a, "place": len(g.active) + 1})
                    break
            if len(g.active) == 1 and not g.finished:
                g.winner = g.active[0]; g.winner["place"] = 1; g.finished = True
            await gm.broadcast(tid, g.to_dict())
    try:
        await ws.send_json({"type": "left_tournament"})
    except Exception:
        pass


async def _broadcast_poker(tid, g):
    if not gm._connections.get(tid):
        return
    for ws in gm._connections[tid]:
        try:
            await ws.send_json(g.to_dict())
        except Exception:
            gm.disconnect(tid, ws)