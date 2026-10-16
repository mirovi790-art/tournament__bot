/* ======================================================================
   wheel.js — колесо Фортуны: общее и 1×1
   ====================================================================== */

let WHEEL_ANGLE = 0;
let WHEEL_LOCK = false;
let WHEEL_NEXT = null;
let WHEEL_LAST_SPIN_SHOWN = 0;
let WHEEL_INITIAL_DONE = false;
let AUTO_SPIN = false;
let AUTO_TIMER = null;
let WCACHE = { key: "", halo: null, sectors: null, hub: null, size: 0 };

let W1V1_VIEW_STATE = "idle";
let W1V1_LAST_SHOWN = 0;
let W1V1_TIMER = null;
let W1V1_ANIM = null;
let MY_READY = false;

const SPIN_TURNS_MIN = 9;
const SPIN_TURNS_MAX = 13;
const SPIN_EASING = k => 1 - Math.pow(1 - k, 4);
function pickSpinTurns() {
  return SPIN_TURNS_MIN + Math.floor(Math.random() * (SPIN_TURNS_MAX - SPIN_TURNS_MIN + 1));
}
function stopAutoSpin() {
  AUTO_SPIN = false;
  if (AUTO_TIMER) { clearTimeout(AUTO_TIMER); AUTO_TIMER = null; }
}

/* ---------- кэш слоёв колеса ---------- */
function _buildWheelHalo(size) {
  const cv = document.createElement("canvas"); cv.width = size; cv.height = size;
  const ctx = cv.getContext("2d");
  const cx = size/2, cy = size/2, r = size/2 - 30;
  const grad = ctx.createRadialGradient(cx, cy, r*0.55, cx, cy, r+30);
  grad.addColorStop(0, "rgba(220,20,60,0)");
  grad.addColorStop(0.65, "rgba(255,140,0,0.22)");
  grad.addColorStop(1, "rgba(255,180,60,0)");
  ctx.fillStyle = grad; ctx.beginPath(); ctx.arc(cx, cy, r+30, 0, Math.PI*2); ctx.fill();
  return cv;
}

function _buildWheelSectors(players, size) {
  const cv = document.createElement("canvas"); cv.width = size; cv.height = size;
  const ctx = cv.getContext("2d");
  const cx = size/2, cy = size/2, r = size/2 - 30;
  const n = Math.max(players.length, 1);
  const palette = [
    {fill:["#ffb04d","#cc6600"]},{fill:["#e6192b","#7a0008"]},
    {fill:["#ffcc00","#a06a00"]},{fill:["#ff6b00","#8b3000"]},
    {fill:["#c41030","#5a0010"]},
  ];
  for (let i = 0; i < n; i++) {
    const a0 = (i / n) * Math.PI * 2 - Math.PI / 2;
    const a1 = ((i + 1) / n) * Math.PI * 2 - Math.PI / 2;
    const col = palette[i % palette.length];
    const grad = ctx.createRadialGradient(cx, cy, r * 0.15, cx, cy, r);
    grad.addColorStop(0, col.fill[0]);
    grad.addColorStop(1, col.fill[1]);
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, r, a0, a1); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();
    ctx.strokeStyle = "rgba(0,0,0,.4)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.lineTo(cx + r * Math.cos(a0), cy + r * Math.sin(a0));
    ctx.stroke();
    const mid = (a0 + a1) / 2;
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(mid);
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.shadowColor = "rgba(0,0,0,.9)"; ctx.shadowBlur = 8;
    ctx.shadowOffsetX = 2; ctx.shadowOffsetY = 2;
    ctx.fillStyle = "#ffffff"; ctx.font = "900 22px 'Segoe UI',sans-serif";
    const label = players[i] ? players[i].name : "";
    const txt = label.length > 18 ? label.slice(0, 18) + "…" : label;
    ctx.fillText(txt, r - 18, 0);
    ctx.restore();
    /* Зелёные точки удалены */
  }
  const innerR = r - 4;
  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, innerR, 0, Math.PI*2);
  const ringGrad = ctx.createLinearGradient(0, 0, size, size);
  ringGrad.addColorStop(0, "#ffcc00"); ringGrad.addColorStop(0.5, "#d4a017"); ringGrad.addColorStop(1, "#ffcc00");
  ctx.strokeStyle = ringGrad; ctx.lineWidth = 4;
  ctx.shadowColor = "#ffb040"; ctx.shadowBlur = 18; ctx.stroke();
  ctx.restore();
  return cv;
}

