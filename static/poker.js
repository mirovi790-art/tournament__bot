/* ======================================================================
   poker.js — лобби покера и игровой стол
   ====================================================================== */

let PK_VIEW = "lobby";
let PK_LOBBY_TAB = "tables";
let PK_TABLE_ID = null;
let PK_LAST_LOBBY = null;
let PK_CUR_BET = 0, PK_MIN = 0, PK_MAX = 0;
let PK_PRE = { check_fold: false, call_any: false };
let PK_MERGE_UNTIL_MS = 0;
let PK_MERGE_MODAL_SHOWN = false;
let PK_UNSEATED_DEADLINE_MS = 0;

/* ---------- тик таймера хода ---------- */
setInterval(() => {
  if (!ACTIVE_TID || GAME_TYPE !== "poker") return;
  if (PK_VIEW !== "table") return;
  const s = LAST_STATE;
  if (!s || s.type !== "poker_state") return;
  if (s.turn_idx < 0 || !s.turn_deadline) return;
  const total = s.turn_time || 20;
  const rem = Math.max(0, s.turn_deadline - (Date.now() / 1000));
  const activeIdx = s.turn_idx;
  document.querySelectorAll(".poker-seat").forEach(seat => {
    const idx = parseInt(seat.dataset.idx, 10);
    const ring = seat.querySelector(".poker-timer-ring");
    if (!ring) return;
    if (idx === activeIdx) {
      const frac = Math.min(1, Math.max(0, rem / total));
      const C = 2 * Math.PI * 45;
      ring.style.strokeDasharray = C;
      ring.style.strokeDashoffset = C * (1 - frac);
      ring.classList.toggle("warn", rem <= total * 0.4 && rem > 5);
      ring.classList.toggle("danger", rem <= 5);
    } else {
      ring.style.strokeDashoffset = 2 * Math.PI * 45;
      ring.classList.remove("warn", "danger");
    }
  });
}, 250);

/* ---------- merge-баннер отсчёт ---------- */
setInterval(() => {
  if (!ACTIVE_TID || GAME_TYPE !== "poker") return;
  const now = Date.now();
  if (PK_MERGE_UNTIL_MS && now >= PK_MERGE_UNTIL_MS) PK_MERGE_UNTIL_MS = 0;
  const banner = document.getElementById("pk-merge-banner");
  if (banner && PK_MERGE_UNTIL_MS) {
    const rem = Math.max(0, Math.ceil((PK_MERGE_UNTIL_MS - now) / 1000));
    const cd = banner.querySelector(".cd");
    if (cd) cd.textContent = rem;
  }
}, 250);

/* ---------- обратный отсчёт до авто-кика нерассаженных ---------- */
setInterval(() => {
  if (!ACTIVE_TID || GAME_TYPE !== "poker") return;
  if (PK_VIEW !== "lobby") return;
  if (!PK_UNSEATED_DEADLINE_MS) return;
  const now = Date.now();
  const rem = Math.max(0, Math.ceil((PK_UNSEATED_DEADLINE_MS - now) / 1000));
  const cd = document.getElementById("pk-unseated-cd");
  if (cd) cd.textContent = rem;
  if (rem <= 0) PK_UNSEATED_DEADLINE_MS = 0;
}, 500);

/* ============================================================
   LOBBY
   ============================================================ */
