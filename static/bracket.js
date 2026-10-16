/* ======================================================================
   bracket.js — турнир с сеткой (плей-офф)
   ====================================================================== */

let BRACKET_ACTIVE_MATCH_ID = null;
let BRACKET_MY_MATCH_ID = null;
let BRACKET_MATCH_VIEW_STATE = {};
let BRACKET_MATCH_VIEW_TIMER = {};
let BRACKET_MATCH_VIEW_ANIM = {};
let BRACKET_MATCH_LAST_SPIN_SHOWN = {};
let BRACKET_MATCH_VIEW_STATE_TS = {};

function setBracketViewState(mid, val) {
  BRACKET_MATCH_VIEW_STATE[mid] = val;
  BRACKET_MATCH_VIEW_STATE_TS[mid] = Date.now();
}
function clearBracketViewState(mid) {
  BRACKET_MATCH_VIEW_STATE[mid] = null;
  BRACKET_MATCH_VIEW_STATE_TS[mid] = 0;
}
function cancelBracketAnim(mid) {
  if (BRACKET_MATCH_VIEW_ANIM[mid]) {
    try { cancelAnimationFrame(BRACKET_MATCH_VIEW_ANIM[mid]); } catch (e) {}
    BRACKET_MATCH_VIEW_ANIM[mid] = null;
  }
}
function ensureWheelCanvasInRoom() { return !!document.getElementById("wheel"); }

/* ---------- watchdog ---------- */
setInterval(() => {
  if (!ACTIVE_TID || GAME_TYPE !== "bracket") return;
  const mid = BRACKET_ACTIVE_MATCH_ID;
  if (!mid) return;
  const state = BRACKET_MATCH_VIEW_STATE[mid];
  const ts = BRACKET_MATCH_VIEW_STATE_TS[mid] || 0;
  const elapsed = Date.now() - ts;
  if (state === "spinning" && elapsed > 30000) {
    cancelBracketAnim(mid);
    clearBracketViewState(mid);
    if (LAST_STATE) renderBracketRoom(LAST_STATE, mid);
  } else if (state === "showing_result" && elapsed > 10000) {
    // ФИКС: не сбрасываем если матч завершён — иначе таймер возврата к сетке
    // перезапускается при перерисовке.
    const s = LAST_STATE;
    const m = s ? findMatch(s, mid) : null;
    if (m && m.finished) return;
    clearBracketViewState(mid);
    if (LAST_STATE) renderBracketRoom(LAST_STATE, mid);
  } else if (state === "spinning" && !BRACKET_MATCH_VIEW_ANIM[mid] && elapsed > 2000) {
    clearBracketViewState(mid);
    if (LAST_STATE) renderBracketRoom(LAST_STATE, mid);
  }
}, 1500);

/* ---------- опрос стейта ---------- */
setInterval(() => {
  if (!ACTIVE_TID || GAME_TYPE !== "bracket") return;
  const mid = BRACKET_ACTIVE_MATCH_ID;
  if (!mid) return;
  const state = BRACKET_MATCH_VIEW_STATE[mid];
  if (state === "spinning" || state === "showing_result") return;
  if (WS && WS.readyState === 1) { try { wsSend({ type: "request_state" }); } catch (e) {} }
}, 3000);

/* ---------- рендер ---------- */
function renderBracket(s) {
  LAST_STATE = s;
  if (BRACKET_ACTIVE_MATCH_ID) {
    if (ACTIVE_ROOM_TAB === "bracket") drawBracketUI(s);
    else renderBracketRoom(s, BRACKET_ACTIVE_MATCH_ID);
  } else {
    if (ACTIVE_ROOM_TAB === "bracket") drawBracketUI(s);
    else drawBracketRoomsList(s);
  }
}

function findMatch(s, match_id) {
  if (!s || !s.rounds) return null;
  for (const rnd of s.rounds) for (const m of rnd) if (m.match_id === match_id) return m;
  return null;
}

function findMyActiveMatch(s) {
  if (!s || !s.rounds) return null;
  for (const rnd of s.rounds) for (const m of rnd) {
    if (m.finished) continue;
    if (m.p1 && m.p1.user_id === MY_ID) return m;
    if (m.p2 && m.p2.user_id === MY_ID) return m;
  }
  return null;
}

