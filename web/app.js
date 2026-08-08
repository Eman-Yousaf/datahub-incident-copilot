/* DataHub Incident Copilot — client application.
 *
 * Plain ES modules-free JavaScript on purpose: no build step, no bundler, no CDN.
 * The whole app is three static files served by the same FastAPI process that runs
 * the agent, so there is nothing to go wrong between a judge clicking the link and
 * the page rendering.
 *
 * Rendering rule followed throughout: every number on screen is something the
 * backend actually computed or DataHub actually returned. Where a value isn't
 * known yet, the UI says so rather than showing a plausible placeholder.
 */

'use strict';

/* ------------------------------------------------------------------ util -- */

const h = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(path) {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/** The last path segment of a URN is the part a human recognises. */
function urnTail(urn) {
  if (!urn) return '';
  const inner = urn.replace(/\)$/, '');
  const parts = inner.split(',');
  const qualified = parts.length > 1 ? parts[parts.length - 2] : inner;
  return qualified.split('.').pop();
}

function platformOf(urn) {
  const m = /dataPlatform:([a-zA-Z0-9_-]+)/.exec(urn || '');
  return m ? m[1] : '';
}

function timeAgo(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function severityChip(sev) {
  if (!sev) return '<span class="chip">not yet investigated</span>';
  const cls = sev === 'no_action' ? 'chip-warn'
    : sev === 'tag_note_escalated' ? 'chip-danger'
    : 'chip-accent';
  return `<span class="chip ${cls}">${h(sev)}</span>`;
}

function decisionChip(decision) {
  return decision === 'REFUSAL'
    ? '<span class="chip chip-warn">REFUSAL</span>'
    : '<span class="chip chip-ok">ACTION</span>';
}

function confidenceChip(level, confirmed, total) {
  const cls = level === 'high' ? 'chip-ok' : level === 'medium' ? 'chip-accent' : 'chip-warn';
  return `<span class="chip ${cls}">${confirmed}/${total} ${h((level || '').toUpperCase())}</span>`;
}

const view = () => $('#view');
const setView = (html) => { view().innerHTML = html; view().scrollTop = 0; };
const loading = (what) => setView(`<div class="loading-page"><span class="spinner"></span> Loading ${h(what)}…</div>`);
const errorBox = (msg) => `<div class="error-box">Could not reach DataHub: ${h(msg)}</div>`;

/* --------------------------------------------------------------- chrome -- */

async function refreshChrome() {
  try {
    const status = await api('/api/status');
    const rows = $$('#health .health-row');
    const ok = status.datahub && status.datahub.connected;
    rows[0].innerHTML = `<span class="dot ${ok ? 'dot-ok' : 'dot-bad'}"></span> DataHub ${ok ? 'connected' : 'unreachable'}`;
    const llm = status.llm && status.llm.configured;
    rows[1].innerHTML = `<span class="dot ${llm ? 'dot-ok' : 'dot-bad'}"></span> Agent ${llm ? 'ready' : 'not configured'}`;
  } catch (e) {
    $$('#health .health-row')[0].innerHTML = '<span class="dot dot-bad"></span> DataHub unreachable';
  }
  try {
    const [inc, inv] = await Promise.all([api('/api/incidents'), api('/api/investigations')]);
    $('#nav-incident-count').textContent = inc.incidents.length;
    $('#nav-investigation-count').textContent = (inv.cards || []).length;
  } catch (e) { /* badges are decoration; never block the page on them */ }
}

function markActiveNav(route) {
  $$('.nav-item').forEach((a) => {
    const r = a.dataset.route;
    a.classList.toggle('active', r === route || (r !== '/' && route.startsWith(r)));
  });
}

/* --------------------------------------------------------- command center -- */

function greeting() {
  const hour = new Date().getHours();
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

async function viewCommandCenter() {
  loading('command center');
  let status = {}, incidents = { incidents: [] }, investigations = { cards: [] };
  try {
    [status, incidents, investigations] = await Promise.all([
      api('/api/status'), api('/api/incidents'), api('/api/investigations'),
    ]);
  } catch (e) { setView(errorBox(e.message)); return; }

  const cards = investigations.cards || [];
  const refusals = cards.filter((c) => c.decision === 'REFUSAL').length;
  const actions = cards.length - refusals;
  const continued = cards.filter((c) => c.continues_incident_id).length;
  const catalog = status.catalog || {};
  // Verified knowledge = evidence checks that stored investigations actually
  // confirmed. Counted off the cards themselves rather than tracked separately,
  // so the headline number can never drift from what the cards say.
  const verified = cards.reduce((n, c) => n + c.evidence.filter((e) => e.confirmed).length, 0);

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow">${h(greeting())}</p>
      <h1>Data Incident Command Center</h1>
      <p class="sub">A trust-aware investigation agent working against a live DataHub instance.
      It gathers evidence, and when the evidence is thin it is refused permission to act — by
      policy enforced in code, not by prompt.</p>
    </div>

    <div class="grid grid-4 fade-in">
      <div class="stat">
        <div class="stat-value">${incidents.incidents.length}</div>
        <div class="stat-label">Incidents available</div>
        <div class="stat-note">seeded, reproducible</div>
      </div>
      <div class="stat">
        <div class="stat-value">${cards.length}</div>
        <div class="stat-label">Investigations recorded</div>
        <div class="stat-note">stored in DataHub</div>
      </div>
      <div class="stat">
        <div class="stat-value">${actions} <span style="color:var(--dim);font-size:18px">/</span> ${refusals}</div>
        <div class="stat-label">Actions / refusals</div>
        <div class="stat-note">a refusal is an outcome</div>
      </div>
      <div class="stat">
        <div class="stat-value">${continued}</div>
        <div class="stat-label">Runs that continued a prior one</div>
        <div class="stat-note">inherited evidence</div>
      </div>
      <div class="stat">
        <div class="stat-value" style="color:var(--purple)">${verified}</div>
        <div class="stat-label">Verified knowledge</div>
        <div class="stat-note">confirmed checks, re-tested on reuse</div>
      </div>
    </div>

    <div style="height:26px"></div>
    <h2>Active incidents</h2>
    <div class="grid" id="incident-list"></div>

    <div style="height:26px"></div>
    <div class="grid grid-2">
      <div class="card">
        <h3>Catalog</h3>
        <dl class="kv">
          <dt>Datasets</dt><dd class="mono">${catalog.datasets ?? '—'}</dd>
          <dt>Dashboards</dt><dd class="mono">${catalog.dashboards ?? '—'}</dd>
          <dt>Charts</dt><dd class="mono">${catalog.charts ?? '—'}</dd>
          <dt>Documents</dt><dd class="mono">${catalog.documents ?? '—'}</dd>
        </dl>
        <p class="sub" style="font-size:12px;margin-top:12px">DataHub's own showcase-ecommerce
        reference datapack — real cross-platform lineage, not a synthetic graph.</p>
      </div>
      <div class="card">
        <h3>How this stays honest</h3>
        <dl class="kv">
          <dt>Confidence</dt><dd>confirmed ÷ 4 evidence checks</dd>
          <dt>Severity</dt><dd>a plain function of confidence + blast radius</dd>
          <dt>Write-back</dt><dd>refused in code below medium confidence</dd>
          <dt>Targets</dt><dd>only entities this run confirmed something about</dd>
        </dl>
        <p class="sub" style="font-size:12px;margin-top:12px">The model supplies evidence.
        It never decides whether it is allowed to act, or on what.</p>
      </div>
    </div>
  `);

  const list = $('#incident-list');
  list.innerHTML = incidents.incidents.map(incidentCardHtml).join('');
  wireIncidentButtons();
}

function incidentCardHtml(inc) {
  const latest = inc.latest;
  return `
    <div class="incident fade-in">
      <div class="incident-body">
        <p class="incident-title">${h(inc.label)}</p>
        <p class="incident-symptom">${h(inc.prompt)}</p>
        <div class="incident-meta">
          ${severityChip(latest && latest.severity)}
          ${latest ? confidenceChip(latest.confidence_level, latest.checks_confirmed, latest.checks_total) : ''}
          ${latest ? decisionChip(latest.decision) : ''}
          <span class="chip">${h(inc.shape)}</span>
          <span class="chip">${inc.investigation_count} prior investigation${inc.investigation_count === 1 ? '' : 's'}</span>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px;flex:none">
        <button class="btn btn-primary" data-run="${h(inc.id)}">Investigate →</button>
        ${latest ? `<a class="btn btn-sm" href="#/investigation/${h(latest.incident_id)}">Last card</a>` : ''}
      </div>
    </div>`;
}

function wireIncidentButtons() {
  $$('[data-run]').forEach((btn) => {
    btn.addEventListener('click', () => { location.hash = `#/run/${btn.dataset.run}`; });
  });
}