function renderPokerLobby(s) {
  LAST_STATE = s;
  PK_LAST_LOBBY = s;
  PK_VIEW = "lobby";
  PK_UNSEATED_DEADLINE_MS = s.unseated_deadline_ms || 0;
  els.roomExitBtn.classList.add("hidden");
  els.rmCounter.textContent = "Покер · лобби";

  const info = `
    <div class="pk-lobby-info">
      <div class="pk-info-cell"><div class="lbl">Стек</div><div class="val">${s.starting_stack || 1000}</div></div>
      <div class="pk-info-cell"><div class="lbl">Блайнды</div><div class="val">${(s.sb || 10)}/${(s.bb || 20)}</div></div>
      <div class="pk-info-cell"><div class="lbl">Ход</div><div class="val">${s.turn_time || 20}с</div></div>
      <div class="pk-info-cell"><div class="lbl">Мест</div><div class="val">${s.max_seats || 7}</div></div>
    </div>`;

  let banner = "";
  if (!s.is_seated && PK_UNSEATED_DEADLINE_MS && PK_UNSEATED_DEADLINE_MS * 1000 > Date.now()) {
    const rem = Math.max(0, Math.ceil((PK_UNSEATED_DEADLINE_MS - Date.now() / 1000)));
    banner = `
      <div class="pk-unseated-banner">
        <span class="big">🪑 Выберите стол</span>
        Если не сесть за стол в течение <span class="cd" id="pk-unseated-cd">${rem}</span> сек — вас исключат из турнира.
      </div>`;
  } else if (!s.is_seated) {
    banner = `
      <div class="pk-unseated-banner">
        <span class="big">🪑 Выберите стол</span>
        Нажмите «Сесть» на любом свободном столе.
      </div>`;
  }

  const tabs = `
    <div class="pk-lobby-tabs">
      <div class="pk-lobby-tab ${PK_LOBBY_TAB === "tables" ? "active" : ""}" data-pktab="tables">🃏 Столы</div>
      <div class="pk-lobby-tab ${PK_LOBBY_TAB === "ranking" ? "active" : ""}" data-pktab="ranking">🏆 Рейтинг</div>
    </div>`;

  let body = "";
  if (PK_LOBBY_TAB === "tables") {
    const tables = s.tables || [];
    if (!tables.length) {
      body = `<div class="waiting-box" style="padding:40px 20px;"><div class="waiting-ico">🃏</div><div class="waiting-txt">Столы ещё не созданы</div><div style="color:var(--text-muted);font-size:12px;margin-top:4px;">Ожидайте запуска турнира.</div></div>`;
    } else {
      body = `<div style="display:flex;flex-direction:column;gap:10px;">`;
      tables.forEach(t => {
        const freeSeats = t.free_seats !== undefined ? t.free_seats : Math.max(0, (s.max_seats || 7) - (t.players_count || 0));
        const isFull = freeSeats <= 0;
        const isMyTable = (s.my_table_id === t.table_id);
        const seatedCount = t.seated_count !== undefined ? t.seated_count : (t.players_count || 0);
        const freeLabel = `${seatedCount} / ${s.max_seats || 7}`;

        let statusCls = "waiting";
        let statusTxt = "Ожидание";
        if (t.merge_warning_until && t.merge_warning_until * 1000 > Date.now()) {
          statusCls = "merge"; statusTxt = "⚠ Слияние";
        } else if (t.started || (t.phase && t.phase !== "waiting")) {
          statusCls = "running"; statusTxt = `Идёт (#${t.hand_number || 0})`;
        } else if (isFull) {
          statusCls = "full"; statusTxt = "Полон";
        }

        let cls = "pk-table-card";
        if (isMyTable) cls += " mine";
        if (isFull && !isMyTable) cls += " full";

        let btn;
        if (isMyTable) {
          btn = `<button class="pkt-btn open" data-pk-open="${t.table_id}">🎮 Открыть стол</button>`;
        } else if (s.is_seated) {
          btn = `<button class="pkt-btn full" disabled>Вы уже за столом #${s.my_table_id}</button>`;
        } else if (isFull) {
          btn = `<button class="pkt-btn full" disabled>Стол полон</button>`;
        } else {
          btn = `<button class="pkt-btn" data-pk-sit="${t.table_id}">🪑 Сесть за стол</button>`;
        }

        const playersChips = (t.player_chips || []);
        const chipsRow = playersChips.length
          ? `<div class="pkt-players">${playersChips.slice(0, 8).map(p => `<span class="pkt-chip">${escapeHtml(p.name || "")}: ${p.chips || 0}</span>`).join("")}</div>`
          : "";

        body += `
          <div class="${cls}">
            <div class="pkt-hdr">
              <div class="pkt-title">🃏 Стол #${t.table_id}</div>
              <div class="pkt-status ${statusCls}">${statusTxt}</div>
            </div>
            <div class="pkt-meta">
              <span>👥 Мест: <b>${freeLabel}</b></span>
              <span>💰 Ср. стек: <b>${t.avg_stack || 0}</b></span>
              <span>🏦 Банк: <b>${t.pot || 0}</b></span>
            </div>
            ${chipsRow}
            ${btn}
          </div>`;
      });
      body += `</div>`;
    }
  } else {
    const players = s.players || [];
    if (!players.length) {
      body = `<div class="waiting-box" style="padding:40px 20px;"><div class="waiting-ico">📊</div><div class="waiting-txt">Рейтинг пуст</div></div>`;
    } else {
      body = `<div class="pk-ranking-list">`;
      let place = 1;
      players.forEach(p => {
        const isOut = p.finish_place !== null && p.finish_place !== undefined;
        const isMe = p.user_id === MY_ID;
        const cls = "pk-rank-row" + (isOut ? " out" : "") + (isMe ? " me" : "");
        const placeShow = isOut ? ("#" + p.finish_place) : place;
        const chipsShow = isOut ? "выбыл" : `💰 ${p.chips}`;
        let seatTag = "";
        if (!isOut) {
          if (p.seated === false) {
            seatTag = `<span class="rk-unseated">не за столом</span>`;
          } else if (p.table_id) {
            seatTag = `<span class="rk-place-seated">стол #${p.table_id}</span>`;
          }
        }
        let kickBtn = "";
        if (IS_ADMIN && !isMe && !isOut) {
          kickBtn = `<button class="rk-kick" data-pk-kick="${p.user_id}" data-pk-kick-name="${escapeHtml(p.name || "")}">🚫 Искл.</button>`;
        }
        body += `
          <div class="${cls}">
            <div class="rk-place">${placeShow}</div>
            <div class="rk-name">${escapeHtml(p.name)}${isMe ? ' <span style="color:#ffcc00;">(вы)</span>' : ""}</div>
            ${seatTag}
            <div class="rk-chips">${chipsShow}</div>
            ${kickBtn}
          </div>`;
        if (!isOut) place++;
      });
      body += `</div>`;
    }
  }

  els.roomContent.innerHTML = `<div class="pk-lobby">${info}${banner}${tabs}${body}</div>`;

  els.roomContent.querySelectorAll(".pk-lobby-tab").forEach(el => {
    el.addEventListener("click", () => {
      PK_LOBBY_TAB = el.dataset.pktab;
      if (PK_LAST_LOBBY) renderPokerLobby(PK_LAST_LOBBY);
    });
  });
  els.roomContent.querySelectorAll("[data-pk-sit]").forEach(el => {
    el.addEventListener("click", e => {
      e.stopPropagation();
      const tid = parseInt(el.dataset.pkSit, 10);
      if (!tid) return;
      wsSend({ type: "poker_sit", table_id: tid });
    });
  });
  els.roomContent.querySelectorAll("[data-pk-open]").forEach(el => {
    el.addEventListener("click", e => {
      e.stopPropagation();
      const tid = parseInt(el.dataset.pkOpen, 10);
      if (!tid) return;
      openPokerTable(tid);
    });
  });
  els.roomContent.querySelectorAll("[data-pk-kick]").forEach(el => {
    el.addEventListener("click", e => {
      e.stopPropagation();
      const uid = parseInt(el.dataset.pkKick, 10);
      const nm = el.dataset.pkKickName || "Игрок";
      if (!uid) return;
      showConfirm(
        "🚫 Исключить игрока?",
        `Игрок <b style="color:#ffcc00;">${escapeHtml(nm)}</b> будет исключён из покер-турнира.`,
        () => wsSend({ type: "admin_kick_player", user_id: uid }),
        { yesLabel: "Исключить", noLabel: "Отмена" }
      );
    });
  });
}

