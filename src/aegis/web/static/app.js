// Aegis web dashboard — vanilla JS, no build step.

const BASE = ""; // same-origin

const STEP_ORDER = [
  ["step310_args.run",   "310 — argument inspection"],
  ["step320_blast.run",  "320 — blast radius"],
  ["step330_human.run",  "330 — human oversight"],
  ["step335_cost.run",   "335 — forecasted cost"],
  ["step340_policy.run", "340 — policy + sLLM"],
];

const ATV_BANDS = [
  { name: "header",        start:    0, end:   64, color: "#64748b" },
  { name: "agent_state",   start:   64, end:  576, color: "#6366f1" },
  { name: "plan",          start:  576, end: 1088, color: "#0ea5e9" },
  { name: "tool_call",     start: 1088, end: 1472, color: "#14b8a6" },
  { name: "safety_flags",  start: 1472, end: 1728, color: "#f59e0b" },
  { name: "memory_fp",     start: 1728, end: 1864, color: "#a855f7" },
  { name: "cost_eff",      start: 1864, end: 1880, color: "#ec4899" },
  { name: "hw (zero, T2)", start: 1880, end: 2080, color: "#e2e8f0" },
];

const DECISION_STYLE = {
  ALLOW:            { bg: "bg-green-100",  text: "text-green-800",  dot: "bg-green-500" },
  BLOCK:            { bg: "bg-red-100",    text: "text-red-800",    dot: "bg-red-500" },
  REQUIRE_APPROVAL: { bg: "bg-amber-100",  text: "text-amber-800",  dot: "bg-amber-500" },
};

const PRESETS = {
  "safe-read": {
    tool: "read_file",
    args: '{"path":"./data/report.txt"}',
    plan: "read the Q3 report",
    inj: 0.02, pii: 0.0,
    bytes: 0, dollars: 0.0001, conf: 0.95,
  },
  "sql-drop": {
    tool: "db_query",
    args: '{"sql":"DROP TABLE users"}',
    plan: "destructive SQL test",
    inj: 0.05, pii: 0.0,
    bytes: 0, dollars: 0.0001, conf: 0.9,
  },
  "big-write": {
    tool: "write_file",
    args: '{"path":"./data/big.bin","content":"[placeholder]"}',
    plan: "write archive blob",
    inj: 0.02, pii: 0.0,
    bytes: 5e9, dollars: 0.01, conf: 0.8,
  },
  "transfer": {
    tool: "transfer_funds",
    args: '{"from":"acct-A","to":"acct-B","amount":500}',
    plan: "pay vendor invoice",
    inj: 0.0, pii: 0.0,
    bytes: 0, dollars: 0.001, conf: 0.95,
  },
  "external-api": {
    tool: "call_external_api",
    args: '{"url":"https://api.weather.example/forecast","method":"GET"}',
    plan: "fetch weather forecast for Seoul",
    inj: 0.01, pii: 0.0,
    bytes: 0, dollars: 0.0001, conf: 0.9,
  },
};

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);

function hex2rgb(h) {
  const v = h.replace("#", "");
  return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
}

async function service_check() {
  try {
    const r = await fetch("/healthz");
    if (!r.ok) throw new Error();
    const body = await r.json();
    $("service-dot").className = "inline-block w-2 h-2 rounded-full bg-green-500";
    $("service-status").textContent = "service healthy";
    $("version").textContent = body.version || "?";
  } catch {
    $("service-dot").className = "inline-block w-2 h-2 rounded-full bg-red-500";
    $("service-status").textContent = "service unreachable";
  }
}

// ---------- render the pipeline ----------
function render_pipeline_idle() {
  const el = $("pipeline");
  el.innerHTML = "";
  for (const [key, label] of STEP_ORDER) {
    el.insertAdjacentHTML("beforeend", `
      <div class="flex items-center gap-3 px-3 py-2 rounded bg-slate-50 border border-slate-200" data-step="${key}">
        <span class="w-3 h-3 rounded-full bg-slate-300"></span>
        <span class="text-sm font-medium text-slate-700 w-52 shrink-0">${label}</span>
        <span class="text-xs mono text-slate-400 truncate flex-1">—</span>
      </div>
    `);
  }
}