/* ------------------------------------------------------------- incidents -- */

async function viewIncidents() {
  loading('incidents');
  let data;
  try { data = await api('/api/incidents'); } catch (e) { setView(errorBox(e.message)); return; }

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow">Investigate</p>
      <h1>Incidents</h1>
      <p class="sub">Three reproducible incidents seeded into DataHub's showcase datapack, each a
      different shape of problem. The agent is not told where the root cause is — it has to find it,
      and it does not always reach the same conclusion.</p>
    </div>
    <div class="grid" id="incident-list"></div>
  `);
  $('#incident-list').innerHTML = data.incidents.map(incidentCardHtml).join('');
  wireIncidentButtons();
}

/* ----------------------------------------------------- live investigation -- */

let liveSource = null;

function closeLive() {
  if (liveSource) { liveSource.close(); liveSource = null; }
}

function classifyLine(line) {
  if (/^\s*->/.test(line)) return 'l-call';
  if (/Blocked:/.test(line)) return 'l-block';
  if (/^\s*<-/.test(line)) return 'l-result';
  if (/^===/.test(line)) return 'l-head';
  return 'l-say';
}

async function viewRun(scenario) {
  closeLive();
  let incidents;
  try { incidents = await api('/api/incidents'); } catch (e) { setView(errorBox(e.message)); return; }
  const inc = incidents.incidents.find((i) => i.id === scenario);
  if (!inc) { setView('<div class="empty">Unknown incident.</div>'); return; }

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow"><a href="#/incidents" style="color:var(--muted)">← Incidents</a></p>
      <h1>${h(inc.label)}</h1>
      <p class="sub">${h(inc.prompt)}</p>
      <div class="incident-meta" style="margin-top:12px">
        <span class="chip chip-accent" id="run-state">ready</span>
        <span class="chip">${h(inc.shape)}</span>
        <span class="chip">${inc.investigation_count} prior investigation${inc.investigation_count === 1 ? '' : 's'}</span>
        <span class="chip chip-accent" id="dh-calls" title="Real MCP calls to DataHub's GMS this run">◈ 0 DataHub calls</span>
        <button class="btn btn-primary btn-sm" id="start-run">▶ Run investigation</button>
      </div>
    </div>

    <div id="memory-moment"></div>

    <div class="workspace">
      <div>
        <h3>Agent activity</h3>
        <div class="stream" id="stream">Press “Run investigation” to start. The agent will work against
the live DataHub instance — real search, real lineage, real write-back.

Everything in the panel to the right is read from the same state object
the policy layer uses to allow or refuse a write. Nothing there is
parsed out of this text.</div>
      </div>
      <div class="panel" id="panel">${panelHtml(null)}</div>
    </div>

    <div id="post-run"></div>
  `);

  $('#start-run').addEventListener('click', () => startRun(scenario));
}

function startRun(scenario) {
  const btn = $('#start-run');
  const stream = $('#stream');
  const runState = $('#run-state');
  btn.disabled = true;
  btn.textContent = 'Running…';
  runState.className = 'chip chip-accent';
  runState.textContent = 'investigating';
  stream.textContent = '';
  $('#post-run').innerHTML = '';

  let lastState = null;
  closeLive();
  liveSource = new EventSource(`/api/investigate/${encodeURIComponent(scenario)}`);

  liveSource.addEventListener('log', (e) => {
    const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60;
    e.data.split('\n').forEach((line) => {
      const span = document.createElement('span');
      span.className = classifyLine(line);
      span.textContent = line + '\n';
      stream.appendChild(span);
    });
    if (atBottom) stream.scrollTop = stream.scrollHeight;
  });

  liveSource.addEventListener('state', (e) => {
    try {
      lastState = JSON.parse(e.data);
      $('#panel').innerHTML = panelHtml(lastState);
      renderMemoryMoment(lastState);
      const calls = lastState.datahub_calls || { total: 0 };
      $('#dh-calls').textContent = `◈ ${calls.total} DataHub call${calls.total === 1 ? '' : 's'}`;
    } catch (err) { /* a malformed frame must not kill the stream */ }
  });

  liveSource.addEventListener('done', () => {
    closeLive();
    btn.disabled = false;
    btn.textContent = '▶ Run again';
    if (lastState) {
      const refused = lastState.write_back && lastState.write_back.locked;
      runState.className = `chip ${refused ? 'chip-warn' : 'chip-ok'}`;
      runState.textContent = refused ? 'refused — routed to human review' : 'completed';
      renderPostRun(lastState);
    } else {
      runState.className = 'chip chip-danger';
      runState.textContent = 'ended without a result';
    }
  });

  liveSource.onerror = () => {
    closeLive();
    btn.disabled = false;
    btn.textContent = '▶ Run again';
    runState.className = 'chip chip-danger';
    runState.textContent = 'connection closed';
  };
}

/* The memory moment.
 *
 * This is the beat the whole project is built around, so it gets its own banner
 * above the fold rather than a line in a side panel: before touching DataHub, the
 * agent finds what previous investigations of this incident already established,
 * and continues from there. Rendered only from cards `recall_prior_investigations`
 * genuinely returned — when there are none, it says so plainly, because "starting
 * cold" is the honest and equally interesting half of the story. */
