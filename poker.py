"""No-Limit Texas Hold'em — соответствует классическим правилам.
Мульти-стол, само-посадка, лобби, авто-слияние, авто-кик.
"""
import logging
import random
import time
from collections import Counter
from itertools import combinations

logger = logging.getLogger(__name__)

RANKS = "23456789TJQKA"
SUITS = "♠♥♦♣"
RV = {r: i for i, r in enumerate(RANKS, 2)}

NEXT_HAND_DELAY = 6
REBUY_COOLDOWN = 4
MERGE_CHECK_INTERVAL = 3
MERGE_WARNING_SEC = 8
MAX_SEATS_PER_TABLE = 7
UNSEATED_TIMEOUT = 180
BOT_ID_BASE = 9_000_000_000

HAND_NAMES = {8: "Стрит-флеш", 7: "Каре", 6: "Фулл-хаус", 5: "Флеш",
              4: "Стрит", 3: "Сет", 2: "Две пары", 1: "Пара", 0: "Старшая карта"}


def is_bot(uid):
    try: return int(uid) >= BOT_ID_BASE
    except Exception: return False


def make_deck():
    d = [r + s for r in RANKS for s in SUITS]
    random.shuffle(d)
    return d


def pc(card):
    if not card or card == "?" or len(card) < 2: return None
    r, s = card[0], card[1:]
    if r not in RV or s not in SUITS: return None
    return (RV[r], s)