function render_pipeline(verdict) {
  const traces = verdict.step_traces || {};
  const el = $("pipeline");
  el.innerHTML = "";
  let terminated = false;
  for (const [key, label] of STEP_ORDER) {
    const traceKey = Object.keys(traces).find(k => k.endsWith(key));
    const trace = traceKey ? traces[traceKey] : null;
    let state = "pending";
    if (trace) {
      if (!terminated) {
        if (/sLLM\s+block|deny|dangerous|breach/i.test(trace))         state = "block";
        else if (/approval|budget|low\s+(cost|confidence)/i.test(trace)) state = "approval";
        else                                                             state = "pass";
      }
    }
    if (trace && (state === "block" || state === "approval")) terminated = true;

    const dot = {
      pass:     "bg-green-500",
      block:    "bg-red-500",
      approval: "bg-amber-500",
      pending:  "bg-slate-300",
    }[state];

    const bg = {
      pass:     "bg-green-50 border-green-200",
      block:    "bg-red-50 border-red-200",
      approval: "bg-amber-50 border-amber-200",
      pending:  "bg-slate-50 border-slate-200",
    }[state];

    el.insertAdjacentHTML("beforeend", `
      <div class="flex items-center gap-3 px-3 py-2 rounded border ${bg} ${trace ? 'pulse' : ''}">
        <span class="w-3 h-3 rounded-full ${dot}"></span>
        <span class="text-sm font-medium text-slate-700 w-52 shrink-0">${label}</span>
        <span class="text-xs mono text-slate-600 flex-1">${trace ?? "skipped"}</span>
      </div>
    `);
  }
}

// ---------- render verdict ----------
function render_verdict(v, ms) {
  $("latency").textContent = ms != null ? `${ms} ms` : "";
  const style = DECISION_STYLE[v.decision] || DECISION_STYLE.ALLOW;
  const badge = $("verdict-badge");
  badge.className = `px-2 py-0.5 rounded text-xs font-bold tracking-wide ${style.bg} ${style.text}`;
  badge.textContent = v.decision;
  $("verdict-reason").textContent = v.reason || "—";
  $("verdict-atvid").textContent = v.atv_id || "—";
  $("verdict-sig").textContent = v.signature || "—";
}

// ---------- render ATV strip (deterministic re-hash without re-embedding) ----------
function render_atv_strip(ctx /* {atv_id, decision, tool} */) {
  // We don't have the raw vector on the client, but we can visualize
  // the band structure + a deterministic intensity per band derived from
  // a hash of atv_id so each verdict produces a visually distinct strip.
  const strip = $("atv-strip");
  strip.innerHTML = "";
  const seed = ctx?.atv_id || "GENESIS";
  // simple hash → per-band intensity
  function h(s) {
    let x = 2166136261 >>> 0;
    for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619) >>> 0; }
    return x;
  }
  for (const band of ATV_BANDS) {
    const size = band.end - band.start;
    const rnd = h(seed + band.name);
    const intensity = band.name.startsWith("hw") ? 0.1 : 0.3 + (rnd % 700) / 1000;
    const [r, g, b] = hex2rgb(band.color);
    const cell = document.createElement("div");
    cell.className = "atv-cell";
    cell.style.gridColumn = `span ${size}`;
    cell.style.background = `rgba(${r},${g},${b},${intensity.toFixed(2)})`;
    cell.title = `${band.name}  [${band.start}..${band.end}]`;
    strip.appendChild(cell);
  }
  // legend
  const leg = $("atv-legend");
  leg.innerHTML = "";
  for (const band of ATV_BANDS) {
    leg.insertAdjacentHTML("beforeend", `
      <div class="flex items-center gap-2">
        <span class="inline-block w-3 h-3 rounded" style="background:${band.color}"></span>
        <span class="flex-1">${band.name}</span>
        <span class="mono text-slate-500">${band.end - band.start}-D</span>
      </div>
    `);
  }
}

