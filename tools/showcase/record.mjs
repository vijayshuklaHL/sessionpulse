// Records the Session Pulse showcase from the demo server (fictional data only).
// Usage: node --experimental-websocket record.mjs <world dir> <frames dir> <cdp port>
import fs from "fs";
import path from "path";

const [,, WORLD, FRAMES, PORT] = process.argv;
const BASE = "http://127.0.0.1:8811";
const TOKEN = fs.readFileSync(path.join(WORLD, "data/sessionpulse/token"), "utf8").trim();
const VW = 1600, VH = 900, DSF = 1.2; // a laptop-sized page, rendered crisp at 1920x1080
const sleep = ms => new Promise(r => setTimeout(r, ms));

const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, {method: "PUT"})).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
let id = 0; const pending = {}; const frames = [];
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending[i] = r; ws.send(JSON.stringify({id: i, method, params})); });
ws.onmessage = m => {
  const d = JSON.parse(m.data);
  if (d.id && pending[d.id]) { pending[d.id](d.result || d.error); delete pending[d.id]; }
  if (d.method === "Page.screencastFrame") {
    const n = frames.length, file = path.join(FRAMES, `f${String(n).padStart(5, "0")}.jpg`);
    fs.writeFileSync(file, Buffer.from(d.params.data, "base64"));
    frames.push({file, t: d.params.metadata.timestamp});
    send("Page.screencastFrameAck", {sessionId: d.params.sessionId});
  }
};
await new Promise(r => ws.onopen = r);
const js = async expr => {
  const r = await send("Runtime.evaluate", {expression: expr, awaitPromise: true, returnByValue: true});
  if (r?.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 400));
  return r?.result?.value;
};

await send("Page.enable");
// Demo only: follow-ups stay off on the server (no real Claude calls on fake data),
// but the page shows them as on, the way a user who enabled them sees it.
await send("Page.addScriptToEvaluateOnNewDocument", {source: `(() => { const f = window.fetch.bind(window);
  window.fetch = async (...a) => { const r = await f(...a); if (!String(a[0]).includes("/api/data") || !r.ok) return r;
    const j = await r.json(); j.followups = {enabled: true, running: false, last_error: null,
      last_run: new Date(Date.now() - 120e3).toISOString()}; return new Response(JSON.stringify(j), {headers: {"Content-Type": "application/json"}}); }; })()`});
await send("Emulation.setDeviceMetricsOverride", {width: VW, height: VH, deviceScaleFactor: DSF, mobile: false});
await send("Page.navigate", {url: `${BASE}/?token=${TOKEN}`});
await sleep(2500);
await js(`try { localStorage.clear() } catch {}; document.documentElement.dataset.theme = "dark"; true`);