def _eval5(cards):
    """Оценка 5 карт. Возвращает кортеж: (rank, *tiebreakers), больше — сильнее."""
    vals = sorted([c[0] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1
    uniq = sorted(set(vals), reverse=True)
    is_straight = False; sh = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            is_straight, sh = True, uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            is_straight, sh = True, 5
    cnt = Counter(vals)
    items = sorted(cnt.items(), key=lambda x: (x[1], x[0]), reverse=True)
    counts = [c for _, c in items]; ov = [v for v, _ in items]
    if is_straight and is_flush: return (8, sh)
    if counts[0] == 4: return (7, ov[0], ov[1])
    if counts[0] == 3 and counts[1] == 2: return (6, ov[0], ov[1])
    if is_flush: return (5, *vals)
    if is_straight: return (4, sh)
    if counts[0] == 3: return (3, ov[0], ov[1], ov[2])
    if counts[0] == 2 and counts[1] == 2: return (2, ov[0], ov[1], ov[2])
    if counts[0] == 2: return (1, ov[0], ov[1], ov[2], ov[3])
    return (0, *vals)


def best_hand(cards):
    parsed = [pc(c) for c in cards if pc(c)]
    if len(parsed) < 5: return None
    best = None
    for combo in combinations(parsed, 5):
        v = _eval5(list(combo))
        if best is None or v > best: best = v
    return best


def hand_name(v):
    if not v: return ""
    return HAND_NAMES.get(v[0], "")


class PokerPlayer:
    __slots__ = ("user_id", "name", "chips", "cards", "folded", "all_in",
                 "bet", "total_bet", "sit_out", "last_action",
                 "hand_value", "seat", "finish_place")

    def __init__(self, user_id, name, chips, seat):
        self.user_id = user_id
        self.name = name
        self.chips = chips
        self.cards = []
        self.folded = False
        self.all_in = False
        self.bet = 0                # вклад в текущем раунде
        self.total_bet = 0          # вклад за всю раздачу
        self.sit_out = False
        self.last_action = ""
        self.hand_value = None
        self.seat = seat
        self.finish_place = None

    def pub(self, reveal=False):
        return {
            "user_id": self.user_id, "name": self.name, "chips": self.chips,
            "bet": self.bet,
            "cards": list(self.cards) if reveal else ["?"] * len(self.cards),
            "folded": self.folded, "all_in": self.all_in,
            "sit_out": self.sit_out, "last_action": self.last_action,
            "seat": self.seat, "finish_place": self.finish_place,
        }


class PokerTable:
    def __init__(self, table_id, players, sb=10, bb=20, starting_stack=1000, turn_time=20):
        self.table_id = table_id
        self.sb = sb
        self.bb = bb
        self.starting_stack = starting_stack
        self.turn_time = turn_time
        self.players = [PokerPlayer(p["user_id"], p["name"], p.get("chips", starting_stack), i)
                        for i, p in enumerate(players)]
        self.dealer_idx = -1
        self.phase = "waiting"
        self.community = []
        self.pot = 0
        self.side_pots = []
        self.current_bet = 0
        self.min_raise = bb
        self.turn_idx = -1
        self.turn_deadline = 0
        self.hand_number = 0
        self.finished = False
        self.winner = None
        self.log = []
        self.deck = []
        self.acted = set()          # кто уже действовал в раунде
        self.raise_locked = set()   # кому запрещён рейз (после короткого all-in)
        self.pre_actions = {}
        self.sb_idx = -1
        self.bb_idx = -1
        self._next_hand_at = 0
        self._rebuy_until = {}
        self.hand_history = []
        self.merge_warning_until = 0
        self.merge_with_table_id = None

    def _log(self, msg):
        self.log.append(msg)
        if len(self.log) > 40: self.log = self.log[-40:]

    def player_by_uid(self, uid):
        for p in self.players:
            if p.user_id == uid: return p
        return None

    def idx_by_uid(self, uid):
        for i, p in enumerate(self.players):
            if p.user_id == uid: return i
        return -1

    def seated_players(self):
        return [p for p in self.players if p.finish_place is None]

    def alive_idx(self, from_idx, skip_allin=False):
        n = len(self.players)
        if n == 0: return -1
        for i in range(1, n + 1):
            j = (from_idx + i) % n
            p = self.players[j]
            if p.sit_out or p.folded: continue
            if skip_allin and p.all_in: continue
            if p.finish_place is not None: continue
            return j
        return -1

    # ---------- SERIALIZATION ----------
    def to_dict(self, for_user_id=None, all_tables_summary=None):
        me_idx = self.idx_by_uid(for_user_id) if for_user_id else -1
        players_pub = []
        for i, p in enumerate(self.players):
            reveal = (self.phase == "showdown" and not p.folded) or (i == me_idx)
            pd = p.pub(reveal=reveal)
            pd["is_turn"] = (i == self.turn_idx)
            pd["is_dealer"] = (i == self.dealer_idx)
            pd["is_sb"] = (i == self.sb_idx)
            pd["is_bb"] = (i == self.bb_idx)
            pd["is_me"] = (i == me_idx)
            players_pub.append(pd)

        me = self.players[me_idx] if me_idx >= 0 else None
        to_call = can_check = can_call = can_bet = can_raise = False
        my_max = my_min_bet = 0
        if me and not me.folded and not me.all_in and self.turn_idx == me_idx:
            to_call = max(0, self.current_bet - me.bet)
            can_check = (to_call == 0)
            can_call = (to_call > 0 and me.chips > 0)
            locked = (me_idx in self.raise_locked)
            can_bet = (self.current_bet == 0) and not locked
            can_raise = (me.chips > to_call) and not locked
            my_max = me.chips + me.bet
            my_min_bet = max(self.current_bet + self.min_raise, self.bb)
            if my_min_bet > my_max: my_min_bet = my_max

        my_hand_name = ""
        if me and me.cards and len(self.community) >= 3:
            v = best_hand(list(me.cards) + list(self.community))
            if v: my_hand_name = hand_name(v)

        pre = self.pre_actions.get(me_idx, {}) if me_idx >= 0 else {}
        now_ms = int(time.time() * 1000)
        return {
            "type": "poker_state", "table_id": self.table_id,
            "phase": self.phase,
            "players": players_pub, "community": list(self.community),
            "pot": self.pot,
            "side_pots": [{"amount": sp["amount"], "eligible": list(sp["eligible"])}
                          for sp in self.side_pots],
            "current_bet": self.current_bet, "min_raise": self.min_raise,
            "turn_idx": self.turn_idx, "turn_deadline": self.turn_deadline,
            "turn_time": self.turn_time, "finished": self.finished,
            "winner": self.winner,
            "log": list(self.log[-12:]), "hand_number": self.hand_number,
            "server_now": now_ms, "me_idx": me_idx,
            "me_to_call": to_call, "can_check": can_check,
            "can_call": can_call, "can_bet": can_bet, "can_raise": can_raise,
            "my_max_bet": my_max, "my_min_bet": my_min_bet,
            "my_hand_name": my_hand_name,
            "pre_actions": {"check_fold": bool(pre.get("check_fold")),
                            "call_any": bool(pre.get("call_any"))},
            "sb": self.sb, "bb": self.bb,
            "merge_warning_until": self.merge_warning_until,
            "merge_with_table_id": self.merge_with_table_id,
            "all_tables_summary": all_tables_summary or [],
            "seated_count": len([p for p in self.players if p.finish_place is None]),
        }

    # ---------- HAND LIFECYCLE ----------
    def start_hand(self):
        if self.finished: return
        seated = [p for p in self.players if p.finish_place is None]
        if len(seated) < 2:
            self.phase = "waiting"
            return
        self.hand_number += 1
        self.community = []
        self.pot = 0
        self.side_pots = []
        self.current_bet = 0
        self.min_raise = self.bb
        self.turn_idx = -1
        self.turn_deadline = 0
        self.acted = set()
        self.raise_locked = set()
        self.pre_actions = {}
        self.log = []
        self.winner = None
        for p in self.players:
            p.cards = []
            p.folded = False
            p.all_in = False
            p.bet = 0
            p.total_bet = 0
            p.last_action = ""
            p.hand_value = None
            if p.chips <= 0 and p.finish_place is None:
                p.sit_out = True

        active = [i for i, p in enumerate(self.players)
                  if p.finish_place is None and not p.sit_out and p.chips > 0]
        if len(active) < 2:
            self.finished = True
            self._log("Недостаточно игроков за столом")
            return

        # Сдвигаем баттон к следующему активному игроку
        for _ in range(len(self.players)):
            self.dealer_idx = (self.dealer_idx + 1) % len(self.players)
            p = self.players[self.dealer_idx]
            if not p.sit_out and p.chips > 0 and p.finish_place is None:
                break

        deck = make_deck()
        for i in active:
            self.players[i].cards = [deck.pop(), deck.pop()]
        self.deck = deck

        # Определяем SB/BB
        if len(active) == 2:
            # Хедз-ап: баттон = SB, второй = BB
            sb_idx = self.dealer_idx
            bb_idx = self.alive_idx(sb_idx)
        else:
            sb_idx = self.alive_idx(self.dealer_idx)
            bb_idx = self.alive_idx(sb_idx)
        self.sb_idx = sb_idx
        self.bb_idx = bb_idx

        self._post_blind(sb_idx, self.sb)
        self._post_blind(bb_idx, self.bb)
        self.current_bet = self.bb
        self.min_raise = self.bb

        first = self.alive_idx(bb_idx, skip_allin=True)
        self.phase = "preflop"
        self._log(f"--- Раздача #{self.hand_number} ---")
        if first < 0:
            self._fast_forward(); return
        self._start_turn(first)

    def _post_blind(self, idx, amt):
        if idx < 0: return
        p = self.players[idx]
        a = min(amt, p.chips)
        p.chips -= a
        p.bet += a
        p.total_bet += a
        if p.chips == 0: p.all_in = True
        self.pot += a
        p.last_action = "SB" if amt == self.sb else "BB"

    def _start_turn(self, idx):
        self.turn_idx = idx
        self.turn_deadline = time.time() + self.turn_time
        self._apply_pre_actions()

    def _end_turn(self):
        self.turn_idx = -1
        self.turn_deadline = 0

    # ---------- ACTIONS ----------
    def act(self, user_id, action, amount=0):
        idx = self.idx_by_uid(user_id)
        if idx < 0: return False
        p = self.players[idx]
        if p.finish_place is not None: return False
        if self.turn_idx != idx:
            return self._set_pre(idx, action)
        if p.folded: return False
        self.acted.add(idx)

        if action == "fold":
            p.folded = True
            p.last_action = "Fold"
            self._log(f"{p.name}: Fold")

        elif action == "check":
            if p.bet < self.current_bet: return False
            p.last_action = "Check"
            self._log(f"{p.name}: Check")

        elif action == "call":
            to_call = self.current_bet - p.bet
            if to_call <= 0:
                p.last_action = "Check"
                self._log(f"{p.name}: Check")
            else:
                a = min(to_call, p.chips)
                p.chips -= a
                p.bet += a
                p.total_bet += a
                self.pot += a
                if p.chips == 0:
                    p.all_in = True
                    p.last_action = "All-in"
                    self._log(f"{p.name}: All-in {p.bet}")
                else:
                    p.last_action = f"Call {a}"
                    self._log(f"{p.name}: Call {a}")

        elif action in ("bet", "raise"):
            if idx in self.raise_locked:
                return False  # короткий all-in закрыл рейз для этого игрока
            try: target = int(amount)
            except: target = self.current_bet + self.min_raise
            min_target = self.current_bet + (self.min_raise if self.current_bet > 0 else self.bb)
            max_target = p.chips + p.bet
            if target > max_target: target = max_target
            if target < min_target:
                if p.chips + p.bet <= min_target:
                    target = max_target  # вынужденный all-in
                else:
                    return False
            add = target - p.bet
            if add > p.chips: add = p.chips
            p.chips -= add
            p.bet += add
            p.total_bet += add
            self.pot += add
            rs = p.bet - self.current_bet
            self.current_bet = p.bet
            # Полноценный рейз: обновляем min_raise, сбрасываем acted и блокировку
            if rs >= self.min_raise:
                self.min_raise = rs
                self.acted = {idx}
                self.raise_locked = set()
            if p.chips == 0:
                p.all_in = True
                p.last_action = "All-in"
                self._log(f"{p.name}: All-in {p.bet}")
            else:
                p.last_action = ("Bet" if action == "bet" else "Raise") + f" {p.bet}"
                self._log(f"{p.name}: {p.last_action}")

        elif action == "allin":
            add = p.chips
            p.bet += add
            p.total_bet += add
            self.pot += add
            p.chips = 0
            p.all_in = True
            if p.bet > self.current_bet:
                prev_acted = set(self.acted)  # уже включает idx
                rs = p.bet - self.current_bet
                is_full = (rs >= self.min_raise)
                self.current_bet = p.bet
                if is_full:
                    self.min_raise = rs
                    self.acted = {idx}
                    self.raise_locked = set()
                else:
                    # Короткий all-in: блокируем рейз для тех, кто уже действовал
                    self.raise_locked |= (prev_acted - {idx})
            p.last_action = "All-in"
            self._log(f"{p.name}: All-in {p.bet}")

        else:
            return False

        self._advance()
        return True

    def _set_pre(self, idx, action):
        p = self.players[idx]
        if p.folded or p.all_in: return False
        if action == "pre_check_fold":
            self.pre_actions.setdefault(idx, {})["check_fold"] = True
            return True
        if action == "pre_call_any":
            self.pre_actions.setdefault(idx, {})["call_any"] = True
            return True
        if action == "pre_clear":
            self.pre_actions.pop(idx, None)
            return True
        return False

    def _apply_pre_actions(self):
        idx = self.turn_idx
        if idx < 0: return
        pa = self.pre_actions.get(idx)
        if not pa: return
        p = self.players[idx]
        try:
            if pa.get("check_fold"):
                if p.bet >= self.current_bet: self.act(p.user_id, "check")
                else: self.act(p.user_id, "fold")
                self.pre_actions.pop(idx, None)
            elif pa.get("call_any"):
                self.act(p.user_id, "call")
                self.pre_actions.pop(idx, None)
        except Exception:
            pass

    # ---------- ROUND / STREET PROGRESSION ----------
    def _advance(self):
        alive = [p for p in self.players if not p.folded and p.finish_place is None]
        if len(alive) <= 1:
            self._win_early(); return
        can_act = [p for p in self.players if not p.folded and not p.all_in
                   and not p.sit_out and p.finish_place is None]
        if len(can_act) <= 1 and self._bets_matched():
            self._fast_forward(); return
        if self._round_done():
            self._next_phase(); return
        nxt = self.alive_idx(self.turn_idx, skip_allin=True)
        if nxt < 0 or nxt == self.turn_idx:
            self._next_phase(); return
        self._start_turn(nxt)

    def _bets_matched(self):
        for p in self.players:
            if p.folded or p.all_in or p.sit_out or p.finish_place is not None: continue
            if p.bet < self.current_bet: return False
        return True

    def _round_done(self):
        for i, p in enumerate(self.players):
            if p.folded or p.all_in or p.sit_out or p.finish_place is not None: continue
            if i not in self.acted: return False
            if p.bet < self.current_bet: return False
        return True

    def _next_phase(self):
        for p in self.players: p.bet = 0
        self.current_bet = 0
        self.min_raise = self.bb
        self.acted = set()
        self.raise_locked = set()

        if self.phase == "preflop":
            self.phase = "flop"
            self.community = [self.deck.pop(), self.deck.pop(), self.deck.pop()]
            self._log("--- Флоп ---")
        elif self.phase == "flop":
            self.phase = "turn"
            self.community.append(self.deck.pop())
            self._log("--- Тёрн ---")
        elif self.phase == "turn":
            self.phase = "river"
            self.community.append(self.deck.pop())
            self._log("--- Ривер ---")
        elif self.phase == "river":
            self._showdown(); return

        can_act = [p for p in self.players if not p.folded and not p.all_in
                   and not p.sit_out and p.finish_place is None]
        if len(can_act) <= 1:
            self._fast_forward(); return
        first = self.alive_idx(self.dealer_idx, skip_allin=True)
        if first < 0:
            self._fast_forward(); return
        self._start_turn(first)

    def _fast_forward(self):
        while self.phase in ("preflop", "flop", "turn"):
            if self.phase == "preflop":
                self.phase = "flop"
                self.community = [self.deck.pop(), self.deck.pop(), self.deck.pop()]
                self._log("--- Флоп ---")
            elif self.phase == "flop":
                self.phase = "turn"
                self.community.append(self.deck.pop()); self._log("--- Тёрн ---")
            elif self.phase == "turn":
                self.phase = "river"
                self.community.append(self.deck.pop()); self._log("--- Ривер ---")
        self._showdown()

    def _win_early(self):
        w = [p for p in self.players if not p.folded and p.finish_place is None][0]
        amount = self.pot
        w.chips += amount
        self.winner = {"user_id": w.user_id, "name": w.name, "amount": amount}
        self._log(f"🏆 {w.name} забирает банк {amount}")
        self.phase = "showdown"
        self._end_turn()
        self._save_history()
        self._schedule_next_hand()

    # ---------- SHOWDOWN & PAYOUT ----------
    def _showdown(self):
        self.phase = "showdown"
        self._end_turn()
        pots = self._build_pots()
        in_hand = [p for p in self.players if not p.folded and p.finish_place is None]
        for p in in_hand:
            p.hand_value = best_hand(list(p.cards) + list(self.community))

        # Раздаём банки по уровням
        for pot in pots:
            eligible = [p for p in in_hand if p.user_id in pot["eligible"]]
            self._award_pot(pot["amount"], eligible, pot.get("label", "pot"))

        self.pot = 0
        self.side_pots = []
        self._save_history()
        self._schedule_next_hand()

    def _build_pots(self):
        """Формируем список банков (main + side) по уровням вкладов."""
        contrib = {p.user_id: p.total_bet for p in self.players if p.total_bet > 0}
        if not contrib: return []
        levels = sorted(set(contrib.values()))
        prev = 0
        pots = []
        for lvl in levels:
            amt = 0
            elig = []
            for p in self.players:
                if p.total_bet >= lvl:
                    amt += (lvl - prev)
                    if not p.folded and p.finish_place is None:
                        elig.append(p.user_id)
            if amt > 0 and elig:
                label = "main" if prev == 0 else f"side{len(pots)}"
                pots.append({"amount": amt, "eligible": elig, "label": label})
            prev = lvl
        return pots

    def _award_pot(self, amount, eligible, label):
        if amount <= 0 or not eligible: return
        best = None
        winners = []
        for p in eligible:
            if p.hand_value is None: continue
            if best is None or p.hand_value > best:
                best = p.hand_value; winners = [p]
            elif p.hand_value == best:
                winners.append(p)
        if not winners: return
        share = amount // len(winners)
        rem = amount - share * len(winners)
        for i, w in enumerate(winners):
            a = share + (1 if i < rem else 0)
            w.chips += a
            self._log(f"💰 {w.name} получает {a} ({label})")
            if not self.winner:
                self.winner = {"user_id": w.user_id, "name": w.name, "amount": amount}

    def _save_history(self):
        self.hand_history.append({
            "hand": self.hand_number,
            "community": list(self.community),
            "players": [{"name": p.name, "user_id": p.user_id,
                         "cards": list(p.cards), "folded": p.folded}
                        for p in self.players],
            "winner": self.winner,
        })
        if len(self.hand_history) > 5:
            self.hand_history = self.hand_history[-5:]

    def _schedule_next_hand(self):
        self._next_hand_at = time.time() + NEXT_HAND_DELAY

    # ---------- TIMER TICK ----------
    def check_tick(self):
        now = time.time()
        if self.turn_idx >= 0 and now >= self.turn_deadline:
            p = self.players[self.turn_idx]
            if p.bet >= self.current_bet:
                self.act(p.user_id, "check")
            else:
                self.act(p.user_id, "fold")
            return True
        if self.phase == "showdown" and not self.finished:
            if self._next_hand_at and now >= self._next_hand_at:
                seated = [p for p in self.players if p.finish_place is None]
                if len(seated) < 2:
                    self.finished = True
                    return True
                self.start_hand()
                return True
        if self.phase == "waiting" and not self.finished:
            seated = [p for p in self.players if p.finish_place is None and p.chips > 0]
            if len(seated) >= 2 and self.hand_number == 0:
                self.start_hand()
                return True
        return False

    # ---------- PLAYER MGMT ----------
    def sit_out_toggle(self, uid):
        p = self.player_by_uid(uid)
        if not p or p.finish_place is not None: return None
        p.sit_out = not p.sit_out
        if p.sit_out and self.turn_idx >= 0 and self.players[self.turn_idx] is p:
            self.act(uid, "fold")
        return p.sit_out

    def rebuy(self, uid, amount=None):
        p = self.player_by_uid(uid)
        if not p or p.finish_place is not None: return False
        now = time.time()
        if now < self._rebuy_until.get(uid, 0): return False
        amt = amount if amount else self.starting_stack
        p.chips += amt
        self._rebuy_until[uid] = now + REBUY_COOLDOWN
        self._log(f"{p.name} докупил {amt}")
        return True

    def leave(self, uid):
        p = self.player_by_uid(uid)
        if not p: return False
        if self.turn_idx >= 0 and self.players[self.turn_idx] is p:
            self.act(uid, "fold")
        elif not p.folded:
            p.folded = True
        p.sit_out = True
        return True

    def add_player(self, pdata):
        in_progress = (self.hand_number > 0
                       and self.phase not in ("waiting", "showdown")
                       and not self.finished)
        pp = PokerPlayer(pdata["user_id"], pdata["name"],
                         pdata.get("chips", self.starting_stack), len(self.players))
        if in_progress:
            pp.folded = True
        self.players.append(pp)
        for i, pl in enumerate(self.players): pl.seat = i
        return pp

    def remove_player(self, uid):
        p = self.player_by_uid(uid)
        if not p: return False
        if self.turn_idx >= 0 and self.players[self.turn_idx] is p:
            try: self.act(uid, "fold")
            except: pass
        self.players = [x for x in self.players if x.user_id != uid]
        for i, pl in enumerate(self.players): pl.seat = i
        if self.players and self.dealer_idx >= len(self.players):
            self.dealer_idx = len(self.players) - 1
        return True


class PokerTournament:
    """Мульти-стол: игроки сами выбирают стол в лобби."""
    def __init__(self, tid, players, prize, sb=10, bb=20, starting_stack=1000,
                 turn_time=20, max_seats=MAX_SEATS_PER_TABLE):
        self.tid = tid
        self.prize = prize or "🏆 Приз"
        self.sb = sb
        self.bb = bb
        self.starting_stack = starting_stack
        self.turn_time = turn_time
        self.max_seats = max(2, min(int(max_seats), MAX_SEATS_PER_TABLE))
        self.tables: list[PokerTable] = []
        self.next_table_id = 1
        self.finished = False
        self.winner = None
        self._last_merge_check = 0
        self._merge_in_progress = False
        self._next_place = len(players)
        self.unseated = {}
        self.started_at = time.time()
        self.unseated_deadline = self.started_at + UNSEATED_TIMEOUT
        for p in players:
            self.unseated[p["user_id"]] = {
                "user_id": p["user_id"],
                "name": p["name"],
                "chips": starting_stack,
            }
        n_tables = max(1, (len(players) + self.max_seats - 1) // self.max_seats)
        for _ in range(n_tables):
            self._create_table()

    def _create_table(self):
        tid = self.next_table_id; self.next_table_id += 1
        t = PokerTable(tid, [], sb=self.sb, bb=self.bb,
                       starting_stack=self.starting_stack, turn_time=self.turn_time)
        self.tables.append(t)
        return t

    def get_table(self, table_id):
        for t in self.tables:
            if t.table_id == table_id: return t
        return None

    def find_user_table(self, user_id):
        for t in self.tables:
            if any(p.user_id == user_id for p in t.players):
                return t
        return None

    def is_seated(self, uid):
        return self.find_user_table(uid) is not None

    def sit(self, table_id, uid):
        if self.is_seated(uid):
            return False, "Уже за столом"
        if uid not in self.unseated:
            return False, "Не участник"
        data = self.unseated[uid]
        t = None
        if table_id:
            t = self.get_table(table_id)
            if not t: return False, "Стол не найден"
            seated = [p for p in t.players if p.finish_place is None]
            if len(seated) >= self.max_seats:
                return False, "Стол полон"
        else:
            best = None; best_n = 999
            for existing in self.tables:
                if existing.finished: continue
                seated = [p for p in existing.players if p.finish_place is None]
                if len(seated) >= self.max_seats: continue
                if len(seated) < best_n:
                    best = existing; best_n = len(seated)
            t = best
            if not t:
                t = self._create_table()
        t.add_player(data)
        del self.unseated[uid]
        seated = [p for p in t.players if p.finish_place is None and p.chips > 0]
        if len(seated) >= 2 and t.hand_number == 0 and not t.finished:
            try: t.start_hand()
            except Exception as e: logger.exception("start_hand on sit failed: %s", e)
        return True, None

    def auto_seat_bots(self):
        bot_uids = [uid for uid in list(self.unseated.keys()) if is_bot(uid)]
        for uid in bot_uids:
            self.sit(0, uid)

    def check_unseated_timeout(self):
        if time.time() < self.unseated_deadline: return []
        return [uid for uid in list(self.unseated.keys()) if not is_bot(uid)]

    def kick_unseated(self, uid):
        if uid in self.unseated:
            del self.unseated[uid]
            return True
        return False

    def kick_player(self, uid):
        if uid in self.unseated:
            del self.unseated[uid]
            return True
        for t in self.tables:
            if any(p.user_id == uid for p in t.players):
                t.remove_player(uid)
                return True
        return False

    def check_tick(self):
        events = []
        now = time.time()
        for t in self.tables:
            try:
                if t.check_tick(): events.append(("table_state", t.table_id))
            except Exception as e:
                logger.exception("table tick err tid=%s table=%s: %s", self.tid, t.table_id, e)
        for t in self.tables:
            if t.finished and not getattr(t, "_finished_broadcasted", False):
                self._assign_remaining_places(t)
                t._finished_broadcasted = True
                events.append(("table_finished", t.table_id))
        remaining = []
        for t in self.tables:
            for p in t.players:
                if p.chips > 0 and p.finish_place is None:
                    remaining.append(p)
        if not remaining and not self.unseated and not self.finished:
            self.finished = True
            events.append(("tournament_finished", None))
            return events
        if len(remaining) <= 1 and not self.unseated and not self.finished:
            if len(remaining) == 1:
                w = remaining[0]; w.finish_place = 1
                self.winner = {"user_id": w.user_id, "name": w.name}
            self.finished = True
            events.append(("tournament_finished", None))
            return events
        if (not self._merge_in_progress and not self.finished
                and now - self._last_merge_check >= MERGE_CHECK_INTERVAL):
            self._last_merge_check = now
            self._check_merge(events)
        for t in self.tables:
            if t.merge_warning_until and now >= t.merge_warning_until:
                self._do_merge(t.table_id, events)
        return events

    def _assign_remaining_places(self, t):
        place = self._next_place
        losers = [p for p in t.players if p.finish_place is None and p.chips <= 0]
        losers.sort(key=lambda p: p.chips)
        for p in losers:
            p.finish_place = place
            place = max(1, place - 1)
        self._next_place = place

    def _check_merge(self, events):
        occupied = [t for t in self.tables
                    if any(p.finish_place is None for p in t.players) and not t.finished]
        if len(occupied) < 2: return
        candidates = sorted(occupied, key=lambda t: len([p for p in t.players if p.finish_place is None]))
        for i in range(len(candidates) - 1):
            a = candidates[i]; b = candidates[i + 1]
            if a.merge_warning_until or b.merge_warning_until: continue
            a_live = [p for p in a.players if p.chips > 0 and p.finish_place is None]
            b_live = [p for p in b.players if p.chips > 0 and p.finish_place is None]
            if len(a_live) + len(b_live) <= self.max_seats:
                until = time.time() + MERGE_WARNING_SEC
                a.merge_warning_until = until; a.merge_with_table_id = b.table_id
                b.merge_warning_until = until; b.merge_with_table_id = a.table_id
                self._merge_in_progress = True
                events.append(("merge_warning", (a.table_id, b.table_id)))
                logger.info("[POKER MERGE warn] tid=%s tables=%s+%s",
                            self.tid, a.table_id, b.table_id)
                return

    def _do_merge(self, triggered_table_id, events):
        self._merge_in_progress = False
        t1 = self.get_table(triggered_table_id)
        if not t1 or not t1.merge_with_table_id:
            if t1:
                t1.merge_warning_until = 0
                t1.merge_with_table_id = None
            return
        t2 = self.get_table(t1.merge_with_table_id)
        if not t2:
            t1.merge_warning_until = 0; t1.merge_with_table_id = None
            return
        live1 = [p for p in t1.players if p.chips > 0 and p.finish_place is None]
        live2 = [p for p in t2.players if p.chips > 0 and p.finish_place is None]
        if len(live1) + len(live2) > self.max_seats:
            t1.merge_warning_until = 0; t1.merge_with_table_id = None
            t2.merge_warning_until = 0; t2.merge_with_table_id = None
            return
        host, guest = (t1, t2)
        if len(live2) > len(live1) or (len(live2) == len(live1) and t2.table_id < t1.table_id):
            host, guest = t2, t1
        moved = []
        for p in list(guest.players):
            if p.chips > 0 and p.finish_place is None:
                p.cards = []; p.bet = 0; p.total_bet = 0; p.folded = False
                p.all_in = False; p.last_action = ""; p.hand_value = None
                host.players.append(p)
                guest.players.remove(p)
                moved.append(p.user_id)
        for i, pl in enumerate(host.players): pl.seat = i
        host.merge_warning_until = 0; host.merge_with_table_id = None
        if guest in self.tables: self.tables.remove(guest)
        host.phase = "showdown"; host._next_hand_at = time.time() + 2.0
        events.append(("merged", (host.table_id, moved)))
        logger.info("[POKER MERGE done] tid=%s host=%s moved=%s",
                    self.tid, host.table_id, moved)

    def tables_summary(self):
        out = []
        for t in self.tables:
            active = [p for p in t.players if p.finish_place is None]
            alive = [p for p in active if p.chips > 0 and not p.sit_out]
            avg = sum(p.chips for p in alive) // len(alive) if alive else 0
            player_chips = [{"name": p.name, "chips": p.chips} for p in active]
            out.append({
                "table_id": t.table_id,
                "players_count": len(active),
                "alive_count": len(alive),
                "seated_count": len(active),
                "free_seats": max(0, self.max_seats - len(active)),
                "avg_stack": avg,
                "pot": t.pot,
                "phase": t.phase,
                "hand_number": t.hand_number,
                "merge_warning_until": t.merge_warning_until,
                "merge_with_table_id": t.merge_with_table_id,
                "finished": t.finished,
                "started": t.hand_number > 0,
                "player_chips": player_chips,
            })
        return out

    def all_players_summary(self):
        out = []
        for t in self.tables:
            for p in t.players:
                out.append({
                    "user_id": p.user_id, "name": p.name, "chips": p.chips,
                    "table_id": t.table_id, "sit_out": p.sit_out,
                    "folded": p.folded, "all_in": p.all_in,
                    "finish_place": p.finish_place, "seated": True,
                })
        for uid, data in self.unseated.items():
            out.append({
                "user_id": uid, "name": data["name"], "chips": data["chips"],
                "table_id": None, "sit_out": False, "folded": False,
                "all_in": False, "finish_place": None, "seated": False,
            })
        out.sort(key=lambda x: (
            1 if x["finish_place"] is not None else 0,
            x["finish_place"] if x["finish_place"] is not None else 0,
            0 if x["seated"] else 1,
            -x["chips"],
        ))
        return out

    def lobby_dict(self, for_user_id=None, is_admin=False):
        my_table = self.find_user_table(for_user_id) if for_user_id else None
        return {
            "type": "poker_lobby",
            "tid": self.tid,
            "prize": self.prize,
            "sb": self.sb, "bb": self.bb,
            "starting_stack": self.starting_stack,
            "turn_time": self.turn_time,
            "max_seats": self.max_seats,
            "tables": self.tables_summary(),
            "players": self.all_players_summary(),
            "finished": self.finished,
            "winner": self.winner,
            "server_now": int(time.time() * 1000),
            "for_user_id": for_user_id,
            "is_admin": is_admin,
            "my_table_id": my_table.table_id if my_table else None,
            "is_seated": my_table is not None,
            "unseated_count": len(self.unseated),
            "unseated_deadline_ms": (int(self.unseated_deadline * 1000) if self.unseated else 0),
        }

    def to_dict(self, table_id, for_user_id=None):
        t = self.get_table(table_id)
        if not t: return None
        return t.to_dict(for_user_id=for_user_id, all_tables_summary=self.tables_summary())