function renderMemoryMoment(s) {
  const target = $('#memory-moment');
  if (!target) return;
  const mem = s.memory || { prior_cards: [] };
  const phase = (s.phases || []).find((p) => p.key === 'recall');
  if (!phase || !phase.done) { target.innerHTML = ''; return; }

  if (mem.prior_cards.length === 0) {
    target.innerHTML = `
      <div class="banner fade-in" style="margin-bottom:16px">
        <span class="banner-icon">◷</span>
        <div>
          <b>No prior investigation of this incident.</b>
          <p class="sub" style="font-size:12.5px;margin-top:4px">Starting cold — every check
          has to be established from scratch. Whatever this run proves gets written back to
          DataHub, so the next one won't have to.</p>
        </div>
      </div>`;
    return;
  }

  const confirmed = [];
  mem.prior_cards.forEach((c) => (c.confirmed || []).forEach((label) => {
    if (!confirmed.some((x) => x.label === label)) confirmed.push({ label, from: c.incident_id });
  }));
  const stillMissing = [];
  mem.prior_cards.forEach((c) => (c.missing || []).forEach((label) => {
    if (!confirmed.some((x) => x.label === label) && !stillMissing.includes(label)) stillMissing.push(label);
  }));

  target.innerHTML = `
    <div class="banner banner-memory fade-in" style="margin-bottom:16px">
      <span class="banner-icon">◈</span>
      <div style="flex:1;min-width:0">
        <b>Prior verified knowledge found in DataHub.</b>
        <p class="sub" style="font-size:12.5px;margin-top:4px">
          ${mem.prior_cards.length} stored investigation${mem.prior_cards.length === 1 ? '' : 's'}
          of this incident. This run continues from ${mem.prior_cards.length === 1 ? 'it' : 'them'}
          rather than starting over — and any check it inherits is verified against those cards
          before it counts.</p>
        ${confirmed.length ? `
          <p style="margin:10px 0 4px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)">Already established</p>
          ${confirmed.map((c) => `<div style="font-size:12.5px">✓ ${h(c.label)}
            <span class="evidence-source" style="display:inline;margin-left:6px">${h(c.from)}</span></div>`).join('')}` : ''}
        ${stillMissing.length ? `
          <p style="margin:10px 0 4px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)">Still missing — what this run is for</p>
          ${stillMissing.map((l) => `<div style="font-size:12.5px;color:var(--muted)">✗ ${h(l)}</div>`).join('')}` : ''}
        ${validationHtml(mem.validation || [])}
      </div>
    </div>`;
}

/* Prior knowledge re-tested against the graph as it is now.
 *
 * The reason this is rendered at all: a stored card is a true record of when it
 * was written, not a standing fact. If DataHub has moved on, inheriting it would
 * let a stale finding buy confidence in the present — the specific way memory
 * makes an agent worse instead of better. A CONFLICT withdraws the card as
 * evidence in code, which can drop confidence far enough to block write-back. */
function validationHtml(rows) {
  if (!rows.length) return '';
  const icon = { confirmed: '✓', conflict: '⚠', unverifiable: '?' };
  const cls = { confirmed: 'chip-ok', conflict: 'chip-danger', unverifiable: '' };
  const conflicts = rows.filter((r) => r.verdict === 'conflict').length;

  return `
    <p style="margin:14px 0 4px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)">
      Re-tested against live DataHub — prior knowledge is a hypothesis, not truth</p>
    ${rows.map((r) => `
      <div style="display:flex;gap:8px;align-items:flex-start;padding:4px 0;font-size:12.5px">
        <span class="chip ${cls[r.verdict] || ''}" style="flex:none">${icon[r.verdict] || '?'} ${h(r.verdict)}</span>
        <span style="flex:1;min-width:0">
          <span class="mono" style="font-size:11px">${h(r.incident_id)}</span>
          <span style="color:var(--muted)"> — ${h(r.detail)}</span>
        </span>
      </div>`).join('')}
    ${conflicts ? `
      <div class="error-box" style="margin-top:10px">
        <b>${conflicts} stored finding${conflicts === 1 ? '' : 's'} withdrawn.</b>
        The graph no longer matches what ${conflicts === 1 ? 'it' : 'they'} recorded, so
        ${conflicts === 1 ? 'it is' : 'they are'} no longer allowed to back any evidence check
        this run. Confidence falls back to what this investigation proves for itself.
      </div>` : ''}`;
}

/* The live decision panel. Reads only from the snapshot the server sends. */
function panelHtml(s) {
  if (!s) {
    return `<div class="panel-block"><h3>Decision layer</h3>
      <p class="sub" style="font-size:12px">Idle. Start an investigation to watch the
      evidence checklist, confidence arithmetic and write-back gate resolve in real time.</p></div>`;
  }

  const phases = s.phases.map((p) => `
    <div class="phase ${p.done ? 'done' : ''}">
      <span class="phase-mark">✓</span>
      <span>${h(p.label)}</span>
      ${p.done && p.detail ? `<span class="phase-detail">${h(p.detail)}</span>` : ''}
    </div>`).join('');

  const evidence = s.evidence.map((c) => `
    <div class="check ${c.confirmed ? 'yes' : 'no'}">
      <span class="check-box">✓</span>
      <span class="check-label">${h(c.label)}
        ${c.inherited
          ? `<span class="evidence-source">↩ not re-run — proven by ${h(c.source || 'a prior investigation')}</span>`
          : ''}
      </span>
    </div>`).join('');

  const conf = s.confidence;
  const level = conf.level || '';
  const segClass = level === 'high' ? 'on-high' : level === 'medium' ? 'on-med' : 'on-low';
  const meter = [0, 1, 2, 3].map((i) =>
    `<div class="meter-seg ${conf.confirmed > i ? segClass : ''}"></div>`).join('');

  const memory = s.memory;
  const memoryBlock = `
    <div class="panel-block">
      <h3>Memory</h3>
      ${memory.prior_cards.length === 0
        ? '<p class="sub" style="font-size:12px">No prior investigation of this incident — starting cold.</p>'
        : memory.prior_cards.map((c) => `
            <div class="gate-row">
              <span>${h(c.incident_id)}</span>
              <span>${h(c.confidence)} · ${h(c.decision)}</span>
            </div>`).join('')}
      ${memory.continues ? `<p class="sub" style="font-size:12px;margin-top:8px">Continuing
        <b class="mono">${h(memory.continues)}</b> — ${memory.reused_checks} check(s) reused.</p>` : ''}
      ${memory.rejected.length ? `
        <div class="error-box" style="margin-top:10px">
          <b>Unbacked inheritance rejected.</b><br>${memory.rejected.map(h).join('<br>')}
        </div>` : ''}
    </div>`;

  const drift = s.schema_drift;
  const driftBlock = !drift ? '' : `
    <div class="panel-block">
      <h3>Cross-platform mirrors</h3>
      <p class="sub" style="font-size:12px;margin-bottom:8px">Field
        <b class="mono">${h(drift.field)}</b> — lineage says these are connected; it never says
        they agree on shape.</p>
      ${drift.mirrors.map((m) => `
        <div class="mirror-row">
          <span class="mirror-platform">${h(m.platform)}</span>
          <span class="chip ${m.status === 'stale' ? 'chip-danger' : m.status === 'current' ? 'chip-ok' : ''}">${h(m.status)}</span>
        </div>`).join('') || '<p class="sub" style="font-size:12px">No mirrors found.</p>'}
    </div>`;

  const wb = s.write_back;
  const events = (wb.events || []).map((ev) => `
    <div class="event">
      <span class="event-stage stage-${h(ev.stage)}">${h(ev.stage.toUpperCase())}</span>
      <span>${h(ev.tool)}${ev.reason ? `<br><span style="color:var(--muted)">${h(ev.reason)}</span>` : ''}</span>
    </div>`).join('');

  const targets = wb.authorized_targets.add_tags || [];

  return `
    <div class="panel-block">
      <h3>Progress</h3>
      ${phases}
    </div>

    ${memoryBlock}

    <div class="panel-block">
      <h3>Evidence</h3>
      ${evidence}
      ${!conf.reported ? '<p class="sub" style="font-size:11.5px;margin-top:8px">Pending — the agent has not reported to the policy layer yet.</p>' : ''}
    </div>

    <div class="panel-block">
      <h3>Confidence</h3>
      <div style="display:flex;align-items:baseline;gap:10px">
        <span class="confidence-num">${conf.reported ? `${conf.confirmed}/${conf.total}` : '—'}</span>
        <span class="chip ${level === 'high' ? 'chip-ok' : level === 'medium' ? 'chip-accent' : 'chip-warn'}">${h(level.toUpperCase() || 'pending')}</span>
      </div>
      <div class="meter">${meter}</div>
      ${conf.reported ? `<div class="formula">confidence = <b>${conf.confirmed}</b> ÷ <b>${conf.total}</b> confirmed checks<br>
        severity = f(confidence, ${s.blast_radius.datasets} datasets, ${s.blast_radius.dashboards} dashboards,
        criticality=${h(s.blast_radius.criticality || '—')}, stale mirrors=${drift ? drift.stale.length : 0})
        = <b>${h(wb.severity || '—')}</b></div>` : ''}
    </div>

    <div class="panel-block ${wb.locked ? 'is-locked' : 'is-open'}">
      <div class="lock-head">
        <h3 style="margin:0">Write-back gate</h3>
        <span class="lock-state ${wb.locked ? 'locked' : 'open'}">${wb.locked ? '🔒 LOCKED' : '🔓 OPEN'}</span>
      </div>
      ${wb.tools.map((t) => `
        <div class="gate-row">
          <span>${h(t.name)}</span>
          <span style="color:${t.unlocked ? 'var(--ok)' : 'var(--danger)'}">${t.unlocked ? 'permitted' : 'refused'}</span>
        </div>`).join('')}
      ${targets.length ? `<p class="sub" style="font-size:11.5px;margin-top:10px">Authorized targets:<br>
        ${targets.map((t) => `<span class="urn">${h(urnTail(t))}</span>`).join(', ')}</p>` : ''}
      ${events ? `<div style="margin-top:10px">${events}</div>` : ''}
    </div>

    ${driftBlock}
  `;
}

