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
        if state:
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


async def _broadcast_poker(tid, g):
    if not gm._connections.get(tid):
        return
    for ws in list(gm._connections[tid]):
        try:
            await ws.send_json(g.to_dict())
        except Exception:
            gm.disconnect(tid, ws)