<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relay size sweep</title>
<style>
:root {
  color-scheme: light;
  --bg: #fcfcfb; --surface: #ffffff; --line: #e6e5e1; --grid: #efeeea;
  --text: #0b0b0b; --text-2: #52514e; --muted: #8a8984;
  --n1: #86b6ef; --n2: #5598e7; --n3: #2a78d6; --n4: #256abf; --n5: #184f95; --n6: #0d366b;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100; --s5: #e87ba4; --s6: #008300;
  --bad: #c0392b; --good: #1f7a3a; --band: rgba(42,120,214,0.06);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --bg: #141413; --surface: #1a1a19; --line: #2e2e2b; --grid: #262624;
    --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e86;
    --n1: #184f95; --n2: #256abf; --n3: #3987e5; --n4: #6da7ec; --n5: #9ec5f4; --n6: #cde2fb;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181; --s6: #008300;
    --bad: #e66767; --good: #4fb06d; --band: rgba(57,135,229,0.10);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #141413; --surface: #1a1a19; --line: #2e2e2b; --grid: #262624;
  --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e86;
  --n1: #184f95; --n2: #256abf; --n3: #3987e5; --n4: #6da7ec; --n5: #9ec5f4; --n6: #cde2fb;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181; --s6: #008300;
  --bad: #e66767; --good: #4fb06d; --band: rgba(57,135,229,0.10);
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 40px 16px 80px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 16px; font-weight: 600; margin: 48px 0 12px; padding-top: 16px; border-top: 1px solid var(--line); }
h3 { font-size: 14px; font-weight: 600; margin: 28px 0 8px; }
p, li { color: var(--text-2); max-width: 78ch; }
.sub { color: var(--muted); margin: 0 0 24px; }
.mono, pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
code { color: var(--text-2); }
.wrap { overflow-x: auto; margin: 8px 0 4px; }
table { border-collapse: collapse; font-variant-numeric: tabular-nums; font-size: 12.5px; }
th, td { padding: 5px 10px; text-align: right; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
td.l, th.l { text-align: left; }
td.peak { font-weight: 650; }
.bad { color: var(--bad); }
.dim { color: var(--muted); }
.note { font-size: 12px; color: var(--muted); margin: 4px 0 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 8px 0 16px; color: var(--text-2); font-size: 12.5px; }
.chips b { color: var(--text); font-weight: 600; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 28px; }
@media (max-width: 760px) { .grid2 { grid-template-columns: 1fr; } }
.chart { position: relative; }
.chart .title { font-size: 12.5px; color: var(--text-2); margin: 10px 0 2px; }
.chart svg { width: 100%; height: auto; display: block; overflow: visible; }
.chart text { fill: var(--muted); font-size: 10.5px; font-family: inherit; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--text-2); margin: 4px 0 0; }
.legend i { display: inline-block; width: 12px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--line); border-radius: 6px;
       padding: 6px 8px; font-size: 12px; color: var(--text); box-shadow: 0 2px 10px rgba(0,0,0,.08); display: none; z-index: 5; white-space: nowrap; }
.tip .k { color: var(--text-2); }
details { border-bottom: 1px solid var(--grid); }
details > summary { cursor: pointer; padding: 8px 0; color: var(--text-2); }
details[open] > summary { color: var(--text); }
pre { white-space: pre-wrap; word-break: break-all; color: var(--text-2); max-height: 520px; overflow: auto; background: var(--surface);
      border: 1px solid var(--line); border-radius: 6px; padding: 10px; }
select { font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; }
.controls { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; margin: 8px 0; color: var(--text-2); font-size: 12.5px; }
nav { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12.5px; margin: 0 0 8px; }
nav a { color: var(--text-2); text-decoration: none; border-bottom: 1px solid var(--line); }
nav a:hover { color: var(--text); }
.fails { font-size: 12px; color: var(--text-2); margin: 6px 0 0 0; padding-left: 18px; }
.fails li { margin: 2px 0; }
</style>
</head>
<body>
<main id="app"></main>
<section style="max-width:1120px;margin:0 auto;padding:0 16px 60px">
<h2 id="raw">Raw machine specs</h2>
<p>As collected on each droplet before its test: DigitalOcean API record, uname, OS release, lscpu (summary and per-CPU), /proc/cpuinfo, CPU vulnerabilities, /proc/meminfo, free, dmidecode (system, BIOS, processor, memory), disks, network interface and driver, socket buffer sysctls, idle vmstat (10 s, steal column <code>st</code>) and openssl benchmarks.</p>
<!--SPECS-->
</section>
<div class="tip" id="tip"></div>
<script>
const D = /*DATA*/null;
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const f = (x, d = 1) => x == null || x === "" ? "–" : (typeof x === "number" ? x.toFixed(d) : x);
const NS = "http://www.w3.org/2000/svg";
const NCOL = n => `var(--n${n})`, SCOL = k => `var(--s${k})`;
const tip = $("#tip");