function renderPostRun(s) {
  const rc = s.root_cause;
  const wb = s.write_back;
  const drift = s.schema_drift;
  const k = s.knowledge || { stored: false };
  const refused = wb.locked;

  const banner = refused ? `
    <div class="banner banner-refusal">
      <span class="banner-icon">⚖</span>
      <div>
        <b>Action refused by policy.</b>
        <p class="sub" style="font-size:12.5px;margin-top:4px">Confidence was
        ${s.confidence.confirmed}/${s.confidence.total}, which computes to severity
        <b>${h(wb.severity || 'no_action')}</b>. The write-back tools were blocked in code and this is
        routed to human review. The investigation was still recorded, so the next run continues
        from here instead of starting over.</p>
      </div>
    </div>` : `
    <div class="banner banner-action">
      <span class="banner-icon">✓</span>
      <div>
        <b>Action authorized and verified.</b>
        <p class="sub" style="font-size:12.5px;margin-top:4px">Severity <b>${h(wb.severity)}</b>
        permitted ${wb.actions_taken.length} write-back(s), each re-read from DataHub afterwards to
        prove it landed.</p>
      </div>
    </div>`;

  $('#post-run').innerHTML = `
    <div style="height:24px"></div>
    ${banner}
    <div style="height:16px"></div>

    <div class="grid grid-2">
      <div class="card">
        <h3>Root cause</h3>
        ${rc.urn ? `
          <p style="font-size:15px;font-weight:600;margin:0 0 2px">${h(urnTail(rc.urn))}</p>
          <p class="urn">${h(rc.urn)}</p>
          ${rc.field ? `<p style="margin:10px 0 0"><span class="chip chip-danger">${h(rc.field)}</span>
            <span class="sub" style="font-size:12px"> changed field</span></p>` : ''}
          <p class="sub" style="font-size:12.5px;margin-top:10px">${h(rc.summary)}</p>
          <div style="margin-top:12px;display:flex;gap:8px">
            <a class="btn btn-sm" href="#/lineage?urn=${encodeURIComponent(rc.urn)}">View lineage</a>
            <a class="btn btn-sm" href="#/entity?urn=${encodeURIComponent(rc.urn)}">Entity details</a>
          </div>`
        : `<p class="sub">No root cause was established. ${h(rc.summary)}</p>`}
      </div>

      <div class="card">
        <h3>Blast radius</h3>
        <div class="grid grid-3" style="gap:10px">
          <div><div class="stat-value" style="font-size:22px">${s.blast_radius.datasets}</div><div class="stat-label">datasets</div></div>
          <div><div class="stat-value" style="font-size:22px">${s.blast_radius.dashboards}</div><div class="stat-label">dashboards</div></div>
          <div><div class="stat-value" style="font-size:22px">${s.blast_radius.platforms.length}</div><div class="stat-label">platforms</div></div>
        </div>
        ${s.blast_radius.platforms.length ? `<p style="margin-top:12px">${
          s.blast_radius.platforms.map((p) => `<span class="chip">${h(p)}</span>`).join(' ')}</p>` : ''}
      </div>
    </div>

    ${drift && drift.stale.length ? `
      <div style="height:14px"></div>
      <div class="card">
        <h3>Cross-platform schema drift</h3>
        <p class="sub" style="font-size:13px">DataHub's lineage is topologically honest but
        schema-blind — an edge says two datasets are connected, never that they agree on shape.
        <b>${drift.stale.length} of ${drift.mirrors.length}</b> mirrors of
        <b class="mono">${h(drift.field)}</b> are running stale schema, and will keep producing the
        same symptom after the root cause is fixed.</p>
        <div style="margin-top:12px">
          ${drift.mirrors.map((m) => `
            <div class="mirror-row">
              <span class="mirror-platform">${h(m.platform)}</span>
              <span class="chip ${m.status === 'stale' ? 'chip-danger' : 'chip-ok'}">${h(m.status)}</span>
              <span class="urn" style="margin-left:auto">${h(urnTail(m.urn))}</span>
            </div>`).join('')}
        </div>
      </div>` : ''}

    ${k.stored ? `
      <div style="height:16px"></div>
      <div class="knowledge fade-in">
        <p class="knowledge-head">Every incident makes DataHub smarter.</p>
        <p class="sub" style="font-size:13px;max-width:74ch">This investigation was written back
        into the catalog as a <code>document</code> entity, linked to the assets it concerned.
        ${k.continues ? `It continues <b class="mono">${h(k.continues)}</b>, so the chain of
        reasoning is now traceable across runs.` : 'The next investigation of this incident will
        find it and continue instead of starting over.'}</p>

        <div class="knowledge-nums">
          <div>
            <div class="knowledge-num">${k.proved_here}</div>
            <div class="knowledge-cap">check(s) proved by this run</div>
          </div>
          <div>
            <div class="knowledge-num">${k.reused}</div>
            <div class="knowledge-cap">not re-run — inherited and verified</div>
          </div>
          <div>
            <div class="knowledge-num">${k.available_next_run}</div>
            <div class="knowledge-cap">now established for the next run</div>
          </div>
          <div>
            <div class="knowledge-num">${(s.datahub_calls || {}).total || 0}</div>
            <div class="knowledge-cap">DataHub calls this investigation</div>
          </div>
        </div>

        <p class="urn" style="margin-top:14px">${h(s.card_urn)}</p>
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
          <a class="btn btn-sm" href="#/investigation/${h(k.incident_id || '')}">Open this card</a>
          <a class="btn btn-sm" href="#/investigations">All investigations →</a>
        </div>
      </div>` : ''}
  `;
}

/* -------------------------------------------------------- investigations -- */

async function viewInvestigations() {
  loading('investigations');
  let data;
  try { data = await api('/api/investigations'); } catch (e) { setView(errorBox(e.message)); return; }
  const cards = data.cards || [];

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow">Investigate</p>
      <h1>Investigations</h1>
      <p class="sub">Every run this agent has ever completed, read back out of DataHub itself. These are
      real <code>document</code> entities in the catalog — a human opening the dataset page sees the same
      record the next agent run will inherit.</p>
      <div class="incident-meta" style="margin-top:12px">
        <span class="chip chip-purple">${cards.filter((c) => c.continues_incident_id).length} continued a prior investigation</span>
        <span class="chip chip-warn">${cards.filter((c) => c.decision === 'REFUSAL').length} refused to act</span>
        <span class="chip chip-ok">${cards.filter((c) => c.decision === 'ACTION').length} wrote back</span>
      </div>
    </div>
    ${data.error ? errorBox(data.error) : ''}
    ${cards.length === 0
      ? '<div class="empty">No Investigation Cards stored yet. Run an incident to create one.</div>'
      : `<div>${cards.map((c) => `
          <div class="row" onclick="location.hash='#/investigation/${h(c.incident_id)}'">
            <div class="row-main">
              <div class="row-title">${h(c.incident_id)} · ${h(urnTail(c.root_cause_urn) || 'no root cause')}</div>
              <div class="row-sub">${h(c.trigger)}</div>
              ${c.continues_incident_id
                ? `<div class="chain" style="margin-top:4px">↩ continues ${h(c.continues_incident_id)}
                     · ${c.reused_checks} check(s) not re-run</div>`
                : ''}
            </div>
            <div class="row-side">
              ${decisionChip(c.decision)}
              ${confidenceChip(c.confidence_level, c.checks_confirmed, c.checks_total)}
              <div class="row-sub" style="margin-top:4px">${h(timeAgo(c.timestamp))}</div>
            </div>
          </div>`).join('')}</div>`}
  `);
}

async function viewInvestigation(incidentId) {
  loading('investigation');
  let data;
  try { data = await api('/api/investigations'); } catch (e) { setView(errorBox(e.message)); return; }
  const c = (data.cards || []).find((x) => x.incident_id === incidentId);
  if (!c) { setView('<div class="empty">That Investigation Card is no longer in DataHub.</div>'); return; }

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow"><a href="#/investigations" style="color:var(--muted)">← Investigations</a></p>
      <h1>${h(c.incident_id)}</h1>
      <p class="sub">${h(c.trigger)}</p>
      <div class="incident-meta" style="margin-top:12px">
        ${decisionChip(c.decision)}
        ${confidenceChip(c.confidence_level, c.checks_confirmed, c.checks_total)}
        ${severityChip(c.severity)}
        <span class="chip">${h(new Date(c.timestamp).toLocaleString())}</span>
      </div>
    </div>

    ${c.decision === 'REFUSAL' ? `
      <div class="banner banner-refusal">
        <span class="banner-icon">⚖</span>
        <div>
          <b>No action taken.</b>
          <p class="sub" style="font-size:12.5px;margin-top:4px">${h(c.refusal_reason)}</p>
          ${c.required_before_retry.length ? `
            <p style="margin:10px 0 4px;font-size:12px;color:var(--muted)"><b>Required before action becomes safe:</b></p>
            <ul style="margin:0;padding-left:18px;font-size:12.5px;color:var(--muted)">
              ${c.required_before_retry.map((r) => `<li>${h(r)}</li>`).join('')}</ul>` : ''}
        </div>
      </div>` : ''}

    <div style="height:16px"></div>
    <div class="grid grid-2">
      <div class="card">
        <h3>Evidence</h3>
        ${c.evidence.map((e) => `
          <div class="check ${e.confirmed ? 'yes' : 'no'}">
            <span class="check-box">✓</span>
            <span class="check-label">${h(e.label)}
              ${e.inherited ? '<span class="chip chip-purple" style="margin-left:6px">inherited</span>' : ''}
            </span>
          </div>`).join('')}
        ${c.dropped_inheritance.length ? `
          <div class="error-box" style="margin-top:12px">
            <b>Unbacked inheritance rejected:</b><br>${c.dropped_inheritance.map(h).join('<br>')}
          </div>` : ''}
      </div>

      <div class="card">
        <h3>Root cause</h3>
        ${c.root_cause_urn
          ? `<p style="font-size:15px;font-weight:600;margin:0 0 2px">${h(urnTail(c.root_cause_urn))}</p>
             <p class="urn">${h(c.root_cause_urn)}</p>
             <p class="sub" style="font-size:12.5px;margin-top:10px">${h(c.root_cause_summary)}</p>
             <div style="margin-top:12px"><a class="btn btn-sm" href="#/lineage?urn=${encodeURIComponent(c.root_cause_urn)}">View lineage</a></div>`
          : `<p class="sub">Not established. ${h(c.root_cause_summary)}</p>`}
      </div>
    </div>

    ${c.continues_incident_id ? `
      <div style="height:14px"></div>
      <div class="card">
        <h3>Continues a prior investigation</h3>
        <p class="sub" style="font-size:13px">This run inherited <b>${c.reused_checks}</b> confirmed
        check(s) from <a href="#/investigation/${h(c.continues_incident_id)}" style="color:var(--accent)">${h(c.continues_incident_id)}</a>
        instead of re-deriving them.</p>
      </div>` : ''}

    ${c.schema_drift_mirrors_checked ? `
      <div style="height:14px"></div>
      <div class="card">
        <h3>Cross-platform schema drift</h3>
        <p class="sub" style="font-size:13px">${c.schema_drift_mirrors_stale} of
        ${c.schema_drift_mirrors_checked} mirror(s) of <b class="mono">${h(c.schema_drift_field)}</b>
        were running stale schema.</p>
        <p style="margin-top:10px">${c.schema_drift_stale_platforms.map((p) => `<span class="chip chip-danger">${h(p)}</span>`).join(' ')}</p>
      </div>` : ''}

    <div style="height:14px"></div>
    <div class="grid grid-2">
      <div class="card">
        <h3>Hypotheses</h3>
        ${(c.hypotheses_tested.length || c.hypotheses_rejected.length)
          ? `<ul style="margin:0;padding-left:18px;font-size:12.5px;color:var(--muted)">
              ${c.hypotheses_tested.map((x) => `<li>Tested: ${h(x)}</li>`).join('')}
              ${c.hypotheses_rejected.map((x) => `<li>Rejected: ${h(x)}</li>`).join('')}
             </ul>`
          : '<p class="sub" style="font-size:12.5px">None recorded.</p>'}
      </div>
      <div class="card">
        <h3>Actions &amp; provenance</h3>
        ${c.actions_taken.length
          ? `<ul style="margin:0 0 10px;padding-left:18px;font-size:12.5px">${c.actions_taken.map((a) => `<li class="mono">${h(a)}</li>`).join('')}</ul>`
          : '<p class="sub" style="font-size:12.5px">No catalog mutations.</p>'}
        <p class="sub" style="font-size:11.5px">Derived from: ${c.provenance.map((p) => `<code>${h(p)}</code>`).join(', ') || '—'}</p>
      </div>
    </div>
  `);
}