function getRoundNames(total) {
  if (total === 1) return ["ФИНАЛ"];
  if (total === 2) return ["1/2","ФИНАЛ"];
  if (total === 3) return ["1/4","1/2","ФИНАЛ"];
  if (total === 4) return ["1/8","1/4","1/2","ФИНАЛ"];
  if (total === 5) return ["1/16","1/8","1/4","1/2","ФИНАЛ"];
  if (total === 6) return ["1/32","1/16","1/8","1/4","1/2","ФИНАЛ"];
  return Array.from({ length: total }, (_, i) => `Раунд ${i + 1}`);
}

function renderScorePanel(m) {
  if (!m) return "";
  const p1Name = m.p1?.name || "—";
  const p2Name = m.p2?.name || "—";
  const p1w = m.p1_wins || 0, p2w = m.p2_wins || 0;
  const target = m.series_target || 3;
  const roundIdx = m.round_num !== undefined ? m.round_num : 0;
  const roundNames = LAST_STATE ? getRoundNames(LAST_STATE.rounds.length) : [];
  const roundName = roundNames[roundIdx] || ("Раунд " + (roundIdx + 1));
  const iAmP1 = m.p1 && m.p1.user_id === MY_ID;
  const iAmP2 = m.p2 && m.p2.user_id === MY_ID;
  const p1RoomReady = (m.room_ready || []).includes(m.p1?.user_id);
  const p2RoomReady = (m.room_ready || []).includes(m.p2?.user_id);
  return `
    <div class="score-panel"><div class="score-frame">
      <div class="score-player">
        <div class="ava">${iAmP1 ? "🙋" : "👤"}</div>
        <div class="nm">${escapeHtml(p1Name)}</div>
        <div class="score-num">${p1w}</div>
        <div class="tag ${p1RoomReady ? "ready" : "notready"}">${p1RoomReady ? "✓ готов" : "не готов"}</div>
      </div>
      <div class="score-vs">
        <div class="score-round">${escapeHtml(roundName)}</div>
        <div class="vs-label">VS</div>
        <div class="target">до ${target}</div>
      </div>
      <div class="score-player">
        <div class="ava">${iAmP2 ? "🙋" : "👤"}</div>
        <div class="nm">${escapeHtml(p2Name)}</div>
        <div class="score-num">${p2w}</div>
        <div class="tag ${p2RoomReady ? "ready" : "notready"}">${p2RoomReady ? "✓ готов" : "не готов"}</div>
      </div>
    </div></div>`;
}

function drawBracketRoomsList(s) {
  els.rmCounter.textContent = `Комнаты · ${s.size} игроков`;
  els.roomExitBtn.classList.add("hidden");
  if (s.waiting || !s.rounds || !s.rounds.length) {
    els.roomContent.innerHTML = `<div class="waiting-box"><div class="waiting-ico">🎮</div><div class="waiting-txt">Ожидание участников…</div></div>`;
    return;
  }
  const roundNames = getRoundNames(s.rounds.length);
  const active = [];
  s.rounds.forEach(rnd => rnd.forEach(m => {
    if (m.finished) return;
    if (!m.p1 || !m.p2) return;
    const bothReady = (m.ready || []).length >= 2;
    if (!bothReady && !m.room_active) return;
    active.push(m);
  }));
  if (!active.length) {
    els.roomContent.innerHTML = `<div class="waiting-box"><div class="waiting-ico">🎮</div><div class="waiting-txt">Пока нет активных комнат</div><div style="color:var(--text-muted);font-size:13px;">Комната появится, когда оба игрока отметятся ✓ в сетке.</div></div>`;
    return;
  }
  let html = `<div class="rooms-list">`;
  active.forEach(m => {
    const rn = roundNames[m.round_num] || ("Раунд " + (m.round_num + 1));
    const isMine = (m.p1 && m.p1.user_id === MY_ID) || (m.p2 && m.p2.user_id === MY_ID);
    const roomReadyCount = (m.room_ready || []).length;
    let statusCls, statusTxt;
    if (roomReadyCount >= 2) { statusCls = "running"; statusTxt = "Идёт серия"; }
    else if (roomReadyCount === 1) { statusCls = "waiting"; statusTxt = "Ждём второго"; }
    else { statusCls = "idle"; statusTxt = "Ожидание"; }
    let btnLabel = isMine ? "🎮 Войти" : "👁 Наблюдать";
    html += `
      <div class="room-card">
        <div class="rc-hdr">
          <div class="rc-title">${escapeHtml(rn)} · матч ${m.pos_in_round + 1}</div>
          <div class="rc-status ${statusCls}">${statusTxt}</div>
        </div>
        <div class="rc-players">
          <div class="rc-player-row"><span class="pname">${escapeHtml(m.p1?.name || "—")}</span><span class="pscore">${m.p1_wins || 0}</span></div>
          <div class="rc-player-row"><span class="pname">${escapeHtml(m.p2?.name || "—")}</span><span class="pscore">${m.p2_wins || 0}</span></div>
        </div>
        <div class="rc-target">до ${m.series_target || 3} побед</div>
        <button class="bracket-enter-btn" data-enter-room="${m.match_id}">${btnLabel}</button>
      </div>`;
  });
  html += `</div>`;
  els.roomContent.innerHTML = html;
  els.roomContent.querySelectorAll("[data-enter-room]").forEach(b => {
    b.addEventListener("click", () => {
      const mid = parseInt(b.dataset.enterRoom, 10);
      if (!mid) return;
      openBracketRoom(mid);
    });
  });
}

