import random
import time
from collections import Counter
from itertools import combinations

RANKS = "23456789TJQKA"
SUITS = "shdc"
RANK_MAP = {r: i for i, r in enumerate(RANKS)}
SUIT_SYM = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
MAX_TABLE = 7


def make_deck(): return [(r, s) for r in RANKS for s in SUITS]
def card_str(c): return f"{c[0]}{SUIT_SYM[c[1]]}"


def _eval5(cards):
    ranks = sorted([RANK_MAP[c[0]] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1
    unique = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = None
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            is_straight = True; straight_high = unique[0]
        elif unique == [12, 3, 2, 1, 0]:
            is_straight = True; straight_high = 3
    cnt = Counter(ranks)
    counts = sorted(cnt.items(), key=lambda x: (-x[1], -x[0]))
    if is_straight and is_flush: return (8, [straight_high])
    if counts[0][1] == 4: return (7, [counts[0][0], counts[1][0]])
    if counts[0][1] == 3 and counts[1][1] == 2: return (6, [counts[0][0], counts[1][0]])
    if is_flush: return (5, ranks)
    if is_straight: return (4, [straight_high])
    if counts[0][1] == 3:
        kk = sorted([r for r in ranks if r != counts[0][0]], reverse=True)
        return (3, [counts[0][0]] + kk[:2])
    if counts[0][1] == 2 and counts[1][1] == 2:
        pairs = sorted([counts[0][0], counts[1][0]], reverse=True)
        kicker = [r for r in ranks if r not in pairs][0]
        return (2, pairs + [kicker])
    if counts[0][1] == 2:
        kk = sorted([r for r in ranks if r != counts[0][0]], reverse=True)
        return (1, [counts[0][0]] + kk[:3])
    return (0, ranks)


def evaluate_hand(cards):
    if len(cards) < 5: return None
    best = None
    for combo in combinations(cards, 5):
        v = _eval5(combo)
        if best is None or v > best: best = v
    return best


HAND_NAMES = ["Хай-кард", "Пара", "Две пары", "Сет", "Стрит", "Флеш",
              "Фулл-хаус", "Каре", "Стрит-флеш"]


class PokerPlayer:
    def __init__(self, user_id, name, chips):
        self.user_id = user_id; self.name = name; self.chips = chips
        self.cards = []; self.bet = 0; self.total_bet = 0
        self.folded = False; self.all_in = False; self.acted = False
        self.place = None


class _Table:
    def __init__(self, tid, table_id, players, start_stack, parent):
        self.tid = tid; self.table_id = table_id; self.parent = parent
        self.players = [PokerPlayer(p["user_id"], p["name"],
                                    p.get("chips", start_stack)) for p in players]
        self.dealer_idx = -1; self.phase = "waiting"; self.community = []
        self.pot = 0; self.current_bet = 0; self.min_raise = parent.big_blind
        self.turn_idx = -1; self.turn_deadline = 0; self.hand_number = 0
        self.finished = False; self.winner_info = None; self.log = []
        self.deck = []; self._showdown_at = None

    def _alive(self): return [p for p in self.players if not p.folded]
    def _can_act(self): return [p for p in self.players if not p.folded and not p.all_in and p.chips > 0]

    def _next_idx(self, start, condition):
        n = len(self.players)
        for i in range(1, n + 1):
            idx = (start + i) % n
            if condition(self.players[idx]): return idx
        return None

    def _betting_round_done(self):
        active = self._can_act()
        if not active: return True
        for p in active:
            if not p.acted or p.bet != self.current_bet: return False
        return True

    def _post_blind(self, idx, amount):
        p = self.players[idx]
        amt = min(amount, p.chips)
        p.chips -= amt; p.bet += amt; p.total_bet += amt; self.pot += amt
        if p.chips == 0: p.all_in = True
        return amt

    def start_hand(self):
        alive = [p for p in self.players if p.chips > 0]
        if len(alive) < 2: self._finish_table(); return
        self.hand_number += 1; self.community = []; self.pot = 0; self.current_bet = 0
        self.phase = "preflop"; self.deck = make_deck(); random.shuffle(self.deck)
        self.min_raise = self.parent.big_blind; self.winner_info = None
        self._showdown_at = None
        for p in self.players:
            p.cards = []; p.bet = 0; p.total_bet = 0
            p.folded = p.chips <= 0; p.all_in = False; p.acted = False
        self.dealer_idx = (self.dealer_idx + 1) % len(self.players)
        for _ in range(len(self.players)):
            if not self.players[self.dealer_idx].folded: break
            self.dealer_idx = (self.dealer_idx + 1) % len(self.players)
        sb_idx = self._next_idx(self.dealer_idx, lambda x: not x.folded)
        bb_idx = self._next_idx(sb_idx, lambda x: not x.folded)
        self._post_blind(sb_idx, self.parent.small_blind)
        self._post_blind(bb_idx, self.parent.big_blind)
        self.current_bet = self.parent.big_blind
        for p in self.players:
            if not p.folded: p.cards = [self.deck.pop(), self.deck.pop()]
        self.turn_idx = self._next_idx(bb_idx, lambda x: not x.folded and not x.all_in)
        self.turn_deadline = time.time() + self.parent.turn_time
        self.log.append(f"--- Стол {self.table_id + 1} · Раздача #{self.hand_number} ---")
        if self.turn_idx is None: self._advance_phase()

    def action(self, user_id, act, amount=0):
        if self.phase not in ("preflop", "flop", "turn", "river"): return False
        if self.turn_idx < 0 or self.players[self.turn_idx].user_id != user_id: return False
        p = self.players[self.turn_idx]
        if p.folded or p.all_in: return False
        if act == "fold":
            p.folded = True; p.acted = True; self.log.append(f"{p.name}: фолд")
        elif act == "check":
            if p.bet != self.current_bet: return False
            p.acted = True; self.log.append(f"{p.name}: чек")
        elif act == "call":
            to_call = self.current_bet - p.bet
            amt = min(to_call, p.chips)
            p.chips -= amt; p.bet += amt; p.total_bet += amt; self.pot += amt
            if p.chips == 0: p.all_in = True
            p.acted = True; self.log.append(f"{p.name}: колл {amt}")
        elif act == "raise":
            to_call = self.current_bet - p.bet
            if amount < self.min_raise: return False
            amt = min(to_call + amount, p.chips)
            p.chips -= amt; p.bet += amt; p.total_bet += amt; self.pot += amt
            if p.chips == 0: p.all_in = True
            self.current_bet = p.bet; self.min_raise = amount
            for other in self.players:
                if other is not p and not other.folded and not other.all_in:
                    other.acted = False
            p.acted = True; self.log.append(f"{p.name}: рейз до {p.bet}")
        elif act == "allin":
            amt = p.chips; p.chips = 0; p.bet += amt; p.total_bet += amt; self.pot += amt
            if p.bet > self.current_bet:
                self.min_raise = p.bet - self.current_bet; self.current_bet = p.bet
                for other in self.players:
                    if other is not p and not other.folded and not other.all_in:
                        other.acted = False
            p.all_in = True; p.acted = True; self.log.append(f"{p.name}: ОЛЛ-ИН {amt}")
        else:
            return False
        if len(self._alive()) <= 1:
            self._end_hand_by_fold(); return True
        if self._betting_round_done() or all(p.all_in or p.folded or p.chips == 0 for p in self.players):
            self._advance_phase()
        else:
            self.turn_idx = self._next_idx(self.turn_idx,
                lambda x: not x.folded and not x.all_in and x.chips > 0)
            self.turn_deadline = time.time() + self.parent.turn_time
        return True

    def check_timeout(self):
        if self.phase not in ("preflop", "flop", "turn", "river"): return False
        if self.turn_idx < 0: return False
        if time.time() < self.turn_deadline: return False
        p = self.players[self.turn_idx]
        if p.folded or p.all_in: return False
        return self.action(p.user_id, "fold")

    def _advance_phase(self):
        for p in self.players: p.bet = 0; p.acted = False
        self.current_bet = 0; self.min_raise = self.parent.big_blind
        if self.phase == "preflop":
            self.community += [self.deck.pop(), self.deck.pop(), self.deck.pop()]
            self.phase = "flop"; self.log.append("ФЛОП: " + " ".join(card_str(c) for c in self.community))
        elif self.phase == "flop":
            self.community.append(self.deck.pop())
            self.phase = "turn"; self.log.append("ТЁРН: " + " ".join(card_str(c) for c in self.community))
        elif self.phase == "turn":
            self.community.append(self.deck.pop())
            self.phase = "river"; self.log.append("РИВЕР: " + " ".join(card_str(c) for c in self.community))
        elif self.phase == "river":
            self._showdown(); return
        if len(self._can_act()) <= 1:
            while self.phase not in ("showdown", "finished"):
                if self.phase == "preflop":
                    self.community += [self.deck.pop(), self.deck.pop(), self.deck.pop()]; self.phase = "flop"
                elif self.phase == "flop":
                    self.community.append(self.deck.pop()); self.phase = "turn"
                elif self.phase == "turn":
                    self.community.append(self.deck.pop()); self.phase = "river"
                elif self.phase == "river":
                    self._showdown(); return
            return
        self.turn_idx = self._next_idx(self.dealer_idx,
            lambda x: not x.folded and not x.all_in and x.chips > 0)
        self.turn_deadline = time.time() + self.parent.turn_time

    def _end_hand_by_fold(self):
        winners = self._alive()
        if not winners: return
        w = winners[0]; w.chips += self.pot
        self.log.append(f"{w.name} забирает банк {self.pot}")
        self.pot = 0
        self.winner_info = {"user_id": w.user_id, "name": w.name, "amount": w.chips}
        self.phase = "showdown"; self._showdown_at = time.time(); self._post_hand()

    def _showdown(self):
        alive = self._alive(); results = []
        for p in alive:
            ev = evaluate_hand(p.cards + self.community); results.append((p, ev))
        results.sort(key=lambda x: x[1], reverse=True)
        best = results[0][1]
        winners = [r for r in results if r[1] == best]
        share = self.pot // len(winners)
        for w, _ in winners: w.chips += share
        self.log.append("ШОУДАУН: " + ", ".join(f"{p.name} ({HAND_NAMES[ev[0]]})" for p, ev in results))
        self.winner_info = {"user_id": winners[0][0].user_id, "name": winners[0][0].name,
                            "amount": share, "hand": HAND_NAMES[best[0]]}
        self.pot = 0; self.phase = "showdown"; self._showdown_at = time.time(); self._post_hand()

    def _post_hand(self):
        alive = [p for p in self.players if p.chips > 0]
        for p in self.players:
            if p.chips <= 0 and p.place is None: p.place = len(alive) + 1
        self.turn_idx = -1
        if len(alive) <= 1: self._finish_table()

    def _finish_table(self):
        self.finished = True
        alive = [p for p in self.players if p.chips > 0]
        if alive:
            self.winner_info = {"user_id": alive[0].user_id, "name": alive[0].name,
                                "amount": alive[0].chips, "hand": f"🏆 Победитель стола {self.table_id + 1}"}
        self.parent._check_all_tables_finished()

    def to_dict(self, for_user_id=None):
        return {"type": "poker_state", "tid": self.tid, "table_id": self.table_id,
                "table_count": len(self.parent.tables), "phase": self.phase, "pot": self.pot,
                "community": [card_str(c) for c in self.community],
                "current_bet": self.current_bet, "min_raise": self.min_raise,
                "big_blind": self.parent.big_blind, "small_blind": self.parent.small_blind,
                "turn_idx": self.turn_idx, "turn_deadline": self.turn_deadline,
                "turn_time": self.parent.turn_time, "dealer_idx": self.dealer_idx,
                "hand_number": self.hand_number, "finished": self.finished,
                "winner_info": self.winner_info, "log": self.log[-8:],
                "players": [{"user_id": p.user_id, "name": p.name, "chips": p.chips,
                             "bet": p.bet, "folded": p.folded, "all_in": p.all_in,
                             "place": p.place,
                             "cards": ([card_str(c) for c in p.cards]
                                       if (for_user_id == p.user_id or self.phase == "showdown" or self.finished)
                                       else (["?", "?"] if p.cards else [])),
                             "is_turn": idx == self.turn_idx}
                            for idx, p in enumerate(self.players)]}


class PokerGame:
    def __init__(self, tid, players, start_stack=1000, turn_time=20, blind_interval=5):
        self.tid = tid; self.turn_time = turn_time; self.blind_interval = blind_interval
        self.small_blind = 10; self.big_blind = 20
        self.finished = False; self.winner_info = None
        self.tables: list[_Table] = []
        self._distribute(players, start_stack)

    def _distribute(self, players, start_stack):
        n = len(players)
        num_tables = max(1, (n + MAX_TABLE - 1) // MAX_TABLE)
        groups = [[] for _ in range(num_tables)]
        for i, p in enumerate(players): groups[i % num_tables].append(p)
        for i, grp in enumerate(groups):
            self.tables.append(_Table(self.tid, i, grp, start_stack, self))

    def start_hand(self):
        for t in self.tables:
            if not t.finished: t.start_hand()

    def action(self, user_id, act, amount=0):
        for t in self.tables:
            for p in t.players:
                if p.user_id == user_id: return t.action(user_id, act, amount)
        return False

    def check_timeout(self):
        changed = False
        for t in self.tables:
            if t.check_timeout(): changed = True
        return changed

    def _alive_players(self):
        res = []
        for t in self.tables:
            for p in t.players:
                if p.chips > 0: res.append({"user_id": p.user_id, "name": p.name, "chips": p.chips})
        return res

    def _rebalance(self):
        if len(self.tables) <= 1: return False
        for t in self.tables:
            if not t.finished and t.phase not in ("showdown", "waiting"): return False
        changed = False
        for st in list(self.tables):
            if st.finished: continue
            alive = [p for p in st.players if p.chips > 0]
            if not (0 < len(alive) < 3): continue
            target = None; best_room = -1
            for t in self.tables:
                if t is st or t.finished: continue
                alive_t = [p for p in t.players if p.chips > 0]
                room = MAX_TABLE - len(alive_t)
                if room >= len(alive) and room > best_room: target = t; best_room = room
            if target is None: continue
            for p in alive: st.players.remove(p); target.players.append(p)
            st.finished = True; changed = True
        if changed:
            self.tables = [t for t in self.tables if not t.finished]
            for i, t in enumerate(self.tables): t.table_id = i
            for t in self.tables:
                if t.phase in ("showdown", "waiting"): t.start_hand()
        return changed

    def _check_all_tables_finished(self):
        alive = self._alive_players()
        if len(alive) <= 1:
            self.finished = True
            if alive:
                self.winner_info = {"user_id": alive[0]["user_id"], "name": alive[0]["name"],
                                    "amount": alive[0]["chips"], "hand": "🏆 ПОБЕДИТЕЛЬ ТУРНИРА"}
            return
        if len(alive) <= MAX_TABLE and len([t for t in self.tables if not t.finished]) > 1:
            self._make_final_table(alive)

    def _make_final_table(self, alive_players):
        players_data = [{"user_id": p["user_id"], "name": p["name"], "chips": p["chips"]}
                        for p in alive_players]
        self.tables = []
        final = _Table(self.tid, 0, players_data, 0, self)
        for p in final.players:
            for old in alive_players:
                if old["user_id"] == p.user_id: p.chips = old["chips"]; break
        final.start_hand(); self.tables.append(final)

    def to_dict(self, for_user_id=None):
        for t in self.tables:
            for p in t.players:
                if p.user_id == for_user_id: return t.to_dict(for_user_id=for_user_id)
        first = self.tables[0] if self.tables else None
        if first:
            d = first.to_dict(for_user_id=for_user_id); d["you_eliminated"] = True; return d
        return {"type": "poker_state", "phase": "eliminated", "players": [],
                "log": [], "community": [], "pot": 0, "turn_idx": -1, "finished": True}


class WheelGame:
    """Обычное колесо. При spin сервер записывает started_at — клиенты синхронизируются по нему."""
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
            "duration": 8000,
        }
        if len(self.active) == 1:
            self.winner = self.active[0]; self.winner["place"] = 1
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
    """Колесо 1х1. Оба игрока жмут «Готов». Затем один спин — кто выпал, тот и победил."""
    def __init__(self, tid, p1, p2, prize):
        self.tid = tid
        self.prize = prize or "🏆 Приз"
        self.players = [dict(p1), dict(p2)]  # [{user_id, name, username}]
        self.ready = set()
        self.spin_count = 0
        self.finished = False
        self.winner = None
        self.last_spin = None

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
        if self.finished or len(self.players) < 2: return None
        idx = random.randrange(2)
        self.winner = self.players[idx]
        self.spin_count += 1
        self.finished = True
        self.last_spin = {
            "winner_index": idx,
            "winner": self.winner,
            "started_at": int(time.time() * 1000),
            "duration": 8000,
        }
        return self.winner

    def to_dict(self):
        return {
            "type": "wheel_1v1_state", "tid": self.tid,
            "players": self.players,
            "ready": list(self.ready),
            "spin_count": self.spin_count, "finished": self.finished,
            "winner": self.winner, "prize": self.prize,
            "last_spin": self.last_spin,
            "server_now": int(time.time() * 1000),
        }


_active_poker: dict[int, PokerGame] = {}
_active_wheel: dict[int, WheelGame] = {}
_active_wheel_1v1: dict[int, Wheel1v1Game] = {}
_connections: dict[int, set] = {}


def get_poker(tid): return _active_poker.get(tid)
def get_wheel(tid): return _active_wheel.get(tid)
def get_wheel_1v1(tid): return _active_wheel_1v1.get(tid)
def set_poker(tid, g): _active_poker[tid] = g
def set_wheel(tid, g): _active_wheel[tid] = g
def set_wheel_1v1(tid, g): _active_wheel_1v1[tid] = g


def connect(tid, ws): _connections.setdefault(tid, set()).add(ws)
def disconnect(tid, ws):
    s = _connections.get(tid)
    if s and ws in s: s.discard(ws)


async def broadcast(tid, message):
    if tid not in _connections: return
    dead = []
    for ws in list(_connections[tid]):
        try: await ws.send_json(message)
        except Exception: dead.append(ws)
    for w in dead: disconnect(tid, w)