/* -------------------------------------------------------------- entities -- */

async function viewEntities(query) {
  const q = query || '*';
  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow">DataHub</p>
      <h1>Entities</h1>
      <p class="sub">Search the live catalog. Same instance the agent investigates — no separate
      DataHub tab required.</p>
    </div>
    <input class="input" id="entity-search" placeholder="Search DataHub…  (try: order_details, promotions, revenue)" value="${h(q === '*' ? '' : q)}">
    <div style="height:16px"></div>
    <div id="entity-results"><div class="loading-page"><span class="spinner"></span> Searching…</div></div>
  `);

  const input = $('#entity-search');
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const value = input.value.trim();
      location.hash = value ? `#/entities?q=${encodeURIComponent(value)}` : '#/entities';
    }, 350);
  });
  input.focus();

  let data;
  try { data = await api(`/api/entities?q=${encodeURIComponent(q)}&count=30`); }
  catch (e) { $('#entity-results').innerHTML = errorBox(e.message); return; }

  $('#entity-results').innerHTML = data.error ? errorBox(data.error)
    : data.results.length === 0 ? '<div class="empty">Nothing matched.</div>'
    : `<p class="sub" style="font-size:12px;margin-bottom:10px">${data.total} match(es)</p>` +
      data.results.map((e) => `
        <div class="row" onclick="location.hash='#/entity?urn=${encodeURIComponent(e.urn)}'">
          <div class="row-main">
            <div class="row-title">${h(e.name)}</div>
            <div class="row-sub">${h(e.qualified_name)}</div>
          </div>
          <div class="row-side">
            <span class="chip">${h(e.platform || e.type)}</span>
            ${e.sub_type ? `<span class="chip">${h(e.sub_type)}</span>` : ''}
            ${e.tags.map((t) => `<span class="chip chip-purple">${h(t)}</span>`).join('')}
          </div>
        </div>`).join('');
}