function openBracketRoom(mid) {
  BRACKET_ACTIVE_MATCH_ID = mid;
  clearBracketViewState(mid);
  const mm = LAST_STATE ? findMatch(LAST_STATE, mid) : null;
  if (mm && mm.last_spin) {
    const now = nowServer();
    const spinEndsAt = (mm.last_spin.started_at || 0) + (mm.last_spin.duration || 20000);
    if (now < spinEndsAt) BRACKET_MATCH_LAST_SPIN_SHOWN[mid] = (mm.spin_count || 1) - 1;
    else BRACKET_MATCH_LAST_SPIN_SHOWN[mid] = mm.spin_count || 0;
  } else BRACKET_MATCH_LAST_SPIN_SHOWN[mid] = 0;
  wsSend({ type: "bracket_enter", match_id: mid });
  ACTIVE_ROOM_TAB = "main";
  document.querySelectorAll("#room-tabs .tab").forEach(x => x.classList.remove("active"));
  const mainTab = document.querySelector('#room-tabs .tab[data-rtab="main"]');
  if (mainTab) mainTab.classList.add("active");
  hide(els.bracketContent);
  show(els.roomContent);
  els.roomExitBtn.classList.remove("hidden");
  if (LAST_STATE) renderBracketRoom(LAST_STATE, mid);
}

function closeBracketRoom() {
  const oldMid = BRACKET_ACTIVE_MATCH_ID;
  if (oldMid && BRACKET_MATCH_VIEW_TIMER[oldMid]) {
    try { clearTimeout(BRACKET_MATCH_VIEW_TIMER[oldMid]); } catch (e) {}
    BRACKET_MATCH_VIEW_TIMER[oldMid] = null;
  }
  if (oldMid) cancelBracketAnim(oldMid);
  BRACKET_ACTIVE_MATCH_ID = null;
  BRACKET_MATCH_VIEW_STATE = {};
  BRACKET_MATCH_VIEW_STATE_TS = {};
  WCACHE = { key: "", halo: null, sectors: null, hub: null, size: 0 };
  els.roomExitBtn.classList.add("hidden");
  if (LAST_STATE) drawBracketRoomsList(LAST_STATE);
}

