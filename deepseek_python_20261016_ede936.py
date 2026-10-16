"""No-Limit Texas Hold'em engine for Telegram Mini App."""
import logging
import random
import time
from collections import Counter
from itertools import combinations

logger = logging.getLogger(__name__)

RANKS = "23456789TJQKA"
SUITS = "♠♥♦♣"
RV = {r: i for i, r in enumerate(RANKS, 2)}

TURN_TIME = 20
REBUY_AMOUNT = 1000
REBUY_COOLDOWN = 5
NEXT_HAND_DELAY = 6

HAND_NAMES = {8: "Стрит-флеш", 7: "Каре", 6: "Фулл-хаус", 5: "Флеш",
              4: "Стрит", 3: "Сет", 2: "Две пары", 1: "Пара", 0: "Старшая карта"}


def make_deck():
    d = [r + s for r in RANKS for s in SUITS]
    random.shuffle(d)
    return d


def pc(card):
    if not card or card == "?" or len(card) < 2:
        return None
    r, s = card[0], card[1:]
    if r not in RV or s not in SUITS:
        return None
    return (RV[r], s)


def _eval5(cards):
    vals = sorted([c[0] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1
    uniq = sorted(set(vals), reverse=True)
    is_straight = False
    sh = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            is_straight, sh = True, uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            is_straight, sh = True, 5
    cnt = Counter(vals)
    items = sorted(cnt.items(), key=lambda x: (x[1], x[0]), reverse=True)
    counts = [c for _, c in items]
    ov = [v for v, _ in items]
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
                 "bet", "total_bet", "sit_out", "ready", "last_action",
                 "hand_value", "seat")

    def __init__(self, user_id, name, chips, seat):
        self.user_id = user_id
        self.name = name
        self.chips = chips
        self.cards = []
        self.folded = False
        self.all_in = False
        self.bet = 0
        self.total_bet = 0
        self.sit_out = False
        self.ready = False
        self.last_action = ""
        self.hand_value = None
        self.seat = seat

    def pub(self, reveal=False):
        return {
            "user_id": self.user_id, "name": self.name, "chips": self.chips,
            "bet": self.bet,
            "cards": list(self.cards) if reveal else ["?"] * len(self.cards),
            "folded": self.folded, "all_in": self.all_in,
            "sit_out": self.sit_out, "last_action": self.last_action,
            "seat": self.seat,
        }


class PokerTable:
    def __init__(self, tid, players, prize, sb=10, bb=20, starting_stack=1000):
        self.tid = tid
        self.prize = prize or "🏆 Приз"
        self.sb = sb
        self.bb = bb
        self.starting_stack = starting_stack
        self.players = [PokerPlayer(p["user_id"], p["name"], starting_stack, i)
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
        self.acted = set()
        self.pre_actions = {}
        self.sb_idx = -1
        self.bb_idx = -1
        self._next_hand_at = 0
        self._rebuy_until = {}
        self.hand_history = []

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

    def alive_idx(self, from_idx, skip_allin=False):
        n = len(self.players)
        for i in range(1, n + 1):
            j = (from_idx + i) % n
            p = self.players[j]
            if p.sit_out or p.folded: continue
            if skip_allin and p.all_in: continue
            return j
        return -1

    # ---------- serialization ----------
    def to_dict(self, for_user_id=None):
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
            can_bet = (self.current_bet == 0)
            can_raise = me.chips > to_call
            my_max = me.chips + me.bet
            my_min_bet = max(self.current_bet + self.min_raise, self.bb)
            if my_min_bet > my_max: my_min_bet = my_max
        my_hand_name = ""
        if me and me.cards and len(self.community) >= 3:
            v = best_hand(list(me.cards) + list(self.community))
            if v: my_hand_name = hand_name(v)
        pre = self.pre_actions.get(me_idx, {}) if me_idx >= 0 else {}
        return {
            "type": "poker_state", "tid": self.tid, "phase": self.phase,
            "players": players_pub, "community": list(self.community),
            "pot": self.pot,
            "side_pots": [{"amount": sp["amount"], "eligible": list(sp["eligible"])}
                          for sp in self.side_pots],
            "current_bet": self.current_bet, "min_raise": self.min_raise,
            "turn_idx": self.turn_idx, "turn_deadline": self.turn_deadline,
            "turn_time": TURN_TIME, "finished": self.finished,
            "winner": self.winner, "prize": self.prize,
            "log": list(self.log[-12:]), "hand_number": self.hand_number,
            "server_now": int(time.time() * 1000), "me_idx": me_idx,
            "me_to_call": to_call, "can_check": can_check,
            "can_call": can_call, "can_bet": can_bet, "can_raise": can_raise,
            "my_max_bet": my_max, "my_min_bet": my_min_bet,
            "my_hand_name": my_hand_name,
            "pre_actions": {"check_fold": bool(pre.get("check_fold")),
                            "call_any": bool(pre.get("call_any"))},
            "sb": self.sb, "bb": self.bb,
        }

    # ---------- hand lifecycle ----------
    def start_hand(self):
        if self.finished: return
        self.hand_number += 1
        self.community = []
        self.pot = 0
        self.side_pots = []
        self.current_bet = 0
        self.min_raise = self.bb
        self.turn_idx = -1
        self.turn_deadline = 0
        self.acted = set()
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
            p.ready = False
            p.hand_value = None
            if p.chips <= 0 and not p.sit_out:
                p.sit_out = True
        active = [i for i, p in enumerate(self.players) if not p.sit_out and p.chips > 0]
        if len(active) < 2:
            self.finished = True
            self._log("Недостаточно игроков")
            return
        for _ in range(len(self.players)):
            self.dealer_idx = (self.dealer_idx + 1) % len(self.players)
            if not self.players[self.dealer_idx].sit_out and self.players[self.dealer_idx].chips > 0:
                break
        deck = make_deck()
        for i in active:
            self.players[i].cards = [deck.pop(), deck.pop()]
        self.deck = deck
        if len(active) == 2:
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
        self.turn_deadline = time.time() + TURN_TIME
        self._apply_pre_actions()

    def _end_turn(self):
        self.turn_idx = -1
        self.turn_deadline = 0

    # ---------- actions ----------
    def act(self, user_id, action, amount=0):
        idx = self.idx_by_uid(user_id)
        if idx < 0: return False
        p = self.players[idx]
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
            try: target = int(amount)
            except: target = self.current_bet + self.min_raise
            min_target = self.current_bet + (self.min_raise if self.current_bet > 0 else self.bb)
            max_target = p.chips + p.bet
            if target > max_target: target = max_target
            if target < min_target:
                if p.chips + p.bet <= min_target:
                    target = max_target
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
            if rs > self.min_raise: self.min_raise = rs
            self.acted = {idx}
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
                rs = p.bet - self.current_bet
                self.current_bet = p.bet
                if rs > self.min_raise: self.min_raise = rs
                self.acted = {idx}
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
                if p.bet >= self.current_bet:
                    self.act(p.user_id, "check")
                else:
                    self.act(p.user_id, "fold")
                self.pre_actions.pop(idx, None)
            elif pa.get("call_any"):
                self.act(p.user_id, "call")
                self.pre_actions.pop(idx, None)
        except Exception:
            pass

    def _advance(self):
        alive = [p for p in self.players if not p.folded]
        if len(alive) <= 1:
            self._win_early(); return
        can_act = [p for p in self.players if not p.folded and not p.all_in and not p.sit_out]
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
            if p.folded or p.all_in or p.sit_out: continue
            if p.bet < self.current_bet: return False
        return True

    def _round_done(self):
        for i, p in enumerate(self.players):
            if p.folded or p.all_in or p.sit_out: continue
            if i not in self.acted: return False
            if p.bet < self.current_bet: return False
        return True

    def _next_phase(self):
        for p in self.players:
            p.bet = 0
        self.current_bet = 0
        self.min_raise = self.bb
        self.acted = set()
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
        can_act = [p for p in self.players if not p.folded and not p.all_in and not p.sit_out]
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
                self.community.append(self.deck.pop())
                self._log("--- Тёрн ---")
            elif self.phase == "turn":
                self.phase = "river"
                self.community.append(self.deck.pop())
                self._log("--- Ривер ---")
        self._showdown()

    def _win_early(self):
        w = [p for p in self.players if not p.folded][0]
        amount = self.pot
        w.chips += amount
        self.winner = {"user_id": w.user_id, "name": w.name, "amount": amount}
        self._log(f"🏆 {w.name} забирает банк {amount}")
        self.phase = "showdown"
        self._end_turn()
        self._save_history()
        self._schedule_next_hand()

    def _showdown(self):
        self.phase = "showdown"
        self._end_turn()
        self._split_pots()
        in_hand = [p for p in self.players if not p.folded]
        for p in in_hand:
            p.hand_value = best_hand(list(p.cards) + list(self.community))
        self._award_pot(self.pot, in_hand, "main")
        for i, sp in enumerate(self.side_pots):
            elig = [p for p in in_hand if p.user_id in sp["eligible"]]
            self._award_pot(sp["amount"], elig, f"side{i}")
        self.pot = 0
        self.side_pots = []
        self._save_history()
        self._schedule_next_hand()

    def _split_pots(self):
        contrib = {p.user_id: p.total_bet for p in self.players if p.total_bet > 0}
        if not contrib: return
        levels = sorted(set(contrib.values()))
        prev = 0
        pots = []
        for lvl in levels:
            amt = 0
            elig = []
            for p in self.players:
                if p.total_bet >= lvl:
                    amt += (lvl - prev)
                    if not p.folded:
                        elig.append(p.user_id)
            if amt > 0 and elig:
                pots.append({"amount": amt, "eligible": elig})
            prev = lvl
        if pots:
            self.pot = pots[0]["amount"]
            self.side_pots = pots[1:]

    def _award_pot(self, amount, eligible, label):
        if amount <= 0 or not eligible: return
        best = None
        winners = []
        for p in eligible:
            if p.hand_value is None: continue
            if best is None or p.hand_value > best:
                best = p.hand_value
                winners = [p]
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

    # ---------- tick ----------
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
                alive = [p for p in self.players if p.chips > 0 and not p.sit_out]
                if len(alive) < 2:
                    self.finished = True
                    return True
                self.start_hand()
                return True
        return False

    # ---------- player mgmt ----------
    def sit_out_toggle(self, uid):
        p = self.player_by_uid(uid)
        if not p: return None
        p.sit_out = not p.sit_out
        if p.sit_out and self.turn_idx >= 0 and self.players[self.turn_idx] is p:
            self.act(uid, "fold")
        return p.sit_out

    def rebuy(self, uid, amount=REBUY_AMOUNT):
        p = self.player_by_uid(uid)
        if not p: return False
        now = time.time()
        if now < self._rebuy_until.get(uid, 0): return False
        p.chips += amount
        self._rebuy_until[uid] = now + REBUY_COOLDOWN
        self._log(f"{p.name} докупил {amount}")
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

    def active_count(self):
        return len([p for p in self.players if p.chips > 0 and not p.sit_out])