function _buildWheelOuterRing(size) {
  const cv = document.createElement("canvas"); cv.width = size; cv.height = size;
  const ctx = cv.getContext("2d");
  const cx = size/2, cy = size/2, r = size/2 - 20;
  ctx.save();
  const outerGrad = ctx.createLinearGradient(0, 0, size, size);
  outerGrad.addColorStop(0, "#ffe680"); outerGrad.addColorStop(0.3, "#c89020");
  outerGrad.addColorStop(0.5, "#ffcc00"); outerGrad.addColorStop(0.7, "#a07010");
  outerGrad.addColorStop(1, "#ffcc00");
  ctx.beginPath(); ctx.arc(cx, cy, r-4, 0, Math.PI*2);
  ctx.strokeStyle = outerGrad; ctx.lineWidth = 12;
  ctx.shadowColor = "rgba(255,180,60,.8)"; ctx.shadowBlur = 30; ctx.stroke();
  ctx.restore();
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, r-11, 0, Math.PI*2);
  ctx.strokeStyle = "rgba(20,15,5,.9)"; ctx.lineWidth = 2; ctx.stroke();
  ctx.restore();
  for (let i = 0; i < 24; i++) {
    const a = (i / 24) * Math.PI * 2;
    const x = cx + (r-4) * Math.cos(a);
    const y = cy + (r-4) * Math.sin(a);
    ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI*2);
    ctx.fillStyle = "rgba(60,40,10,.8)"; ctx.fill();
  }
  return cv;
}

function _buildWheelHub(size) {
  const cv = document.createElement("canvas"); cv.width = size; cv.height = size;
  const ctx = cv.getContext("2d");
  const cx = size/2, cy = size/2;
  const hubR = 62;
  ctx.save();
  const glowGrad = ctx.createRadialGradient(cx, cy, hubR*0.6, cx, cy, hubR+20);
  glowGrad.addColorStop(0, "rgba(255,180,60,.6)");
  glowGrad.addColorStop(1, "rgba(255,140,0,0)");
  ctx.beginPath(); ctx.arc(cx, cy, hubR+20, 0, Math.PI*2);
  ctx.fillStyle = glowGrad; ctx.fill();
  ctx.restore();
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, hubR, 0, Math.PI*2);
  const ringGrad = ctx.createRadialGradient(cx, cy, hubR*0.7, cx, cy, hubR);
  ringGrad.addColorStop(0, "#ffe680"); ringGrad.addColorStop(0.5, "#d4a017"); ringGrad.addColorStop(1, "#8b6508");
  ctx.fillStyle = ringGrad; ctx.fill();
  ctx.restore();
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, hubR-6, 0, Math.PI*2);
  const hubGrad = ctx.createRadialGradient(cx, cy, hubR*0.1, cx, cy, hubR-6);
  hubGrad.addColorStop(0, "#2a1f10"); hubGrad.addColorStop(1, "#0a0a0a");
  ctx.fillStyle = hubGrad;
  ctx.shadowColor = "rgba(255,180,60,.6)"; ctx.shadowBlur = 20; ctx.fill();
  ctx.restore();
  ctx.save(); ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.font = "44px sans-serif";
  ctx.shadowColor = "#ffcc00"; ctx.shadowBlur = 18;
  ctx.fillText("🎡", cx, cy);
  ctx.restore();
  return cv;
}

function _getWheelCache(players, key, size) {
  if (WCACHE.key === key && WCACHE.size === size &&
      WCACHE.sectors && WCACHE.halo && WCACHE.outer && WCACHE.hub) return WCACHE;
  WCACHE = {
    key, size,
    halo: _buildWheelHalo(size),
    sectors: _buildWheelSectors(players, size),
    outer: _buildWheelOuterRing(size),
    hub: _buildWheelHub(size),
  };
  return WCACHE;
}

function _drawBracketWheelFrame(canvas, players, match_id) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  const W = canvas.width, H = canvas.height, cx = W/2, cy = H/2;
  const key = match_id + "|" + players.map(p => (p && p.user_id) || "_").join(",");
  const cache = _getWheelCache(players, key, W);
  ctx.clearRect(0, 0, W, H);
  ctx.drawImage(cache.halo, 0, 0);
  ctx.save(); ctx.translate(cx, cy); ctx.rotate(WHEEL_ANGLE);
  ctx.drawImage(cache.sectors, -W/2, -H/2);
  ctx.restore();
  ctx.drawImage(cache.outer, 0, 0);
  ctx.drawImage(cache.hub, 0, 0);
}
function drawBracketWheelStatic(canvas, p1, p2, match_id) {
  if (!canvas) return;
  _drawBracketWheelFrame(canvas, [p1, p2].filter(Boolean), match_id || "static");
}