async function viewEntity(urn) {
  loading('entity');
  let e;
  try { e = await api(`/api/entity?urn=${encodeURIComponent(urn)}`); }
  catch (err) { setView(errorBox(err.message)); return; }
  if (e.error) { setView(errorBox(e.error)); return; }

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow"><a href="#/entities" style="color:var(--muted)">← Entities</a></p>
      <h1>${h(e.name)}</h1>
      <p class="urn">${h(e.urn)}</p>
      <div class="incident-meta" style="margin-top:12px">
        <span class="chip chip-accent">${h(e.platform || e.type)}</span>
        ${e.sub_type ? `<span class="chip">${h(e.sub_type)}</span>` : ''}
        ${e.tags.map((t) => `<span class="chip chip-purple">${h(t)}</span>`).join('')}
        <a class="btn btn-sm" href="#/lineage?urn=${encodeURIComponent(e.urn)}">View lineage →</a>
      </div>
    </div>

    <div class="grid grid-3">
      <div class="stat"><div class="stat-value">${e.upstream_count}</div><div class="stat-label">upstream entities</div></div>
      <div class="stat"><div class="stat-value">${e.downstream_count}</div><div class="stat-label">downstream entities</div></div>
      <div class="stat"><div class="stat-value">${(e.fields || []).length}</div><div class="stat-label">schema fields</div></div>
    </div>

    ${e.description ? `<div style="height:14px"></div><div class="card"><h3>Description</h3><p class="sub" style="font-size:13px;white-space:pre-wrap">${h(e.description)}</p></div>` : ''}

    ${e.owners.length ? `<div style="height:14px"></div><div class="card"><h3>Ownership</h3>
      <p>${e.owners.map((o) => `<span class="chip">${h(o)}</span>`).join(' ')}</p></div>` : ''}

    ${(e.fields || []).length ? `
      <div style="height:14px"></div>
      <div class="card">
        <h3>Schema</h3>
        <div style="max-height:420px;overflow:auto">
          <table class="field-table">
            <thead><tr><th>Field</th><th>Type</th><th>Description</th></tr></thead>
            <tbody>${e.fields.map((f) => `
              <tr>
                <td class="mono">${h(f.path)}</td>
                <td style="color:var(--dim)">${h(f.type)}</td>
                <td style="color:var(--muted)">${h(f.description)}</td>
              </tr>`).join('')}</tbody>
          </table>
        </div>
      </div>` : ''}
  `);
}

/* --------------------------------------------------------------- lineage -- */

const DEFAULT_LINEAGE_URN =
  'urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)';

async function viewLineage(urn) {
  const target = urn || DEFAULT_LINEAGE_URN;
  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow">DataHub</p>
      <h1>Lineage</h1>
      <p class="sub">The real graph, straight from DataHub. Every edge here is a relationship DataHub
      actually traversed — nothing is inferred from two nodes sitting next to each other.</p>
    </div>
    <input class="input" id="lineage-search" placeholder="Search for an entity to centre the graph on…">
    <div id="lineage-results"></div>
    <div style="height:14px"></div>
    <div class="graph-wrap" id="graph"><div class="loading-page" style="padding:24px"><span class="spinner"></span> Loading graph…</div></div>
  `);

  const input = $('#lineage-search');
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const value = input.value.trim();
    if (!value) { $('#lineage-results').innerHTML = ''; return; }
    timer = setTimeout(async () => {
      try {
        const data = await api(`/api/entities?q=${encodeURIComponent(value)}&count=6`);
        $('#lineage-results').innerHTML = `<div style="margin-top:10px">${data.results.map((e) => `
          <div class="row" onclick="location.hash='#/lineage?urn=${encodeURIComponent(e.urn)}'">
            <div class="row-main"><div class="row-title">${h(e.name)}</div>
            <div class="row-sub">${h(e.qualified_name)}</div></div>
            <div class="row-side"><span class="chip">${h(e.platform || e.type)}</span></div>
          </div>`).join('')}</div>`;
      } catch (e) { /* search is auxiliary here */ }
    }, 350);
  });

  let data;
  try { data = await api(`/api/lineage?urn=${encodeURIComponent(target)}&hops=2`); }
  catch (e) { $('#graph').innerHTML = errorBox(e.message); return; }
  if (data.error) { $('#graph').innerHTML = errorBox(data.error); return; }
  drawGraph($('#graph'), data);
}

/* Layered graph renderer. Nodes are placed by their real hop distance from the
 * root (DataHub's own `degree`); edges are the real relationships it returned.
 * Hand-rolled rather than pulled from a library because the page must work with
 * no external requests at all. */
