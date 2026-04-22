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
    tokens: 100, dollars: 0.0001, forecast: 0.01,
  },
  "sql-drop": {
    tool: "db_query",
    args: '{"sql":"DROP TABLE users"}',
    plan: "destructive SQL test",
    inj: 0.05, pii: 0.0,
    tokens: 80, dollars: 0.0001, forecast: 0.01,
  },
  "big-write": {
    tool: "write_file",
    args: '{"path":"./data/big.bin","content":"[placeholder]"}',
    plan: "write archive blob",
    inj: 0.02, pii: 0.0,
    tokens: 500, dollars: 0.05, forecast: 5.0,
  },
  "transfer": {
    tool: "transfer_funds",
    args: '{"from":"acct-A","to":"acct-B","amount":500}',
    plan: "pay vendor invoice",
    inj: 0.0, pii: 0.0,
    tokens: 60, dollars: 0.001, forecast: 0.01,
  },
  "external-api": {
    tool: "call_external_api",
    args: '{"url":"https://api.weather.example/forecast","method":"GET"}',
    plan: "fetch weather forecast for Seoul",
    inj: 0.01, pii: 0.0,
    tokens: 80, dollars: 0.0001, forecast: 0.01,
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

// ---------- attestation ----------
function _hex_to_bytes(h) {
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.slice(2 * i, 2 * i + 2), 16);
  return out;
}

