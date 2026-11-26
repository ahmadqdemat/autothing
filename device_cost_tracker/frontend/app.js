/**
 * Device Power Cost Tracker — Frontend
 * Pure vanilla JS, no dependencies.
 */

// ── Base path (handles HA ingress prefix) ─────────────────────────────────
const BASE = (() => {
  const p = window.location.pathname.replace(/\/$/, '');
  return p.endsWith('/index.html') ? p.slice(0, -'/index.html'.length) : p;
})();

const api = (path) => `${BASE}${path}`;

// ── State ─────────────────────────────────────────────────────────────────
let allDevices = [];
let editEntityId = null;         // null = add mode, string = edit mode
let refreshTimer = null;
let allEntities = [];
const DEFAULT_PRICE = 0.62;
let CURRENCY = 'AED';

// ── Init ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initModal();
  initTokenForm();
  loadDashboard();
  loadDevices();
  loadConfig();
  loadStatus();
  refreshTimer = setInterval(loadDashboard, 15_000);
});

// ── Tabs ──────────────────────────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(`page-${tab.dataset.tab}`).classList.add('active');
      if (tab.dataset.tab === 'devices') loadDevices();
      if (tab.dataset.tab === 'settings') { loadConfig(); loadStatus(); }
    });
  });
}

// ── Dashboard ─────────────────────────────────────────────────────────────
async function loadDashboard() {
  try {
    const [dash, status] = await Promise.all([
      fetchJson(api('/api/dashboard')),
      fetchJson(api('/api/status')),
    ]);

    // Status dots
    setDot('ha-dot', 'ha-label', status.ha_connected, 'HA');
    setDot('influx-dot', 'influx-label', status.influx_status === 'connected', 'InfluxDB');

    // Stats
    setText('stat-active', dash.active_count);
    setText('stat-total-sub', `of ${dash.total_devices} configured`);
    setText('stat-today-cost', formatAed(dash.today_cost));
    setText('stat-today-energy', `${(dash.today_energy_kwh || 0).toFixed(3)} kWh`);
    setText('stat-month-cost', formatAed(dash.month_cost));
    setText('stat-month-energy', `${(dash.month_energy_kwh || 0).toFixed(3)} kWh`);

    const runningCost = (dash.active_devices || []).reduce((s, d) => s + d.current_cost, 0);
    setText('stat-running-cost', formatAed(runningCost));

    // Active device cards
    renderActiveGrid(dash.active_devices || []);

    // Last refresh time
    const now = new Date();
    setText('last-refresh', `Updated ${now.toLocaleTimeString()}`);
  } catch (e) {
    console.error('Dashboard error:', e);
  }
}

