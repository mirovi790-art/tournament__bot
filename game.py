"""Общий реестр активных игр и WebSocket-соединений.
Классы игр живут в wheel.py, bracket.py, poker.py."""
import logging

logger = logging.getLogger(__name__)

BOT_ID_BASE = 9_000_000_000


def is_bot(uid):
    try:
        return int(uid) >= BOT_ID_BASE
    except Exception:
        return False


# -------- реестры игр --------
_active_wheel = {}       # tid -> WheelGame (общее колесо)
_active_wheel_1v1 = {}   # tid -> Wheel1v1Game
_active_bracket = {}     # tid -> BracketGame
_active_poker = {}       # tid -> PokerTournament


def get_wheel(tid): return _active_wheel.get(tid)
def get_wheel_1v1(tid): return _active_wheel_1v1.get(tid)
def get_bracket(tid): return _active_bracket.get(tid)
def get_poker(tid): return _active_poker.get(tid)

def set_wheel(tid, g): _active_wheel[tid] = g
def set_wheel_1v1(tid, g): _active_wheel_1v1[tid] = g
def set_bracket(tid, g): _active_bracket[tid] = g
def set_poker(tid, g): _active_poker[tid] = g

def pop_poker(tid): return _active_poker.pop(tid, None)


# -------- реестр WS-соединений --------
_connections = {}


def connect(tid, ws):
    _connections.setdefault(tid, set()).add(ws)


def disconnect(tid, ws):
    s = _connections.get(tid)
    if s and ws in s:
        s.discard(ws)


async def broadcast(tid, message):
    if tid not in _connections: return
    dead = []
    for ws in list(_connections[tid]):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for w in dead:
        disconnect(tid, w)