function openPokerTable(tableId) {
  PK_VIEW = "table";
  PK_TABLE_ID = tableId;
  PK_MERGE_MODAL_SHOWN = false;
  wsSend({ type: "poker_enter_table", table_id: tableId });
  els.roomExitBtn.classList.remove("hidden");
  els.roomExitBtn.textContent = "⬅️ В лобби";
  els.roomContent.innerHTML = `<div class="waiting-box"><div class="waiting-ico">🃏</div><div class="waiting-txt">Подключение к столу…</div></div>`;
}

/* ============================================================
   TABLE
   ============================================================ */
function pokerSeatPositions(total) {
  const positions = [];
  const nOpp = Math.max(0, total - 1);
  const cx = 50, cy = 50;
  const rx = 42, ry = 40;
  if (nOpp === 1) {
    positions.push([50, 14]);
  } else if (nOpp > 1) {
    for (let i = 0; i < nOpp; i++) {
      const t = i / (nOpp - 1);
      const angleDeg = 165 - t * 150;
      const rad = angleDeg * Math.PI / 180;
      const x = cx + rx * Math.cos(rad);
      const y = cy - ry * Math.sin(rad);
      positions.push([x, y]);
    }
  }
  positions.push([50, 90]);
  return positions;
}

function cardMiniHtml(card) {
  if (!card || card === "?" || card.length < 2) return `<div class="mini"></div>`;
  const r = card[0] === "T" ? "10" : card[0];
  const s = card.slice(1);
  const red = (s === "♥" || s === "♦") ? " red" : "";
  return `<div class="mini face${red}">${r}<span class="s">${s}</span></div>`;
}
function cardBigHtml(card) {
  if (!card || card === "?" || card.length < 2) return `<div class="card empty"></div>`;
  const r = card[0] === "T" ? "10" : card[0];
  const s = card.slice(1);
  const red = (s === "♥" || s === "♦") ? " red" : "";
  return `<div class="card${red}"><span class="r">${r}</span><span class="s">${s}</span></div>`;
}