function _pem_to_spki(pem) {
  const b64 = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

async function verify_ed25519_in_browser(pubPem, sigHex, msgStr) {
  if (!crypto?.subtle?.importKey) return null;
  try {
    const key = await crypto.subtle.importKey(
      "spki", _pem_to_spki(pubPem), { name: "Ed25519" }, false, ["verify"]
    );
    return await crypto.subtle.verify(
      "Ed25519", key, _hex_to_bytes(sigHex), new TextEncoder().encode(msgStr)
    );
  } catch (e) {
    console.warn("browser Ed25519 verify unsupported:", e);
    return null;
  }
}

function render_layer_row(name, layer) {
  const present = layer.present ?? true;
  const dot = present ? "bg-green-500" : "bg-slate-300";
  const noteOrHash = layer.hash
    ? `<span class="mono text-xs text-slate-700 break-all">${layer.hash}</span>`
    : `<span class="text-xs text-slate-400 italic">${layer.note ?? ""}</span>`;
  let extras = "";
  if (name === "L3_code" && layer.files_counted != null) {
    extras = `<span class="text-xs text-slate-500">${layer.files_counted} .py files</span>`;
  } else if (name === "L4_config") {
    const polCount = Object.keys(layer.policies || {}).length;
    extras = `<span class="text-xs text-slate-500">embed=<b>${layer.embedding_provider}</b> · judge=<b>${layer.judge_provider}</b> · ${polCount} policies</span>`;
  } else if (name === "L5_key_binding" && layer.public_key_fingerprint) {
    extras = `<span class="text-xs text-slate-500">pubkey-fp ${layer.public_key_fingerprint.slice(0,12)}…</span>`;
  }
  return `
    <li class="flex items-start gap-3 px-3 py-2 rounded border border-slate-200 ${present ? 'bg-white' : 'bg-slate-50'}">
      <span class="w-2.5 h-2.5 rounded-full ${dot} mt-1.5 shrink-0"></span>
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-3">
          <span class="font-medium text-slate-700 mono text-sm">${name}</span>
          ${extras}
        </div>
        <div class="mt-0.5">${noteOrHash}</div>
      </div>
    </li>
  `;
}

async function load_attestation() {
  const status = $("attest-status");
  status.textContent = "loading…"; status.className = "text-xs mono ml-auto text-slate-400";
  try {
    const r = await fetch("/attestation");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const a = await r.json();
    $("attest-id").textContent = a.burn_in_id;
    $("attest-aegis-ver").textContent = a.aegis_version;
    $("attest-atv-ver").textContent = a.atv_version;
    $("attest-sig").textContent = a.signed.signature;

    const layers = $("attest-layers");
    layers.innerHTML = "";
    for (const name of ["L1_hardware_ek","L2_firmware","L3_code","L4_config","L5_key_binding"]) {
      layers.insertAdjacentHTML("beforeend", render_layer_row(name, a.layers[name] || {}));
    }

    // Client-side Ed25519 verification
    const verifyBadge = $("attest-verify");
    const ok = await verify_ed25519_in_browser(a.public_key_pem, a.signed.signature, a.burn_in_id);
    if (ok === true) {
      verifyBadge.textContent = "✓ verified (browser Ed25519)";
      verifyBadge.className = "text-xs px-2 py-0.5 rounded bg-green-100 text-green-800 font-medium";
    } else if (ok === false) {
      verifyBadge.textContent = "✗ signature INVALID";
      verifyBadge.className = "text-xs px-2 py-0.5 rounded bg-red-100 text-red-800 font-bold";
    } else {
      verifyBadge.textContent = "verify unsupported in this browser";
      verifyBadge.className = "text-xs px-2 py-0.5 rounded bg-slate-200 text-slate-600";
    }
    status.textContent = `burn_in ${a.burn_in_id.slice(0,16)}…`;
    status.className = "text-xs mono ml-auto text-slate-500";
  } catch (e) {
    status.textContent = `error: ${e.message}`;
    status.className = "text-xs mono ml-auto text-red-600";
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
    // ATV-2080-v1 cost_efficiency_metrics shape (¶[0045] s-1..s-16).
    // The dashboard exposes 3 of the 16 slots; the rest default to 0.
    cost_estimate: {
      input_token_count: parseFloat($("cost-tokens").value) || 0,
      cumulative_dollars: parseFloat($("cost-dollars").value) || 0,
      forecasted_cost_to_completion: parseFloat($("cost-forecast").value) || 0,
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
    load_burnin();    // refresh M11 sample counts after each evaluate
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
  { tool: "read_file",     args: '{"path":"./data/report.txt"}',                           plan: "read Q3 report",        inj: .02, pii: .0, tokens: 100, dollars: 0.0001, forecast: 0.01 },
  { tool: "write_file",    args: '{"path":"./data/summary.txt","content":"..."}',           plan: "write summary",         inj: .02, pii: .0, tokens: 170, dollars: 0.0002, forecast: 0.01 },
  { tool: "execute_shell", args: '{"command":"rm -rf /"}',                                  plan: "cleanup",               inj: .02, pii: .0, tokens: 80,  dollars: 0.0001, forecast: 0.01 },
  { tool: "write_file",    args: '{"path":"./data/big.bin","content":"[5GB]"}',             plan: "archive",               inj: .02, pii: .0, tokens: 500, dollars: 0.05,   forecast: 5.0  },
  { tool: "transfer_funds",args: '{"from":"acct-A","to":"acct-B","amount":500}',            plan: "pay invoice",           inj: .0,  pii: .0, tokens: 60,  dollars: 0.001,  forecast: 0.01 },
];

async function run_demo() {
  const btn = $("btn-demo");
  btn.disabled = true; btn.textContent = "Running…";
  const aid = "demo-web-" + crypto.randomUUID().slice(0, 8);
  $("aid").value = aid;
  try {
    for (const step of DEMO_SCRIPT) {
      $("tool-name").value = step.tool;
      $("tool-args").value = step.args;
      $("plan-text").value = step.plan;
      $("inj").value = step.inj; $("inj-val").textContent = step.inj.toFixed(2);
      $("pii").value = step.pii; $("pii-val").textContent = step.pii.toFixed(2);
      $("cost-tokens").value = step.tokens;
      $("cost-dollars").value = step.dollars;
      $("cost-forecast").value = step.forecast;
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
  $("cost-tokens").value = p.tokens;
  $("cost-dollars").value = p.dollars;
  $("cost-forecast").value = p.forecast;
}

// ---------- Burn-in baseline (M11) ----------
const PHASE_BADGE = {
  observation: "bg-slate-200 text-slate-700",
  shadow:      "bg-blue-100 text-blue-800",
  assisted:    "bg-amber-100 text-amber-800",
  production:  "bg-green-100 text-green-800",
};

async function load_burnin() {
  const status = $("burnin-status");
  const tbody = $("burnin-rows");
  status.textContent = "loading…";
  status.className = "text-xs text-slate-400 mono ml-auto";
  try {
    const r = await fetch("/burnin-status");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    tbody.innerHTML = "";
    if (!body.layers || body.layers.length === 0) {
      tbody.innerHTML = `
        <tr><td colspan="8" class="py-4 text-center text-slate-400 italic">
          no observations yet — run a /evaluate to populate layer slots
        </td></tr>`;
      status.textContent = "0 slots";
      return;
    }
    for (const layer of body.layers) {
      const badge = PHASE_BADGE[layer.phase] || "bg-slate-200 text-slate-700";
      tbody.insertAdjacentHTML("beforeend", `
        <tr class="border-b border-slate-100 hover:bg-slate-50">
          <td class="py-1.5 pr-3 mono font-bold text-slate-700">${layer.layer}</td>
          <td class="py-1.5 pr-3 mono text-slate-600 truncate max-w-xs" title="${layer.key}">${layer.key}</td>
          <td class="py-1.5 pr-3"><span class="px-2 py-0.5 rounded text-[11px] font-semibold ${badge}">${layer.phase}</span></td>
          <td class="py-1.5 pr-3 mono text-right">${layer.samples}</td>
          <td class="py-1.5 pr-3 mono text-right text-slate-500">${layer.tpr.toFixed(3)}</td>
          <td class="py-1.5 pr-3 mono text-right text-slate-500">${layer.fpr.toFixed(3)}</td>
          <td class="py-1.5 pr-3 mono text-right text-slate-500">${layer.precision.toFixed(3)}</td>
          <td class="py-1.5 mono text-right text-slate-500">${layer.override_rate.toFixed(3)}</td>
        </tr>
      `);
    }
    status.textContent = `${body.layers.length} layer slots`;
  } catch (e) {
    status.textContent = `error: ${e.message}`;
    status.className = "text-xs ml-auto text-red-600 mono";
  }
}

// ---------- AID admin (M14) ----------
async function load_aid_quarantine() {
  const status = $("aid-status");
  const list = $("aid-list");
  status.textContent = "loading…";
  status.className = "text-xs text-slate-400 mono ml-auto";
  try {
    const r = await fetch("/admin/aid");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    list.innerHTML = "";
    const q = body.quarantined || [];
    if (q.length === 0) {
      list.innerHTML = `<li class="text-slate-400 italic text-sm py-2">no AIDs quarantined — circuit closed for all agents.</li>`;
      status.textContent = "0 quarantined";
      return;
    }
    for (const item of q) {
      const ts = item.quarantined_at_ns
        ? new Date(Number(BigInt(item.quarantined_at_ns) / 1_000_000n)).toISOString().replace("T", " ").slice(0, 19)
        : "—";
      list.insertAdjacentHTML("beforeend", `
        <li class="flex items-center gap-3 px-3 py-2 rounded border border-red-200 bg-red-50">
          <span class="w-2 h-2 rounded-full bg-red-500 shrink-0"></span>
          <span class="mono font-semibold text-red-900 truncate">${item.aid}</span>
          <span class="text-xs text-red-700">${item.violations} violations</span>
          <span class="text-xs text-slate-500 ml-auto mono">${ts}</span>
          <button class="aid-fill text-xs bg-white border border-slate-300 hover:bg-slate-100 rounded px-2 py-0.5"
            data-aid="${item.aid}">use ↑</button>
        </li>
      `);
    }
    for (const b of document.querySelectorAll(".aid-fill")) {
      b.addEventListener("click", () => { $("aid-release-aid").value = b.dataset.aid; });
    }
    status.textContent = `${q.length} quarantined`;
    status.className = "text-xs ml-auto text-red-600 mono";
  } catch (e) {
    status.textContent = `error: ${e.message}`;
    status.className = "text-xs ml-auto text-red-600 mono";
  }
}

async function release_aid() {
  const aid = $("aid-release-aid").value.trim();
  const reason = $("aid-release-reason").value.trim() || "manual";
  const token = $("aid-admin-token").value;
  const result = $("aid-release-result");
  if (!aid) { result.textContent = "aid required"; result.className = "text-xs mono mt-2 text-red-600"; return; }
  try {
    const r = await fetch("/admin/aid/release", {
      method: "POST",
      headers: { "content-type": "application/json", "X-Aegis-Admin-Token": token },
      body: JSON.stringify({ aid, reason }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    result.textContent = `✓ released — status=${body.status}, violations counter retained=${body.violations}`;
    result.className = "text-xs mono mt-2 text-green-700";
    await load_aid_quarantine();
  } catch (e) {
    result.textContent = `✗ ${e.message}`;
    result.className = "text-xs mono mt-2 text-red-600";
  }
}

// ---------- Forensic replay (M15) ----------
async function load_replay() {
  const status = $("replay-status");
  status.textContent = "replaying…";
  status.className = "text-xs ml-auto text-slate-400 mono";
  try {
    const r = await fetch("/forensic/replay");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    if (!body.available) {
      status.textContent = body.reason || "unavailable";
      $("replay-ok").textContent = "—";
      $("replay-bad").textContent = "—";
      $("replay-aids").textContent = "—";
      $("replay-heads").innerHTML = "";
      return;
    }
    $("replay-ok").textContent = body.decrypted_count ?? 0;
    $("replay-bad").textContent = body.tampered_count ?? 0;
    $("replay-aids").textContent = (body.aids_seen || []).length;
    const heads = $("replay-heads");
    heads.innerHTML = "";
    for (const [aid, head] of Object.entries(body.per_aid_head || {})) {
      const valid = body.per_aid_chain_valid?.[aid] === true;
      const dot = valid ? "bg-green-500" : "bg-red-500";
      heads.insertAdjacentHTML("beforeend", `
        <li class="flex items-center gap-2 px-2 py-1 rounded border border-slate-200 bg-slate-50">
          <span class="w-2 h-2 rounded-full ${dot}"></span>
          <span class="font-semibold truncate w-44">${aid}</span>
          <span class="text-slate-500 truncate">${head.slice(0, 40)}…</span>
        </li>
      `);
    }
    const tampered = body.tampered_count || 0;
    status.textContent = tampered === 0
      ? `✓ all ${body.decrypted_count} records decrypted, chains valid`
      : `✗ ${tampered} tampered records`;
    status.className = `text-xs ml-auto mono ${tampered === 0 ? 'text-green-700' : 'text-red-600'}`;
  } catch (e) {
    status.textContent = `error: ${e.message}`;
    status.className = "text-xs ml-auto text-red-600 mono";
  }
}

// ---------- HAM (M16) ----------
function _ham_ids() {
  return { aid: $("ham-aid").value.trim(), tenant_id: $("ham-tenant").value.trim() };
}

function _split_csv(s) {
  return (s || "").split(",").map(x => x.trim()).filter(Boolean);
}

async function ham_store() {
  const out = $("ham-store-result");
  let body;
  try { body = JSON.parse($("ham-body").value); }
  catch (e) { out.textContent = `bad JSON: ${e.message}`; out.className = "text-xs mono break-all text-red-600"; return; }
  try {
    const r = await fetch("/ham/memory", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ..._ham_ids(), body, tags: _split_csv($("ham-tags").value) }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    out.innerHTML = `✓ object_id=<b>${j.object_id}</b> seq=${j.seq}`;
    out.className = "text-xs mono break-all text-green-700";
    // refresh recall + ground refs hint
    await ham_recall();
  } catch (e) {
    out.textContent = `✗ ${e.message}`;
    out.className = "text-xs mono break-all text-red-600";
  }
}

async function _ham_post(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

function _render_ham_items(items) {
  const list = $("ham-items");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = `<li class="text-slate-400 italic">no items</li>`;
    return;
  }
  for (const it of items) {
    const ts = new Date(Number(BigInt(it.ts_ns) / 1_000_000n)).toISOString().replace("T", " ").slice(11, 19);
    const tags = (it.tags || []).map(t => `<span class="bg-slate-200 text-slate-700 rounded px-1">${t}</span>`).join(" ");
    const bodyJson = it.body ? JSON.stringify(it.body) : "";
    list.insertAdjacentHTML("beforeend", `
      <li class="flex items-start gap-2 px-2 py-1.5 rounded border border-slate-200 bg-slate-50">
        <input type="checkbox" class="ham-pick mt-1" data-id="${it.object_id}" />
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2">
            <span class="mono text-slate-700 truncate">${it.object_id}</span>
            <span class="text-slate-400">${ts}</span>
            <span class="ml-auto">${tags}</span>
            <button class="ham-forget bg-red-100 hover:bg-red-200 text-red-800 rounded px-2 text-[10px]"
              data-id="${it.object_id}">forget</button>
          </div>
          <div class="mono text-[11px] text-slate-600 break-all">${bodyJson}</div>
        </div>
      </li>
    `);
  }
  for (const b of document.querySelectorAll(".ham-forget")) {
    b.addEventListener("click", () => ham_forget(b.dataset.id));
  }
  // auto-fill ref ids field with the currently checked items
  document.querySelectorAll(".ham-pick").forEach(cb =>
    cb.addEventListener("change", () => {
      $("ham-refs").value = Array.from(document.querySelectorAll(".ham-pick:checked"))
        .map(x => x.dataset.id).join(",");
    })
  );
}

async function ham_recall() {
  const status = $("ham-status");
  status.textContent = "recalling…";
  status.className = "text-xs text-slate-400 mono ml-auto";
  try {
    const j = await _ham_post("/ham/recall", {
      ..._ham_ids(),
      tags: _split_csv($("ham-recall-tags").value),
      limit: 20,
    });
    _render_ham_items(j.items || []);
    status.textContent = `${j.length} items`;
    status.className = "text-xs ml-auto text-slate-600 mono";
  } catch (e) {
    status.textContent = `error: ${e.message}`;
    status.className = "text-xs ml-auto text-red-600 mono";
  }
}

async function ham_context() {
  try {
    const j = await _ham_post("/ham/context", { ..._ham_ids(), max_items: 5 });
    _render_ham_items((j.bundle && j.bundle.items) || []);
    $("ham-status").textContent = `bundle = ${(j.source_ids || []).length} items`;
    $("ham-status").className = "text-xs ml-auto text-indigo-700 mono";
  } catch (e) {
    $("ham-status").textContent = `error: ${e.message}`;
    $("ham-status").className = "text-xs ml-auto text-red-600 mono";
  }
}

async function ham_forget(object_id) {
  try {
    await _ham_post("/ham/forget", { ..._ham_ids(), object_id, reason: "user dashboard" });
    await ham_recall();
  } catch (e) {
    alert(`forget failed: ${e.message}`);
  }
}

async function ham_summarize() {
  try {
    const j = await _ham_post("/ham/summarize", { ..._ham_ids() });
    $("ham-summary").textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    $("ham-summary").textContent = `error: ${e.message}`;
  }
}

async function ham_ground() {
  const claim = $("ham-claim").value.trim();
  const refs = _split_csv($("ham-refs").value);
  if (!claim || !refs.length) { $("ham-ground-result").textContent = "claim + ref ids required"; return; }
  try {
    const j = await _ham_post("/ham/ground", { ..._ham_ids(), claim, reference_ids: refs });
    $("ham-ground-result").textContent =
      `claim_hash=${j.claim_hash.slice(0, 24)}…\n` +
      `bound=${j.references.length}, missing=${j.missing.length}`;
  } catch (e) {
    $("ham-ground-result").textContent = `error: ${e.message}`;
  }
}

function wire() {
  $("btn-evaluate").addEventListener("click", evaluate);
  $("btn-demo").addEventListener("click", run_demo);
  $("btn-audit").addEventListener("click", load_chain);
  $("btn-attest").addEventListener("click", load_attestation);
  $("btn-burnin").addEventListener("click", load_burnin);
  // M14
  $("btn-aid-refresh").addEventListener("click", load_aid_quarantine);
  $("btn-aid-release").addEventListener("click", release_aid);
  // M15
  $("btn-replay").addEventListener("click", load_replay);
  // M16
  $("btn-ham-store").addEventListener("click", ham_store);
  $("btn-ham-recall").addEventListener("click", ham_recall);
  $("btn-ham-context").addEventListener("click", ham_context);
  $("btn-ham-summarize").addEventListener("click", ham_summarize);
  $("btn-ham-ground").addEventListener("click", ham_ground);

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
  load_attestation();
  load_burnin();
  load_aid_quarantine();
  load_replay();
});