function drawBracketUI(s) {
  els.rmCounter.textContent = `Турнир · ${s.size} игроков`;
  els.roomExitBtn.classList.add("hidden");
  if (s.waiting || !s.rounds || !s.rounds.length) {
    els.bracketContent.innerHTML = `<div class="waiting-box"><div class="waiting-ico">🏆</div><div class="waiting-txt">Ожидание всех участников…</div></div>`;
    return;
  }
  const match = findMyActiveMatch(s);
  BRACKET_MY_MATCH_ID = match ? match.match_id : null;
  const roundNames = getRoundNames(s.rounds.length);
  let html = `<div class="bracket-wrap"><div class="bracket-columns">`;
  s.rounds.forEach((rnd, rIdx) => {
    html += `<div class="bracket-col"><div class="bracket-col-title">${roundNames[rIdx] || ("Раунд " + (rIdx + 1))}</div>`;
    rnd.forEach(m => {
      const isMyMatch = (m.p1 && m.p1.user_id === MY_ID) || (m.p2 && m.p2.user_id === MY_ID);
      const bothReady = (m.ready || []).length >= 2;
      const cls = "bracket-match" + (m.finished ? " finished" : (isMyMatch ? " active" : ""));
      html += `<div class="${cls}">`;
      html += renderBracketPlayer(m, m.p1, m.ready, m.finished, m.winner, isMyMatch);
      html += `<div class="bracket-vs">VS</div>`;
      html += renderBracketPlayer(m, m.p2, m.ready, m.finished, m.winner, isMyMatch);
      const canShowSpectate = !isMyMatch && m.p1 && m.p2;
      if (isMyMatch && !m.finished && m.p1 && m.p2) {
        const roomActive = m.room_active;
        let btnLabel = roomActive ? "🎮 Открыть" : "🎮 Войти";
        let dis = !bothReady && !roomActive;
        if (!bothReady && !roomActive) btnLabel = "⏳ Ждём";
        html += `<button class="bracket-enter-btn" data-enter="${m.match_id}" ${dis ? "disabled" : ""}>${btnLabel}</button>`;
      } else if (IS_ADMIN && !m.finished && m.p1 && m.p2) {
        const roomActive = m.room_active;
        let btnLabel = roomActive ? "🎮 Открыть" : "🎮 Войти";
        let dis = !bothReady && !roomActive;
        html += `<button class="bracket-enter-btn admin-view" data-enter="${m.match_id}" ${dis ? "disabled" : ""}>${btnLabel}</button>`;
      } else if (canShowSpectate) {
        let label = "👁 Смотреть";
        if (m.finished) label = "👁 Результат";
        html += `<button class="bracket-enter-btn spectate" data-enter="${m.match_id}">${label}</button>`;
      }
      html += `</div>`;
    });
    html += `</div>`;
  });
  html += `</div></div>`;
  if (s.finished && s.winner) {
    html += `<div class="waiting-box" style="padding:24px;"><div class="waiting-ico">🏆</div><div class="waiting-txt">Победитель: <b>${escapeHtml(s.winner.name)}</b></div></div>`;
  }
  els.bracketContent.innerHTML = html;
  els.bracketContent.querySelectorAll(".bracket-check").forEach(btn => {
    btn.addEventListener("click", () => {
      const mid = parseInt(btn.dataset.mid, 10);
      if (!mid || btn.disabled) return;
      wsSend({ type: "bracket_ready", match_id: mid });
    });
  });
  els.bracketContent.querySelectorAll(".bracket-enter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const mid = parseInt(btn.dataset.enter, 10);
      if (!mid || btn.disabled) return;
      openBracketRoom(mid);
    });
  });
}

function renderBracketPlayer(m, p, readyList, finished, winner, isMyMatch) {
  if (!p) return `<div class="bracket-player"><div class="bp-name" style="color:#555;">—</div></div>`;
  const isReady = (readyList || []).includes(p.user_id);
  const isWinner = winner && winner.user_id === p.user_id;
  const isLoser = finished && winner && winner.user_id !== p.user_id;
  const isMe = p.user_id === MY_ID;
  let cls = "bracket-player";
  if (finished && isWinner) cls += " winner";
  else if (finished && isLoser) cls += " loser";
  let checkCls = "bracket-check", checkText = "", disabled = "disabled";
  if (isReady) { checkCls += " checked"; checkText = "✓"; }
  if (isMe && !finished && m.p1 && m.p2 && !isReady) disabled = "";
  if (isMe && isReady) disabled = "disabled";
  if (isMe) checkCls += " mine"; else checkCls += " notmine";
  let scoreText = "";
  if (m.series_target && m.series_target > 1) {
    const myWins = (p.user_id === (m.p1 && m.p1.user_id)) ? (m.p1_wins || 0) : (m.p2_wins || 0);
    if (myWins > 0 || (m.p1_wins || 0) > 0 || (m.p2_wins || 0) > 0) {
      scoreText = `<span class="bracket-score">${myWins}</span>`;
    }
  }
  return `<div class="${cls}">
    <div class="bp-name">${escapeHtml(p.name)}${isMe ? ' <span style="color:#ffcc00;">(вы)</span>' : ""}</div>
    ${scoreText}
    <button class="${checkCls}" data-mid="${m.match_id}" data-uid="${p.user_id}" ${disabled}>${checkText}</button>
  </div>`;
}