function renderPokerState(s) {
  LAST_STATE = s;
  if (PK_VIEW !== "table") {
    PK_VIEW = "table";
    PK_TABLE_ID = s.table_id;
    els.roomExitBtn.classList.remove("hidden");
    els.roomExitBtn.textContent = "⬅️ В лобби";
  }
  if (s.table_id) PK_TABLE_ID = s.table_id;

  if (!s.players || !s.players.length) {
    els.roomContent.innerHTML = `<div class="waiting-box"><div class="waiting-ico">🃏</div><div class="waiting-txt">Ожидание игроков…</div></div>`;
    return;
  }

  const players = s.players;
  const meIdx = (s.me_idx !== undefined && s.me_idx >= 0) ? s.me_idx : 0;
  const me = players[meIdx] || {};
  const positions = pokerSeatPositions(players.length);
  els.rmCounter.textContent = `Стол #${s.table_id || 0} · Раздача #${s.hand_number || 0} · Блайнды ${s.sb || 10}/${s.bb || 20}`;

  let seatsHtml = "";
  const order = [];
  for (let i = 0; i < players.length; i++) if (i !== meIdx) order.push(i);
  order.push(meIdx);
  order.forEach((idx, posIdx) => {
    const p = players[idx];
    const pos = positions[posIdx] || [50, 50];
    const isTurn = (idx === s.turn_idx);
    const isDealer = !!p.is_dealer;
    const folded = !!p.folded;
    const sitOut = !!p.sit_out;
    const isOut = (p.finish_place !== null && p.finish_place !== undefined);
    const lastAction = p.last_action || "";
    const lastActionCls = lastAction.toLowerCase().split(/\s+/)[0];
    const cardsHtml = (p.cards || []).map(c => cardMiniHtml(c)).join("");
    let cls = "poker-seat";
    if (isTurn) cls += " turn";
    if (isDealer) cls += " dealer";
    if (folded) cls += " folded";
    if (sitOut) cls += " sitout";
    if (isOut) cls += " out";

    let betChipHtml = "";
    if (p.bet > 0) {
      betChipHtml = `<div class="bet-chip">${p.bet}</div>`;
    }

    seatsHtml += `
      <div class="${cls}" data-idx="${idx}" style="left:${pos[0]}%;top:${pos[1]}%;">
        ${lastAction ? `<div class="last-action ${lastActionCls}">${escapeHtml(lastAction)}</div>` : ""}
        <div class="poker-avatar-wrap">
          <svg class="poker-timer-svg" viewBox="0 0 100 100">
            <circle class="poker-timer-bg" cx="50" cy="50" r="45"/>
            <circle class="poker-timer-ring" cx="50" cy="50" r="45"
                    stroke-dasharray="283" stroke-dashoffset="283"/>
          </svg>
          <div class="poker-avatar">${idx === meIdx ? "🙋" : "👤"}</div>
        </div>
        <div class="pname">${escapeHtml(p.name)}${idx === meIdx ? " (вы)" : ""}</div>
        <div class="pchips">💰 ${p.chips}</div>
        ${!isOut && cardsHtml ? `<div class="pcards">${cardsHtml}</div>` : ""}
        ${isOut ? `<div class="pfinish">#${p.finish_place}</div>` : ""}
        ${betChipHtml}
      </div>`;
  });

  const community = s.community || [];
  let commHtml = "";
  for (let i = 0; i < 5; i++) {
    commHtml += community[i] ? cardBigHtml(community[i]) : `<div class="card empty"></div>`;
  }

  const sidePotsHtml = (s.side_pots && s.side_pots.length)
    ? `<div class="poker-sidepots">${s.side_pots.map((sp, i) => `<span class="sp">Side ${i + 1}: ${sp.amount}</span>`).join("")}</div>`
    : "";

  const phaseNames = {
    preflop: "Префлоп", flop: "Флоп", turn: "Тёрн",
    river: "Ривер", showdown: "Вскрытие", waiting: "Ожидание",
  };
  const phaseLabel = phaseNames[s.phase] || s.phase || "";

  let mergeBanner = "";
  if (s.merge_warning_until && s.merge_warning_until * 1000 > Date.now()) {
    const rem = Math.max(0, Math.ceil((s.merge_warning_until * 1000 - Date.now()) / 1000));
    mergeBanner = `
      <div class="pk-merge-banner" id="pk-merge-banner">
        <span class="big">⚠️ Столы объединяются</span>
        Слияние со столом #${s.merge_with_table_id || "—"} через <span class="cd">${rem}</span> сек
      </div>`;
  }

  const feltHtml = `
    <div class="poker-table-wrap">
      <div class="poker-felt">
        ${seatsHtml}
        <div class="poker-center">
          <div class="poker-pot">💰 ${s.pot || 0}</div>
          ${sidePotsHtml}
          <div class="poker-community">${commHtml}</div>
          <div class="poker-phase">${phaseLabel}</div>
        </div>
      </div>
    </div>`;

  const myCardsHtml = (me.cards || []).map(c => {
    if (!c || c === "?" || c.length < 2) return "";
    const r = c[0] === "T" ? "10" : c[0];
    const suit = c.slice(1);
    const red = (suit === "♥" || suit === "♦") ? " red" : "";
    return `<div class="card${red}"><span class="r">${r}</span><span class="s">${suit}</span></div>`;
  }).join("");
  const myHandName = s.my_hand_name ? `🃏 ${s.my_hand_name}` : "";
  const myZoneHtml = `
    <div class="poker-myzone">
      <div class="my-cards">${myCardsHtml || '<div class="hand-name muted" style="padding:16px;">Ждём раздачи…</div>'}</div>
      ${myHandName ? `<div class="hand-name">${escapeHtml(myHandName)}</div>` : ""}
    </div>`;

  const isMyTurn = s.turn_idx === meIdx && !me.folded && !me.all_in && !me.sit_out && !me.finish_place;
  const canCheck = s.can_check;
  const canCall = s.can_call;
  const canBet = s.can_bet;
  const canRaise = s.can_raise;
  const toCall = s.me_to_call || 0;
  const myMaxBet = s.my_max_bet || 0;
  const myMinBet = s.my_min_bet || 0;

  PK_MIN = myMinBet;
  PK_MAX = myMaxBet;
  if (PK_CUR_BET < PK_MIN) PK_CUR_BET = PK_MIN;
  if (PK_CUR_BET > PK_MAX) PK_CUR_BET = PK_MAX;

  let controlsHtml = "";
  if (isMyTurn) {
    const mainAction2Label = canCheck ? "CHECK" : `CALL ${toCall}`;
    const mainAction3Label = canBet ? `BET ${PK_CUR_BET}` : (canRaise ? `RAISE ${PK_CUR_BET}` : "BET");
    controlsHtml = `
      <div class="poker-controls"><div class="frame">
        <div class="bet-slider-row">
          <button class="stepbtn" id="bet-minus" ${PK_CUR_BET <= PK_MIN ? "disabled" : ""}>−</button>
          <div class="slider-wrap">
            <input type="range" id="bet-slider" min="${PK_MIN}" max="${PK_MAX}" value="${PK_CUR_BET}" step="${Math.max(1, Math.floor((s.bb || 20) / 2))}"/>
            <div class="bet-val" id="bet-val">${PK_CUR_BET}</div>
          </div>
          <button class="stepbtn" id="bet-plus" ${PK_CUR_BET >= PK_MAX ? "disabled" : ""}>+</button>
        </div>
        <div class="quick-bets">
          <button data-quick="min">MIN</button>
          <button data-quick="half">½ POT</button>
          <button data-quick="pot">POT</button>
          <button data-quick="allin">ALL-IN</button>
        </div>
        <div class="main-actions">
          <button class="act-fold" data-act="fold">FOLD</button>
          <button class="${canCheck ? "act-check" : "act-call"}" data-act="${canCheck ? "check" : "call"}">${mainAction2Label}</button>
          <button class="${canBet ? "act-bet" : "act-raise"}" data-act="${canBet ? "bet" : "raise"}" ${(!canBet && !canRaise) ? "disabled" : ""}>${mainAction3Label}</button>
        </div>
        <div class="poker-utility">
          <button id="pk-sitout" class="${me.sit_out ? "active" : ""}">${me.sit_out ? "↩️ I'm back" : "💤 Sit out"}</button>
          <button class="warn" id="pk-rebuy">💰 Rebuy</button>
          <button class="danger" id="pk-leave">🚪 Leave</button>
        </div>
      </div></div>`;
  } else {
    const preCF = (s.pre_actions && s.pre_actions.check_fold) || false;
    const preCA = (s.pre_actions && s.pre_actions.call_any) || false;
    PK_PRE.check_fold = preCF;
    PK_PRE.call_any = preCA;
    const activePlayer = (s.turn_idx >= 0 && players[s.turn_idx]) ? players[s.turn_idx] : null;
    const waitingLabel = activePlayer
      ? `Ход: <span class="who">${escapeHtml(activePlayer.name)}</span>`
      : (s.phase === "showdown" ? "Вскрытие…" : "Ожидание…");
    const canPreAct = !me.folded && !me.sit_out && !me.finish_place && s.phase !== "showdown" && !s.finished;
    controlsHtml = `
      <div class="poker-controls"><div class="frame">
        ${canPreAct ? `
          <div class="pre-actions">
            <div class="pre-row ${preCF ? "on" : ""}" data-pre="check_fold">
              <div class="cb">✓</div>
              <div>Check / Fold <span style="color:#9a9a9a;font-size:10px;margin-left:4px;">(чек, если бесплатно — иначе фолд)</span></div>
            </div>
            <div class="pre-row ${preCA ? "on" : ""}" data-pre="call_any">
              <div class="cb">✓</div>
              <div>Call Any <span style="color:#9a9a9a;font-size:10px;margin-left:4px;">(колл текущей ставки)</span></div>
            </div>
          </div>` : ""}
        <div class="waiting-actions">${waitingLabel}</div>
        <div class="poker-utility">
          <button id="pk-sitout" class="${me.sit_out ? "active" : ""}">${me.sit_out ? "↩️ I'm back" : "💤 Sit out"}</button>
          <button class="warn" id="pk-rebuy">💰 Rebuy</button>
          <button class="danger" id="pk-leave">🚪 Leave</button>
        </div>
      </div></div>`;
  }

  els.roomContent.innerHTML = mergeBanner + feltHtml + myZoneHtml + controlsHtml;

  /* ---------- навешивание событий ---------- */
  const sl = document.getElementById("bet-slider");
  if (sl) sl.addEventListener("input", () => { PK_CUR_BET = parseInt(sl.value, 10) || 0; pkUpdateBetUI(); });
  const bm = document.getElementById("bet-minus");
  if (bm) bm.addEventListener("click", () => { PK_CUR_BET = Math.max(PK_MIN, PK_CUR_BET - (s.bb || 20)); pkUpdateBetUI(); });
  const bp = document.getElementById("bet-plus");
  if (bp) bp.addEventListener("click", () => { PK_CUR_BET = Math.min(PK_MAX, PK_CUR_BET + (s.bb || 20)); pkUpdateBetUI(); });

  document.querySelectorAll(".quick-bets button").forEach(b => {
    b.addEventListener("click", () => {
      const kind = b.dataset.quick;
      const potNow = s.pot || 0;
      const cur = s.current_bet || 0;
      if (kind === "min") PK_CUR_BET = PK_MIN;
      else if (kind === "half") PK_CUR_BET = Math.max(PK_MIN, Math.min(PK_MAX, Math.floor(potNow * 0.5) + cur));
      else if (kind === "pot") PK_CUR_BET = Math.max(PK_MIN, Math.min(PK_MAX, potNow + cur));
      else if (kind === "allin") PK_CUR_BET = PK_MAX;
      pkUpdateBetUI();
    });
  });

  document.querySelectorAll(".main-actions button").forEach(b => {
    b.addEventListener("click", () => {
      const act = b.dataset.act;
      if (act === "fold") pkSend({ action: "fold" });
      else if (act === "check") pkSend({ action: "check" });
      else if (act === "call") pkSend({ action: "call" });
      else if (act === "bet") pkSend({ action: "bet", amount: PK_CUR_BET });
      else if (act === "raise") pkSend({ action: "raise", amount: PK_CUR_BET });
    });
  });

  document.querySelectorAll(".pre-row").forEach(r => {
    r.addEventListener("click", () => {
      const key = r.dataset.pre;
      const willBeOn = !r.classList.contains("on");
      if (key === "check_fold") {
        PK_PRE.check_fold = willBeOn;
        PK_PRE.call_any = false;
        pkSend({ action: willBeOn ? "pre_check_fold" : "pre_clear" });
      } else if (key === "call_any") {
        PK_PRE.call_any = willBeOn;
        PK_PRE.check_fold = false;
        pkSend({ action: willBeOn ? "pre_call_any" : "pre_clear" });
      }
    });
  });

  const soBtn = document.getElementById("pk-sitout");
  if (soBtn) soBtn.addEventListener("click", () => wsSend({ type: "poker_sit_out", table_id: PK_TABLE_ID }));
  const rbBtn = document.getElementById("pk-rebuy");
  if (rbBtn) rbBtn.addEventListener("click", () => wsSend({ type: "poker_rebuy", table_id: PK_TABLE_ID }));
  const lvBtn = document.getElementById("pk-leave");
  if (lvBtn) lvBtn.addEventListener("click", () => {
    showConfirm(
      "🚪 Покинуть стол?",
      "Вы выйдете из раздачи. Подтвердить?",
      () => {
        wsSend({ type: "poker_leave", table_id: PK_TABLE_ID });
        PK_VIEW = "lobby";
        PK_TABLE_ID = null;
        wsSend({ type: "request_lobby" });
        els.roomExitBtn.classList.add("hidden");
      },
      { yesLabel: "Выйти", noLabel: "Остаться" }
    );
  });
}

function pkSend(payload) {
  wsSend(Object.assign({ type: "poker_action", table_id: PK_TABLE_ID }, payload));
}

function pkUpdateBetUI() {
  const sl = document.getElementById("bet-slider");
  const val = document.getElementById("bet-val");
  if (sl) sl.value = PK_CUR_BET;
  if (val) val.textContent = PK_CUR_BET;
  const bm = document.getElementById("bet-minus");
  const bp = document.getElementById("bet-plus");
  if (bm) bm.disabled = PK_CUR_BET <= PK_MIN;
  if (bp) bp.disabled = PK_CUR_BET >= PK_MAX;
  const raiseBtn = document.querySelector(".main-actions .act-bet, .main-actions .act-raise");
  if (raiseBtn) {
    const isBet = raiseBtn.classList.contains("act-bet");
    raiseBtn.textContent = isBet ? `BET ${PK_CUR_BET}` : `RAISE ${PK_CUR_BET}`;
  }
}

function handleMergeWarning(msg) {
  const until = msg.merge_until_ms || 0;
  PK_MERGE_UNTIL_MS = until;
  const withTable = msg.with_table_id || "—";
  const rem = Math.max(0, Math.ceil((until - Date.now()) / 1000));
  if (PK_MERGE_MODAL_SHOWN) return;
  PK_MERGE_MODAL_SHOWN = true;
  els.modalTitle.textContent = "⚠️ Слияние столов";
  els.modalBody.innerHTML = `
    Ваш стол объединяется со столом <b style="color:#ffcc00;">#${withTable}</b>.<br><br>
    Через <b id="pk-merge-cd" style="color:#ff8080;font-size:20px;">${rem}</b> секунд все игроки будут пересажены за один стол.
    <br><br><span style="color:#9a9a9a;font-size:12px;">Раздача завершится, деньги сохранятся.</span>`;
  els.modalBtns.innerHTML = `<button class="btn primary" id="pk-merge-ok">Понятно</button>`;
  document.getElementById("pk-merge-ok").onclick = () => hide(els.modal);
  show(els.modal);
  const tick = setInterval(() => {
    if (!PK_MERGE_UNTIL_MS) { clearInterval(tick); hide(els.modal); return; }
    const r = Math.max(0, Math.ceil((PK_MERGE_UNTIL_MS - Date.now()) / 1000));
    const cd = document.getElementById("pk-merge-cd");
    if (cd) cd.textContent = r;
    if (r <= 0) { clearInterval(tick); hide(els.modal); }
  }, 250);
  setTimeout(() => {
    if (PK_MERGE_MODAL_SHOWN) { hide(els.modal); PK_MERGE_MODAL_SHOWN = false; }
  }, 9000);
  try { tg?.HapticFeedback?.notificationOccurred?.("warning"); } catch (e) {}
}

/* ============================================================
   ХУКИ
   ============================================================ */
registerHooks({
  poker: renderPokerState,
  pokerLobby: renderPokerLobby,
  pokerMergeWarning: handleMergeWarning,
  pokerSitError: (msg) => showModal("⚠ Не удалось сесть", escapeHtml(msg.error || "Ошибка"), []),
  cleanup: () => {
    PK_VIEW = "lobby";
    PK_LOBBY_TAB = "tables";
    PK_TABLE_ID = null;
    PK_LAST_LOBBY = null;
    PK_CUR_BET = 0; PK_MIN = 0; PK_MAX = 0;
    PK_PRE = { check_fold: false, call_any: false };
    PK_MERGE_UNTIL_MS = 0;
    PK_MERGE_MODAL_SHOWN = false;
    PK_UNSEATED_DEADLINE_MS = 0;
  },
  roomExit: () => {
    if (GAME_TYPE === "poker" && PK_VIEW === "table") {
      PK_VIEW = "lobby";
      PK_TABLE_ID = null;
      wsSend({ type: "request_lobby" });
      els.roomExitBtn.classList.add("hidden");
      return true;
    }
    return false;
  },
});
