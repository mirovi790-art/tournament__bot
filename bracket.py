"""Турнир с сеткой (плей-офф 4/8/16/32)."""
import logging
import random
import time

logger = logging.getLogger(__name__)

BRACKET_SERIES_TARGET = 3
BRACKET_SPIN_DURATION_MS = 20000
BRACKET_NEXT_DELAY_S = 3.5


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


class BracketMatch:
    def __init__(self, match_id, round_num, pos_in_round, series_target=3):
        self.match_id = match_id
        self.round_num = round_num
        self.pos_in_round = pos_in_round
        self.series_target = series_target
        self.p1 = None
        self.p2 = None
        self.p1_wins = 0
        self.p2_wins = 0
        self.ready = set()
        self.room_active = False
        self.room_ready = set()
        self.finished = False
        self.winner = None
        self.loser = None
        self.last_spin = None
        self.spin_count = 0
        self.history = []
        self._spin_task_running = False
        self.pending_winner_idx = None


class BracketGame:
    def __init__(self, tid, players, prize, series_target=3):
        self.tid = tid
        self.prize = prize or "🏆 Приз"
        self.size = len(players)
        self.series_target = max(1, int(series_target))

        n = max(2, self.size)
        rounds_count = 0
        while (1 << rounds_count) < n:
            rounds_count += 1
        self.total_rounds = max(1, rounds_count)

        self.finished = False
        self.winner = None
        self._next_match_id = 0
        self.rounds = []

        shuffled = list(players)
        random.shuffle(shuffled)

        first_round = []
        for i in range(0, len(shuffled), 2):
            m = BracketMatch(self._new_id(), 0, len(first_round), self.series_target)
            m.p1 = shuffled[i] if i < len(shuffled) else None
            m.p2 = shuffled[i + 1] if i + 1 < len(shuffled) else None
            first_round.append(m)
        self.rounds.append(first_round)

        prev = len(first_round)
        rnum = 1
        while prev > 1:
            nxt = (prev + 1) // 2
            rnd = []
            for pos in range(nxt):
                rnd.append(BracketMatch(self._new_id(), rnum, pos, self.series_target))
            self.rounds.append(rnd)
            prev = nxt
            rnum += 1

        for rnd in self.rounds:
            for m in rnd:
                self._mark_bot_ready(m)

        logger.info("[BRACKET tid=%s] created size=%s target=%s rounds=%s",
                    tid, self.size, self.series_target, len(self.rounds))

    def _mark_bot_ready(self, m):
        for p in (m.p1, m.p2):
            if p and _is_bot(p.get("user_id")):
                m.ready.add(p["user_id"])
                m.room_ready.add(p["user_id"])
        if m.p1 and m.p2:
            if _is_bot(m.p1.get("user_id")) and _is_bot(m.p2.get("user_id")):
                m.room_active = True

    def _new_id(self):
        self._next_match_id += 1
        return self._next_match_id

    def find_match(self, match_id):
        for rnd in self.rounds:
            for m in rnd:
                if m.match_id == match_id: return m
        return None

    def find_user_match(self, user_id):
        for rnd in self.rounds:
            for m in rnd:
                if m.finished: continue
                if m.p1 and m.p1["user_id"] == user_id: return m
                if m.p2 and m.p2["user_id"] == user_id: return m
        return None

    def _is_user_in_bracket(self, user_id):
        for rnd in self.rounds:
            for m in rnd:
                if m.p1 and m.p1["user_id"] == user_id: return True
                if m.p2 and m.p2["user_id"] == user_id: return True
        return False

    def press_ready(self, match_id, user_id):
        m = self.find_match(match_id)
        if not m or m.finished or not m.p1 or not m.p2: return False
        if user_id not in (m.p1["user_id"], m.p2["user_id"]): return False
        m.ready.add(user_id)
        return True

    def enter_match(self, match_id, user_id, force=False):
        m = self.find_match(match_id)
        if not m: return False
        if force:
            m.room_active = True
            return True
        p1_id = m.p1["user_id"] if m.p1 else None
        p2_id = m.p2["user_id"] if m.p2 else None
        if user_id == p1_id or user_id == p2_id:
            if m.finished or not m.p1 or not m.p2: return False
            if len(m.ready) < 2: return False
            m.room_active = True
            return True
        return self._is_user_in_bracket(user_id)

    def press_room_ready(self, match_id, user_id):
        m = self.find_match(match_id)
        if not m or m.finished or not m.room_active: return False
        if not m.p1 or not m.p2: return False
        if user_id not in (m.p1["user_id"], m.p2["user_id"]): return False
        m.room_ready.add(user_id)
        return True

    def spin_match(self, match_id):
        m = self.find_match(match_id)
        if not m or m.finished: return None
        self._mark_bot_ready(m)
        if len(m.room_ready) < 2: return None
        if m.pending_winner_idx is not None: return None
        idx = random.randrange(2)
        winner = m.p1 if idx == 0 else m.p2
        m.spin_count += 1
        m.last_spin = {
            "winner_index": idx,
            "winner_user_id": winner["user_id"],
            "started_at": int(time.time() * 1000),
            "duration": BRACKET_SPIN_DURATION_MS,
            "final_offset_ratio": _rand_offset_ratio(),
        }
        m.pending_winner_idx = idx
        m.history.append({"winner_index": idx, "winner_user_id": winner["user_id"]})
        return m

    def finalize_spin(self, match_id):
        m = self.find_match(match_id)
        if not m or m.finished: return None, []
        if m.pending_winner_idx is None: return None, []
        idx = m.pending_winner_idx
        if idx == 0: m.p1_wins += 1
        else: m.p2_wins += 1
        m.pending_winner_idx = None
        advanced_list = []
        series_over = (m.p1_wins >= m.series_target
                       or m.p2_wins >= m.series_target)
        if series_over:
            m.finished = True
            if m.p1_wins > m.p2_wins:
                m.winner = m.p1; m.loser = m.p2
            else:
                m.winner = m.p2; m.loser = m.p1
            self._advance_winner(m, advanced_list)
        return m, advanced_list

    def _advance_winner(self, m, advanced_log=None):
        if advanced_log is None: advanced_log = []
        next_round = m.round_num + 1
        if next_round >= len(self.rounds):
            self.finished = True
            self.winner = m.winner
            return advanced_log
        next_match = self.rounds[next_round][m.pos_in_round // 2]
        slot_is_p1 = (m.pos_in_round % 2 == 0)
        if slot_is_p1:
            if next_match.p1 is None: next_match.p1 = m.winner
            else: next_match.p2 = m.winner
        else:
            if next_match.p2 is None: next_match.p2 = m.winner
            else: next_match.p1 = m.winner
        self._mark_bot_ready(next_match)
        self.check_auto_advance(next_match.match_id, advanced_log)
        return advanced_log

    def _slot_source_finished(self, m, slot):
        if m.round_num == 0: return True
        prev_round = self.rounds[m.round_num - 1]
        src_pos = m.pos_in_round * 2 + (0 if slot == 0 else 1)
        if src_pos >= len(prev_round): return True
        return prev_round[src_pos].finished

    def check_auto_advance(self, match_id, advanced_log=None):
        if advanced_log is None: advanced_log = []
        m = self.find_match(match_id)
        if not m or m.finished: return advanced_log
        has_p1 = m.p1 is not None
        has_p2 = m.p2 is not None
        if has_p1 and not has_p2:
            if not self._slot_source_finished(m, 1): return advanced_log
            m.finished = True; m.winner = m.p1; m.loser = None
            advanced_log.append(m.p1)
            self._advance_winner(m, advanced_log)
        elif has_p2 and not has_p1:
            if not self._slot_source_finished(m, 0): return advanced_log
            m.finished = True; m.winner = m.p2; m.loser = None
            advanced_log.append(m.p2)
            self._advance_winner(m, advanced_log)
        return advanced_log

    def remove_player(self, uid):
        advanced_log = []
        for rnd in self.rounds:
            for m in rnd:
                if m.finished: continue
                if m.p1 and m.p1["user_id"] == uid:
                    m.p1 = None
                    self.check_auto_advance(m.match_id, advanced_log)
                    return advanced_log
                if m.p2 and m.p2["user_id"] == uid:
                    m.p2 = None
                    self.check_auto_advance(m.match_id, advanced_log)
                    return advanced_log
        return advanced_log

    def to_dict(self):
        return {
            "type": "bracket_state", "tid": self.tid,
            "size": self.size, "total_rounds": self.total_rounds,
            "series_target": self.series_target,
            "prize": self.prize, "finished": self.finished,
            "winner": self.winner,
            "rounds": [
                [{
                    "match_id": m.match_id, "round_num": m.round_num,
                    "pos_in_round": m.pos_in_round,
                    "p1": m.p1, "p2": m.p2,
                    "p1_wins": m.p1_wins, "p2_wins": m.p2_wins,
                    "series_target": m.series_target,
                    "ready": list(m.ready),
                    "room_active": m.room_active,
                    "room_ready": list(m.room_ready),
                    "finished": m.finished,
                    "winner": m.winner, "loser": m.loser,
                    "spin_count": m.spin_count,
                    "last_spin": m.last_spin,
                    "pending_winner_idx": m.pending_winner_idx,
                } for m in rnd] for rnd in self.rounds],
            "server_now": int(time.time() * 1000),
        }