function renderBracketRoom(s, match_id) {
  const m = findMatch(s, match_id);
  if (!m) {
    BRACKET_ACTIVE_MATCH_ID = null;
    clearBracketViewState(match_id);
    els.roomExitBtn.classList.add("hidden");
    if (ACTIVE_ROOM_TAB === "bracket") drawBracketUI(s);
    else drawBracketRoomsList(s);
    return;
  }
  let state = BRACKET_MATCH_VIEW_STATE[match_id];
  if (state === "spinning" && !BRACKET_MATCH_VIEW_ANIM[match_id]) {
    clearBracketViewState(match_id);
    state = null;
  }
  if (state === "spinning") return;
  if (state === "showing_result") return;
  const lastShown = BRACKET_MATCH_LAST_SPIN_SHOWN[match_id] || 0;
  const hasNewSpin = m.last_spin && m.spin_count > lastShown;
  if (hasNewSpin) {
    BRACKET_MATCH_LAST_SPIN_SHOWN[match_id] = m.spin_count;
    setBracketViewState(match_id, "spinning");
    clearBracketTimer(match_id);
    if (!ensureWheelCanvasInRoom()) drawBracketRoomWaiting(s, m);
    animateBracketSpin(s, m, () => {
      setTimeout(() => {
        if (BRACKET_ACTIVE_MATCH_ID !== match_id) return;
        showMatchResult(match_id);
      }, 1000);
    });
    return;
  }
  if (m.finished) { showMatchResult(match_id); return; }
  drawBracketRoomWaiting(s, m);
}

function clearBracketTimer(match_id) {
  if (BRACKET_MATCH_VIEW_TIMER[match_id]) {
    try { clearTimeout(BRACKET_MATCH_VIEW_TIMER[match_id]); } catch (e) {}
    BRACKET_MATCH_VIEW_TIMER[match_id] = null;
  }
}

function showMatchResult(match_id) {
  const s = LAST_STATE;
  if (!s) return;
  const m = findMatch(s, match_id);
  if (!m) {
    BRACKET_ACTIVE_MATCH_ID = null;
    clearBracketViewState(match_id);
    els.roomExitBtn.classList.add("hidden");
    drawBracketUI(s);
    return;
  }
  // ФИКС: защита от повторного вызова, когда уже показываем результат
  if (BRACKET_MATCH_VIEW_STATE[match_id] === "showing_result" && m.finished) return;
  setBracketViewState(match_id, "showing_result");
  clearBracketTimer(match_id);
  if (m.finished) {
    drawBracketRoomFinalResult(m);
    BRACKET_MATCH_VIEW_TIMER[match_id] = setTimeout(() => {
      if (BRACKET_ACTIVE_MATCH_ID !== match_id) return;
      clearBracketViewState(match_id);
      BRACKET_ACTIVE_MATCH_ID = null;
      WCACHE = { key: "", halo: null, sectors: null, hub: null, size: 0 };
      ACTIVE_ROOM_TAB = "bracket";
      document.querySelectorAll("#room-tabs .tab").forEach(x => x.classList.remove("active"));
      const bt = document.querySelector('#room-tabs .tab[data-rtab="bracket"]');
      if (bt) bt.classList.add("active");
      hide(els.roomContent);
      show(els.bracketContent);
      els.roomExitBtn.classList.add("hidden");
      if (LAST_STATE) drawBracketUI(LAST_STATE);
    }, 7000);
  } else {
    drawBracketRoomRoundResult(m);
    BRACKET_MATCH_VIEW_TIMER[match_id] = setTimeout(() => {
      if (BRACKET_ACTIVE_MATCH_ID !== match_id) return;
      clearBracketViewState(match_id);
      if (LAST_STATE) renderBracketRoom(LAST_STATE, match_id);
    }, 3500);
  }
}