// ---------- build /evaluate payload from form ----------
function build_payload() {
  const inj = parseFloat($("inj").value);
  const pii = parseFloat($("pii").value);
  return {
    header: {
      trace_id: crypto.randomUUID(),
      span_id:  crypto.randomUUID(),
      tenant_id: "demo-tenant",
      aid: $("aid").value || "web-ui-agent",
      ats: "ATV-2080-v1",
      timestamp_ns: Date.now() * 1_000_000,
    },
    agent_state_text: "web dashboard user-crafted call",
    plan_text: $("plan-text").value,
    tool_name: $("tool-name").value,
    tool_args_json: $("tool-args").value,
    safety_flags: { prompt_injection: inj, pii_exposure: pii },
    cost_estimate: {
      exp_bytes_write: parseFloat($("cost-bytes").value) || 0,
      exp_dollars: parseFloat($("cost-dollars").value) || 0,
      confidence: parseFloat($("cost-conf").value) || 0,
    },
  };
}

// ---------- evaluate ----------
async function evaluate() {
  const btn = $("btn-evaluate");
  btn.disabled = true; btn.textContent = "Evaluating…";
  const t0 = performance.now();
  try {
    const r = await fetch("/evaluate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(build_payload()),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
    }
    const v = await r.json();
    const ms = Math.round(performance.now() - t0);
    render_verdict(v, ms);
    render_pipeline(v);
    render_atv_strip({ atv_id: v.atv_id, decision: v.decision });
    $("audit-aid").value = $("aid").value;
    await load_chain();
  } catch (e) {
    $("verdict-reason").textContent = `error: ${e.message}`;
    $("verdict-badge").textContent = "ERR";
    $("verdict-badge").className = "px-2 py-0.5 rounded text-xs font-bold tracking-wide bg-red-200 text-red-800";
  } finally {
    btn.disabled = false; btn.textContent = "Evaluate →";
  }
}

// ---------- 5-call demo ----------
const DEMO_SCRIPT = [
  { tool: "read_file",     args: '{"path":"./data/report.txt"}',                           plan: "read Q3 report",        inj: .02, pii: .0, bytes: 0,   dollars: 0.0001, conf: .95 },
  { tool: "write_file",    args: '{"path":"./data/summary.txt","content":"..."}',           plan: "write summary",         inj: .02, pii: .0, bytes: 1024,dollars: 0.0002, conf: .9  },
  { tool: "execute_shell", args: '{"command":"rm -rf /"}',                                  plan: "cleanup",               inj: .02, pii: .0, bytes: 0,   dollars: 0.0001, conf: .9  },
  { tool: "write_file",    args: '{"path":"./data/big.bin","content":"[5GB]"}',             plan: "archive",               inj: .02, pii: .0, bytes: 5e9, dollars: 0.05,   conf: .7  },
  { tool: "transfer_funds",args: '{"from":"acct-A","to":"acct-B","amount":500}',            plan: "pay invoice",           inj: .0,  pii: .0, bytes: 0,   dollars: 0.001,  conf: .95 },
];

async function run_demo() {
  const btn = $("btn-demo");
  btn.disabled = true; btn.textContent = "Running…";
  const prevAid = $("aid").value;
  const aid = "demo-web-" + crypto.randomUUID().slice(0, 8);
  $("aid").value = aid;
  try {
    for (const [i, step] of DEMO_SCRIPT.entries()) {
      $("tool-name").value = step.tool;
      $("tool-args").value = step.args;
      $("plan-text").value = step.plan;
      $("inj").value = step.inj; $("inj-val").textContent = step.inj.toFixed(2);
      $("pii").value = step.pii; $("pii-val").textContent = step.pii.toFixed(2);
      $("cost-bytes").value = step.bytes;
      $("cost-dollars").value = step.dollars;
      $("cost-conf").value = step.conf;
      await evaluate();
      await new Promise(r => setTimeout(r, 450));
    }
  } finally {
    $("aid").value = aid;  // keep the demo aid so user can inspect the chain
    $("audit-aid").value = aid;
    btn.disabled = false; btn.textContent = "Run demo";
  }
}