function svg(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}

/* Line chart: series [{name, color, pts: [[x, y|null]]}], one y axis. bands: [{x0, x1, label}] */
function lineChart(host, {series, xLabel, yLabel, yMax, bands = [], fmtY = v => f(v, 1), unit = ""}) {
  const W = 720, H = 220, L = 44, R = 10, T = 10, B = 30;
  const wrap = document.createElement("div"); wrap.className = "chart";
  host.appendChild(wrap);
  const xs = series.flatMap(s => s.pts.map(p => p[0]));
  const ys = series.flatMap(s => s.pts.map(p => p[1]).filter(v => v != null));
  const x0 = Math.min(0, ...xs), x1 = Math.max(1, ...xs);
  const ym = yMax ?? niceMax(Math.max(...ys, 0) * 1.05);
  const X = x => L + (x - x0) / (x1 - x0 || 1) * (W - L - R), Y = y => T + (1 - Math.min(y, ym) / ym) * (H - T - B);
  const s = svg("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": yLabel}, wrap);
  for (const b of bands) {
    svg("rect", {x: X(b.x0), y: T, width: Math.max(0, X(b.x1) - X(b.x0)), height: H - T - B, fill: "var(--band)"}, s);
    if (b.label) { const t = svg("text", {x: X(b.x0) + 3, y: T + 10}, s); t.textContent = b.label; }
  }
  for (let i = 0; i <= 4; i++) {
    const v = ym * i / 4, y = Y(v);
    svg("line", {x1: L, x2: W - R, y1: y, y2: y, stroke: i ? "var(--grid)" : "var(--line)", "stroke-width": 1}, s);
    const t = svg("text", {x: L - 6, y: y + 3.5, "text-anchor": "end"}, s); t.textContent = fmtY(v);
  }
  const step = niceMax((x1 - x0) / 6);
  for (let v = Math.ceil(x0 / step) * step; v <= x1; v += step) {
    const t = svg("text", {x: X(v), y: H - B + 14, "text-anchor": "middle"}, s); t.textContent = Math.round(v);
  }
  const xl = svg("text", {x: W - R, y: H - 4, "text-anchor": "end"}, s); xl.textContent = xLabel;
  const yl = svg("text", {x: L, y: T - 1, "text-anchor": "start"}, s); yl.textContent = "";
  for (const se of series) {
    let d = "", pen = false;
    for (const [x, y] of se.pts) {
      if (y == null) { pen = false; continue; }
      d += (pen ? "L" : "M") + X(x).toFixed(1) + "," + Y(y).toFixed(1); pen = true;
    }
    svg("path", {d, fill: "none", stroke: se.color, "stroke-width": se.width || 1.6, "stroke-linejoin": "round", "stroke-linecap": "round"}, s);
  }
  const cross = svg("line", {y1: T, y2: H - B, stroke: "var(--muted)", "stroke-width": 1, "stroke-dasharray": "2 3", visibility: "hidden"}, s);
  const hit = svg("rect", {x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent"}, s);
  hit.addEventListener("mousemove", ev => {
    const r = s.getBoundingClientRect(), sx = (ev.clientX - r.left) / r.width * W;
    const xv = x0 + (sx - L) / (W - L - R) * (x1 - x0);
    cross.setAttribute("x1", sx); cross.setAttribute("x2", sx); cross.setAttribute("visibility", "visible");
    let rows = "";
    for (const se of series) {
      let best = null;
      for (const p of se.pts) if (best == null || Math.abs(p[0] - xv) < Math.abs(best[0] - xv)) best = p;
      if (best && Math.abs(best[0] - xv) <= Math.max(2, (x1 - x0) / 150))
        rows += `<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${se.color};margin-right:6px"></i><span class="k">${esc(se.name)}</span> ${best[1] == null ? "–" : fmtY(best[1]) + unit}</div>`;
    }
    tip.innerHTML = `<div class="k">${xLabel} ${Math.round(xv)}</div>` + rows;
    tip.style.display = rows ? "block" : "none";
    const host = wrap.getBoundingClientRect();
    tip.style.left = (window.scrollX + host.left + Math.min(sx / W * host.width + 14, host.width - 170)) + "px";
    tip.style.top = (window.scrollY + ev.clientY - 20) + "px";
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
  if (series.length > 1) {
    const lg = document.createElement("div"); lg.className = "legend";
    lg.innerHTML = series.map(se => `<span><i style="background:${se.color}"></i>${esc(se.name)}</span>`).join("");
    wrap.appendChild(lg);
  }
  return wrap;
}
function chartBox(host, title) {
  const d = document.createElement("div"); d.className = "chart";
  d.innerHTML = `<div class="title">${title}</div>`; host.appendChild(d); return d;
}
function table(cols, rows, cls = "") {
  return `<div class="wrap"><table class="${cls}"><thead><tr>${cols.map(c => `<th class="${c.l ? "l" : ""}">${c.h}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(r => `<tr>${cols.map(c => { const v = c.v(r); return `<td class="${c.l ? "l" : ""} ${c.cls ? c.cls(r) : ""}">${v}</td>`; }).join("")}</tr>`).join("") +
    `</tbody></table></div>`;
}

const R = D.relays, NS6 = [1, 2, 3, 4, 5, 6];
const byN = (r, n) => r.summary.find(x => x.n === n);
const peak = r => r.summary.reduce((a, x) => x.agg != null && (a == null || x.agg > a.agg) ? x : a, null);
const name = r => `${r.label === "s8b" ? "s-8 (run 2)" : r.label === "s8" ? "s-8 (run 1)" : r.size.replace(/vcpu.*/, "")}`;
const short = r => r.label.replace(/^c(\d+)$/, "c-$1").replace(/^s(\d+)b$/, "s-$1 run 2").replace(/^s(\d+)$/, "s-$1");
const app = $("#app");
let H = "";
const first = R[0]?.stack || {};
H += `<h1>Relay size sweep: one relay, 1 to 6 concurrent clients</h1>
<p class="sub">Fleet <b>${esc(D.name)}</b>, DigitalOcean lon1, 2026-10-07 · report generated ${esc(D.generated)}</p>
<nav>${["findings", "setup", "summary", "relays", "drill", "machines", "timing", "gaps", "raw"].map(a => `<a href="#${a}">${a}</a>`).join("")}</nav>`;

H += `<h2 id="findings">Findings</h2><ul>
<li><b>Where one relay degrades.</b> c-16 holds up to 3 clients, about 32 Mbit/s in total. c-8 and s-8 hold up to 2 clients, about 15 to 16 Mbit/s. Relays with 4 vCPU or fewer cannot carry even one client at the rate a large relay gives it (about 9 Mbit/s): c-4 and s-4 deliver about 7.5, c-2 and s-2 about 4, s-1 about 2.8.</li>
<li><b>The limit is the relay's CPU.</b> On every CPU-optimized size the hoprd process levels off at 60 to 69 % of the machine, and s-1 runs its one core at 97 to 100 %. The openssl benchmarks show why: a c-N droplet scales like N/2 physical cores (multi-thread speed-up 1.07x on c-2, 2.2x on c-4, 4.3x on c-8, 8.5x on c-16), so 60 to 69 % of its vCPUs is the whole machine.</li>
<li><b>Basic against CPU-optimized.</b> s-4 matches c-4. s-8 lands about 10 % below c-8. Its two droplets benchmarked 1,755 and 2,394 MB/s over all threads and still delivered the same 14 to 15 Mbit/s.</li>
<li><b>Steal time on the shared sizes.</b> While loaded, the CPU-optimized relays lost almost nothing to the hypervisor (mean steal 0 to 0.6 %). The basic relays did: s-8 3.3 % and 4.4 % on average (p95 9 and 12 %, peaks 28 and 23 %), s-2 1.4 %, s-4 0.4 % (peak 17 %), s-1 0.5 % (peak 36 %). That is part of why s-8 stays below c-8.</li>
<li><b>Latency degrades before throughput.</b> The median ping through the tunnel passes 1 s at 2 clients on the 8-vCPU relays, and is already above 1 s with one client on 4 vCPU and fewer.</li>
<li><b>Gaps.</b> s-8 rungs 4 to 6 (both runs) and s-2 rungs 2 to 6 are missing. In those runs one client never resolved the packet keys of its exit's channel and failed every connect: a client-side chain-indexing problem, not relay load (see <a href="#gaps">gaps</a>).</li>
</ul>`;

H += `<h2 id="setup">Setup</h2>
<p>Topology T34-single-relay-scaling: n clients, each with its own exit, all through one relay in both directions (client k → relay → exit k, return exit k → relay → client k), pinned by the channel graph alone (each client and exit holds exactly one open channel, to the relay). One rung per n = 1 to 6: all n clients connect, idle 10 s, then download 100 MB from a target behind their exit at one common start second, capped at 300 s. Every probe pings the target through the tunnel once a second for the length of its transfer. Rates are over the overlap window (from the last first byte to the first last byte).</p>
<div class="chips">
<span>hoprd <b>${esc(first.hoprd?.relay)}</b> (relay and exits)</span><span>client <b>${esc(first.client)}</b></span><span>image <b>${esc(first.client_image)}</b></span>
<span>clients and exits <b>12 × c-8</b></span><span>control (chain, runner) <b>c-8</b></span><span>region <b>lon1</b></span>
<span>knobs ${Object.entries(D.knobs).map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>`).join(" ")}</span>
</div>
<p>Each relay was created ahead of its turn from plain Ubuntu 24.04, specs and a CPU benchmark taken, then its hoprd started, funded and announced on the running chain; no channel to it was opened before its turn (any open channel to a relay would let traffic cross it). Each swap restarted the six exits and the clients with fresh identities. The relay droplet was deleted right after its test.</p>`;

// summary
const pk = Object.fromEntries(R.map(r => [r.label, peak(r)]));
H += `<h2 id="summary">Summary</h2><h3>Aggregate download, Mbit/s</h3>
<p class="note">All clients together over the window in which all were downloading; in brackets the mean per client; <span class="bad">x/n</span>: transfers that finished 100 MB within 300 s; bold: the rung with the highest aggregate.</p>` +
  table([{h: "relay", v: r => `<b>${short(r)}</b>`, l: 1}, {h: "size", v: r => `<span class="dim">${esc(r.size)}</span>`, l: 1},
    ...NS6.map(n => ({h: `n=${n}`, v: r => { const x = byN(r, n); if (!x) return '<span class="dim">–</span>';
      return `${f(x.agg)} <span class="dim">(${f(x.mean)})</span>${x.complete < n ? ` <span class="bad">${x.complete}/${n}</span>` : ""}`; },
      cls: r => pk[r.label] && pk[r.label].n === n ? "peak" : ""}))], R);
H += `<div id="ceil"></div>`;
H += `<h3>Ping through the tunnel under load, median / p95 ms (loss %)</h3>` +
  table([{h: "relay", v: r => `<b>${short(r)}</b>`, l: 1}, ...NS6.map(n => ({h: `n=${n}`, v: r => { const x = byN(r, n); return x ? `${f(x.ping_med, 0)} / ${f(x.ping_p95, 0)} <span class="dim">(${f(x.ping_loss)})</span>` : "–"; }}))], R);
H += `<h3>Relay machine CPU % (all its vCPUs) / hoprd process in cores</h3>` +
  table([{h: "relay", v: r => `<b>${short(r)}</b>`, l: 1}, ...NS6.map(n => ({h: `n=${n}`, v: r => { const x = byN(r, n); return x ? `${f(x.relay_cpu)} <span class="dim">/ ${x.relay_proc == null ? "–" : (x.relay_proc / 100).toFixed(2)}</span>` : "–"; }}))], R);
H += `<h3>Relay machine UDP receive-buffer drops during the download (RcvbufErrors)</h3>` +
  table([{h: "relay", v: r => `<b>${short(r)}</b>`, l: 1}, ...NS6.map(n => ({h: `n=${n}`, v: r => { const x = byN(r, n); return x ? f(x.udp_rcvbuf, 0) : "–"; }}))], R);
H += `<h3>Relay: the droplet behind each row</h3>` +
  table([{h: "relay", v: r => `<b>${short(r)}</b>`, l: 1}, {h: "droplet", v: r => esc(r.droplet), l: 1}, {h: "id", v: r => esc(r.droplet_id)},
    {h: "class", v: r => esc(r.kind), l: 1}, {h: "CPU", v: r => esc((D.machines.find(m => m.droplet === r.droplet) || {}).cpu_model), l: 1},
    {h: "sha256 MB/s 1 thread", v: r => f((D.machines.find(m => m.droplet === r.droplet) || {}).sha256_1thread_MBps)},
    {h: "all threads", v: r => f((D.machines.find(m => m.droplet === r.droplet) || {}).sha256_allthreads_MBps)},
    {h: "speed-up", v: r => f((D.machines.find(m => m.droplet === r.droplet) || {}).thread_scaling, 2)},
    {h: "verdict", v: r => r.fails.length ? `<span class="bad">${r.fails.length} FAIL</span>` : "pass"}], R);

// per relay
H += `<h2 id="relays">Per relay</h2><p>Per second, from the probes (download rate summed over the rung's clients; ping: mean round trip of the echoes sent in that second) and from <code>vmstat 1</code> on the relay (busy = user + system; steal = time the hypervisor gave to others). Shaded: the rungs.</p>`;
R.forEach((r, i) => { H += `<h3 id="r-${r.label}">${short(r)} · ${esc(r.size)} · ${esc(r.kind)} · <span class="dim mono">${esc(r.droplet)}</span></h3><div id="rel-${i}"></div>`; });

H += `<h2 id="drill">Per client, per second</h2><div class="controls">relay <select id="dr"></select> clients <select id="dn"></select></div><div id="drillbox"></div>`;
H += `<h2 id="machines">Machines</h2><p>Every droplet of the sweep, with the CPU it landed on and its benchmark (openssl speed, 16 KB blocks). Speed-up: all threads over one thread. Idle steal: mean of the <code>st</code> column over 10 s before any load.</p><div id="mtab"></div>`;
H += `<h2 id="timing">Timing</h2><div id="ttab"></div>`;
H += `<h2 id="gaps">Gaps and notes</h2><ul>
<li><b>Missing rungs.</b> s-8 run 1 and run 2: n = 4 to 6 (client 4 could not connect to exit 4; in run 2 client 5 also failed at n = 5 and 6). s-2: n = 2 to 6 (client 2 could not connect to exit 2). In both cases the channels were Open and every node was connected to every other, but the client's own chain view never resolved the packet keys of its exit's channel ("could not find packet keys for channel endpoints", "cannot find 1 hop path"), so it could not build a path. It happened in 3 of 9 bring-ups, to one client each time, and not with the relay's size. A per-client health check after each bring-up (restart the client that fails it) would close the gap.</li>
<li><b>Two s-2 droplets.</b> The first one was deleted before its test: DigitalOcean gave it the private address of the deleted c-16 relay and the old host key blocked ssh. Its specs are kept under <code>…-first-droplet-607053195-not-tested</code>. The measured s-2 is droplet ${esc((R.find(r => r.label === "s2") || {}).droplet_id)}.</li>
<li><b>Two s-8 runs.</b> Run 2 (s8b) was a fresh droplet, made to fill rungs 4 to 6. It reproduced rungs 1 to 3 within 1 Mbit/s and lost rungs 4 to 6 the same way.</li>
<li><b>Mixed CPUs.</b> The twelve c-8 client and exit droplets landed on three generations (Xeon 8168, 8280, 8358). Per-client results are in each relay's client table and the per-client chart.</li>
<li><b>Timeouts.</b> A transfer that did not finish within 300 s counts its bytes up to then. Some ended earlier on a TCP read timeout after about 2 minutes without progress (c-2 at n = 4, n = 6).</li>
</ul>`;
app.innerHTML = H;

// ceiling bars
(() => {
  const host = $("#ceil"), W = 720, rowH = 22, L = 120, Rr = 60, rows = R.filter(r => pk[r.label]);
  const Hh = rows.length * rowH + 24, mx = niceMax(Math.max(...rows.map(r => pk[r.label].agg)));
  const box = chartBox(host, "Ceiling: highest aggregate download of any rung, Mbit/s");
  const s = svg("svg", {viewBox: `0 0 ${W} ${Hh}`}, box);
  rows.forEach((r, i) => {
    const y = 6 + i * rowH, w = pk[r.label].agg / mx * (W - L - Rr);
    const t = svg("text", {x: L - 8, y: y + 13, "text-anchor": "end"}, s); t.textContent = short(r);
    svg("rect", {x: L, y: y + 3, width: Math.max(2, w), height: 14, rx: 3, fill: "var(--s1)"}, s);
    const v = svg("text", {x: L + w + 6, y: y + 14}, s); v.textContent = `${f(pk[r.label].agg)} (n=${pk[r.label].n})`;
    v.setAttribute("style", "fill: var(--text-2)");
  });
})();

// per relay detail
R.forEach((r, i) => {
  const host = $("#rel-" + i);
  const tl = r.timeline || {};
  let h = `<div class="chips"><span>swap start <b>${esc(tl.swap_start || "–")}</b></span><span>test <b>${esc(tl.test_start || "–")}</b> → <b>${esc(tl.test_end || "–")}</b> (local time)</span><span>clocks within <b>${f(r.stack?.clock?.worst_s, 2)} s</b> of the runner</span></div>`;
  h += table([{h: "n", v: x => x.n, l: 1}, {h: "start (UTC)", v: x => esc(x.start || "–")}, {h: "agg Mbit/s", v: x => f(x.agg)}, {h: "per client mean", v: x => f(x.mean)}, {h: "min", v: x => f(x.min)},
    {h: "done", v: x => x.complete < x.n ? `<span class="bad">${x.complete}/${x.n}</span>` : `${x.complete}/${x.n}`}, {h: "slowest s", v: x => f(x.slowest_s)},
    {h: "window s", v: x => f(x.window_s)}, {h: "5-s buckets min/med/max", v: x => x.buckets ? x.buckets.map(v => f(v)).join(" / ") : "–"}, {h: "longest stall s", v: x => f(x.stall_max_s)},
    {h: "ping med", v: x => f(x.ping_med, 0)}, {h: "p95", v: x => f(x.ping_p95, 0)}, {h: "loss %", v: x => f(x.ping_loss)}, {h: "echoes", v: x => f(x.pings, 0)},
    {h: "relay CPU %", v: x => f(x.relay_cpu)}, {h: "hoprd cores", v: x => x.relay_proc == null ? "–" : (x.relay_proc / 100).toFixed(2)},
    {h: "exits CPU % mean/max", v: x => `${f(x.exits_cpu)} / ${f(x.exits_cpu_max)}`}, {h: "clients CPU % mean/max", v: x => `${f(x.clients_cpu)} / ${f(x.clients_cpu_max)}`},
    {h: "UDP rcvbuf / sndbuf / in", v: x => `${f(x.udp_rcvbuf, 0)} / ${f(x.udp_sndbuf, 0)} / ${f(x.udp_in, 0)}`},
    {h: "relay wire rx / tx MB", v: x => `${f(x.wire_rx_mb, 0)} / ${f(x.wire_tx_mb, 0)}`}, {h: "packets forwarded", v: x => f(x.forwarded, 0)},
    {h: "frames discarded", v: x => f(x.frames_discarded, 0)}, {h: "reconnects (ping timeouts)", v: x => `${f(x.reconnects, 0)} (${f(x.ping_timeouts, 0)})`},
    {h: "connect ms", v: x => (x.connect_ms || []).map(v => v ?? "–").join(", ")}], r.summary);
  if (r.missing.length) h += `<p class="note bad">Rungs not measured: n = ${r.missing.join(", ")}.</p>`;
  if (r.fails.length) h += `<ul class="fails">${r.fails.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
  host.innerHTML = h;
  const g = document.createElement("div"); g.className = "grid2"; host.appendChild(g);
  const ns = Object.keys(r.per).map(Number);
  lineChart(chartBox(g, "Aggregate download per second, Mbit/s (by rung)"), {xLabel: "s since rung start", yLabel: "Mbit/s", unit: " Mbit/s",
    series: ns.map(n => ({name: `n=${n}`, color: NCOL(n), pts: r.per[n].agg.map(a => [a[0], a[1]])}))});
  lineChart(chartBox(g, "Ping per second, mean of the rung's clients, ms"), {xLabel: "s since rung start", yLabel: "ms", unit: " ms", fmtY: v => f(v, 0),
    series: ns.map(n => ({name: `n=${n}`, color: NCOL(n), pts: r.per[n].agg.map(a => [a[0], a[2]])}))});
  if (r.relay_vm.length) {
    const w0 = r.windows.length ? r.windows[0][1] : 0;
    const bands = r.windows.map(w => ({x0: w[1] - w0, x1: w[2] - w0, label: `n=${w[0]}`}));
    lineChart(chartBox(g, "Relay CPU per second, % of all vCPUs"), {xLabel: "s since first rung", yLabel: "%", yMax: 100, unit: " %", fmtY: v => f(v, 0), bands,
      series: [{name: "busy (user + system)", color: SCOL(1), pts: r.relay_vm.map(x => [x[0], x[1] + x[2]])}, {name: "steal", color: SCOL(2), pts: r.relay_vm.map(x => [x[0], x[3]])}]});
    lineChart(chartBox(g, "Relay free memory per second, MB"), {xLabel: "s since first rung", yLabel: "MB", unit: " MB", fmtY: v => f(v, 0), bands,
      series: [{name: "free", color: SCOL(3), pts: r.relay_vm.map(x => [x[0], x[4]])}]});
  }
  const d1 = document.createElement("details");
  d1.innerHTML = `<summary>Per-client results (${r.clients.length} transfers)</summary>` + table([{h: "n", v: x => x.n, l: 1}, {h: "client", v: x => x.client, l: 1}, {h: "exit", v: x => x.exit, l: 1},
    {h: "client CPU", v: x => esc((D.machines.find(m => m.droplet === `gvpn-${D.name}-${x.client}`) || {}).cpu_model?.replace(/Intel\(R\) Xeon\(R\) Platinum /, "").replace(/ CPU.*/, "")), l: 1},
    {h: "exit CPU", v: x => esc((D.machines.find(m => m.droplet === `gvpn-${D.name}-${x.exit}`) || {}).cpu_model?.replace(/Intel\(R\) Xeon\(R\) Platinum /, "").replace(/ CPU.*/, "")), l: 1},
    {h: "MB", v: x => f(x.bytes_mb)}, {h: "complete", v: x => x.complete ? "yes" : `<span class="bad">no</span>`}, {h: "first→last byte s", v: x => f(x.secs)},
    {h: "overlap Mbit/s", v: x => f(x.overlap_mbit, 2)}, {h: "longest stall s", v: x => f(x.stall_s)}, {h: "tunnel rx MB", v: x => f(x.tunnel_rx_mb)},
    {h: "reconnects", v: x => f(x.reconnects, 0)}, {h: "ping timeouts", v: x => f(x.ping_timeouts, 0)}, {h: "frames discarded", v: x => f(x.frames_discarded, 0)},
    {h: "warn/error lines", v: x => f(x.warn_error_lines, 0)}, {h: "error", v: x => esc(x.error || ""), l: 1}], r.clients);
  host.appendChild(d1);
  const d2 = document.createElement("details");
  const vs = Object.entries(r.vm_sum).filter(([, v]) => v);
  d2.innerHTML = `<summary>System stats of every machine during this test (vmstat 1, ${vs.length} machines)</summary>` + table([{h: "machine", v: x => esc(x[0]), l: 1},
    {h: "samples", v: x => x[1].samples}, {h: "busy % mean", v: x => f(x[1].busy_mean)}, {h: "p95", v: x => f(x[1].busy_p95, 0)}, {h: "max", v: x => f(x[1].busy_max, 0)},
    {h: "user % mean", v: x => f(x[1].us_mean)}, {h: "system % mean", v: x => f(x[1].sy_mean)}, {h: "iowait max", v: x => f(x[1].wa_max, 0)},
    {h: "steal % mean", v: x => f(x[1].st_mean, 2)}, {h: "steal max", v: x => f(x[1].st_max, 0)}, {h: "run queue mean/max", v: x => `${f(x[1].runq_mean)} / ${x[1].runq_max}`},
    {h: "free MB mean", v: x => f(x[1].free_mean_mb, 0)}, {h: "free MB min", v: x => f(x[1].free_min_mb, 0)}], vs);
  host.appendChild(d2);
  const d3 = document.createElement("details");
  d3.innerHTML = `<summary>CPU % of every machine per rung (over the overlap window)</summary>` + (() => {
    const names = [...new Set(r.summary.flatMap(x => Object.keys(x.machine_cpu || {})))].sort();
    return table([{h: "machine", v: m => esc(m), l: 1}, ...r.summary.map(x => ({h: `n=${x.n}`, v: m => f((x.machine_cpu || {})[m])}))], names);
  })();
  host.appendChild(d3);
});

// drill-down
(() => {
  const dr = $("#dr"), dn = $("#dn"), box = $("#drillbox");
  dr.innerHTML = R.map((r, i) => `<option value="${i}">${short(r)}</option>`).join("");
  function fillN() { const r = R[+dr.value]; dn.innerHTML = Object.keys(r.per).map(n => `<option>${n}</option>`).join(""); dn.value = Object.keys(r.per).pop(); }
  function draw() {
    const r = R[+dr.value], p = r.per[dn.value]; box.innerHTML = "";
    if (!p) return;
    const g = document.createElement("div"); g.className = "grid2"; box.appendChild(g);
    const ks = Object.keys(p.clients).map(Number).sort((a, b) => a - b);
    lineChart(chartBox(g, "Download per second per client, Mbit/s"), {xLabel: "s since rung start", yLabel: "Mbit/s", unit: " Mbit/s",
      series: ks.map(k => ({name: `client-${k}`, color: SCOL(k), pts: p.clients[k].map((x, s) => [s, x[0]])}))});
    lineChart(chartBox(g, "Ping per second per client, ms (gap: echo lost)"), {xLabel: "s since rung start", yLabel: "ms", unit: " ms", fmtY: v => f(v, 0),
      series: ks.map(k => ({name: `client-${k}`, color: SCOL(k), pts: p.clients[k].map((x, s) => [s, x[1] == null || x[1] < 0 ? null : x[1]])}))});
  }
  dr.onchange = () => { fillN(); draw(); }; dn.onchange = draw; fillN(); draw();
})();

// machines
$("#mtab").innerHTML = table([{h: "droplet", v: m => esc(m.droplet), l: 1}, {h: "id", v: m => esc(m.droplet_id)}, {h: "role", v: m => esc(m.role), l: 1},
  {h: "size", v: m => esc(m.size), l: 1}, {h: "vCPU", v: m => esc(m.vcpus)}, {h: "RAM MB", v: m => esc(m.memory_mb)}, {h: "disk GB", v: m => esc(m.disk_gb)},
  {h: "CPU model", v: m => esc(m.cpu_model), l: 1}, {h: "MHz", v: m => esc(Math.round(+m.cpu_mhz) || "")}, {h: "L2", v: m => esc(m.l2_cache)}, {h: "L3", v: m => esc(m.l3_cache)},
  {h: "MemTotal MB", v: m => esc(Math.round(+m.mem_total_kb / 1024) || "")}, {h: "sha256 1 thread MB/s", v: m => esc(m.sha256_1thread_MBps)},
  {h: "all threads", v: m => esc(m.sha256_allthreads_MBps)}, {h: "speed-up", v: m => esc(m.thread_scaling)}, {h: "idle steal %", v: m => esc(m.idle_steal_pct_mean)},
  {h: "created", v: m => esc(m.created), l: 1}, {h: "note", v: m => esc(m.note), l: 1}], D.machines);

// timing
$("#ttab").innerHTML = `<p class="note">Local time (EDT). Swap: exits restarted with fresh identities, clients started, channels to the relay opened. Rungs run to their cap on weak relays, so weak relays take longest.</p>` +
  table([{h: "relay", v: r => `<b>${short(r)}</b>`, l: 1}, {h: "swap start", v: r => esc(r.timeline.swap_start || "–")}, {h: "test start", v: r => esc(r.timeline.test_start || "–")},
    {h: "test end", v: r => esc(r.timeline.test_end || "–")},
    {h: "swap min", v: r => { const t = r.timeline; if (!t.swap_start || !t.test_start) return "–"; const s = x => x.split(":").reduce((a, b) => a * 60 + +b, 0); return ((s(t.test_start) - s(t.swap_start)) / 60).toFixed(1); }},
    {h: "test min", v: r => { const t = r.timeline; if (!t.test_end || !t.test_start) return "–"; const s = x => x.split(":").reduce((a, b) => a * 60 + +b, 0); return ((s(t.test_end) - s(t.test_start)) / 60).toFixed(1); }}], R);
</script>
</body>
</html>
