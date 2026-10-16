/* ======================================================================
   core.js — общие утилиты, WS-подключение, авторизация, роутинг
   ====================================================================== */

const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand?.();
const INIT_DATA = tg?.initData || "";

const els = {
  title: document.getElementById("title"),
  titleText: document.getElementById("title-text"),
  status: document.getElementById("status"),
  tabs: document.getElementById("tabs"),
  roomTabs: document.getElementById("room-tabs"),
  profileView: document.getElementById("profile-view"),
  tournamentsView: document.getElementById("tournaments-view"),
  roomView: document.getElementById("room-view"),
  roomContent: document.getElementById("room-content"),
  bracketContent: document.getElementById("bracket-content"),
  trList: document.getElementById("tr-list"),
  pfName: document.getElementById("pf-name"),
  pfUname: document.getElementById("pf-uname"),
  pfRole: document.getElementById("pf-role"),
  pfWins: document.getElementById("pf-wins"),
  rmBadge1: document.getElementById("rm-badge-1"),
  rmCounter: document.getElementById("rm-counter"),
  rmTitleExtra: document.getElementById("rm-title-extra"),
  leaveBtn: document.getElementById("leave-btn"),
  roomExitBtn: document.getElementById("room-exit-btn"),
  adminToolbar: document.getElementById("admin-toolbar"),
  adKick: document.getElementById("ad-kick"),
  adEnd: document.getElementById("ad-end"),
  modal: document.getElementById("modal"),
  modalTitle: document.getElementById("modal-title"),
  modalBody: document.getElementById("modal-body"),
  modalBtns: document.getElementById("modal-btns"),
  confetti: document.getElementById("confetti"),
};

/* ---------- общий стейт ---------- */
let MY_ID = null;
let IS_ADMIN = false;
let WS = null;
let ACTIVE_TID = null;
let GAME_TYPE = null;
let LAST_STATE = null;
let MY_WINS = 0;
let EXIT_TIMER = null;
let SERVER_OFFSET = 0;
let PENDING_ENDED = null;
let ACTIVE_ROOM_TAB = "bracket";

/* ---------- хуки (регистрируются в wheel.js / bracket.js / poker.js) ---------- */
const _hooks = {
  wheel: () => {},
  wheel1v1: () => {},
  bracket: () => {},
  poker: () => {},
  pokerLobby: () => {},
  pokerMergeWarning: () => {},
  pokerSitError: () => {},
  cleanup: () => {},
  roomExit: () => false,
};
function registerHooks(h) { Object.assign(_hooks, h); }

/* ---------- базовые хелперы ---------- */
function show(el) { if (el) el.classList.remove("hidden"); }
function hide(el) { if (el) el.classList.add("hidden"); }
function setStatus(t, c) { els.status.textContent = t; els.status.className = "status " + (c || ""); }
function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
  ));
}
function syncServerTime(s) { if (s && s.server_now) { SERVER_OFFSET = s.server_now - Date.now(); } }
function nowServer() { return Date.now() + SERVER_OFFSET; }
function wsSend(o) { if (WS && WS.readyState === 1) WS.send(JSON.stringify(o)); }
function clearExitTimer() { if (EXIT_TIMER) { clearTimeout(EXIT_TIMER); EXIT_TIMER = null; } }

/* ---------- Модалки ---------- */
function showModal(title, body, buttons) {
  els.modalTitle.textContent = title;
  els.modalBody.innerHTML = body;
  if (!buttons || !buttons.length) {
    els.modalBtns.innerHTML = '<button class="btn" id="mc">Закрыть</button>';
    document.getElementById("mc").onclick = () => hide(els.modal);
  } else {
    els.modalBtns.innerHTML = "";
    buttons.forEach(b => {
      const btn = document.createElement("button");
      btn.className = "btn";
      btn.style.background = b.color || "#1e1e1e";
      btn.textContent = b.text;
      btn.onclick = b.onClick;
      els.modalBtns.appendChild(btn);
    });
  }
  show(els.modal);
}

function showConfirm(title, body, onYes) {
  els.modalTitle.textContent = title;
  els.modalBody.innerHTML = body;
  els.modalBtns.innerHTML = `
    <button class="btn" style="background:linear-gradient(135deg,#8b0000,#dc2626);" id="cy">Да</button>
    <button class="btn" style="background:#1e1e1e;" id="cn">Отмена</button>`;
  document.getElementById("cy").onclick = () => { hide(els.modal); onYes(); };
  document.getElementById("cn").onclick = () => hide(els.modal);
  show(els.modal);
}