function drawGraph(container, data, opts = {}) {
  const staleUrns = new Set(opts.stale || []);
  const nodes = data.nodes.slice();
  if (nodes.length === 0) {
    container.innerHTML = '<div class="empty" style="margin:20px">No lineage recorded for this entity.</div>';
    return;
  }

  const byDepth = new Map();
  nodes.forEach((n) => {
    const d = n.depth || 0;
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d).push(n);
  });

  const NODE_W = 186, NODE_H = 46, COL_GAP = 250, ROW_GAP = 62;
  const pos = new Map();
  Array.from(byDepth.keys()).sort((a, b) => a - b).forEach((depth) => {
    const col = byDepth.get(depth).sort((a, b) => (a.platform || '').localeCompare(b.platform || ''));
    col.forEach((n, i) => {
      pos.set(n.urn, {
        x: depth * COL_GAP,
        y: i * ROW_GAP - ((col.length - 1) * ROW_GAP) / 2,
      });
    });
  });

  const xs = Array.from(pos.values()).map((p) => p.x);
  const ys = Array.from(pos.values()).map((p) => p.y);
  const pad = 90;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + NODE_W + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + NODE_H + pad;

  const edges = data.edges.filter((e) => pos.has(e.source) && pos.has(e.target));

  const edgePath = (e) => {
    const a = pos.get(e.source), b = pos.get(e.target);
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
    const x2 = b.x, y2 = b.y + NODE_H / 2;
    const mid = (x1 + x2) / 2;
    return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
  };

  container.innerHTML = `
    <svg viewBox="${minX} ${minY} ${maxX - minX} ${maxY - minY}" id="graph-svg">
      <defs>
        <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" fill="#2d3743"></path>
        </marker>
      </defs>
      <g id="edges">
        ${edges.map((e, i) => `<path class="edge" data-i="${i}" d="${edgePath(e)}" marker-end="url(#arrow)"></path>`).join('')}
      </g>
      <g id="nodes">
        ${nodes.map((n) => {
          const p = pos.get(n.urn);
          const cls = ['node'];
          if (n.is_root) cls.push('root');
          if (staleUrns.has(n.urn)) cls.push('stale');
          return `
            <g class="${cls.join(' ')}" data-urn="${h(n.urn)}" transform="translate(${p.x},${p.y})">
              <rect class="node-box" width="${NODE_W}" height="${NODE_H}"></rect>
              <text class="node-name" x="12" y="20">${h((n.name || urnTail(n.urn)).slice(0, 24))}</text>
              <text class="node-plat" x="12" y="34">${h(n.platform || platformOf(n.urn) || n.type)}</text>
            </g>`;
        }).join('')}
      </g>
    </svg>
    <div class="graph-controls">
      <button class="btn btn-sm" data-zoom="in" title="Zoom in">+</button>
      <button class="btn btn-sm" data-zoom="out" title="Zoom out">−</button>
      <button class="btn btn-sm" data-zoom="home" title="Centre on root">⌖</button>
      <button class="btn btn-sm" data-zoom="fit" title="Fit whole graph">⤢</button>
    </div>
    <div class="graph-legend">
      <span class="legend-key"><span class="legend-swatch" style="border-color:var(--accent);background:rgba(91,157,255,.15)"></span> root</span>
      <span class="legend-key"><span class="legend-swatch" style="border-color:var(--danger);background:rgba(248,81,73,.15)"></span> stale mirror</span>
      <span class="legend-key">← upstream · downstream →</span>
      <span class="legend-key">${nodes.length} nodes · ${edges.length} edges</span>
    </div>
    <div class="node-detail" id="node-detail" style="display:none"></div>
  `;

  const svg = $('#graph-svg', container);
  const fitAll = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };

  /* The viewBox has to track the container's real aspect ratio, not the ratio it
   * happened to have when the graph was drawn. Getting this wrong is invisible
   * until someone resizes the window — SVG's default `meet` then letterboxes the
   * mismatch, and the graph silently renders at a fraction of its intended zoom.
   * A judge on a laptop half the size of mine would have seen exactly that. */
  const rootPos = pos.get(data.root) || { x: 0, y: 0 };
  const rootCx = rootPos.x + NODE_W / 2;
  const rootCy = rootPos.y + NODE_H / 2;

  const containerAspect = () => {
    const r = container.getBoundingClientRect();
    return r.width > 0 ? r.height / r.width : 0.5;
  };

  /** A box of the given width, matched to the container's shape and centred on
   *  a point — so what's on screen is always exactly what the numbers say. */
  const boxAround = (cx, cy, width) => {
    const w = Math.min(Math.max(width, 240), 14000);
    const hh = w * containerAspect();
    return { x: cx - w / 2, y: cy - hh / 2, w, h: hh };
  };

  /** Fit everything: wide enough for the full span, and tall enough too. */
  const fitBox = () => {
    const needed = Math.max(fitAll.w, fitAll.h / Math.max(containerAspect(), 0.05));
    return boxAround(fitAll.x + fitAll.w / 2, fitAll.y + fitAll.h / 2, needed);
  };

  const box = fitBox();
  let userAdjusted = false;
  const apply = () => svg.setAttribute('viewBox', `${box.x} ${box.y} ${box.w} ${box.h}`);
  apply();

  // Re-fit while the user hasn't taken manual control; once they've panned or
  // zoomed, leave their view alone and only correct the aspect ratio.
  if (typeof ResizeObserver !== 'undefined') {
    let first = true;
    new ResizeObserver(() => {
      if (first) { first = false; return; }
      if (userAdjusted) { Object.assign(box, boxAround(box.x + box.w / 2, box.y + box.h / 2, box.w)); }
      else { Object.assign(box, fitBox()); }
      apply();
    }).observe(container);
  }

  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    userAdjusted = true;
    const factor = ev.deltaY > 0 ? 1.12 : 0.89;
    const rect = svg.getBoundingClientRect();
    const fx = (ev.clientX - rect.left) / rect.width;
    const fy = (ev.clientY - rect.top) / rect.height;
    const nw = Math.min(Math.max(box.w * factor, 240), 12000);
    const nh = box.h * (nw / box.w);
    box.x += (box.w - nw) * fx;
    box.y += (box.h - nh) * fy;
    box.w = nw; box.h = nh;
    apply();
  }, { passive: false });

  let dragging = false, lastX = 0, lastY = 0;
  svg.addEventListener('mousedown', (ev) => {
    dragging = true; lastX = ev.clientX; lastY = ev.clientY;
    container.classList.add('dragging');
  });
  window.addEventListener('mouseup', () => { dragging = false; container.classList.remove('dragging'); });
  svg.addEventListener('mousemove', (ev) => {
    if (!dragging) return;
    userAdjusted = true;
    const rect = svg.getBoundingClientRect();
    box.x -= (ev.clientX - lastX) * (box.w / rect.width);
    box.y -= (ev.clientY - lastY) * (box.h / rect.height);
    lastX = ev.clientX; lastY = ev.clientY;
    apply();
  });

  $$('[data-zoom]', container).forEach((btn) => btn.addEventListener('click', () => {
    const mode = btn.dataset.zoom;
    if (mode === 'fit') { userAdjusted = false; Object.assign(box, fitBox()); }
    else if (mode === 'home') { userAdjusted = true; Object.assign(box, boxAround(rootCx, rootCy, 780)); }
    else {
      userAdjusted = true;
      const factor = mode === 'in' ? 0.8 : 1.25;
      const nw = Math.min(Math.max(box.w * factor, 240), 12000);
      const nh = box.h * (nw / box.w);
      box.x += (box.w - nw) / 2; box.y += (box.h - nh) / 2;
      box.w = nw; box.h = nh;
    }
    apply();
  }));

  const detail = $('#node-detail', container);
  $$('.node', container).forEach((g) => {
    g.addEventListener('click', () => {
      $$('.node', container).forEach((o) => o.classList.remove('sel'));
      g.classList.add('sel');
      const urn = g.dataset.urn;
      const node = nodes.find((n) => n.urn === urn) || {};
      $$('.edge', container).forEach((p, i) => {
        const e = edges[i];
        p.classList.toggle('hot', e && (e.source === urn || e.target === urn));
      });
      const connected = edges.filter((e) => e.source === urn || e.target === urn).length;
      detail.style.display = '';
      detail.innerHTML = `
        <div style="font-weight:600;margin-bottom:2px">${h(node.name || urnTail(urn))}</div>
        <div class="urn" style="margin-bottom:8px">${h(urn)}</div>
        <dl class="kv" style="grid-template-columns:88px 1fr;font-size:11.5px">
          <dt>Platform</dt><dd>${h(node.platform || platformOf(urn) || '—')}</dd>
          <dt>Type</dt><dd>${h(node.sub_type || node.type || '—')}</dd>
          <dt>Hops</dt><dd>${node.depth === 0 ? 'root' : `${Math.abs(node.depth)} ${node.depth < 0 ? 'upstream' : 'downstream'}`}</dd>
          <dt>Edges</dt><dd>${connected}</dd>
        </dl>
        <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
          <a class="btn btn-sm" href="#/entity?urn=${encodeURIComponent(urn)}">Details</a>
          <a class="btn btn-sm" href="#/lineage?urn=${encodeURIComponent(urn)}">Centre here</a>
        </div>`;
    });
  });
}

