/* J.A.R.V.I.S. HUD front end: live events over SSE, an arc-reactor core on <canvas>,
 * diagnostics, transcript and event log. No dependencies. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const TAU = Math.PI * 2;
  const token = new URLSearchParams(location.search).get("t") || "";
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const LOOKS = {
    starting: { c: [120, 190, 215], label: "INITIALIZING", spin: 0.6 },
    idle: { c: [0, 229, 255], label: "STANDBY", spin: 1 },
    listening: { c: [80, 255, 225], label: "LISTENING", spin: 1.7 },
    thinking: { c: [255, 176, 32], label: "PROCESSING", spin: 3.4 },
    speaking: { c: [40, 200, 255], label: "RESPONDING", spin: 1.3 },
    muted: { c: [255, 59, 92], label: "AUDIO OFFLINE", spin: 0.3 },
    error: { c: [255, 45, 70], label: "SYSTEM FAULT", spin: 0.12 },
  };

  const S = {
    state: "idle", note: "starting", error: false, muted: false, mode: "wake",
    name: "Jarvis", user: "", hotkey: "f9", relink: false,
    level: 0, levelTarget: 0, packets: 0, tool: "", toolAt: -1e9, look: "",
  };

  // ------------------------------------------------------------------ state & look

  function lookKey() {
    if (S.error) return "error";
    if (S.note === "starting" || S.note === "loading") return "starting";
    if (S.muted) return "muted";
    if (S.state === "listening" || S.state === "follow_up" || S.state === "wake_detected") return "listening";
    if (S.state === "thinking") return "thinking";
    if (S.state === "speaking") return "speaking";
    return "idle";
  }

  function hintFor(key) {
    const k = (S.hotkey || "").toUpperCase();
    switch (key) {
      case "starting": return "Warming up speech recognition";
      case "muted": return "Microphone disengaged // Ctrl+M to re-engage";
      case "listening": return S.state === "follow_up" ? "Still listening // go ahead" : "Go ahead, I'm listening";
      case "thinking":
        return performance.now() - S.toolAt < 4000 && S.tool ? `Executing module // ${S.tool.replace(/_/g, " ")}` : "Analyzing request";
      case "speaking": return k ? `Type or hold ${k} to interrupt` : "Type to interrupt";
      case "error": return "Fault logged to logs/jarvis.log // auto-recovery engaged";
      default:
        if (S.mode === "wake") return k ? `Say "Hey ${S.name}" or hold ${k}` : `Say "Hey ${S.name}"`;
        return k ? `Hold ${k} to speak or type a command` : "Type a command";
    }
  }

  function decode(el, text) {
    const glyphs = "ABCDEF0123456789#$%&*<>/\\=+";
    const start = performance.now();
    const dur = REDUCED ? 1 : 460;
    cancelAnimationFrame(el._raf);
    const step = (now) => {
      const p = Math.min(1, (now - start) / dur);
      const n = Math.floor(p * text.length);
      let out = text.slice(0, n);
      for (let i = n; i < text.length; i++) out += text[i] === " " ? " " : glyphs[(Math.random() * glyphs.length) | 0];
      el.textContent = out;
      if (p < 1) el._raf = requestAnimationFrame(step);
    };
    el._raf = requestAnimationFrame(step);
  }

  function setChip(id, value, cls) {
    const el = $(id);
    el.className = "chip" + (cls ? " " + cls : "");
    el.querySelector("b").textContent = value;
  }

  function applyLook() {
    const key = lookKey();
    if (key !== S.look) {
      S.look = key;
      document.body.dataset.look = key;
      document.body.style.setProperty("--a", `rgb(${LOOKS[key].c.join(",")})`);
      decode($("state-label"), LOOKS[key].label);
    }
    $("state-hint").textContent = hintFor(key);
    document.body.classList.toggle("muted", S.muted);
    if (S.error) setChip("chip-link", "FAULT", "bad");
    else if (S.relink) setChip("chip-link", "RELINKING", "warn");
    else if (S.note === "starting" || S.note === "loading") setChip("chip-link", "BOOTING", "warn");
    else if (S.note === "offline") setChip("chip-link", "OFFLINE", "bad");
    else setChip("chip-link", "ONLINE");
    setChip("chip-mic", S.muted ? "MUTED" : "ACTIVE", S.muted ? "bad" : "");
    setChip("chip-mode", S.mode === "wake" ? "WAKE WORD" : "PUSH-TO-TALK", S.mode === "wake" ? "" : "dim");
    $("input").placeholder = S.mode === "wake" ? `ENTER COMMAND OR SAY "HEY ${S.name.toUpperCase()}"` : "ENTER COMMAND";
  }

  function brandName(name) {
    return name.toLowerCase() === "jarvis" ? "J.A.R.V.I.S." : name.toUpperCase();
  }

  // ------------------------------------------------------------------ transcript

  const feed = $("feed");
  const msgs = [];

  function nearBottom() {
    return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  }

  function addMessage(m, instant) {
    const empty = feed.querySelector(".feed-empty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = "msg " + (m.role === "user" ? "user" : "ai");
    const meta = document.createElement("div");
    meta.className = "meta";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = m.role === "user" ? (S.user ? S.user.toUpperCase() : "USER") : S.name.toUpperCase();
    const time = document.createElement("span");
    time.textContent = m.time || "";
    meta.append(who, time);
    const body = document.createElement("div");
    body.className = "body";
    const text = document.createTextNode("");
    const caret = document.createElement("span");
    caret.className = "caret";
    body.append(text, caret);
    el.append(meta, body);
    const stick = nearBottom() || m.role === "user";
    feed.append(el);
    const rec = { el, text, caret, role: m.role, full: m.text || "", shown: 0, streaming: !!m.streaming };
    if (instant || m.role === "user" || REDUCED) rec.shown = rec.full.length;
    msgs.push(rec);
    while (msgs.length > 80) msgs.shift().el.remove();
    $("msg-count").textContent = String(msgs.length).padStart(3, "0");
    renderMessage(rec);
    if (stick) feed.scrollTop = feed.scrollHeight;
    return rec;
  }

  function renderMessage(rec) {
    rec.text.data = rec.full.slice(0, rec.shown);
    rec.caret.style.display = rec.streaming || rec.shown < rec.full.length ? "" : "none";
  }

  function typeTick() {
    let changed = false;
    const stick = nearBottom();
    for (let i = Math.max(0, msgs.length - 3); i < msgs.length; i++) {
      const rec = msgs[i];
      if (rec.shown < rec.full.length) {
        rec.shown = Math.min(rec.full.length, rec.shown + Math.max(1, Math.ceil((rec.full.length - rec.shown) / 10)));
        renderMessage(rec);
        changed = true;
      }
    }
    if (changed && stick) feed.scrollTop = feed.scrollHeight;
  }

  function lastStreaming() {
    const last = msgs[msgs.length - 1];
    return last && last.role === "assistant" && last.streaming ? last : null;
  }

  function finishReply() {
    const last = lastStreaming();
    if (last) {
      last.streaming = false;
      last.full = last.full.trim();
      renderMessage(last);
    }
  }

  // ------------------------------------------------------------------ event log & hexdump

  const logEl = $("log");
  function addLog(l) {
    const row = document.createElement("div");
    row.className = "ll t-" + l.tag;
    const time = document.createElement("time");
    time.textContent = l.time;
    const tag = document.createElement("b");
    tag.textContent = l.tag;
    const text = document.createElement("span");
    text.textContent = l.text;
    row.append(time, tag, text);
    logEl.append(row);
    while (logEl.children.length > 30) logEl.firstChild.remove();
  }

  const hexEl = $("hexdump");
  let hexAddr = (Math.random() * 0xffff) | 0;
  function hexLine() {
    const row = document.createElement("div");
    hexAddr = (hexAddr + 16) & 0xffff;
    let html = "0x" + hexAddr.toString(16).toUpperCase().padStart(4, "0") + "  ";
    for (let i = 0; i < 8; i++) {
      const byte = ((Math.random() * 256) | 0).toString(16).toUpperCase().padStart(2, "0");
      html += Math.random() < 0.12 ? `<b>${byte}</b> ` : byte + " ";
    }
    row.innerHTML = html;
    hexEl.append(row);
    while (hexEl.children.length > 24) hexEl.firstChild.remove();
  }

  // ------------------------------------------------------------------ diagnostics

  const hist = { cpu: new Array(60).fill(0), down: new Array(60).fill(0), up: new Array(60).fill(0) };
  const gauges = { cpu: { cur: 0, target: 0, el: $("g-cpu") }, mem: { cur: 0, target: 0, el: $("g-mem") } };

  function fmtRate(kb) {
    return kb >= 1024 ? (kb / 1024).toFixed(1) + " MB/S" : kb.toFixed(0) + " KB/S";
  }
  function fmtUptime(s) {
    const d = Math.floor(s / 86400);
    const hms = [Math.floor(s / 3600) % 24, Math.floor(s / 60) % 60, s % 60].map((n) => String(n).padStart(2, "0")).join(":");
    return d ? `${d}D ${hms}` : hms;
  }
  function setBar(id, pct, hot) {
    const el = $(id);
    el.style.width = Math.max(0, Math.min(100, pct)) + "%";
    el.classList.toggle("hot", !!hot);
  }

  function updateStats(m) {
    gauges.cpu.target = m.cpu ?? 0;
    gauges.mem.target = m.mem ?? 0;
    hist.cpu.push(m.cpu ?? 0); hist.cpu.shift();
    hist.down.push(m.down ?? 0); hist.down.shift();
    hist.up.push(m.up ?? 0); hist.up.shift();
    $("v-ghz").textContent = m.ghz ? `${m.ghz.toFixed(2)} GHZ` : "-";
    $("v-down").textContent = fmtRate(m.down ?? 0);
    $("v-up").textContent = fmtRate(m.up ?? 0);
    $("v-disk").textContent = `${Math.round(m.disk ?? 0)}%`;
    setBar("b-disk", m.disk ?? 0, (m.disk ?? 0) > 90);
    $("v-mem").textContent = m.mem_total ? `${m.mem_used}/${Math.round(m.mem_total)}G` : "-";
    setBar("b-mem", m.mem ?? 0, (m.mem ?? 0) > 85);
    if (m.battery != null) {
      $("v-bat").textContent = `${m.battery}%${m.plugged ? "+" : ""}`;
      setBar("b-bat", m.battery, m.battery < 20 && !m.plugged);
    } else {
      $("v-bat").textContent = "AC";
      setBar("b-bat", 100);
    }
    $("v-uptime").textContent = m.uptime != null ? fmtUptime(m.uptime) : "-";
    $("v-procs").textContent = m.procs ?? "-";
    $("v-cores").textContent = m.cores ?? "-";
  }

  function fit(canvas) {
    const dpr = Math.min(2, devicePixelRatio || 1);
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return [ctx, w, h];
  }

  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

  function drawGauge(g) {
    g.cur += (g.target - g.cur) * 0.12;
    const canvas = g.el.querySelector("canvas");
    const [ctx, w, h] = fit(canvas);
    if (!w) return;
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) * 0.38;
    const a0 = Math.PI * 0.75, span = Math.PI * 1.5;
    ctx.lineCap = "butt";
    ctx.beginPath();
    for (let i = 0; i <= 40; i++) {
      const a = a0 + (span * i) / 40;
      const r1 = r * 1.18, r2 = r * (i % 5 ? 1.12 : 1.08);
      ctx.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
      ctx.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2);
    }
    ctx.strokeStyle = rgba(col, 0.35);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.lineWidth = r * 0.13;
    ctx.beginPath();
    ctx.arc(cx, cy, r, a0, a0 + span);
    ctx.strokeStyle = rgba(col, 0.12);
    ctx.stroke();
    const v = Math.max(0, Math.min(100, g.cur)) / 100;
    const hot = v > 0.85;
    const c = hot ? [255, 176, 32] : col;
    ctx.save();
    ctx.shadowColor = rgba(c, 0.9);
    ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.arc(cx, cy, r, a0, a0 + span * v);
    ctx.strokeStyle = rgba(c, 0.95);
    ctx.stroke();
    ctx.restore();
    ctx.beginPath();
    ctx.arc(cx, cy, r * 0.8, 0, TAU);
    ctx.strokeStyle = rgba(col, 0.2);
    ctx.lineWidth = 1;
    ctx.stroke();
    g.el.querySelector(".g-val b").textContent = Math.round(g.cur);
  }

  function drawSpark(canvas, series, max) {
    const [ctx, w, h] = fit(canvas);
    if (!w) return;
    ctx.strokeStyle = rgba(col, 0.08);
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 4; i++) { ctx.moveTo(0, (h * i) / 4); ctx.lineTo(w, (h * i) / 4); }
    for (let i = 1; i < 12; i++) { ctx.moveTo((w * i) / 12, 0); ctx.lineTo((w * i) / 12, h); }
    ctx.stroke();
    series.forEach(([data, alpha, fill]) => {
      const n = data.length;
      ctx.beginPath();
      data.forEach((v, i) => {
        const x = (w * i) / (n - 1), y = h - 2 - (h - 4) * Math.min(1, v / max);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      if (fill) {
        ctx.save();
        ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
        const g = ctx.createLinearGradient(0, 0, 0, h);
        g.addColorStop(0, rgba(col, 0.28)); g.addColorStop(1, rgba(col, 0));
        ctx.fillStyle = g;
        ctx.fill();
        ctx.restore();
        ctx.beginPath();
        data.forEach((v, i) => {
          const x = (w * i) / (n - 1), y = h - 2 - (h - 4) * Math.min(1, v / max);
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        });
      }
      ctx.strokeStyle = rgba(col, alpha);
      ctx.lineWidth = 1.4;
      ctx.stroke();
    });
  }

  function drawPanels() {
    if (document.hidden || !document.body.classList.contains("ready")) return;
    drawGauge(gauges.cpu);
    drawGauge(gauges.mem);
    drawSpark($("spark-cpu"), [[hist.cpu, 0.95, true]], 100);
    const netMax = Math.max(64, ...hist.down, ...hist.up) * 1.1;
    drawSpark($("spark-net"), [[hist.down, 0.95, true], [hist.up, 0.5, false]], netMax);
  }

  // ------------------------------------------------------------------ clock

  const DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
  const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  const sessionStart = Date.now();
  function tickClock() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    $("clock-time").textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    $("clock-date").textContent = `${DAYS[d.getDay()]} ${p(d.getDate())} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
    $("v-session").textContent = fmtUptime(Math.floor((Date.now() - sessionStart) / 1000));
  }

  // ------------------------------------------------------------------ background fx

  const fx = $("fx"), fxc = fx.getContext("2d");
  const grid = document.createElement("canvas");
  const motes = Array.from({ length: 70 }, () => ({ x: Math.random(), y: Math.random(), s: Math.random() * 1.6 + 0.4, v: Math.random() * 0.012 + 0.004, p: Math.random() * TAU }));

  function buildGrid() {
    const dpr = Math.min(2, devicePixelRatio || 1);
    fx.width = Math.round(innerWidth * dpr);
    fx.height = Math.round(innerHeight * dpr);
    grid.width = fx.width;
    grid.height = fx.height;
    const g = grid.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const size = 22, w = size * Math.sqrt(3), hh = size * 1.5;
    g.strokeStyle = "rgba(0,229,255,0.045)";
    g.lineWidth = 1;
    g.beginPath();
    for (let row = -1; row * hh < innerHeight + size; row++) {
      for (let col = -1; col * w < innerWidth + w; col++) {
        const x = col * w + (row % 2 ? w / 2 : 0), y = row * hh;
        for (let k = 0; k < 6; k++) {
          const a = Math.PI / 6 + (k * TAU) / 6;
          const px = x + Math.cos(a) * size, py = y + Math.sin(a) * size;
          k ? g.lineTo(px, py) : g.moveTo(px, py);
        }
        g.closePath();
      }
    }
    g.stroke();
  }

  function drawFx(t) {
    const dpr = fx.width / innerWidth;
    fxc.setTransform(1, 0, 0, 1, 0, 0);
    fxc.clearRect(0, 0, fx.width, fx.height);
    fxc.drawImage(grid, 0, 0);
    fxc.setTransform(dpr, 0, 0, dpr, 0, 0);
    const beam = ((t * 0.04) % 1.4 - 0.2) * innerHeight;
    const g = fxc.createLinearGradient(0, beam - 60, 0, beam + 2);
    g.addColorStop(0, rgba(col, 0));
    g.addColorStop(1, rgba(col, 0.05));
    fxc.fillStyle = g;
    fxc.fillRect(0, beam - 60, innerWidth, 62);
    for (const m of motes) {
      m.y -= m.v / 60;
      if (m.y < -0.02) { m.y = 1.02; m.x = Math.random(); }
      const a = 0.25 + 0.25 * Math.sin(t * 1.3 + m.p);
      fxc.fillStyle = rgba(col, a);
      fxc.fillRect(m.x * innerWidth, m.y * innerHeight, m.s, m.s);
    }
  }

  // ------------------------------------------------------------------ the reactor

  const rc = $("reactor"), rx = rc.getContext("2d");
  const bloom = document.createElement("canvas"), bx = bloom.getContext("2d");
  let W = 0, H = 0, DPR = 1, quality = 1;
  const col = LOOKS.starting.c.slice();
  let spin = LOOKS.starting.spin;
  let phase = 0;
  let frames = 0;

  const segs = [];
  for (let a = 0; a < TAU - 0.05;) {
    const l = 0.04 + Math.random() * 0.22;
    segs.push({ a, l, hi: Math.random() < 0.25 });
    a += l + 0.025 + Math.random() * 0.05;
  }
  const blips = Array.from({ length: 9 }, () => ({ a: Math.random() * TAU, r: 0.2 + Math.random() * 0.75 }));
  const HEX = "0123456789ABCDEF";
  const ringChars = Array.from({ length: 72 }, () => HEX[(Math.random() * 16) | 0]);

  function sizeReactor() {
    const r = rc.getBoundingClientRect();
    W = r.width; H = r.height;
    DPR = Math.min(2, devicePixelRatio || 1) * quality;
    rc.width = Math.max(1, Math.round(W * DPR));
    rc.height = Math.max(1, Math.round(H * DPR));
    bloom.width = Math.max(1, Math.round(rc.width / 4));
    bloom.height = Math.max(1, Math.round(rc.height / 4));
  }

  function A(a) { return rgba(col, a); }
  function Wh(a, k = 0.65) { return rgba([col[0] + (255 - col[0]) * k, col[1] + (255 - col[1]) * k, col[2] + (255 - col[2]) * k], a); }

  function circle(cx, cy, r, stroke, lw) {
    rx.beginPath();
    rx.arc(cx, cy, r, 0, TAU);
    rx.strokeStyle = stroke;
    rx.lineWidth = lw;
    rx.stroke();
  }

  function drawReactor(t, dt) {
    const look = LOOKS[S.look] || LOOKS.idle;
    const k = 1 - Math.exp(-dt * 3.5);
    for (let i = 0; i < 3; i++) col[i] += (look.c[i] - col[i]) * k;
    spin += (look.spin - spin) * k;
    phase += dt * spin;

    const listening = S.look === "listening", speaking = S.look === "speaking", thinking = S.look === "thinking";
    if (!listening) S.levelTarget = 0;
    S.level += (S.levelTarget - S.level) * (S.levelTarget > S.level ? 0.45 : 0.1);
    const voice = speaking ? 0.5 + 0.3 * Math.sin(t * 9.3) * Math.sin(t * 3.1) + 0.15 * Math.sin(t * 17.7) : 0;
    const energy = listening ? S.level : speaking ? Math.max(0, voice) : 0;

    rx.setTransform(DPR, 0, 0, DPR, 0, 0);
    rx.clearRect(0, 0, W, H);
    const R = Math.max(40, Math.min(W * 0.9, H - 96) / 2.45);
    const cx = W / 2, cy = (H - 70) / 2 + 8;

    // backdrop glow
    let g = rx.createRadialGradient(cx, cy, R * 0.05, cx, cy, R * 1.25);
    g.addColorStop(0, A(0.2 + 0.15 * energy));
    g.addColorStop(0.45, A(0.06));
    g.addColorStop(1, A(0));
    rx.fillStyle = g;
    rx.fillRect(cx - R * 1.3, cy - R * 1.3, R * 2.6, R * 2.6);

    // outer degree ring with labels
    const r0 = R * 0.98, rot0 = phase * 0.05;
    rx.beginPath();
    for (let i = 0; i < 180; i++) {
      const a = rot0 + (i * TAU) / 180;
      const len = i % 15 === 0 ? 0.055 : i % 5 === 0 ? 0.032 : 0.016;
      rx.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
      rx.lineTo(cx + Math.cos(a) * (r0 - R * len), cy + Math.sin(a) * (r0 - R * len));
    }
    rx.strokeStyle = A(0.55);
    rx.lineWidth = 1;
    rx.stroke();
    circle(cx, cy, r0 + R * 0.014, A(0.22), 1);
    rx.font = `${Math.max(9, R * 0.038)}px "Share Tech Mono", Consolas, monospace`;
    rx.textAlign = "center";
    rx.textBaseline = "middle";
    rx.fillStyle = A(0.6);
    for (let i = 0; i < 12; i++) {
      const a = rot0 + (i * TAU) / 12;
      rx.save();
      rx.translate(cx + Math.cos(a) * (r0 - R * 0.1), cy + Math.sin(a) * (r0 - R * 0.1));
      rx.rotate(a + Math.PI / 2);
      rx.fillText(String(i * 30).padStart(3, "0"), 0, 0);
      rx.restore();
    }

    // fixed brackets and crosshair
    rx.lineWidth = 2;
    rx.strokeStyle = A(0.75);
    for (let q = 0; q < 4; q++) {
      const a = Math.PI / 4 + (q * TAU) / 4;
      rx.beginPath();
      rx.arc(cx, cy, R * 1.06, a - 0.2, a + 0.2);
      rx.stroke();
    }
    rx.lineWidth = 1;
    rx.beginPath();
    for (let q = 0; q < 4; q++) {
      const a = (q * TAU) / 4;
      rx.moveTo(cx + Math.cos(a) * R * 1.03, cy + Math.sin(a) * R * 1.03);
      rx.lineTo(cx + Math.cos(a) * R * 1.16, cy + Math.sin(a) * R * 1.16);
    }
    rx.strokeStyle = A(0.45);
    rx.stroke();

    // heavy rotating arcs
    const r2 = R * 0.885, rot2 = phase * 0.32;
    circle(cx, cy, r2, A(0.14), 1);
    rx.lineWidth = Math.max(2, R * 0.02);
    rx.strokeStyle = A(0.9);
    for (const [off, len] of [[0, 0.95], [2.3, 0.45], [3.7, 1.35]]) {
      rx.beginPath();
      rx.arc(cx, cy, r2, rot2 + off, rot2 + off + len);
      rx.stroke();
    }

    // segmented counter-rotating ring
    const r3 = R * 0.8, rot3 = -phase * 0.17;
    rx.lineWidth = Math.max(3, R * 0.034);
    for (const hi of [false, true]) {
      rx.beginPath();
      for (const s of segs) {
        if (s.hi !== hi) continue;
        rx.moveTo(cx + Math.cos(rot3 + s.a) * r3, cy + Math.sin(rot3 + s.a) * r3);
        rx.arc(cx, cy, r3, rot3 + s.a, rot3 + s.a + s.l);
      }
      rx.strokeStyle = A(hi ? 0.6 : 0.22);
      rx.stroke();
    }

    // radar sweep with fading blips
    const r4 = R * 0.72, sweep = phase * 1.05;
    if (rx.createConicGradient) {
      const cg = rx.createConicGradient(sweep, cx, cy);
      cg.addColorStop(0, A(0));
      cg.addColorStop(0.8, A(0));
      cg.addColorStop(0.998, A(0.32));
      cg.addColorStop(1, A(0));
      rx.fillStyle = cg;
      rx.beginPath();
      rx.arc(cx, cy, r4, 0, TAU);
      rx.fill();
    }
    const lead = sweep + TAU * 0.998;
    rx.beginPath();
    rx.moveTo(cx, cy);
    rx.lineTo(cx + Math.cos(lead) * r4, cy + Math.sin(lead) * r4);
    rx.strokeStyle = A(0.65);
    rx.lineWidth = 1.2;
    rx.stroke();
    for (const b of blips) {
      const behind = (((lead - b.a) % TAU) + TAU) % TAU;
      const a = Math.max(0, 1 - behind / 2.2);
      if (a <= 0) continue;
      rx.fillStyle = Wh(a);
      rx.beginPath();
      rx.arc(cx + Math.cos(b.a) * r4 * b.r, cy + Math.sin(b.a) * r4 * b.r, 2.2, 0, TAU);
      rx.fill();
    }
    circle(cx, cy, r4, A(0.35), 1);

    // rotating hex data ring
    if (frames % 6 === 0) {
      for (let i = 0; i < 6; i++) ringChars[(Math.random() * ringChars.length) | 0] = HEX[(Math.random() * 16) | 0];
    }
    const r5 = R * 0.655, rot5 = phase * 0.12;
    rx.font = `${Math.max(8, R * 0.032)}px "Share Tech Mono", Consolas, monospace`;
    rx.fillStyle = A(0.45);
    for (let i = 0; i < ringChars.length; i++) {
      if (i % 12 > 9) continue;
      const a = rot5 + (i * TAU) / ringChars.length;
      rx.save();
      rx.translate(cx + Math.cos(a) * r5, cy + Math.sin(a) * r5);
      rx.rotate(a + Math.PI / 2);
      rx.fillText(ringChars[i], 0, 0);
      rx.restore();
    }

    // audio spectrum ring
    const N = 144, rb = R * 0.5;
    rx.beginPath();
    for (let i = 0; i < N; i++) {
      const a = (i * TAU) / N - Math.PI / 2;
      const wave = 0.55 + 0.25 * Math.sin(3 * a + t * 5.1) + 0.12 * Math.sin(7 * a - t * 8.3) + 0.08 * Math.sin(13 * a + t * 11.7);
      let h;
      if (listening) h = (0.12 + 0.88 * S.level) * wave;
      else if (speaking) h = energy * wave;
      else if (thinking) h = 0.22 * (0.5 + 0.5 * Math.sin(i * 0.35 - t * 9));
      else if (S.look === "idle") h = 0.06 + 0.04 * Math.sin(i * 0.5 + t * 2);
      else h = 0.025;
      const len = R * (0.012 + 0.13 * Math.max(0, Math.min(1, h)));
      rx.moveTo(cx + Math.cos(a) * rb, cy + Math.sin(a) * rb);
      rx.lineTo(cx + Math.cos(a) * (rb + len), cy + Math.sin(a) * (rb + len));
    }
    rx.strokeStyle = A(0.85);
    rx.lineWidth = Math.max(1.2, R * 0.0065);
    rx.lineCap = "round";
    rx.stroke();
    rx.lineCap = "butt";

    // listening ripples
    if (listening) {
      for (let q = 0; q < 3; q++) {
        const p = (t * 0.55 + q / 3) % 1;
        circle(cx, cy, R * (0.44 + p * 0.58), A(0.45 * (1 - p)), 1.5);
      }
    }

    // thinking comets
    if (thinking) {
      for (const head of [0, Math.PI]) {
        for (let q = 0; q < 22; q++) {
          const a = t * 3.6 + head - q * 0.045;
          rx.fillStyle = q === 0 ? Wh(1) : A(1 - q / 22);
          rx.beginPath();
          rx.arc(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2, Math.max(0.6, 3.4 * (1 - q / 22)), 0, TAU);
          rx.fill();
        }
      }
    }

    // arc reactor
    const pulse = 1 + 0.045 * Math.sin(t * 2.2) + 0.22 * energy;
    const ro = R * 0.43, ri = R * 0.31, rot6 = phase * 0.22, gap = 0.05;
    circle(cx, cy, R * 0.455, A(0.5), 1);
    for (let q = 0; q < 10; q++) {
      const a0 = rot6 + (q * TAU) / 10 + gap, a1 = a0 + TAU / 10 - 2 * gap;
      rx.beginPath();
      rx.arc(cx, cy, ro, a0, a1);
      rx.arc(cx, cy, ri, a1, a0, true);
      rx.closePath();
      rx.fillStyle = A(0.14 + 0.12 * (0.5 + 0.5 * Math.sin(t * 3 + q)) + 0.25 * energy);
      rx.fill();
      rx.strokeStyle = A(0.85);
      rx.lineWidth = 1;
      rx.stroke();
    }
    circle(cx, cy, R * 0.285, Wh(0.6, 0.4), 1.2);

    rx.globalCompositeOperation = "lighter";
    const rc0 = R * 0.24 * pulse;
    g = rx.createRadialGradient(cx, cy, 0, cx, cy, rc0);
    g.addColorStop(0, "rgba(255,255,255,0.95)");
    g.addColorStop(0.28, Wh(0.85, 0.7));
    g.addColorStop(0.6, A(0.5));
    g.addColorStop(1, A(0));
    rx.fillStyle = g;
    rx.beginPath();
    rx.arc(cx, cy, rc0, 0, TAU);
    rx.fill();
    rx.beginPath();
    for (let q = 0; q < 3; q++) {
      const a = Math.PI / 2 + (q * TAU) / 3;
      const px = cx + Math.cos(a) * R * 0.17, py = cy + Math.sin(a) * R * 0.17;
      q ? rx.lineTo(px, py) : rx.moveTo(px, py);
    }
    rx.closePath();
    rx.strokeStyle = Wh(0.55, 0.5);
    rx.lineWidth = 1.4;
    rx.stroke();
    rx.globalCompositeOperation = "source-over";

    // bloom: a blurred quarter-size copy added back on top
    if (quality >= 0.75) {
      bx.setTransform(1, 0, 0, 1, 0, 0);
      bx.clearRect(0, 0, bloom.width, bloom.height);
      bx.filter = "blur(3px)";
      bx.drawImage(rc, 0, 0, bloom.width, bloom.height);
      bx.filter = "none";
      rx.setTransform(1, 0, 0, 1, 0, 0);
      rx.globalCompositeOperation = "lighter";
      rx.globalAlpha = 0.9;
      rx.drawImage(bloom, 0, 0, rc.width, rc.height);
      rx.globalAlpha = 1;
      rx.globalCompositeOperation = "source-over";
    }
  }

  function updateReadouts() {
    $("ro-az").textContent = ((((phase * 57.2958 * 0.35) % 360) + 360) % 360).toFixed(1).padStart(5, "0") + "°";
    $("ro-el").textContent = (12 + 9 * Math.sin(phase * 0.21)).toFixed(2).padStart(5, "0") + "°";
    $("ro-sig").textContent = `${Math.round(-60 + S.level * 55)} DB`;
    $("ro-pkt").textContent = String(S.packets).padStart(6, "0");
    $("ro-cyc").textContent = String(frames % 10000).padStart(4, "0");
    $("ro-int").textContent = S.error ? `${(62 + Math.random() * 9).toFixed(1)}%` : `${(99.2 + Math.random() * 0.7).toFixed(1)}%`;
    if (S.look === "thinking") $("state-hint").textContent = hintFor("thinking");
  }

  // ------------------------------------------------------------------ main loop (adaptive quality)

  let last = performance.now(), fxAcc = 0, roAcc = 0, slow = 0, fast = 0, avg = 16;
  function loop(now) {
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    const t = now / 1000;
    frames++;
    if (document.body.classList.contains("ready")) {
      drawReactor(t, dt);
      typeTick();
      fxAcc += dt;
      if (fxAcc >= 1 / 30) { fxAcc = 0; drawFx(t); }
      roAcc += dt;
      if (roAcc >= 0.1) { roAcc = 0; updateReadouts(); }
    }
    // Frame interval, not JS time, so GPU-bound slowness counts too. Below ~40 fps for
    // a couple of seconds: lower the resolution, then drop the bloom pass.
    avg += (dt * 1000 - avg) * 0.05;
    if (avg > 25 && quality > 0.5) {
      if (++slow > 120) { quality = quality > 0.75 ? 0.75 : 0.5; slow = 0; fast = 0; sizeReactor(); }
    } else if (avg < 18 && quality < 1) {
      if (++fast > 1800) { quality = quality < 0.75 ? 0.75 : 1; fast = 0; sizeReactor(); }
    } else {
      slow = Math.max(0, slow - 1);
    }
    requestAnimationFrame(loop);
  }

  // ------------------------------------------------------------------ server link

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Jarvis-Token": token },
      body: JSON.stringify(body || {}),
    }).catch(() => {});
  }

  const handlers = {
    hello(m) {
      Object.assign(S, { name: m.name, user: m.user || "", hotkey: m.hotkey || "", state: m.state, note: m.note, error: m.error, muted: m.muted, mode: m.mode });
      $("brand").textContent = brandName(S.name);
      document.title = "J.A.R.V.I.S.";
      msgs.splice(0).forEach((r) => r.el.remove());
      feed.innerHTML = "";
      if (!m.messages.length) feed.innerHTML = '<div class="feed-empty">AWAITING INPUT<span class="caret"></span></div>';
      m.messages.forEach((x) => addMessage(x, true));
      logEl.innerHTML = "";
      m.log.forEach(addLog);
      applyLook();
    },
    state(m) { S.state = m.state; applyLook(); },
    user(m) { addMessage(m); },
    reply(m) {
      const last = lastStreaming();
      if (last) { last.full += m.chunk; renderMessage(last); }
      else addMessage({ role: "assistant", text: m.chunk.replace(/^\s+/, ""), time: m.time, streaming: true });
    },
    done() { finishReply(); },
    notice(m) { finishReply(); addMessage(m); },
    note(m) { S.note = m.note; applyLook(); },
    error(m) { S.error = m.error; applyLook(); },
    controls(m) { S.muted = m.muted; S.mode = m.mode; applyLook(); },
    level(m) { S.levelTarget = m.v; },
    stats(m) { updateStats(m); },
    tool(m) { S.tool = m.name; S.toolAt = performance.now(); applyLook(); },
    log(m) { addLog(m); },
    shutdown() {
      es.close();
      $("offline").hidden = false;
      setTimeout(() => window.close(), 900);
    },
  };

  const es = new EventSource(`/events?t=${encodeURIComponent(token)}`);
  es.onmessage = (e) => {
    S.packets++;
    try {
      const m = JSON.parse(e.data);
      if (handlers[m.type]) handlers[m.type](m);
    } catch (err) {
      console.error(err);
    }
  };
  es.onopen = () => { S.relink = false; applyLook(); };
  es.onerror = () => { S.relink = true; applyLook(); };

  // ------------------------------------------------------------------ input

  const input = $("input");
  $("cmd").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return input.focus();
    input.value = "";
    post("/api/send", { text });
  });
  $("mic").addEventListener("click", () => post("/api/mute"));
  $("quick").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-say]");
    if (!btn) return;
    btn.classList.add("sent");
    setTimeout(() => btn.classList.remove("sent"), 450);
    post("/api/send", { text: btn.dataset.say });
  });
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && (e.key === "m" || e.key === "M")) {
      e.preventDefault();
      post("/api/mute");
      return;
    }
    if (dlg.open) return;
    if (e.key === "Escape") { input.value = ""; input.blur(); return; }
    if (document.activeElement !== input && e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) input.focus();
  });

  // ------------------------------------------------------------------ audio routing

  const dlg = $("audio");
  const selIn = $("dev-in");
  const selOut = $("dev-out");
  const API_ORDER = ["MME", "DirectSound", "WASAPI", "WDM-KS"];
  const apiRank = (name) => { const i = API_ORDER.indexOf(name); return i < 0 ? 99 : i; };
  const devArg = (value) => (value === "" ? null : Number(value));

  async function api(path, body) {
    const headers = { "X-Jarvis-Token": token };
    const opts = body === undefined ? { headers }
      : { method: "POST", headers: { ...headers, "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    if (!res.ok && res.status !== 400) throw new Error(`link error ${res.status}`);
    return res.json();
  }

  function fillSelect(sel, info) {
    const options = info.options || [];
    const fallback = options.find((o) => o.index === info.default);
    sel.textContent = "";
    sel.append(new Option(fallback ? `Windows default (${fallback.name})` : "Windows default", ""));
    const groups = new Map();
    options.forEach((o) => { if (!groups.has(o.api)) groups.set(o.api, []); groups.get(o.api).push(o); });
    [...groups.keys()].sort((a, b) => apiRank(a) - apiRank(b)).forEach((name) => {
      const group = document.createElement("optgroup");
      group.label = name;
      groups.get(name).forEach((o) => {
        const shared = options.some((x) => x.name === o.name && x.api !== o.api);
        group.append(new Option(shared ? `${o.name} · ${o.api}` : o.name, String(o.index)));
      });
      sel.append(group);
    });
    sel.value = info.selected == null ? "" : String(info.selected);
  }

  function note(kind, text, cls) {
    const el = $(kind === "input" ? "note-in" : "note-out");
    el.textContent = text;
    el.className = "dev-note" + (cls ? " " + cls : "");
  }

  function setStatus(text, cls) {
    const el = $("audio-status");
    el.textContent = text;
    el.className = "dlg-status" + (cls ? " " + cls : "");
  }

  function setMeter(cls, level) {
    const meter = $("meter-in");
    meter.className = "meter" + (cls ? " " + cls : "");
    meter.firstElementChild.style.width = level == null ? "" : `${Math.max(3, Math.round(level * 100))}%`;
  }

  async function openAudio() {
    if (dlg.open) return;
    setStatus("");
    setMeter("", 0);
    note("input", "Press TEST MIC and say something.");
    note("output", "Press TEST SPEAKER to hear a tone.");
    [selIn, selOut].forEach((sel) => { sel.textContent = ""; sel.append(new Option("SCANNING DEVICES...", "")); sel.disabled = true; });
    $("audio-apply").disabled = true;
    dlg.showModal();
    try {
      const info = await api("/api/audio");
      if (info.error) throw new Error(info.error);
      fillSelect(selIn, info.input || {});
      fillSelect(selOut, info.output || {});
      if (info.input && info.input.error) note("input", info.input.error, "bad");
      if (info.output && info.output.error) note("output", info.output.error, "bad");
      $("audio-apply").disabled = false;
    } catch (err) {
      setStatus(`Couldn't read the audio devices: ${err.message}`);
    } finally {
      selIn.disabled = selOut.disabled = false;
      selIn.focus();
    }
  }

  async function runTest(btn, label, busyLabel, work) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add("busy");
    btn.textContent = busyLabel;
    try {
      await work();
    } finally {
      btn.disabled = false;
      btn.classList.remove("busy");
      btn.textContent = label;
    }
  }

  $("test-in").addEventListener("click", () => runTest($("test-in"), "TEST MIC", "LISTENING", async () => {
    setMeter("scan", null);
    note("input", "Listening for 2 seconds. Say something now.", "");
    try {
      const r = await api("/api/audio/test", { kind: "input", device: devArg(selIn.value) });
      if (!r.ok) { setMeter("none", 0); note("input", r.error || "That microphone didn't work.", "bad"); return; }
      if (r.level >= 0.45) { setMeter("", r.level); note("input", "Signal locked. This microphone hears you.", "ok"); }
      else if (r.level >= 0.2) { setMeter("weak", r.level); note("input", "Faint signal. Move closer or raise the mic volume in Windows.", "warn"); }
      else { setMeter("none", r.level); note("input", "No signal. Check the mic's mute switch, Windows microphone privacy, or pick another entry.", "bad"); }
    } catch (err) {
      setMeter("none", 0);
      note("input", err.message, "bad");
    }
  }));

  $("test-out").addEventListener("click", () => runTest($("test-out"), "TEST SPEAKER", "PLAYING", async () => {
    note("output", "Playing a tone...", "");
    try {
      const r = await api("/api/audio/test", { kind: "output", device: devArg(selOut.value) });
      if (r.ok) note("output", "Tone sent. If you didn't hear it, pick another output.", "ok");
      else note("output", r.error || "That speaker didn't work.", "bad");
    } catch (err) {
      note("output", err.message, "bad");
    }
  }));

  $("audio-apply").addEventListener("click", async () => {
    const btn = $("audio-apply");
    if (btn.disabled) return;
    btn.disabled = true;
    setStatus("Applying...", "ok");
    try {
      const r = await api("/api/audio", { input: devArg(selIn.value), output: devArg(selOut.value) });
      if (r.ok) {
        setStatus("Saved. Switching audio now.", "ok");
        setTimeout(() => dlg.close(), 700);
      } else {
        setStatus(r.error || "Couldn't apply those devices.");
      }
    } catch (err) {
      setStatus(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  selIn.addEventListener("change", () => { setMeter("", 0); note("input", "Press TEST MIC and say something."); setStatus(""); });
  selOut.addEventListener("change", () => { note("output", "Press TEST SPEAKER to hear a tone."); setStatus(""); });
  $("gear").addEventListener("click", openAudio);
  $("audio-cancel").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener("close", () => input.focus());

  // ------------------------------------------------------------------ boot

  function boot() {
    let seen = false;
    try { seen = sessionStorage.getItem("jarvis-booted") === "1"; sessionStorage.setItem("jarvis-booted", "1"); } catch (_) { /* private mode */ }
    if (seen || REDUCED) { document.body.classList.add("ready"); return; }
    const lines = [
      "INITIALIZING NEURAL INTERFACE",
      "CALIBRATING ACOUSTIC SENSOR ARRAY",
      "SYNCHRONIZING LANGUAGE CORE",
      "MOUNTING TOOL MODULES",
      "ESTABLISHING SECURE LOOPBACK UPLINK",
      "ALL SYSTEMS NOMINAL",
    ];
    const out = $("boot-log"), bar = document.querySelector(".boot-bar i");
    let i = 0;
    const next = () => {
      if (i < lines.length) {
        const row = document.createElement("div");
        row.textContent = "> " + lines[i].padEnd(38, ".") + " ";
        const ok = document.createElement("span");
        ok.className = "ok";
        ok.textContent = "[ OK ]";
        row.append(ok);
        out.append(row);
        bar.style.width = `${((i + 1) / (lines.length + 1)) * 100}%`;
        i++;
        setTimeout(next, 170 + Math.random() * 120);
      } else {
        const row = document.createElement("div");
        row.textContent = `\nWELCOME BACK${S.user ? ", " + S.user.toUpperCase() : ""}.`;
        out.append(row);
        bar.style.width = "100%";
        setTimeout(() => document.body.classList.add("ready"), 550);
      }
    };
    setTimeout(next, 250);
  }

  // ------------------------------------------------------------------ start

  sizeReactor();
  buildGrid();
  new ResizeObserver(sizeReactor).observe(rc);
  addEventListener("resize", buildGrid);
  applyLook();
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(drawPanels, 66);
  setInterval(() => { if (!document.hidden) hexLine(); }, 140);
  for (let i = 0; i < 20; i++) hexLine();
  boot();
  requestAnimationFrame(loop);
})();