function drawBracketRoomWaiting(s, m) {
  const p1 = m.p1, p2 = m.p2;
  const iAmPlayer = (p1 && p1.user_id === MY_ID) || (p2 && p2.user_id === MY_ID);
  const myRoomReady = m.room_ready && m.room_ready.includes(MY_ID);
  const p1RoomReady = (m.room_ready || []).includes(p1?.user_id);
  const p2RoomReady = (m.room_ready || []).includes(p2?.user_id);
  const bothReady = p1RoomReady && p2RoomReady;
  const roundIdx = m.round_num;
  const roundNames = getRoundNames(s.rounds.length);
  const roundName = roundNames[roundIdx] || ("Раунд " + (roundIdx + 1));
  els.rmCounter.textContent = `${roundName} · матч ${m.pos_in_round + 1}`;
  const scoreHtml = renderScorePanel(m);
  const wheelCaption = `<div class="wheel-caption"><span class="ic">🎡</span><span>${bothReady ? "Серия идёт" : (p1RoomReady && p2RoomReady ? "Крутится" : "2 на колесе")}</span></div>`;
  const wheelHtml = `
    <div class="wheel-arena">
      ${wheelCaption}
      <div class="wheel-frame"><div class="wheel-frame-inner">
        <div class="pointer"></div>
        <canvas id="wheel" width="640" height="640"></canvas>
      </div></div>
    </div>`;
  let bottomHtml = "";
  if (iAmPlayer) {
    bottomHtml += `<div class="bottom-title">Управление</div>`;
    if (!myRoomReady) {
      bottomHtml += `<div class="ready-btn"><button id="bracket-room-ready">🤝 ГОТОВ</button></div>`;
      bottomHtml += `<div class="bottom-info muted">${bothReady ? "Колесо крутится…" : "Оба игрока должны нажать «Готов»"}</div>`;
    } else {
      bottomHtml += `<div class="ready-btn"><button class="pressed" disabled>✅ ЖДЁМ СЕРИЮ</button></div>`;
      bottomHtml += `<div class="bottom-info">${bothReady ? "🎲 Серия идёт…" : "Ждём второго игрока"}</div>`;
    }
  } else {
    bottomHtml += `<div class="bottom-title">Наблюдение</div>`;
    bottomHtml += `<div class="bottom-info">👁 Вы наблюдаете</div>`;
  }
  els.roomContent.innerHTML = `
    ${scoreHtml}
    ${wheelHtml}
    <div class="bottom-panel"><div class="bottom-frame">${bottomHtml}</div></div>`;
  const canvas = document.getElementById("wheel");
  drawBracketWheelStatic(canvas, p1, p2, m.match_id);
  const rr = document.getElementById("bracket-room-ready");
  if (rr) rr.addEventListener("click", () => {
    wsSend({ type: "bracket_room_ready", match_id: m.match_id });
    rr.disabled = true;
    rr.textContent = "✅ ОТПРАВЛЕНО";
    rr.classList.add("pressed");
  });
}

function drawBracketRoomRoundResult(m) {
  const winName = m.last_spin && m.last_spin.winner_user_id === (m.p1 && m.p1.user_id)
    ? (m.p1 && m.p1.name) : (m.p2 && m.p2.name);
  const p1w = m.p1_wins || 0, p2w = m.p2_wins || 0;
  const target = m.series_target || 3;
  const roundsLeft = `До победы: ${Math.max(0, target - Math.max(p1w, p2w))}`;
  els.roomContent.innerHTML = `
    ${renderScorePanel(m)}
    <div class="bottom-panel"><div class="bottom-frame">
      <div class="bottom-title">Раунд завершён</div>
      <div style="text-align:center;padding:6px 0;">
        <div style="font-size:44px;filter:drop-shadow(0 0 12px rgba(255,180,60,.5));">🎯</div>
        <div style="color:#fff;font-size:15px;font-weight:900;margin-top:6px;">Раунд выиграл: <b style="color:#ffcc00;">${escapeHtml(winName || "—")}</b></div>
        <div style="color:#e5e5e5;font-size:14px;margin-top:6px;">Счёт: <b style="color:#ffcc00;">${p1w}:${p2w}</b></div>
        <div style="color:#9a9a9a;font-size:12px;margin-top:4px;">${roundsLeft}</div>
        <div style="color:#ffcc00;font-size:12px;margin-top:10px;font-weight:900;">Следующий спин через <span id="br-next-cd">3</span>…</div>
      </div>
    </div></div>`;
  let sec = 3;
  const tick = setInterval(() => {
    sec--;
    const el = document.getElementById("br-next-cd");
    if (el) el.textContent = Math.max(0, sec);
    if (sec <= 0) clearInterval(tick);
  }, 1000);
}

