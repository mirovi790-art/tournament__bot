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
    """Строим начальный state, если игра ещё не запущена."""
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
            "type": "poker_state", "tid": tid, "phase": "waiting",
            "pot": 0, "community": [], "current_bet": 0,
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
    else:
        return {
            "type": "wheel_state", "tid": tid,
            "active": players_info,
            "eliminated": [], "spin_count": 0,
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

    @app.get("/api/tournament/{tid}")
    async def api_tournament(tid: int, initData: str):
        parsed = validate_init_data(initData)
        if not parsed:
            raise HTTPException(status_code=401, detail="Invalid initData")
        user_id = parsed.get("user_obj", {}).get("id")
        if not user_id:
            raise HTTPException(status_code=401, detail="No user")
        async with SessionLocal() as session:
            if await session.get(Ban, user_id):
                raise HTTPException(status_code=403, detail="Banned")
            t = await session.get(Tournament, tid)
            if not t:
                raise HTTPException(status_code=404, detail="Not found")
            r = await session.execute(select(Participant).where(
                Participant.tournament_id == tid,
                Participant.user_id == user_id,
                Participant.status.in_(("approved", "winner"))))
            p = r.scalar_one_or_none()
            if not p and t.creator_id != user_id:
                raise HTTPException(status_code=403, detail="Not a participant")
        return {
            "id": t.id, "name": t.name, "game_type": t.game_type,
            "access_type": t.access_type, "status": t.status,
            "prize": t.prize, "user_id": user_id,
        }

    @app.websocket("/ws/{tid}")
    async def ws_endpoint(websocket: WebSocket, tid: int):
        init_data = websocket.query_params.get("initData", "")
        parsed = validate_init_data(init_data)
        if not parsed:
            await websocket.close(code=4001)
            return
        user_id = parsed.get("user_obj", {}).get("id")
        if not user_id:
            await websocket.close(code=4001)
            return
        async with SessionLocal() as session:
            u = await session.get(User, user_id)
            if not u:
                session.add(User(
                    id=user_id,
                    username=parsed.get("user_obj", {}).get("username"),
                    first_name=parsed.get("user_obj", {}).get("first_name"),
                ))
                await session.commit()
            t = await session.get(Tournament, tid)
            if not t:
                await websocket.close(code=4004)
                return
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
                await _handle(websocket, tid, user_id, msg, t)
        except WebSocketDisconnect:
            gm.disconnect(tid, websocket)
        except Exception:
            gm.disconnect(tid, websocket)

    return app


async def _handle(ws, tid, user_id, msg, t):
    mtype = msg.get("type")
    if mtype == "ping":
        await ws.send_json({"type": "pong"})
        return

    if mtype == "leave_tournament":
        await _handle_leave(ws, tid, user_id, t)
        return

    if t.game_type == "poker":
        g = gm.get_poker(tid)
        if not g:
            return
        if mtype == "poker_action":
            act = msg.get("action")
            amount = int(msg.get("amount", 0) or 0)
            g.action(user_id, act, amount)
            await _broadcast_poker(tid, g)
        elif mtype == "start_hand":
            if g.phase in ("showdown", "finished"):
                g.start_hand()
                await _broadcast_poker(tid, g)
        elif mtype == "request_state":
            await ws.send_json(g.to_dict(for_user_id=user_id))
    else:
        g = gm.get_wheel(tid)
        if not g:
            return
        if mtype == "spin":
            g.spin()
            await gm.broadcast(tid, g.to_dict())
        elif mtype == "request_state":
            await ws.send_json(g.to_dict())


async def _handle_leave(ws, tid, user_id, t):
    # Удаляем из БД
    async with SessionLocal() as session:
        r = await session.execute(select(Participant).where(
            Participant.tournament_id == tid,
            Participant.user_id == user_id))
        p = r.scalar_one_or_none()
        if p:
            p.status = "left"
            await session.commit()
    # Обновляем активную игру
    if t.game_type == "poker":
        g = gm.get_poker(tid)
        if g:
            for pl in g.players:
                if pl.user_id == user_id:
                    pl.folded = True
                    pl.chips = 0
                    if pl.place is None:
                        alive = [x for x in g.players if x.chips > 0]
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
                g.winner = g.active[0]
                g.winner["place"] = 1
                g.finished = True
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