function renderActiveGrid(devices) {
  const grid = document.getElementById('active-grid');
  if (!devices.length) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1">
        <div class="empty-icon">💤</div>
        <div>No devices are currently running</div>
      </div>`;
    return;
  }
  grid.innerHTML = devices.map(d => `
    <div class="device-card">
      <div class="device-card-name">${esc(d.name)}</div>
      <div class="device-card-eid">${esc(d.entity_id)}</div>
      <div class="device-card-row">
        <span class="label">State</span>
        <span class="state-badge">${esc(d.state)}</span>
      </div>
      <div class="device-card-row">
        <span class="label">Runtime</span>
        <span class="value">${formatDuration(d.runtime_seconds)}</span>
      </div>
      <div class="device-card-row">
        <span class="label">Power</span>
        <span class="value">${d.power_w} W</span>
      </div>
      <div class="device-card-row">
        <span class="label">Energy</span>
        <span class="value">${(d.current_energy_kwh || 0).toFixed(4)} kWh</span>
      </div>
      <div class="device-card-row">
        <span class="label">Est. Cost</span>
        <span class="value" style="color:var(--yellow);font-weight:700">${formatAed(d.current_cost)}</span>
      </div>
    </div>`).join('');
}

// ── Devices table ─────────────────────────────────────────────────────────
async function loadDevices() {
  try {
    allDevices = await fetchJson(api('/api/devices'));
    renderDevicesTable(allDevices);
  } catch (e) {
    console.error('Devices error:', e);
  }
}

function renderDevicesTable(devices) {
  const tbody = document.getElementById('devices-tbody');
  if (!devices.length) {
    tbody.innerHTML = `<tr><td colspan="9">
      <div class="empty-state"><div class="empty-icon">📋</div><div>No devices configured. Click "+ Add Device" to start.</div></div>
    </td></tr>`;
    return;
  }

  tbody.innerHTML = devices.map(d => {
    const isOn = d.is_active;
    const live = d.live || {};
    return `
      <tr>
        <td><span class="active-dot ${isOn ? 'on' : ''}"></span>${isOn ? 'Running' : 'Idle'}</td>
        <td style="font-weight:500">${esc(d.name)}</td>
        <td style="font-family:monospace;font-size:11px;color:var(--muted)">
          ${esc(d.entity_id)}
          ${d.power_entity_id ? `<div style="margin-top:2px;color:var(--accent,#58a6ff)">${esc(d.power_entity_id)}</div>` : ''}
        </td>
        <td>${d.power_w} W</td>
        <td>${formatAed(d.price_per_kwh)}</td>
        <td style="color:var(--yellow)">${formatAed(d.today_cost + (live.current_cost || 0))}</td>
        <td style="color:var(--purple)">${formatAed(d.month_cost + (live.current_cost || 0))}</td>
        <td>${((d.total_energy_kwh || 0) + (live.current_energy_kwh || 0)).toFixed(3)} kWh</td>
        <td>
          <button class="btn btn-ghost btn-sm" onclick="openEditModal('${esc(d.entity_id)}')">Edit</button>
          <button class="btn btn-danger btn-sm" style="margin-left:4px" onclick="deleteDevice('${esc(d.entity_id)}', '${esc(d.name)}')">Delete</button>
        </td>
      </tr>`;
  }).join('');
}

// ── Add / Edit modal ──────────────────────────────────────────────────────
function initModal() {
  document.getElementById('btn-add-device').addEventListener('click', openAddModal);
  document.getElementById('modal-close-btn').addEventListener('click', closeModal);
  document.getElementById('modal-cancel-btn').addEventListener('click', closeModal);
  document.getElementById('modal-save-btn').addEventListener('click', saveDevice);
  document.getElementById('modal-add').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });

  // Auto-fill name when entity selected
  document.getElementById('form-entity').addEventListener('change', e => {
    const eid = e.target.value;
    const ent = allEntities.find(x => x.entity_id === eid);
    if (ent && !document.getElementById('form-name').value) {
      document.getElementById('form-name').value = ent.name;
    }
  });
}

async function openAddModal() {
  editEntityId = null;
  document.getElementById('modal-title').textContent = 'Add Device';
  document.getElementById('form-name').value = '';
  document.getElementById('form-power').value = '';
  document.getElementById('form-price').value = DEFAULT_PRICE;
  document.getElementById('form-error').style.display = 'none';
  document.getElementById('form-entity').disabled = false;
  await loadEntitySelector();
  document.getElementById('modal-add').classList.add('open');
}

function openEditModal(entityId) {
  editEntityId = entityId;
  const dev = allDevices.find(d => d.entity_id === entityId);
  if (!dev) return;
  document.getElementById('modal-title').textContent = 'Edit Device';
  document.getElementById('form-entity').innerHTML = `<option value="${esc(dev.entity_id)}">${esc(dev.entity_id)}</option>`;
  document.getElementById('form-entity').disabled = true;
  document.getElementById('form-name').value = dev.name;
  document.getElementById('form-power').value = dev.power_w;
  document.getElementById('form-price').value = dev.price_per_kwh;
  document.getElementById('form-error').style.display = 'none';
  document.getElementById('modal-add').classList.add('open');
}

function closeModal() {
  document.getElementById('modal-add').classList.remove('open');
  editEntityId = null;
}

async function loadEntitySelector() {
  const sel = document.getElementById('form-entity');
  sel.innerHTML = '<option value="">Loading…</option>';
  try {
    allEntities = await fetchJson(api('/api/entities'));
    const configured = new Set(allDevices.map(d => d.entity_id));
    const available = allEntities.filter(e => !configured.has(e.entity_id));
    if (!available.length) {
      sel.innerHTML = '<option value="">All entities already configured</option>';
      return;
    }
    sel.innerHTML = '<option value="">— select entity —</option>' +
      available.map(e =>
        `<option value="${esc(e.entity_id)}">${esc(e.name)} (${esc(e.entity_id)})</option>`
      ).join('');
  } catch {
    sel.innerHTML = '<option value="">Failed to load entities</option>';
  }
}

async function saveDevice() {
  const entity_id = document.getElementById('form-entity').value.trim();
  const name      = document.getElementById('form-name').value.trim();
  const power_w   = parseFloat(document.getElementById('form-power').value);
  const price     = parseFloat(document.getElementById('form-price').value || DEFAULT_PRICE);

  const errEl = document.getElementById('form-error');
  errEl.style.display = 'none';

  if (!editEntityId && !entity_id) { showFormError('Please select an entity.'); return; }
  if (!name) { showFormError('Please enter a device name.'); return; }
  if (!power_w || power_w <= 0) { showFormError('Please enter a valid power value (> 0 W).'); return; }

  const body = { entity_id: editEntityId || entity_id, name, power_w, price_per_kwh: price };
  const saveBtn = document.getElementById('modal-save-btn');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';

  try {
    if (editEntityId) {
      await fetchJson(api(`/api/devices/${encodeURIComponent(editEntityId)}`), {
        method: 'PUT', body: JSON.stringify({ name, power_w, price_per_kwh: price }),
      });
      showToast(`Updated ${name}`, 'success');
    } else {
      await fetchJson(api('/api/devices'), {
        method: 'POST', body: JSON.stringify(body),
      });
      showToast(`Added ${name}`, 'success');
    }
    closeModal();
    await loadDevices();
    loadDashboard();
  } catch (e) {
    showFormError(e.message || 'Failed to save device.');
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save Device';
  }
}

function showFormError(msg) {
  const el = document.getElementById('form-error');
  el.textContent = msg;
  el.style.display = 'block';
}

async function deleteDevice(entityId, name) {
  if (!confirm(`Remove "${name}" from tracking?\n\nAccumulated history will be preserved in InfluxDB.`)) return;
  try {
    await fetchJson(api(`/api/devices/${encodeURIComponent(entityId)}`), { method: 'DELETE' });
    showToast(`Removed ${name}`, 'success');
    await loadDevices();
    loadDashboard();
  } catch (e) {
    showToast(`Failed to delete: ${e.message}`, 'error');
  }
}

// ── Token form ────────────────────────────────────────────────────────────
function initTokenForm() {
  const saveBtn = document.getElementById('token-save-btn');
  if (!saveBtn) return;
  saveBtn.addEventListener('click', async () => {
    const token = document.getElementById('token-input').value.trim();
    const msgEl = document.getElementById('token-msg');
    if (!token) { msgEl.textContent = 'Please paste a token.'; msgEl.style.color = 'var(--red)'; return; }
    saveBtn.disabled = true; saveBtn.textContent = 'Saving…';
    try {
      await fetchJson(api('/api/token'), { method: 'POST', body: JSON.stringify({ token }) });
      msgEl.textContent = 'Token saved! HA will reconnect in a few seconds.';
      msgEl.style.color = 'var(--green)';
      document.getElementById('token-input').value = '';
      setTimeout(() => { loadStatus(); loadDashboard(); }, 5000);
    } catch(e) {
      msgEl.textContent = 'Error: ' + e.message;
      msgEl.style.color = 'var(--red)';
    } finally { saveBtn.disabled = false; saveBtn.textContent = 'Save & Connect'; }
  });
}

function updateTokenStatusBar(status) {
  const bar = document.getElementById('token-status-bar');
  if (!bar) return;
  if (status.ha_connected) {
    bar.style.display = 'block';
    bar.style.background = 'rgba(16,185,129,.15)'; bar.style.color = 'var(--green)';
    bar.textContent = 'Connected to Home Assistant';
  } else if (status.has_token) {
    bar.style.display = 'block';
    bar.style.background = 'rgba(245,158,11,.15)'; bar.style.color = 'var(--yellow)';
    bar.textContent = 'Token saved but not connected yet — waiting for reconnect…';
  } else {
    bar.style.display = 'block';
    bar.style.background = 'rgba(239,68,68,.15)'; bar.style.color = 'var(--red)';
    bar.textContent = 'No token configured — paste your HA token below to connect.';
  }
}

// ── Settings ──────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const cfg = await fetchJson(api('/api/config'));
    if (cfg.currency) CURRENCY = cfg.currency;
    const el = document.getElementById('config-table');
    el.innerHTML = Object.entries(cfg).map(([k, v]) => `
      <div class="config-row">
        <span class="config-key">${esc(k)}</span>
        <span class="config-val">${esc(String(v))}</span>
      </div>`).join('');
  } catch {}
}

async function loadStatus() {
  try {
    const s = await fetchJson(api('/api/status'));
    const el = document.getElementById('status-table');
    el.innerHTML = `
      <div class="config-row"><span class="config-key">HA WebSocket</span>
        <span style="color:${s.ha_connected ? 'var(--green)' : 'var(--red)'}">${s.ha_connected ? '✓ Connected' : '✗ Disconnected'}</span></div>
      <div class="config-row"><span class="config-key">InfluxDB</span>
        <span style="color:${s.influx_status === 'connected' ? 'var(--green)' : 'var(--red)'}">${s.influx_status === 'connected' ? '✓ Connected' : '✗ Offline'}</span></div>
      <div class="config-row"><span class="config-key">Active Sessions</span><span class="config-val">${s.active_sessions}</span></div>
      <div class="config-row"><span class="config-key">Total Devices</span><span class="config-val">${s.total_devices}</span></div>
      <div class="config-row"><span class="config-key">Version</span><span class="config-val">${s.version}</span></div>`;
    updateTokenStatusBar(s);
  } catch {}
}

// ── Utilities ─────────────────────────────────────────────────────────────
async function fetchJson(url, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const resp = await fetch(url, { ...opts, headers });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(body.message || body.reason || `HTTP ${resp.status}`);
  }
  return body;
}

function formatAed(val) {
  const n = parseFloat(val) || 0;
  return `${CURRENCY} ${n.toFixed(2)}`;
}

function formatDuration(seconds) {
  if (!seconds) return '0s';
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setDot(dotId, labelId, ok, label) {
  const dot = document.getElementById(dotId);
  const lbl = document.getElementById(labelId);
  if (dot) dot.classList.toggle('ok', ok);
  if (lbl) lbl.textContent = `${label}: ${ok ? 'OK' : 'Offline'}`;
}

let toastTimer;
function showToast(msg, type = 'info') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.classList.remove('show'); }, 3500);
}