function fireConfetti() {
  const canvas = els.confetti;
  show(canvas);
  canvas.width = window.innerWidth;
  canvas.height = window.innerHeight;
  const ctx = canvas.getContext("2d");
  const colors = ["#ffcc00","#ffa500","#ff8800","#dc143c","#e91e63","#ffb040"];
  const pieces = Array.from({length:160}, () => ({
    x: Math.random() * canvas.width,
    y: -20 - Math.random() * 200,
    vy: 2 + Math.random() * 3,
    vx: -1 + Math.random() * 2,
    r: 4 + Math.random() * 5,
    color: colors[Math.floor(Math.random() * colors.length)],
    rot: Math.random() * Math.PI,
    vr: -0.1 + Math.random() * 0.2,
  }));
  let f = 0;
  function loop() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    pieces.forEach(p => {
      p.x += p.vx; p.y += p.vy; p.rot += p.vr;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.r, -p.r, p.r * 2, p.r * 0.6);
      ctx.restore();
    });
    f++;
    if (f < 400) requestAnimationFrame(loop);
    else hide(canvas);
  }
  loop();
}

/* ---------- Табы ---------- */
document.querySelectorAll("#tabs .tab").forEach(t => {
  t.addEventListener("click", () => {
    if (ACTIVE_TID) return;
    document.querySelectorAll("#tabs .tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    hide(els.profileView);
    hide(els.tournamentsView);
    if (t.dataset.tab === "profile") show(els.profileView);
    else { show(els.tournamentsView); loadTournaments(); }
  });
});

document.querySelectorAll("#room-tabs .tab").forEach(t => {
  t.addEventListener("click", () => {
    ACTIVE_ROOM_TAB = t.dataset.rtab;
    document.querySelectorAll("#room-tabs .tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    if (ACTIVE_ROOM_TAB === "bracket") {
      hide(els.roomContent);
      show(els.bracketContent);
      if (LAST_STATE && LAST_STATE.type === "bracket_state") drawBracketUI(LAST_STATE);
    } else {
      hide(els.bracketContent);
      show(els.roomContent);
      if (LAST_STATE && LAST_STATE.type === "bracket_state") {
        if (typeof BRACKET_ACTIVE_MATCH_ID !== "undefined" && BRACKET_ACTIVE_MATCH_ID)
          renderBracketRoom(LAST_STATE, BRACKET_ACTIVE_MATCH_ID);
        else
          drawBracketRoomsList(LAST_STATE);
      }
    }
  });
});

/* ---------- Профиль ---------- */
async function loadProfile() {
  try {
    const r = await fetch(`/api/profile?initData=${encodeURIComponent(INIT_DATA)}`);
    if (!r.ok) throw new Error(await r.text());
    const p = await r.json();
    MY_ID = p.user_id;
    IS_ADMIN = p.is_admin;
    MY_WINS = p.wins || 0;
    els.pfName.textContent = p.first_name || "Игрок";
    els.pfUname.textContent = p.username ? "@" + p.username : "";
    els.pfWins.textContent = MY_WINS;
    els.pfRole.textContent = IS_ADMIN ? "🛠 Администратор" : "🎮 Игрок";
    els.pfRole.classList.toggle("player", !IS_ADMIN);
    els.rmBadge1.textContent = MY_WINS;
  } catch (e) { console.error(e); }
}

/* ---------- Список турниров ---------- */
async function loadTournaments() {
  els.trList.innerHTML = '<div class="empty">Загрузка…</div>';
  try {
    const r = await fetch(`/api/profile?initData=${encodeURIComponent(INIT_DATA)}`);
    const p = await r.json();
    const tours = p.tournaments || [];
    if (!tours.length) {
      els.trList.innerHTML = '<div class="empty">📭 Нет активных турниров</div>';
      return;
    }
    els.trList.innerHTML = "";
    tours.forEach(t => {
      const emoji = t.game_type === "poker" ? "🃏" :
        (t.game_type === "wheel_1v1" ? "⚔️" :
        (t.game_type === "bracket" ? "🏆" : "🎡"));
      const gameName = t.game_type === "poker" ? "Покер" :
        (t.game_type === "wheel_1v1" ? "Колесо 1×1" :
        (t.game_type === "bracket" ? "Турнир" : "Колесо Фортуны"));
      let canStart = t.status === "running" || t.status === "paused";
      let btnLabel = canStart ? "🎮 НАЧАТЬ" :
        (t.status === "registration" ? "⏳ Ожидание" : "🏁 Завершён");
      let disabled = !canStart;
      if (t.game_type === "wheel_1v1") {
        if (t.my_status === "pending") { btnLabel = "⏳ Ждём подтверждения"; disabled = true; }
        else if (t.status === "registration") { btnLabel = "⏳ Ждём второго"; disabled = true; }
      }
      if (t.game_type === "bracket") {
        if (t.my_status === "pending") { btnLabel = "⏳ Ждём подтверждения"; disabled = true; }
        else if (t.status === "registration") { btnLabel = "⏳ Ждём игроков"; disabled = true; }
        else if (t.my_status === "kicked" || t.my_status === "left") {
          btnLabel = "👁 СМОТРЕТЬ"; disabled = false; canStart = true;
        }
      }
      if (t.game_type === "poker") {
        if (t.my_status === "pending") { btnLabel = "⏳ Ждём подтверждения"; disabled = true; }
        else if (t.status === "registration") { btnLabel = "⏳ Ждём игроков"; disabled = true; }
      }
      let badge = "";
      if (t.my_status === "admin") badge = '<span style="color:#ffcc00;font-weight:900;font-size:11px;">👑 АДМИН</span>';
      else if (t.my_status === "creator") badge = '<span style="color:#ff8800;font-weight:900;font-size:11px;">🎨 СОЗДАТЕЛЬ</span>';
      else if (t.my_status === "pending") badge = '<span style="color:#ffa500;font-weight:900;font-size:11px;">📩 ПРИГЛАШЕНИЕ</span>';
      else if (t.my_status === "kicked") badge = '<span style="color:#ff6b6b;font-weight:900;font-size:11px;">🚫 ИСКЛЮЧЁН</span>';
      else if (t.my_status === "left") badge = '<span style="color:#ff6b6b;font-weight:900;font-size:11px;">🚪 ВЫШЕЛ</span>';
      const card = document.createElement("div");
      card.className = "tr-card";
      card.innerHTML = `
        <div class="hdr"><div class="name">${escapeHtml(t.name)}</div><div class="emoji">${emoji}</div></div>
        <div class="meta">
          <span>Игра: <b>${gameName}</b></span>
          <span>Статус: <b>${t.display_status || t.status}</b></span>
          ${badge}
        </div>
        ${t.prize ? `<div class="meta">🎁 Приз: <b>${escapeHtml(t.prize)}</b></div>` : ""}
        <button class="btn-start" ${disabled ? "disabled" : ""}>${btnLabel}</button>`;
      card.querySelector(".btn-start").addEventListener("click", () => { if (canStart) openRoom(t.tid); });
      els.trList.appendChild(card);
    });
  } catch (e) {
    els.trList.innerHTML = '<div class="empty">Ошибка: ' + escapeHtml(e.message) + '</div>';
  }
}

/* ---------- Открытие комнаты ---------- */
async function openRoom(tid) {
  try {
    const r = await fetch(`/api/tournament/${tid}?initData=${encodeURIComponent(INIT_DATA)}`);
    if (!r.ok) {
      const e = await r.json().catch(() => ({detail:"Ошибка"}));
      showModal("⚠ Ошибка", e.detail || "Не удалось войти", []);
      return;
    }
    const info = await r.json();
    GAME_TYPE = info.game_type;
    ACTIVE_TID = tid;
    IS_ADMIN = info.is_admin;
    PENDING_ENDED = null;
    ACTIVE_ROOM_TAB = (info.game_type === "bracket") ? "bracket" : "main";
    hide(els.tabs);
    hide(els.profileView);
    hide(els.tournamentsView);
    show(els.roomView);
    els.titleText.textContent = info.name;
    els.rmTitleExtra.textContent = info.name;
    els.roomExitBtn.classList.add("hidden");
    show(els.leaveBtn);
    if (IS_ADMIN) show(els.adminToolbar); else hide(els.adminToolbar);
    if (info.game_type === "bracket") {
      show(els.roomTabs);
      document.querySelectorAll("#room-tabs .tab").forEach(x => x.classList.remove("active"));
      document.querySelector('#room-tabs .tab[data-rtab="bracket"]').classList.add("active");
      hide(els.roomContent);
      show(els.bracketContent);
    } else {
      hide(els.roomTabs);
      show(els.roomContent);
      hide(els.bracketContent);
    }
    if (_hooks.cleanup) _hooks.cleanup();
    connectWS(tid);
  } catch (e) { showModal("⚠ Ошибка", e.message, []); }
}

/* ---------- Выход ---------- */
function exitRoom() {
  clearExitTimer();
  if (_hooks.cleanup) _hooks.cleanup();
  PENDING_ENDED = null;
  if (WS) try { WS.close(); } catch (e) {}
  WS = null;
  ACTIVE_TID = null;
  GAME_TYPE = null;
  LAST_STATE = null;
  ACTIVE_ROOM_TAB = "bracket";
  hide(els.roomView);
  hide(els.leaveBtn);
  hide(els.roomExitBtn);
  hide(els.adminToolbar);
  hide(els.roomTabs);
  hide(els.modal);
  hide(els.confetti);
  els.titleText.textContent = "Турнир";
  show(els.tabs);
  document.querySelectorAll("#tabs .tab").forEach(x => x.classList.remove("active"));
  document.querySelector('#tabs .tab[data-tab="tournaments"]').classList.add("active");
  show(els.tournamentsView);
  loadTournaments();
  loadProfile();
}

/* ---------- WS ---------- */
function connectWS(tid) {
  if (WS) try { WS.close(); } catch (e) {}
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/${tid}?initData=${encodeURIComponent(INIT_DATA)}`;
  WS = new WebSocket(url);
  WS.onopen = () => setStatus("Онлайн", "ok");
  WS.onclose = () => {
    if (!ACTIVE_TID) return;
    setStatus("Оффлайн", "err");
    setTimeout(() => { if (ACTIVE_TID) connectWS(ACTIVE_TID); }, 2000);
  };
  WS.onerror = () => setStatus("Ошибка", "err");
  WS.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { console.error("bad json", ev.data); return; }
    try {
      if (msg.type === "poker_state") { syncServerTime(msg); _hooks.poker(msg); }
      else if (msg.type === "poker_lobby") { syncServerTime(msg); _hooks.pokerLobby(msg); }
      else if (msg.type === "poker_merge_warning") { _hooks.pokerMergeWarning(msg); }
      else if (msg.type === "poker_sit_error") { _hooks.pokerSitError(msg); }
      else if (msg.type === "wheel_state") { syncServerTime(msg); _hooks.wheel(msg); }
      else if (msg.type === "wheel_1v1_state") { syncServerTime(msg); _hooks.wheel1v1(msg); }
      else if (msg.type === "bracket_state") { syncServerTime(msg); _hooks.bracket(msg); }
      else if (msg.type === "castle_1v1_ready" || msg.type === "wheel_1v1_ready") {}
      else if (msg.type === "not_ready") { showModal("⏳ Не готово","Оба игрока должны нажать «Готов».",[]); }
      else if (msg.type === "spin_denied") { showModal("⛔ Нет доступа","Крутить может только администратор.",[]); }
      else if (msg.type === "left_tournament") {
        showModal("🚪 Вы вышли","Возврат в меню...",[]);
        clearExitTimer();
        EXIT_TIMER = setTimeout(() => exitRoom(), 3000);
      }
      else if (msg.type === "player_kicked" && msg.user_id === MY_ID) {
        showModal("🚫 Исключение","Администратор удалил вас.",[]);
        if (GAME_TYPE !== "bracket" && GAME_TYPE !== "poker") {
          clearExitTimer();
          EXIT_TIMER = setTimeout(() => exitRoom(), 5000);
        }
      }
      else if (msg.type === "tournament_ended") { handleTournamentEnded(msg); }
      else if (msg.type === "tournament_cancelled") {
        showModal("🛑 Отменён","Турнир отменён.",[]);
        clearExitTimer();
        EXIT_TIMER = setTimeout(() => exitRoom(), 5000);
      }
      else if (msg.type === "final_table") {
        showModal("🎯 ФИНАЛЬНЫЙ СТОЛ","Все игроки собраны за одним столом!",[]);
        setTimeout(() => hide(els.modal), 3000);
      }
    } catch (e) { console.error("handler error", e); }
  };
}

/* ---------- Финализация турнира ---------- */
function handleTournamentEnded(msg) {
  if ((GAME_TYPE === "wheel" || GAME_TYPE === "wheel_1v1") && WHEEL_LOCK) {
    PENDING_ENDED = msg;
    return;
  }
  _showEndedModal(msg);
}

function _showEndedModal(msg) {
  const manual = !!msg.manual, winnerId = msg.winner_id, winnerName = msg.winner_name;
  let title, body, countdown;
  if (manual) {
    title = "🏁 Турнир завершён";
    body = "Администратор остановил турнир.<br>Победитель не определён.";
    countdown = 5;
  } else if (winnerId) {
    if (winnerId === MY_ID) {
      title = "🏆 ПОБЕДА!";
      body = `Вы победили!<br><b>${escapeHtml(winnerName || "")}</b>`;
    } else {
      title = "🏁 Турнир завершён";
      body = `🥇 Победитель: <b>${escapeHtml(winnerName || "—")}</b>`;
    }
    countdown = 15;
    fireConfetti();
  } else {
    title = "🏁 Турнир завершён";
    body = "Победитель не определён.";
    countdown = 5;
  }
  let remaining = countdown;
  els.modalTitle.textContent = title;
  els.modalBody.innerHTML = body + `<div class="countdown-text" id="cd">Возврат в меню через ${remaining}…</div>`;
  els.modalBtns.innerHTML = `<button class="btn" id="mc">Закрыть сейчас</button>`;
  document.getElementById("mc").onclick = () => { clearExitTimer(); exitRoom(); };
  show(els.modal);
  const tick = setInterval(() => {
    remaining--;
    const cdel = document.getElementById("cd");
    if (cdel) cdel.textContent = `Возврат в меню через ${remaining}…`;
    if (remaining <= 0) { clearInterval(tick); exitRoom(); }
  }, 1000);
  clearExitTimer();
  EXIT_TIMER = setTimeout(() => { clearInterval(tick); exitRoom(); }, countdown * 1000 + 200);
}

/* ---------- Кнопки в топбаре ---------- */
els.roomExitBtn.addEventListener("click", () => {
  // даём играм шанс обработать по-своему (покер → назад в лобби)
  if (_hooks.roomExit && _hooks.roomExit()) return;
  if (typeof closeBracketRoom === "function") closeBracketRoom();
});

els.leaveBtn.addEventListener("click", () => {
  if (GAME_TYPE === "poker") {
    showConfirm("🚪 Покинуть турнир?", "Вы выйдете из покерного турнира. Подтвердить?", () => {
      wsSend({ type: "leave_tournament" });
    });
    return;
  }
  showConfirm("🚪 Покинуть турнир?", "Вы выйдете досрочно. Подтвердить?", () => {
    wsSend({ type: "leave_tournament" });
  });
});

els.adEnd.addEventListener("click", () => {
  showConfirm("🏁 Закончить турнир?", "Турнир будет остановлен. Победитель НЕ засчитается.", () => {
    wsSend({ type: "admin_end_tournament" });
  });
});

/* ---------- Кик игрока (админ) ---------- */
function getKickablePlayers() {
  const s = LAST_STATE;
  if (!s) return [];
  let players = [];
  if (GAME_TYPE === "poker") players = (s.players || []).map(p => ({user_id: p.user_id, name: p.name}));
  else if (GAME_TYPE === "wheel_1v1") players = (s.players || []).map(p => ({user_id: p.user_id, name: p.name}));
  else if (GAME_TYPE === "wheel") players = [
    ...(s.active || []).map(p => ({user_id: p.user_id, name: p.name})),
    ...(s.eliminated || []).map(p => ({user_id: p.user_id, name: p.name})),
  ];
  else if (GAME_TYPE === "bracket") {
    const seen = new Set();
    for (const rnd of (s.rounds || [])) for (const m of rnd) {
      if (m.p1 && !seen.has(m.p1.user_id)) { seen.add(m.p1.user_id); players.push(m.p1); }
      if (m.p2 && !seen.has(m.p2.user_id)) { seen.add(m.p2.user_id); players.push(m.p2); }
    }
  }
  return players.filter(p => p.user_id !== MY_ID);
}

els.adKick.addEventListener("click", () => {
  const list = getKickablePlayers();
  if (!list.length) { showModal("🚫 Кик игрока","<div style='color:#666;'>Нет доступных.</div>",[]); return; }
  const rows = list.map(p =>
    `<div class="kick-row"><span class="kick-name">${escapeHtml(p.name || ("Игрок " + p.user_id))}</span>` +
    `<button class="kick-x" data-uid="${p.user_id}" data-name="${escapeHtml(p.name || "Игрок")}">❌</button></div>`
  ).join("");
  showModal("🚫 Удалить игрока", `<div class="kick-list">${rows}</div>`, []);
  document.querySelectorAll("#modal-body .kick-x").forEach(b => {
    b.addEventListener("click", () => {
      const uid = parseInt(b.dataset.uid, 10);
      const name = b.dataset.name || "Игрок";
      hide(els.modal);
      showConfirm("🚫 Удалить игрока?", `Игрок <b style="color:#ffcc00;">${escapeHtml(name)}</b> будет исключён.`, () => {
        wsSend({ type: "admin_kick_player", user_id: uid });
      });
    });
  });
});

/* ---------- Boot ---------- */
(async function boot() {
  if (!INIT_DATA) { setStatus("Нет данных", "err"); return; }
  await loadProfile();
  setStatus("Онлайн", "ok");
})();
