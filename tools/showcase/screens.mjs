// README screenshots from the demo server (fictional data only).
// Usage: node --experimental-websocket screens.mjs <world dir> <out dir> <cdp port>
import fs from "fs";
import path from "path";

const [,, WORLD, OUT, PORT] = process.argv;
const BASE = "http://127.0.0.1:8811";
const TOKEN = fs.readFileSync(path.join(WORLD, "data/sessionpulse/token"), "utf8").trim();
const sleep = ms => new Promise(r => setTimeout(r, ms));
const t = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, {method: "PUT"})).json();
const ws = new WebSocket(t.webSocketDebuggerUrl);
let id = 0; const P = {};
const send = (m, p = {}) => new Promise(r => { const i = ++id; P[i] = r; ws.send(JSON.stringify({id: i, method: m, params: p})); });
ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && P[d.id]) P[d.id](d.result || d.error); };
await new Promise(r => ws.onopen = r);
const js = e => send("Runtime.evaluate", {expression: e, awaitPromise: true, returnByValue: true});

async function shot(name, w, h, setup, theme = "dark") {
  await send("Emulation.setDeviceMetricsOverride", {width: w, height: h, deviceScaleFactor: 1.5, mobile: false});
  await js(`document.documentElement.dataset.theme = "${theme}"; ${setup}; true`);
  await sleep(1200);
  const s = await send("Page.captureScreenshot", {format: "png"});
  fs.writeFileSync(path.join(OUT, name + ".png"), Buffer.from(s.data, "base64"));
}

await send("Page.enable");
// Demo only: follow-ups stay off on the server (no real Claude calls on fake data),
// but the page shows them as on, the way a user who enabled them sees it.
await send("Page.addScriptToEvaluateOnNewDocument", {source: `(() => { const f = window.fetch.bind(window);
  window.fetch = async (...a) => { const r = await f(...a); if (!String(a[0]).includes("/api/data") || !r.ok) return r;
    const j = await r.json(); j.followups = {enabled: true, running: false, last_error: null,
      last_run: new Date(Date.now() - 120e3).toISOString()}; return new Response(JSON.stringify(j), {headers: {"Content-Type": "application/json"}}); }; })()`});
await send("Emulation.setDeviceMetricsOverride", {width: 1440, height: 900, deviceScaleFactor: 1.5, mobile: false});
await send("Page.navigate", {url: `${BASE}/?token=${TOKEN}`});
await sleep(2500);
await js(`document.head.insertAdjacentHTML("beforeend", "<style>.board{grid-auto-columns:minmax(190px,1fr)!important}</style>")`);
await shot("overview", 1440, 900, `setView("overview"); window.scrollTo(0, 0)`);
await shot("overview-light", 1440, 900, `setView("overview"); window.scrollTo(0, 0)`, "light");
await shot("todo", 1440, 820, `setView("overview"); document.querySelector("#todo-list").closest(".grid").scrollIntoView({block: "start"}); window.scrollBy(0, -16)`);
await shot("board", 1600, 900, `setView("board"); window.scrollTo(0, 0)`);
await shot("learn", 1440, 560, `setView("overview"); document.querySelector("#suggestions").closest(".card").scrollIntoView({block: "start"}); window.scrollBy(0, -16)`);
process.exit(0);