/* ---------- общее колесо ---------- */
function renderWheel(s) {
  LAST_STATE = s;
  els.roomExitBtn.classList.add("hidden");
  if (WHEEL_LOCK) { WHEEL_NEXT = s; return; }
  if (!WHEEL_INITIAL_DONE) {
    WHEEL_INITIAL_DONE = true;
    WHEEL_LAST_SPIN_SHOWN = s.spin_count;
    drawWheelUI(s);
    return;
  }
  if (s.spin_count > WHEEL_LAST_SPIN_SHOWN && s.last_spin) { startSpinAnimation(s); return; }
  drawWheelUI(s);
}

function updateWheelDOMOnly(s) {
  els.rmCounter.textContent = `На колесе: ${(s.active || []).length}`;
  const plist = document.querySelector(".p-list");
  if (!plist) return;
  const activeArr = s.active || [];
  const eliminatedArr = s.eliminated || [];
  let html = "";
  activeArr.forEach(p => {
    html += `<div class="p-row"><span><span class="dot"></span>${escapeHtml(p.name)}</span><span>в игре</span></div>`;
  });
  eliminatedArr.slice().sort((a, b) => b.place - a.place).forEach(p => {
    const cls = p.place === 1 ? "p-row win" : "p-row elim";
    html += `<div class="${cls}"><span>${p.place === 1 ? "🏆" : "❌"} ${escapeHtml(p.name)}</span><span>место ${p.place}</span></div>`;
  });
  plist.innerHTML = html;
}

function drawWheelUI(s) {
  const activeArr = s.active || [];
  const eliminatedArr = s.eliminated || [];
  els.rmCounter.textContent = `На колесе: ${activeArr.length}`;
  const canSpin = IS_ADMIN && !s.finished && activeArr.length >= 2;
  let btnHTML = "";
  if (IS_ADMIN) {
    btnHTML = `<button id="spin-btn" ${canSpin && !WHEEL_LOCK && !AUTO_SPIN ? "" : "disabled"}>${AUTO_SPIN ? "🔄 Крутится..." : "🎡 Крутить"}</button>`;
  } else {
    btnHTML = `<button disabled style="background:#1e1e1e;color:#555;">🎡 Крутит админ</button>`;
  }
  const wheelCaption = `<div class="wheel-caption"><span class="ic">🎡</span><span>${activeArr.length} на колесе</span></div>`;
  const wheelHtml = `<div class="wheel-arena">${wheelCaption}<div class="wheel-frame"><div class="wheel-frame-inner"><div class="pointer"></div><canvas id="wheel" width="640" height="640"></canvas></div></div></div>`;
  let participantsHtml = `<div class="participants"><div class="participants-frame"><h3>👥 Участники (${activeArr.length + eliminatedArr.length})</h3><div class="p-list">`;
  activeArr.forEach(p => {
    participantsHtml += `<div class="p-row"><span><span class="dot"></span>${escapeHtml(p.name)}</span><span>в игре</span></div>`;
  });
  eliminatedArr.slice().sort((a, b) => b.place - a.place).forEach(p => {
    const cls = p.place === 1 ? "p-row win" : "p-row elim";
    participantsHtml += `<div class="${cls}"><span>${p.place === 1 ? "🏆" : "❌"} ${escapeHtml(p.name)}</span><span>место ${p.place}</span></div>`;
  });
  participantsHtml += `</div></div></div>`;
  els.roomContent.innerHTML = `${wheelHtml}<div class="bottom-panel"><div class="bottom-frame"><div class="bottom-title">Управление колесом</div><div style="display:flex;justify-content:center;">${btnHTML}</div></div></div>${participantsHtml}`;
  const canvas = document.getElementById("wheel");
  drawWheel(canvas, activeArr);
  const spinBtn = document.getElementById("spin-btn");
  if (spinBtn) spinBtn.addEventListener("click", () => {
    if (!IS_ADMIN) { showModal("⛔ Нет доступа", "Только админ", []); return; }
    if (WHEEL_LOCK) return;
    if (!AUTO_SPIN) { AUTO_SPIN = true; wsSend({ type: "spin" }); }
  });
}