// ---------- overlay helpers injected into the page ----------
await js(String.raw`(() => {
  const st = document.createElement("style");
  st.textContent = ${"`"}
  #sp-cap { position: fixed; left: 50%; bottom: 40px; transform: translate(-50%, 20px); opacity: 0; z-index: 9999;
    background: rgba(9,12,18,.92); color: #f1f3f6; border: 1px solid #33404e; border-radius: 16px; padding: 13px 24px;
    font: 600 27px/1.3 Inter, "DejaVu Sans", system-ui, sans-serif; letter-spacing: -.01em; max-width: 1300px; text-align: center;
    box-shadow: 0 20px 60px rgba(0,0,0,.45); transition: opacity .45s ease, transform .45s ease; }
  #sp-cap.on { opacity: 1; transform: translate(-50%, 0); }
  #sp-cap small { display: block; font-weight: 400; font-size: 19px; color: #9aa7b6; margin-top: 4px; }
  #sp-card { position: fixed; inset: 0; z-index: 9998; background: #090c12; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 22px; opacity: 0; transition: opacity .7s ease; pointer-events: none; }
  #sp-card.on { opacity: 1; }
  #sp-card img { width: 1080px; max-width: 80vw; height: auto; border-radius: 22px; }
  #sp-card .line { color: #f1f3f6; font: 650 36px/1.2 Inter, "DejaVu Sans", sans-serif; letter-spacing: -.02em; text-align: center; }
  #sp-card .sub2 { color: #8695a5; font: 400 21px/1.4 Inter, "DejaVu Sans", sans-serif; text-align: center; }
  #sp-card .fine { color: #5d6b7b; font: 400 15px/1.4 Inter, "DejaVu Sans", sans-serif; }
  #sp-cursor { position: fixed; left: 1000px; top: 700px; width: 26px; height: 26px; z-index: 10000; pointer-events: none;
    transition: left .9s cubic-bezier(.4,.1,.2,1), top .9s cubic-bezier(.4,.1,.2,1), transform .15s; filter: drop-shadow(0 3px 6px rgba(0,0,0,.5)); }
  #sp-cursor.press { transform: scale(.85); }
  #sp-float { position: fixed; z-index: 9997; pointer-events: none; transition: left .9s cubic-bezier(.4,.1,.2,1), top .9s cubic-bezier(.4,.1,.2,1);
    box-shadow: 0 24px 50px rgba(0,0,0,.55); transform: rotate(-2deg); }
  #sp-panel { position: fixed; top: 0; left: 0; right: 0; height: 40px; z-index: 9996; background: #000; color: #eee;
    display: flex; align-items: center; justify-content: flex-end; gap: 18px; padding: 0 22px;
    font: 500 16px "DejaVu Sans", sans-serif; transform: translateY(-48px); transition: transform .5s ease; }
  #sp-panel.on { transform: none; }
  #sp-panel .ic { display: flex; align-items: center; gap: 8px; background: #2a2a2a; border-radius: 8px; padding: 4px 10px; }
  #sp-menu { position: fixed; top: 44px; right: 110px; z-index: 9996; background: #2b2b2b; color: #eee; border-radius: 12px;
    padding: 10px 0; width: 470px; font: 400 15px/1.2 "DejaVu Sans", sans-serif; box-shadow: 0 20px 60px rgba(0,0,0,.6);
    opacity: 0; transform: translateY(-8px); transition: opacity .35s, transform .35s; }
  #sp-menu.on { opacity: 1; transform: none; }
  #sp-menu div { padding: 8px 18px; } #sp-menu .dim { color: #8b8b8b; } #sp-menu hr { border: 0; border-top: 1px solid #444; margin: 6px 0; }
  #sp-menu .hl { background: #3a3a3a; }
  .board { grid-auto-columns: minmax(180px, 1fr) !important; }
  .sp-new { animation: sp-new 2.4s ease; }
  @keyframes sp-new { 0% { background: rgba(240,162,74,.35); } 100% { background: transparent; } }
  ${"`"};
  document.head.appendChild(st);
  const mk = (id, html = "") => { const d = document.createElement("div"); d.id = id; d.innerHTML = html; document.body.appendChild(d); return d; };
  mk("sp-cap"); mk("sp-card");
  mk("sp-cursor", '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2 L4 19 L8.5 15 L11.5 22 L14.5 20.8 L11.6 14 L18 14 Z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>');
  const icon = c => '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M2.6 7.2 L6.4 11 L2.6 14.8" fill="none" stroke="#eee" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M8.4 11 H10.2 L11.6 8.6 L13.4 14.6 L15.4 5.4 L17.2 12.6 L18.2 11 H20.2" fill="none" stroke="' + c + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  mk("sp-panel", '<span style="color:#aaa">Wi-Fi</span><span style="color:#aaa">Vol</span><span class="ic">' + icon("#f0a24a") + '<b>3</b></span><span>Thu 10:42</span>');
  mk("sp-menu", '<div class="dim">3 open · 1 working · 2 idle</div><hr><div class="hl">●  work/orders-api  —  working · 1h 41m</div><div>○  work/infra-terraform  —  idle · 46m</div><div>○  work/mobile-app  —  idle · 24m</div><hr><div class="dim">Today: 5 sessions · 4.2h active · 96k output tokens</div><hr><div>Open dashboard</div><div>Open to-do board (21 open)</div>');
  window.SPX = {
    cap(t, s) { const c = document.getElementById("sp-cap"); c.classList.remove("on");
      setTimeout(() => { if (!t) return; c.innerHTML = t + (s ? "<small>" + s + "</small>" : ""); c.classList.add("on"); }, t && c.textContent ? 380 : 0); },
    card(html) { const c = document.getElementById("sp-card"); if (html) { c.innerHTML = html; c.classList.add("on"); } else c.classList.remove("on"); },
    to(x, y) { const k = document.getElementById("sp-cursor"); k.style.left = x + "px"; k.style.top = y + "px"; },
    toEl(sel, dx = .5, dy = .5) { const e = typeof sel === "string" ? document.querySelector(sel) : sel; if (!e) return false;
      const r = e.getBoundingClientRect(); this.to(r.left + r.width * dx, r.top + r.height * dy); return true; },
    press() { const k = document.getElementById("sp-cursor"); k.classList.add("press"); setTimeout(() => k.classList.remove("press"), 180); },
  };
  return true;
})()`);

// ---------- world helpers ----------
const live = JSON.parse(fs.readFileSync(path.join(WORLD, "live.json"), "utf8"));
const setStatus = (proj, status) => {
  const l = live.find(x => x.project === proj);
  const f = path.join(WORLD, `home/.claude/sessions/${l.pid}.json`);
  const j = JSON.parse(fs.readFileSync(f, "utf8"));
  j.status = status; j.statusUpdatedAt = Date.now();
  fs.writeFileSync(f, JSON.stringify(j));
};
const todosFile = path.join(WORLD, "data/sessionpulse/todos.json");
const arrivals = JSON.parse(fs.readFileSync(path.join(WORLD, "arrivals.json"), "utf8"));
const localIso = d => { const o = -d.getTimezoneOffset(), p = n => String(Math.floor(Math.abs(n))).padStart(2, "0");
  const l = new Date(d.getTime() + o * 60e3).toISOString().slice(0, 19);
  return l + (o >= 0 ? "+" : "-") + p(o / 60) + ":" + p(o % 60); };
const arrive = item => {
  const t = JSON.parse(fs.readFileSync(todosFile, "utf8"));
  item = {...item, created: localIso(new Date()), session_at: localIso(new Date(Date.now() - 25 * 60e3))};
  t.push(item); fs.writeFileSync(todosFile, JSON.stringify(t, null, 1));
  return item.id;
};

// ---------- the film ----------
await send("Page.startScreencast", {format: "jpeg", quality: 90, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1});
const T0 = Date.now(); const mark = l => console.log(((Date.now() - T0) / 1000).toFixed(1) + "s " + l);

// 1. Title
mark("title");
await js(`SPX.card('<img src="/brand/session-pulse-working-animated.svg" alt="">'); true`);
await sleep(1200);
await js(`SPX.cap("Know what every Claude Code session is doing", "and what each one left for you to do"); true`);
await sleep(4600);

// 2. Live sessions
mark("overview");
await js(`SPX.card(null); SPX.cap(null); window.scrollTo(0, 0); true`);
await sleep(900);
await js(`SPX.cap("Every session, live", "working or idle, how long it has run, what it has used"); SPX.toEl(".live:nth-child(1) .name"); true`);
await sleep(2600);
setStatus("work/mobile-app", "busy");
await js(`load(); SPX.toEl([...document.querySelectorAll(".live")].find(e => e.textContent.includes("mobile-app")) || ".live", .7, .2); true`);
await sleep(2600);
setStatus("work/infra-terraform", "busy"); await js(`load(); true`); await sleep(1400);
setStatus("work/infra-terraform", "idle"); await js(`load(); true`); await sleep(800);

// 3. Charts
mark("charts");
await js(`SPX.cap("Tokens per day by model, and your real active hours"); document.querySelector("#usage-chart").closest(".grid").scrollIntoView({behavior: "smooth", block: "center"}); true`);
await sleep(1500);
await js(`(() => { const h = document.querySelectorAll("#usage-chart .hit"); const e = h[h.length - 3]; SPX.toEl(e, .5, .55);
  setTimeout(() => e.dispatchEvent(new MouseEvent("mousemove", {bubbles: true, clientX: e.getBoundingClientRect().left + 5, clientY: e.getBoundingClientRect().top + 120})), 950); return true; })()`);
await sleep(3200);
await js(`document.querySelectorAll("#usage-chart .tip").forEach(t => t.style.display = "none"); true`);

// 4. Follow-ups arrive on their own
mark("todo");
await js(`document.querySelector("#todo-list").closest(".grid").scrollIntoView({behavior: "smooth", block: "start"}); SPX.cap("When a session goes quiet, Claude lists what it left open", "decisions, outside work, checklists, things you're waiting on"); SPX.to(1100, 800); true`);
await js(`document.querySelectorAll("#todo-list details.pgroup").forEach(d => GOPEN.set(d.dataset.p, false)); GOPEN.set("work/orders-api", true); renderTodos(); true`);
await sleep(2600);
for (const a of arrivals) {
  const nid = arrive(a);
  await js(`load().then(() => { const cb = document.querySelector('#todo-list input[data-id="${nid}"]'); const li = cb && cb.closest("li");
    if (li) { li.classList.add("sp-new"); } }); true`);
  await sleep(1500);
}
await js(`SPX.toEl(document.querySelector('#todo-list .from .copy'), .5, .5); true`);
await js(`SPX.cap("Each item links back to the session it came from"); true`);
await sleep(3000);

// 5. Board
mark("board");
await js(`window.scrollTo({top: 0, behavior: "smooth"}); SPX.cap(null); true`);
await sleep(700);
await js(`SPX.toEl('.tabs button[data-v="board"]'); true`);
await sleep(1000);
await js(`SPX.press(); setView("board"); SPX.cap("One board for everything you still owe", "colour-coded by kind, filter by project, search"); true`);
await sleep(3600);
// drag the top "Decide" card to Done
await js(`(() => {
  const card = document.querySelector('.col[data-col="decision"] .card2'); const done = document.querySelector('.col[data-col="done"] .col-body');
  window.SP_ID = card.dataset.id; SPX.toEl(card, .35, .25); return true; })()`);
await sleep(1100);
await js(`(() => {
  DRAG = window.SP_ID; const card = document.querySelector('.card2[data-id="' + SP_ID + '"]'); const r = card.getBoundingClientRect();
  const f = card.cloneNode(true); f.id = "sp-float"; f.style.left = r.left + "px"; f.style.top = r.top + "px"; f.style.width = r.width + "px";
  document.body.appendChild(f); card.style.opacity = ".25"; SPX.press(); document.querySelector('.col[data-col="done"]').classList.add("over");
  setTimeout(() => { const d = document.querySelector('.col[data-col="done"] .col-body').getBoundingClientRect();
    f.style.left = d.left + 8 + "px"; f.style.top = d.top + 8 + "px"; SPX.to(d.left + 8 + r.width * .35, d.top + 8 + r.height * .25); }, 150);
  return true; })()`);
await js(`SPX.cap("Drag it to Done when it's handled", "later sessions close items on their own"); true`);
await sleep(1500);
await js(`(async () => { document.getElementById("sp-float").remove(); DRAG = null;
  const i = DATA.todos.find(t => t.id === SP_ID); await todoOp(moveOp(i, "done"));
  const c = document.querySelector('.card2[data-id="' + SP_ID + '"]'); if (c) c.classList.add("sp-new"); return true; })()`);
await sleep(2600);
await js(`(() => { document.querySelector('#board-by button[data-b="status"]').click(); SPX.toEl('#board-by button[data-b="status"]'); return true; })()`);
await js(`SPX.cap("Or by status: To do, Doing, Done"); true`);
await sleep(3000);
await js(`document.querySelector('#board-by button[data-b="kind"]').click(); true`);

// 6. Learning
mark("learn");
await js(`setView("overview"); SPX.cap(null); true`);
await sleep(600);
await js(`document.querySelector("#suggestions").closest(".card").scrollIntoView({behavior: "smooth", block: "center"}); true`);
await sleep(900);
await js(`SPX.cap("Get better at Claude Code", "tips from how you actually work, with free official docs and courses"); SPX.toEl("#suggestions .tip-card a"); true`);
await sleep(4200);

// 7. Panel icon
mark("panel");
await js(`SPX.cap(null); window.scrollTo({top: 0, behavior: "smooth"}); document.getElementById("sp-panel").classList.add("on"); true`);
await sleep(900);
await js(`SPX.toEl("#sp-panel .ic"); true`);
await sleep(1000);
await js(`SPX.press(); document.getElementById("sp-menu").classList.add("on"); SPX.cap("Lives in your panel", "orange while Claude works, green when it's waiting for you"); true`);
await sleep(3800);

// 8. End card
mark("end");
await js(`document.getElementById("sp-menu").classList.remove("on"); document.getElementById("sp-panel").classList.remove("on"); SPX.cap(null); SPX.to(1700, 1000);
  SPX.card('<img src="/brand/session-pulse-idle-animated.svg" alt="" style="width:860px"><div class="line">Session Pulse for Claude Code</div><div class="sub2">Runs on your machine · nothing leaves it · free and open source</div><div class="fine">An independent project, not made or endorsed by Anthropic.</div>'); true`);
await sleep(5200);

await send("Page.stopScreencast");
await sleep(300);
mark("done");
// ffmpeg concat list with real frame durations
let list = "";
for (let i = 0; i < frames.length; i++) {
  const d = i + 1 < frames.length ? frames[i + 1].t - frames[i].t : 0.5;
  list += `file '${frames[i].file}'\nduration ${Math.max(0.001, d).toFixed(4)}\n`;
}
list += `file '${frames[frames.length - 1].file}'\n`;
fs.writeFileSync(path.join(FRAMES, "list.txt"), list);
console.log(frames.length, "frames", (frames.at(-1).t - frames[0].t).toFixed(1) + "s");
process.exit(0);
