// UI audit screenshots from the demo server (made-up data only).
// Usage: node --experimental-websocket audit.mjs <world dir> <out dir> <cdp port> [only]
// Start the demo server and headless Chrome as run.sh does; shots cover desktop and phone, dark and light,
// and print any horizontal overflow.
import fs from "fs";
import path from "path";

const [,, WORLD, OUT, PORT, ONLY] = process.argv;
const BASE = "http://127.0.0.1:8811";
const TOKEN = fs.readFileSync(path.join(WORLD, "data/sessionpulse/token"), "utf8").trim();
const sleep = ms => new Promise(r => setTimeout(r, ms));
fs.mkdirSync(OUT, {recursive: true});
const t = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, {method: "PUT"})).json();
const ws = new WebSocket(t.webSocketDebuggerUrl);
let id = 0; const P = {};
const send = (m, p = {}) => new Promise(r => { const i = ++id; P[i] = r; ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && P[d.id]) P[d.id](d.result || d.error); };
await new Promise(r => ws.onopen = r);
const js = async e => { const r = await send("Runtime.evaluate", {expression: e, awaitPromise: true, returnByValue: true});
  if (r?.exceptionDetails) console.log("JS error:", JSON.stringify(r.exceptionDetails).slice(0, 300)); return r?.result?.value; };

await send("Page.enable");
await send("Page.addScriptToEvaluateOnNewDocument", {source: `(() => { const f = window.fetch.bind(window);
  window.fetch = async (...a) => { const r = await f(...a); if (!String(a[0]).includes("/api/data") || !r.ok) return r;
    const j = await r.json(); j.followups = {enabled: true, running: false, last_error: null,
      last_run: new Date(Date.now() - 120e3).toISOString()}; return new Response(JSON.stringify(j), {headers: {"Content-Type": "application/json"}}); }; })()`});

const sizes = {desk: [1440, 900, false], phone: [390, 844, true]};
async function shot(name, size, theme, setup, full = false) {
  if (ONLY && !name.includes(ONLY)) return;
  const [w, h, mobile] = sizes[size];
  await send("Emulation.setDeviceMetricsOverride", {width: w, height: h, deviceScaleFactor: mobile ? 2 : 1, mobile});
  await js(`closeWhy?.(); DISMISSING = null; OPEN.clear(); document.documentElement.dataset.theme = "${theme}"; render(); true`);
  await sleep(300);
  const msg = await js(`(async () => { ${setup} })()`);
  if (msg) console.log(name, msg);
  await sleep(700);
  let params = {format: "png"};
  if (full) {
    const m = await send("Page.getLayoutMetrics");
    params = {format: "png", captureBeyondViewport: true,
      clip: {x: 0, y: 0, width: w, height: Math.min(m.cssContentSize.height, 9000), scale: 1}};
  }
  const s = await send("Page.captureScreenshot", params);
  const file = path.join(OUT, `${name}-${size}-${theme}.png`);
  fs.writeFileSync(file, Buffer.from(s.data, "base64"));
  const overflow = await js(`document.documentElement.scrollWidth - innerWidth`);
  console.log("wrote", path.basename(file), overflow > 0 ? `HORIZONTAL OVERFLOW ${overflow}px` : "");
}

await send("Emulation.setDeviceMetricsOverride", {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
await send("Page.navigate", {url: `${BASE}/?token=${TOKEN}`});
await sleep(3000);
await js(`clearInterval(TIMER); true`);  // no refreshes while a shot is set up

const top = `setView("overview"); scrollTo(0, 0)`;
const at = sel => `document.querySelector(${JSON.stringify(sel)}).scrollIntoView({block: "start"}); scrollBy(0, -12)`;
const histOpen = `setView("overview"); const s = DATA.sessions.filter(x => DATA.briefs[x.id]).sort((a, b) => b.end.localeCompare(a.end))
  .find(x => DATA.todos.some(i => i.session === x.id)); OPEN.add(s.id); render();
  document.querySelector('tr.row[data-id="' + s.id + '"]').scrollIntoView({block: "start"}); scrollBy(0, -40);`;
const dismiss = `setView("overview"); const i = DATA.todos.find(i => i.source === "session" && isOpen(i)); askDismiss(i.id);
  document.querySelector('[data-reason]').closest("li").scrollIntoView({block: "center"});`;
const why = `setView("overview"); openWhy(DATA.todos.find(i => i.evidence && isOpen(i)).id);
  await new Promise(r => setTimeout(r, 200)); document.querySelector("[data-excerpt]")?.click();
  await new Promise(r => setTimeout(r, 800));`;
const whyClosed = `setView("overview"); openWhy(DATA.todos.find(i => i.closed_by && i.done_at).id);`;
const board = `setView("board"); scrollTo(0, 0)`;
const dup = `setView("board"); const e = document.querySelector(".dup"); e ? e.scrollIntoView({block: "center"}) : 0; return e ? "" : "no dup note on the board"`;

for (const theme of ["dark", "light"]) {
  await shot("1-overview", "desk", theme, top, true);
  await shot("2-history-open", "desk", theme, histOpen);
  await shot("4-board", "desk", theme, board, true);
}
await shot("3-todos", "desk", "dark", `setView("overview"); ${at("#todo-list")}`);
await shot("5-dismiss", "desk", "dark", dismiss);
await shot("6-why", "desk", "dark", why);
await shot("7-why-closed", "desk", "dark", whyClosed);
await shot("8-dup", "desk", "dark", dup);
for (const theme of ["dark", "light"]) {
  await shot("1-overview", "phone", theme, top, true);
}
await shot("2-history-open", "phone", "dark", histOpen);
await shot("4-board", "phone", "dark", board, true);
await shot("5-dismiss", "phone", "dark", dismiss);
await shot("6-why", "phone", "dark", why);
process.exit(0);