const NEON_PALETTE = [
  {fill:["#ffb04d","#cc6600"]},{fill:["#e6192b","#7a0008"]},
  {fill:["#ffcc00","#a06a00"]},{fill:["#ff6b00","#8b3000"]},
  {fill:["#c41030","#5a0010"]},{fill:["#ff8040","#a03000"]},
];

function drawWheel(canvas, active) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height, cx = W/2, cy = H/2, r = Math.min(W, H)/2 - 30;
  ctx.clearRect(0, 0, W, H);
  const haloGrad = ctx.createRadialGradient(cx, cy, r*0.55, cx, cy, r+30);
  haloGrad.addColorStop(0, "rgba(220,20,60,0)");
  haloGrad.addColorStop(0.65, "rgba(255,140,0,0.22)");
  haloGrad.addColorStop(1, "rgba(255,180,60,0)");
  ctx.fillStyle = haloGrad; ctx.beginPath(); ctx.arc(cx, cy, r+30, 0, Math.PI*2); ctx.fill();
  const n = Math.max(active.length, 1);
  if (active.length === 0) {
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2);
    const emptyGrad = ctx.createRadialGradient(cx, cy, r*0.3, cx, cy, r);
    emptyGrad.addColorStop(0, "#1a1a1a"); emptyGrad.addColorStop(1, "#0a0a0a");
    ctx.fillStyle = emptyGrad; ctx.fill();
    ctx.strokeStyle = "rgba(255,180,60,.4)"; ctx.lineWidth = 3;
    ctx.shadowColor = "#ff9500"; ctx.shadowBlur = 30; ctx.stroke(); ctx.shadowBlur = 0;
    ctx.fillStyle = "#666"; ctx.font = "bold 24px sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("Ожидание", cx, cy);
    drawHub(ctx, cx, cy);
    return;
  }
  for (let i = 0; i < n; i++) {
    const a0 = (i / n) * Math.PI * 2 - Math.PI / 2 + WHEEL_ANGLE;
    const a1 = ((i + 1) / n) * Math.PI * 2 - Math.PI / 2 + WHEEL_ANGLE;
    const col = NEON_PALETTE[i % NEON_PALETTE.length];
    const grad = ctx.createRadialGradient(cx, cy, r * 0.15, cx, cy, r);
    grad.addColorStop(0, col.fill[0]);
    grad.addColorStop(1, col.fill[1]);
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, r, a0, a1); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();
    ctx.save();
    ctx.strokeStyle = "rgba(0,0,0,.4)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.lineTo(cx + r * Math.cos(a0), cy + r * Math.sin(a0));
    ctx.stroke();
    ctx.restore();
    const mid = (a0 + a1) / 2;
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(mid);
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.shadowColor = "rgba(0,0,0,.9)"; ctx.shadowBlur = 8;
    ctx.shadowOffsetX = 2; ctx.shadowOffsetY = 2;
    ctx.fillStyle = "#ffffff"; ctx.font = "900 20px 'Segoe UI',sans-serif";
    const label = active[i].name || "";
    const txt = label.length > 14 ? label.slice(0, 14) + "…" : label;
    ctx.fillText(txt, r - 18, 0);
    ctx.restore();
  }
  ctx.save();
  const outerGrad = ctx.createLinearGradient(0, 0, W, H);
  outerGrad.addColorStop(0, "#ffe680"); outerGrad.addColorStop(0.5, "#ffcc00"); outerGrad.addColorStop(1, "#a07010");
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2);
  ctx.strokeStyle = outerGrad; ctx.lineWidth = 10;
  ctx.shadowColor = "rgba(255,180,60,.8)"; ctx.shadowBlur = 30; ctx.stroke();
  ctx.restore();
  drawHub(ctx, cx, cy);
}

