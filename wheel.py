"""Колесо Фортуны: общий режим и 1×1."""
import logging
import random
import time

logger = logging.getLogger(__name__)

WHEEL_GENERAL_SPIN_DURATION_MS = 8000
WHEEL_1V1_SPIN_DURATION_MS = 8000
WHEEL_1V1_NEXT_DELAY_S = 2.5


def _rand_offset_ratio():
    return random.uniform(-0.4, 0.4)


def _is_bot(uid):
    try:
        from game import is_bot
        return is_bot(uid)
    except Exception:
        try:
            return int(uid) >= 9_000_000_000
        except Exception:
            return False


class WheelGame:
    """Общее колесо: админ крутит, каждый спин выбивает одного игрока."""
    def __init__(self, tid, participants, prize):
        self.tid = tid
        self.prize = prize or "🏆 Приз"
        self.active = [{"user_id": p["user_id"], "name": p["name"]} for p in participants]
        self.eliminated = []
        self.spin_count = 0
        self.finished = False
        self.winner = None
        self.last_spin = None

    def spin(self):
        if self.finished or len(self.active) < 2: return None
        before_active = [dict(p) for p in self.active]
        idx = random.randrange(len(self.active))
        eliminated = self.active.pop(idx)
        place = len(self.active) + 1
        self.eliminated.append({**eliminated, "place": place})
        self.spin_count += 1
        self.last_spin = {
            "eliminated": eliminated,
            "sector_index": idx,
            "before_active": before_active,
            "started_at": int(time.time() * 1000),
            "duration": WHEEL_GENERAL_SPIN_DURATION_MS,
            "final_offset_ratio": _rand_offset_ratio(),
        }
        if len(self.active) == 1:
            self.winner = self.active[0]
            self.winner["place"] = 1
            self.finished = True
        return eliminated

    def to_dict(self):
        return {
            "type": "wheel_state", "tid": self.tid,
            "active": self.active, "eliminated": self.eliminated,
            "spin_count": self.spin_count, "finished": self.finished,
            "winner": self.winner, "prize": self.prize,
            "last_spin": self.last_spin,
            "server_now": int(time.time() * 1000),
        }


class Wheel1v1Game:
    """Колесо 1×1: серия спинов между двумя игроками."""
    def __init__(self, tid, p1, p2, prize, series_target=1):
        self.tid = tid
        self.prize = prize or "🏆 Приз"
        self.players = [dict(p1), dict(p2)]
        self.series_target = max(1, int(series_target))
        self.ready = set()
        self.p1_wins = 0
        self.p2_wins = 0
        self.spin_count = 0
        self.finished = False
        self.winner = None
        self.last_spin = None
        self.pending_winner_idx = None
        for p in self.players:
            if _is_bot(p.get("user_id")):
                self.ready.add(p["user_id"])

    def user_ids(self):
        return [p["user_id"] for p in self.players]

    def press_ready(self, user_id):
        if self.finished: return False
        if user_id not in self.user_ids(): return False
        self.ready.add(user_id)
        return True

    def both_ready(self):
        return len(self.ready) >= 2

    def spin(self):
        if self.finished: return None
        if not self.both_ready(): return None
        if self.pending_winner_idx is not None: return None
        idx = random.randrange(2)
        winner = self.players[idx]
        self.spin_count += 1
        self.last_spin = {
            "winner_index": idx,
            "winner_user_id": winner["user_id"],
            "winner_name": winner["name"],
            "started_at": int(time.time() * 1000),
            "duration": WHEEL_1V1_SPIN_DURATION_MS,
            "final_offset_ratio": _rand_offset_ratio(),
        }
        self.pending_winner_idx = idx
        return winner

    def finalize_spin(self):
        if self.pending_winner_idx is None: return None
        idx = self.pending_winner_idx
        if idx == 0: self.p1_wins += 1
        else: self.p2_wins += 1
        self.pending_winner_idx = None
        series_over = (self.p1_wins >= self.series_target
                       or self.p2_wins >= self.series_target)
        if series_over:
            self.finished = True
            self.winner = (self.players[0] if self.p1_wins > self.p2_wins
                           else self.players[1])
        return series_over, self.winner

    def to_dict(self):
        return {
            "type": "wheel_1v1_state", "tid": self.tid,
            "players": self.players, "ready": list(self.ready),
            "spin_count": self.spin_count, "finished": self.finished,
            "winner": self.winner, "prize": self.prize,
            "series_target": self.series_target,
            "p1_wins": self.p1_wins, "p2_wins": self.p2_wins,
            "pending_winner_idx": self.pending_winner_idx,
            "last_spin": self.last_spin,
            "server_now": int(time.time() * 1000),
        }