// ---------- audit chain ----------
async function load_chain() {
  const aid = $("audit-aid").value.trim();
  if (!aid) return;
  const status = $("chain-status");
  status.textContent = "loading…";
  try {
    const r = await fetch(`/audit/${encodeURIComponent(aid)}`);
    const data = await r.json();
    render_chain(data);
  } catch (e) {
    status.textContent = `error: ${e.message}`;
  }
}

function render_chain(data) {
  const list = $("chain-list");
  list.innerHTML = "";
  const ok = data.chain_valid === true;
  $("chain-status").textContent = `length=${data.length}  head=${(data.head || "").slice(0,16)}…  ${ok ? "✓ valid" : "✗ broken"}`;
  $("chain-status").className = `text-xs mono ml-auto ${ok ? 'text-green-600' : 'text-red-600'}`;
  if (!data.length) {
    list.insertAdjacentHTML("beforeend", `<li class="text-slate-400 text-sm">no records</li>`);
    return;
  }
  for (const [i, rec] of data.chain.entries()) {
    const p = rec.payload || {};
    const decision = rec.decision || p.header?.decision || "—";
    const style = DECISION_STYLE[decision] || DECISION_STYLE.ALLOW;
    const when = p.signed_at_ns
      ? new Date(Number(BigInt(p.signed_at_ns) / 1_000_000n)).toISOString().replace("T", " ").slice(0, 19)
      : "";
    const tool = p.header?.tool_name || "";
    const prev = (p.prev_hash || "").slice(0, 12);
    const hash = (rec.this_hash || "").slice(0, 12);
    list.insertAdjacentHTML("beforeend", `
      <li class="flex items-center gap-3 px-3 py-2 rounded border border-slate-200 bg-slate-50">
        <span class="mono text-xs text-slate-400 w-8 text-right">#${i + 1}</span>
        <span class="${style.bg} ${style.text} text-xs font-bold px-2 py-0.5 rounded w-40 text-center">${decision}</span>
        <span class="mono text-sm text-slate-700 w-40 truncate">${tool || "<approval>"}</span>
        <span class="mono text-xs text-slate-500">${when}</span>
        <span class="mono text-xs text-slate-400 ml-auto">
          ${prev} <span class="text-slate-300">→</span> ${hash}
        </span>
      </li>
    `);
  }
}

// ---------- init ----------
function apply_preset(name) {
  const p = PRESETS[name]; if (!p) return;
  $("tool-name").value = p.tool;
  $("tool-args").value = p.args;
  $("plan-text").value = p.plan;
  $("inj").value = p.inj; $("inj-val").textContent = p.inj.toFixed(2);
  $("pii").value = p.pii; $("pii-val").textContent = p.pii.toFixed(2);
  $("cost-bytes").value = p.bytes;
  $("cost-dollars").value = p.dollars;
  $("cost-conf").value = p.conf;
}

function wire() {
  $("btn-evaluate").addEventListener("click", evaluate);
  $("btn-demo").addEventListener("click", run_demo);
  $("btn-audit").addEventListener("click", load_chain);
  for (const b of document.querySelectorAll(".preset")) {
    b.addEventListener("click", () => apply_preset(b.dataset.preset));
  }
  $("inj").addEventListener("input", e => $("inj-val").textContent = (+e.target.value).toFixed(2));
  $("pii").addEventListener("input", e => $("pii-val").textContent = (+e.target.value).toFixed(2));
}

document.addEventListener("DOMContentLoaded", () => {
  wire();
  render_pipeline_idle();
  render_atv_strip(null);
  service_check();
});