function drawHub(ctx, cx, cy) {
  const hubR = 62;
  ctx.save();
  const glowGrad = ctx.createRadialGradient(cx, cy, hubR*0.6, cx, cy, hubR+20);
  glowGrad.addColorStop(0, "rgba(255,180,60,.6)");
  glowGrad.addColorStop(1, "rgba(255,140,0,0)");
  ctx.beginPath(); ctx.arc(cx, cy, hubR+20, 0, Math.PI*2);
  ctx.fillStyle = glowGrad; ctx.fill();
  ctx.restore();
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, hubR, 0, Math.PI*2);
  const ringGrad = ctx.createRadialGradient(cx, cy, hubR*0.7, cx, cy, hubR);
  ringGrad.addColorStop(0, "#ffe680"); ringGrad.addColorStop(0.5, "#d4a017"); ringGrad.addColorStop(1, "#8b6508");
  ctx.fillStyle = ringGrad; ctx.fill();
  ctx.restore();
  ctx.save();
  ctx.beginPath(); ctx.arc(cx, cy, hubR-6, 0, Math.PI*2);
  const hubGrad = ctx.createRadialGradient(cx, cy, hubR*0.1, cx, cy, hubR-6);
  hubGrad.addColorStop(0, "#2a1f10"); hubGrad.addColorStop(1, "#0a0a0a");
  ctx.fillStyle = hubGrad;
  ctx.shadowColor = "rgba(255,180,60,.6)"; ctx.shadowBlur = 20; ctx.fill();
  ctx.restore();
  ctx.save(); ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.font = "44px sans-serif";
  ctx.shadowColor = "#ffcc00"; ctx.shadowBlur = 18;
  ctx.fillText("🎡", cx, cy);
  ctx.restore();
}

function startSpinAnimation(s) {
  WHEEL_LOCK = true;
  WHEEL_LAST_SPIN_SHOWN = s.spin_count;
  let activeWithElim;
  if (s.last_spin.before_active) activeWithElim = s.last_spin.before_active.map(p => ({...p}));
  else {
    activeWithElim = s.active.slice();
    activeWithElim.splice(s.last_spin.sector_index, 0, s.last_spin.eliminated);
  }
  const canvas = document.getElementById("wheel");
  if (!canvas) { WHEEL_LOCK = false; return; }
  const n = activeWithElim.length;
  const idx = s.last_spin.sector_index;
  const offsetRatio = s.last_spin.final_offset_ratio || 0;
  const duration = s.last_spin.duration || 8000;
  const serverStart = s.last_spin.started_at || nowServer();
  const turns = pickSpinTurns();
  const span = Math.PI*2 / n, center = -(idx + 0.5) * span;
  const turnsRad = turns * Math.PI * 2;
  const target = center + offsetRatio * span + turnsRad;
  const startAngle = target - turnsRad;
  function setPhase(force) {
    const el = nowServer() - serverStart;
    const k = Math.min(1, Math.max(0, el / duration));
    WHEEL_ANGLE = startAngle + (target - startAngle) * SPIN_EASING(k);
    if (force) {
      const c = document.getElementById("wheel");
      if (c) drawWheel(c, activeWithElim);
    }
    return k;
  }
  let k = setPhase(true);
  if (k >= 1) {
    WHEEL_ANGLE = target;
    const c = document.getElementById("wheel");
    if (c) drawWheel(c, activeWithElim);
    setTimeout(() => {
      hide(els.modal);
      WHEEL_LOCK = false;
      const finalState = WHEEL_NEXT || s;
      WHEEL_NEXT = null;
      updateWheelDOMOnly(finalState);
      if (PENDING_ENDED) { const e = PENDING_ENDED; PENDING_ENDED = null; setTimeout(() => _showEndedModal(e), 500); return; }
      if (finalState.finished) { stopAutoSpin(); return; }
      if (AUTO_SPIN && (finalState.active || []).length >= 2 && ACTIVE_TID && WS && WS.readyState === 1) {
        AUTO_TIMER = setTimeout(() => { if (AUTO_SPIN && WS.readyState === 1) wsSend({ type: "spin" }); }, 7000);
      } else stopAutoSpin();
    }, 100);
    return;
  }
  function frame() {
    const kk = setPhase(false);
    const c = document.getElementById("wheel");
    if (c) drawWheel(c, activeWithElim);
    if (kk < 1) requestAnimationFrame(frame);
    else {
      WHEEL_ANGLE = target;
      const c2 = document.getElementById("wheel");
      if (c2) drawWheel(c2, activeWithElim);
      showModal("❌ Выбывает", `<b style="color:#ffcc00;">${escapeHtml(s.last_spin.eliminated.name)}</b><br>покидает турнир!`, []);
      setTimeout(() => {
        hide(els.modal);
        WHEEL_LOCK = false;
        const finalState = WHEEL_NEXT || s;
        WHEEL_NEXT = null;
        updateWheelDOMOnly(finalState);
        if (PENDING_ENDED) { const e = PENDING_ENDED; PENDING_ENDED = null; setTimeout(() => _showEndedModal(e), 500); return; }
        if (finalState.finished) { stopAutoSpin(); return; }
        if (AUTO_SPIN && (finalState.active || []).length >= 2 && ACTIVE_TID && WS && WS.readyState === 1) {
          AUTO_TIMER = setTimeout(() => { if (AUTO_SPIN && WS.readyState === 1) wsSend({ type: "spin" }); }, 7000);
        } else stopAutoSpin();
      }, 2500);
    }
  }
  requestAnimationFrame(frame);
}