function drawBracketRoomFinalResult(m) {
  const winName = m.winner ? m.winner.name : "—";
  const iWon = m.winner && m.winner.user_id === MY_ID;
  const iLost = m.loser && m.loser.user_id === MY_ID;
  const p1w = m.p1_wins || 0, p2w = m.p2_wins || 0;
  let icon = "🏆", title = "";
  if (iWon) title = "Вы прошли дальше!";
  else if (iLost) title = "Вы выбыли из турнира";
  else title = `Победитель: ${escapeHtml(winName)}`;
  els.roomContent.innerHTML = `
    ${renderScorePanel(m)}
    <div class="bottom-panel"><div class="bottom-frame">
      <div class="bottom-title">Серия завершена</div>
      <div style="text-align:center;padding:6px 0;">
        <div style="font-size:44px;">${icon}</div>
        <div style="color:#fff;font-size:15px;font-weight:900;margin-top:6px;">${title}</div>
        <div style="color:#e5e5e5;font-size:14px;margin-top:6px;">Финальный счёт: <b style="color:#ffcc00;">${p1w}:${p2w}</b></div>
        <div style="color:#ffcc00;font-size:12px;margin-top:10px;font-weight:900;">Возврат к сетке через <span id="br-exit-cd">7</span>…</div>
      </div>
    </div></div>`;
  let sec = 7;
  const tick = setInterval(() => {
    sec--;
    const el = document.getElementById("br-exit-cd");
    if (el) el.textContent = Math.max(0, sec);
    if (sec <= 0) clearInterval(tick);
  }, 1000);
}

function animateBracketSpin(s, m, onDone) {
  const mid = m.match_id;
  const canvas = document.getElementById("wheel");
  if (!canvas) { if (onDone) setTimeout(onDone, 50); return; }
  cancelBracketAnim(mid);
  const players = [m.p1, m.p2].filter(Boolean);
  const n = players.length;
  const winnerIdx = m.last_spin.winner_index;
  const offsetRatio = m.last_spin.final_offset_ratio || 0;
  const duration = m.last_spin.duration || 20000;
  const serverStart = m.last_spin.started_at || nowServer();
  const turns = pickSpinTurns();
  const span = Math.PI*2 / n, center = -(winnerIdx + 0.5) * span;
  const turnsRad = turns * Math.PI * 2;
  const target = center + offsetRatio * span + turnsRad;
  const startAngle = target - turnsRad;
  const el = nowServer() - serverStart;
  const initialK = Math.min(1, Math.max(0, el / duration));
  WHEEL_ANGLE = startAngle + (target - startAngle) * SPIN_EASING(initialK);
  _drawBracketWheelFrame(canvas, players, mid);
  if (initialK >= 1) {
    WHEEL_ANGLE = target;
    _drawBracketWheelFrame(canvas, players, mid);
    if (onDone) onDone();
    return;
  }
  function frame() {
    if (BRACKET_ACTIVE_MATCH_ID !== mid) {
      BRACKET_MATCH_VIEW_ANIM[mid] = null;
      return;
    }
    const elapsed = nowServer() - serverStart;
    const k = Math.min(1, Math.max(0, elapsed / duration));
    WHEEL_ANGLE = startAngle + (target - startAngle) * SPIN_EASING(k);
    _drawBracketWheelFrame(canvas, players, mid);
    if (k < 1) {
      BRACKET_MATCH_VIEW_ANIM[mid] = requestAnimationFrame(frame);
    } else {
      WHEEL_ANGLE = target;
      _drawBracketWheelFrame(canvas, players, mid);
      BRACKET_MATCH_VIEW_ANIM[mid] = null;
      if (onDone) onDone();
    }
  }
  BRACKET_MATCH_VIEW_ANIM[mid] = requestAnimationFrame(frame);
}

registerHooks({
  bracket: renderBracket,
  cleanup: () => {
    for (const mid in BRACKET_MATCH_VIEW_TIMER) {
      try { clearTimeout(BRACKET_MATCH_VIEW_TIMER[mid]); } catch (e) {}
    }
    for (const mid in BRACKET_MATCH_VIEW_ANIM) cancelBracketAnim(mid);
    BRACKET_MATCH_VIEW_TIMER = {};
    BRACKET_MATCH_VIEW_ANIM = {};
    BRACKET_MATCH_VIEW_STATE = {};
    BRACKET_MATCH_LAST_SPIN_SHOWN = {};
    BRACKET_MATCH_VIEW_STATE_TS = {};
    BRACKET_ACTIVE_MATCH_ID = null;
    BRACKET_MY_MATCH_ID = null;
  },
});