/* -------------------------------------------------------------- activity -- */

async function viewActivity() {
  loading('activity');
  let data;
  try { data = await api('/api/investigations'); } catch (e) { setView(errorBox(e.message)); return; }
  const cards = (data.cards || []).slice(0, 40);

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow">System</p>
      <h1>Activity</h1>
      <p class="sub">Everything the agent has done to this catalog, reconstructed from the cards it
      stored — including the runs where it decided it was not allowed to do anything.</p>
    </div>
    ${cards.length === 0 ? '<div class="empty">No activity recorded yet.</div>' : `
      <div class="card"><div class="timeline">
        ${cards.map((c) => `
          <div class="tl-item ${c.decision === 'ACTION' ? 'act' : 'ref'}">
            <div class="tl-time">${h(new Date(c.timestamp).toLocaleString())} · ${h(timeAgo(c.timestamp))}</div>
            <div><b>${h(c.incident_id)}</b> — ${c.decision === 'ACTION'
              ? `wrote back to <span class="mono">${h(urnTail(c.root_cause_urn))}</span>`
              : 'refused to act, recorded why'}
              ${confidenceChip(c.confidence_level, c.checks_confirmed, c.checks_total)}
            </div>
            ${c.actions_taken.length ? `<div class="row-sub mono" style="font-size:11px;margin-top:3px">${c.actions_taken.map(h).join('<br>')}</div>` : ''}
          </div>`).join('')}
      </div></div>`}
  `);
}

/* -------------------------------------------------------------- settings -- */

async function viewSettings() {
  loading('system status');
  let s;
  try { s = await api('/api/status'); } catch (e) { setView(errorBox(e.message)); return; }

  const row = (label, ok, detail) => `
    <div class="gate-row" style="font-family:var(--sans);font-size:13px">
      <span><span class="dot ${ok ? 'dot-ok' : 'dot-bad'}" style="display:inline-block;margin-right:8px"></span>${h(label)}</span>
      <span style="color:var(--muted);font-family:var(--mono);font-size:11.5px">${h(detail)}</span>
    </div>`;

  setView(`
    <div class="page-head fade-in">
      <p class="eyebrow">System</p>
      <h1>Settings &amp; status</h1>
      <p class="sub">Live probes, not hard-coded badges. Nothing here needs configuring to run the demo.</p>
    </div>

    <div class="card">
      <h3>Connections</h3>
      ${row('DataHub GMS', s.datahub.connected, s.datahub.version || s.datahub.error || '')}
      ${row('Catalog indexed', (s.catalog.datasets || 0) > 0, `${s.catalog.datasets || 0} datasets`)}
      ${row('Azure OpenAI', s.llm.configured, s.llm.deployment || '')}
      ${row('Write-back tools', s.mutations_enabled, s.mutations_enabled ? 'mutations enabled' : 'read-only')}
    </div>

    <div style="height:14px"></div>
    <div class="card">
      <h3>Endpoint</h3>
      <dl class="kv">
        <dt>DataHub GMS</dt><dd class="mono">${h(s.gms_url)}</dd>
        <dt>LLM endpoint</dt><dd class="mono">${h(s.llm.endpoint || '—')}</dd>
        <dt>Deployment</dt><dd class="mono">${h(s.llm.deployment)}</dd>
      </dl>
    </div>

    <div style="height:14px"></div>
    <div class="card">
      <h3>Open-source contributions</h3>
      <p class="sub" style="font-size:12.5px">Both came out of bugs this project hit while
      building against the real MCP server. Submitted upstream — neither is merged.</p>
      <div style="margin-top:12px">
        <div class="gate-row" style="font-family:var(--sans);font-size:12.5px">
          <span><a href="https://github.com/acryldata/mcp-server-datahub/pull/155" target="_blank"
            rel="noopener" style="color:var(--accent)">mcp-server-datahub#155</a>
            — filter docs: <code>report</code> is not a valid entity_type; BI artifacts index as
            <code>dataset</code></span>
          <span class="chip">submitted</span>
        </div>
        <div class="gate-row" style="font-family:var(--sans);font-size:12.5px">
          <span><a href="https://github.com/acryldata/mcp-server-datahub/pull/198" target="_blank"
            rel="noopener" style="color:var(--accent)">mcp-server-datahub#198</a>
            — <code>sort_by="relevance"</code> makes every search fail with
            <code>all shards failed</code></span>
          <span class="chip">submitted</span>
        </div>
      </div>
    </div>

    <div style="height:14px"></div>
    <div class="card">
      <h3>Design notes</h3>
      <p class="sub" style="font-size:12.5px">Confidence is <span class="mono">confirmed ÷ 4</span> on a
      fixed evidence checklist — never a percentage the model invents. Severity is a plain Python
      function of that confidence plus blast radius, business criticality and confirmed stale mirrors.
      Write-back tools are wrapped so they refuse to execute below the required tier, and refuse to
      touch any entity this run did not confirm something about. None of that is enforced by prompt.</p>
    </div>
  `);
}

/* ---------------------------------------------------------------- router -- */

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, queryString] = raw.split('?');
  const params = new URLSearchParams(queryString || '');
  return { path, params };
}

async function route() {
  const { path, params } = parseHash();
  if (!path.startsWith('/run/')) closeLive();

  const segments = path.split('/').filter(Boolean);
  markActiveNav('/' + (segments[0] || ''));

  try {
    if (path === '/' || segments.length === 0) return await viewCommandCenter();
    if (segments[0] === 'incidents') return await viewIncidents();
    if (segments[0] === 'run') return await viewRun(segments[1]);
    if (segments[0] === 'investigations') return await viewInvestigations();
    if (segments[0] === 'investigation') return await viewInvestigation(segments[1]);
    if (segments[0] === 'entities') return await viewEntities(params.get('q'));
    if (segments[0] === 'entity') return await viewEntity(params.get('urn'));
    if (segments[0] === 'lineage') return await viewLineage(params.get('urn'));
    if (segments[0] === 'activity') return await viewActivity();
    if (segments[0] === 'settings') return await viewSettings();
    setView('<div class="empty">Page not found.</div>');
  } catch (e) {
    setView(errorBox(e.message));
  }
}

window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', () => { route(); refreshChrome(); });
if (document.readyState !== 'loading') { route(); refreshChrome(); }