/* ---------- колесо 1×1 ---------- */
function renderWheel1v1(s) {
  LAST_STATE = s;
  els.roomExitBtn.classList.add("hidden");
  if (s.waiting) {
    els.rmCounter.textContent = `Ожидание…`;
    els.roomContent.innerHTML = `<div class="waiting-box"><div class="waiting-ico">⚔️</div><div class="waiting-txt">Ожидание принятия…</div></div>`;
    return;
  }
  if (W1V1_VIEW_STATE === "spinning") return;
  if (W1V1_VIEW_STATE === "showing_result" || W1V1_VIEW_STATE === "final_result") return;
  const lastShown = W1V1_LAST_SHOWN || 0;
  const hasNewSpin = s.last_spin && s.spin_count > lastShown;
  if (hasNewSpin) {
    W1V1_LAST_SHOWN = s.spin_count;
    W1V1_VIEW_STATE = "spinning";
    startWheel1v1Animation(s, () => {
      setTimeout(() => {
        if (W1V1_VIEW_STATE !== "spinning") return;
        const st = LAST_STATE;
        if (!st) return;
        if (st.finished) {
          W1V1_VIEW_STATE = "final_result";
          drawWheel1v1FinalResult(st);
          if (W1V1_TIMER) clearTimeout(W1V1_TIMER);
          W1V1_TIMER = setTimeout(() => {
            W1V1_VIEW_STATE = "idle";
            if (W1V1_TIMER) { clearTimeout(W1V1_TIMER); W1V1_TIMER = null; }
            exitRoom();
          }, 8000);
        } else {
          W1V1_VIEW_STATE = "showing_result";
          drawWheel1v1RoundResult(st);
          if (W1V1_TIMER) clearTimeout(W1V1_TIMER);
          W1V1_TIMER = setTimeout(() => {
            W1V1_VIEW_STATE = "idle";
            if (W1V1_TIMER) { clearTimeout(W1V1_TIMER); W1V1_TIMER = null; }
            if (LAST_STATE) renderWheel1v1(LAST_STATE);
          }, 3500);
        }
      }, 1000);
    });
    return;
  }
  if (s.finished) {
    W1V1_VIEW_STATE = "final_result";
    drawWheel1v1FinalResult(s);
    return;
  }
  drawWheel1v1UI(s);
}

function _w1v1ScoreHtml(s) {
  const players = s.players || [];
  const p1 = players[0] || {user_id:null, name:"—"};
  const p2 = players[1] || {user_id:null, name:"—"};
  const p1w = s.p1_wins || 0, p2w = s.p2_wins || 0;
  const target = s.series_target || 1;
  const readyIds = new Set(s.ready || []);
  const p1Ready = readyIds.has(p1.user_id);
  const p2Ready = readyIds.has(p2.user_id);
  const iAmP1 = p1.user_id === MY_ID;
  const iAmP2 = p2.user_id === MY_ID;
  return `
    <div class="score-panel"><div class="score-frame">
      <div class="score-player">
        <div class="ava">${iAmP1 ? "🙋" : "👤"}</div>
        <div class="nm">${escapeHtml(p1.name)}</div>
        <div class="score-num">${p1w}</div>
        <div class="tag ${p1Ready ? "ready" : "notready"}">${p1Ready ? "✓ готов" : "не готов"}</div>
      </div>
      <div class="score-vs">
        <div class="vs-label">VS</div>
        <div class="target">до ${target} побед</div>
      </div>
      <div class="score-player">
        <div class="ava">${iAmP2 ? "🙋" : "👤"}</div>
        <div class="nm">${escapeHtml(p2.name)}</div>
        <div class="score-num">${p2w}</div>
        <div class="tag ${p2Ready ? "ready" : "notready"}">${p2Ready ? "✓ готов" : "не готов"}</div>
      </div>
    </div></div>`;
}

function drawWheel1v1UI(s) {
  const players = s.players || [];
  const readyIds = new Set(s.ready || []);
  const iAmReady = MY_ID !== null && readyIds.has(MY_ID);
  const iAmPlayer = players.some(p => p.user_id === MY_ID);
  const target = s.series_target || 1;
  els.rmCounter.textContent = `Колесо 1×1 · до ${target}`;
  const scoreHtml = _w1v1ScoreHtml(s);
  const wheelCaption = `<div class="wheel-caption"><span class="ic">🎡</span><span>2 на колесе</span></div>`;
  const wheelHtml = `
    <div class="wheel-arena">${wheelCaption}
      <div class="wheel-frame"><div class="wheel-frame-inner">
        <div class="pointer"></div>
        <canvas id="wheel" width="640" height="640"></canvas>
      </div></div>
    </div>`;
  let bottomHtml = "";
  if (iAmPlayer && !s.finished) {
    bottomHtml += `<div class="bottom-title">Управление колесом</div>`;
    if (iAmReady) {
      bottomHtml += `<div class="ready-btn"><button class="pressed" disabled>✅ ВЫ ГОТОВЫ</button></div>`;
      bottomHtml += `<div class="bottom-info muted">Ждём серию…</div>`;
    } else {
      bottomHtml += `<div class="ready-btn"><button id="ready-btn">🤝 ГОТОВ</button></div>`;
      bottomHtml += `<div class="bottom-info muted">Оба должны нажать «Готов»</div>`;
    }
  } else if (!iAmPlayer) {
    bottomHtml += `<div class="bottom-title">Наблюдение</div>`;
    bottomHtml += `<div class="bottom-info">👁 Вы наблюдаете</div>`;
  }
  let participantsHtml = `<div class="participants"><div class="participants-frame"><h3>👥 Участники (${players.length})</h3><div class="p-list">`;
  players.forEach(p => {
    const r = readyIds.has(p.user_id);
    participantsHtml += `<div class="p-row ${r ? "" : "elim"}" style="${r ? "" : "opacity:1;text-decoration:none;"}">
      <span><span class="dot"></span>${escapeHtml(p.name)}</span>
      <span>${r ? "готов" : "в игре"}</span>
    </div>`;
  });
  participantsHtml += `</div></div></div>`;
  els.roomContent.innerHTML = `${scoreHtml}${wheelHtml}<div class="bottom-panel"><div class="bottom-frame">${bottomHtml}</div></div>${participantsHtml}`;
  const canvas = document.getElementById("wheel");
  _drawBracketWheelFrame(canvas, players, "1v1_" + (s.tid || ""));
  const readyBtn = document.getElementById("ready-btn");
  if (readyBtn) readyBtn.addEventListener("click", () => {
    if (MY_READY) return;
    MY_READY = true;
    wsSend({ type: "ready" });
    readyBtn.disabled = true;
    readyBtn.textContent = "✅ ОТПРАВЛЕНО";
    readyBtn.classList.add("pressed");
  });
}

function drawWheel1v1RoundResult(s) {
  const winName = s.last_spin && s.last_spin.winner_name ? s.last_spin.winner_name : "—";
  const p1w = s.p1_wins || 0, p2w = s.p2_wins || 0;
  const target = s.series_target || 1;
  const roundsLeft = `До победы: ${Math.max(0, target - Math.max(p1w, p2w))}`;
  els.roomContent.innerHTML = `
    ${_w1v1ScoreHtml(s)}
    <div class="bottom-panel"><div class="bottom-frame">
      <div class="bottom-title">Раунд завершён</div>
      <div style="text-align:center;padding:6px 0;">
        <div style="font-size:44px;">🎯</div>
        <div style="color:#fff;font-size:15px;font-weight:900;margin-top:6px;">Раунд выиграл: <b style="color:#ffcc00;">${escapeHtml(winName)}</b></div>
        <div style="color:#e5e5e5;font-size:14px;margin-top:6px;">Счёт: <b style="color:#ffcc00;">${p1w}:${p2w}</b></div>
        <div style="color:#9a9a9a;font-size:12px;margin-top:4px;">${roundsLeft}</div>
        <div style="color:#ffcc00;font-size:12px;margin-top:10px;font-weight:900;">Следующий спин…</div>
      </div>
    </div></div>`;
}

function drawWheel1v1FinalResult(s) {
  const winner = s.winner;
  const iWon = winner && winner.user_id === MY_ID;
  const p1w = s.p1_wins || 0, p2w = s.p2_wins || 0;
  let icon = "🏆", title = "";
  if (iWon) title = "ВЫ ПОБЕДИЛИ!";
  else title = `Победитель: ${escapeHtml(winner?.name || "—")}`;
  els.roomContent.innerHTML = `
    ${_w1v1ScoreHtml(s)}
    <div class="bottom-panel"><div class="bottom-frame">
      <div class="bottom-title">Серия завершена</div>
      <div style="text-align:center;padding:6px 0;">
        <div style="font-size:48px;">${icon}</div>
        <div style="color:#fff;font-size:16px;font-weight:900;margin-top:8px;">${title}</div>
        <div style="color:#e5e5e5;font-size:14px;margin-top:6px;">Финальный счёт: <b style="color:#ffcc00;">${p1w}:${p2w}</b></div>
        <div style="color:#ffcc00;font-size:12px;margin-top:10px;font-weight:900;">Возврат в меню через <span id="w1v1-exit-cd">8</span>…</div>
      </div>
    </div></div>`;
  if (iWon) fireConfetti();
  let sec = 8;
  const tick = setInterval(() => {
    sec--;
    const el = document.getElementById("w1v1-exit-cd");
    if (el) el.textContent = Math.max(0, sec);
    if (sec <= 0) clearInterval(tick);
  }, 1000);
}

function startWheel1v1Animation(s, onDone) {
  WHEEL_LOCK = true;
  const players = s.players || [];
  els.roomContent.innerHTML = `
    ${_w1v1ScoreHtml(s)}
    <div class="wheel-arena"><div class="wheel-frame"><div class="wheel-frame-inner">
      <div class="pointer"></div>
      <canvas id="wheel" width="640" height="640"></canvas>
    </div></div></div>`;
  const canvas = document.getElementById("wheel");
  if (!canvas) { WHEEL_LOCK = false; onDone && onDone(); return; }
  if (W1V1_ANIM) { try { cancelAnimationFrame(W1V1_ANIM); } catch (e) {} W1V1_ANIM = null; }
  const n = players.length;
  const winnerIdx = s.last_spin.winner_index;
  const offsetRatio = s.last_spin.final_offset_ratio || 0;
  const duration = s.last_spin.duration || 8000;
  const serverStart = s.last_spin.started_at || nowServer();
  const turns = pickSpinTurns();
  const span = Math.PI*2 / n, center = -(winnerIdx + 0.5) * span;
  const turnsRad = turns * Math.PI * 2;
  const target = center + offsetRatio * span + turnsRad;
  const startAngle = target - turnsRad;
  const matchKey = "1v1spin_" + (s.tid || "");
  function setPhase() {
    const el = nowServer() - serverStart;
    const k = Math.min(1, Math.max(0, el / duration));
    WHEEL_ANGLE = startAngle + (target - startAngle) * SPIN_EASING(k);
    return k;
  }
  setPhase();
  _drawBracketWheelFrame(canvas, players, matchKey);
  function frame() {
    const k = setPhase();
    _drawBracketWheelFrame(canvas, players, matchKey);
    if (k < 1) W1V1_ANIM = requestAnimationFrame(frame);
    else {
      WHEEL_ANGLE = target;
      _drawBracketWheelFrame(canvas, players, matchKey);
      WHEEL_LOCK = false;
      W1V1_ANIM = null;
      if (PENDING_ENDED) { const e = PENDING_ENDED; PENDING_ENDED = null; setTimeout(() => _showEndedModal(e), 600); }
      if (onDone) onDone();
    }
  }
  W1V1_ANIM = requestAnimationFrame(frame);
}

/* ---------- хуки ---------- */
registerHooks({
  wheel: renderWheel,
  wheel1v1: renderWheel1v1,
  cleanup: () => {
    stopAutoSpin();
    if (W1V1_TIMER) { clearTimeout(W1V1_TIMER); W1V1_TIMER = null; }
    if (W1V1_ANIM) { try { cancelAnimationFrame(W1V1_ANIM); } catch (e) {} W1V1_ANIM = null; }
    WHEEL_ANGLE = 0;
    WHEEL_LOCK = false;
    WHEEL_NEXT = null;
    WHEEL_LAST_SPIN_SHOWN = 0;
    WHEEL_INITIAL_DONE = false;
    WCACHE = { key: "", halo: null, sectors: null, hub: null, size: 0 };
    W1V1_VIEW_STATE = "idle";
    W1V1_LAST_SHOWN = 0;
    MY_READY = false;
  },
});
