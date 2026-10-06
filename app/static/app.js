/* JAISafe LLM Gateway - WebUI */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const FORMAT_LABEL = {
  openai: 'OpenAI Chat',
  anthropic: 'Anthropic Messages',
  openai_responses: 'OpenAI Responses',
  openai_completions: 'OpenAI Completions',
  passthrough: '透传',
};

const state = {
  channels: [],
  channelTypes: [],
  keys: [],
  logOffset: 0,
  logLimit: 30,
  logTotal: 0,
  logTimer: null,
  siteName: 'JAISafe LLM Gateway',
};

/* ------------------------------------------------------------------ */
/* 基础工具                                                             */
/* ------------------------------------------------------------------ */
function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(path, opts = {}) {
  const res = await fetch('/admin/api' + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.status === 401) {
    showLogin();
    throw new Error('登录已过期');
  }
  if (!res.ok) throw new Error((data && (data.detail || data.message)) || ('HTTP ' + res.status));
  return data;
}

function fmtNum(n) {
  n = Number(n || 0);
  return n.toLocaleString('en-US');
}

function fmtDuration(ms) {
  ms = Number(ms || 0);
  if (ms < 1000) return ms + ' ms';
  return (ms / 1000).toFixed(2) + ' s';
}

function pretty(obj) {
  if (obj === null || obj === undefined || obj === '') return '—';
  if (typeof obj === 'string') {
    try { return JSON.stringify(JSON.parse(obj), null, 2); } catch (e) { return obj; }
  }
  try { return JSON.stringify(obj, null, 2); } catch (e) { return String(obj); }
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(
    () => toast('已复制', 'ok'),
    () => toast('复制失败', 'err'));
}

function statusBadge(status, error) {
  const s = Number(status || 0);
  if (s >= 200 && s < 300) return `<span class="badge ok">${s}</span>`;
  if (s === 0) return `<span class="badge err" title="${esc(error || '')}">失败</span>`;
  return `<span class="badge err" title="${esc(error || '')}">${s}</span>`;
}

/* ------------------------------------------------------------------ */
/* 登录                                                                */
/* ------------------------------------------------------------------ */
function showLogin() {
  $('#loginWrap').classList.remove('hidden');
  $('#app').classList.add('hidden');
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
}

function showApp() {
  $('#loginWrap').classList.add('hidden');
  $('#app').classList.remove('hidden');
}

async function doLogin() {
  const pw = $('#loginPassword').value;
  $('#loginError').textContent = '';
  try {
    const res = await fetch('/admin/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ password: pw }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      $('#loginError').textContent = d.detail || '登录失败';
      return;
    }
    $('#loginPassword').value = '';
    showApp();
    await boot();
    switchPage(location.hash.replace('#', '') || 'overview');
  } catch (e) {
    $('#loginError').textContent = String(e);
  }
}

async function doLogout() {
  try { await fetch('/admin/api/logout', { method: 'POST', credentials: 'same-origin' }); }
  catch (e) { /* ignore */ }
  showLogin();
}

/* ------------------------------------------------------------------ */
/* 导航                                                                */
/* ------------------------------------------------------------------ */
function switchPage(name, updateHash = true) {
  if (!name || !$('#page-' + name)) name = 'overview';
  $$('#nav button').forEach(b => b.classList.toggle('active', b.dataset.page === name));
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + name));
  if (updateHash && location.hash !== '#' + name) location.hash = name;
  if (name === 'overview') loadOverview();
  if (name === 'channels') loadChannels();
  if (name === 'keys') loadKeys();
  if (name === 'logs') loadLogs(0);
  if (name === 'mask') loadMask();
  if (name === 'settings') loadSettings();
  if (name === 'playground') fillPlaygroundDefaults();
}

/* ------------------------------------------------------------------ */
/* 概览                                                                */
/* ------------------------------------------------------------------ */
async function loadOverview() {
  try {
    const d = await api('/overview');
    const s = d.stats || {};
    $('#statCards').innerHTML = [
      card('总请求数', fmtNum(s.total_requests), true),
      card('今日请求', fmtNum(s.today_requests)),
      card('输入 Tokens', fmtNum(s.prompt_tokens)),
      card('输出 Tokens', fmtNum(s.completion_tokens)),
      card('平均耗时', fmtDuration(s.avg_duration_ms)),
      card('失败请求', fmtNum(s.errors)),
      card('渠道数', fmtNum((d.channels || []).length)),
      card('API 密钥', fmtNum(d.keys)),
    ].join('');

    $('#overviewSub').textContent =
      `${(d.channels || []).filter(c => c.enabled).length} 个启用渠道 · ` +
      (d.require_api_key ? '已开启密钥校验' : '未开启密钥校验');

    $('#overviewChannels').innerHTML = table(
      ['名称', '类型', 'Base URL', '模型', '优先级', '状态'],
      (d.channels || []).map(c => [
        esc(c.name),
        `<span class="badge type">${esc(FORMAT_LABEL[c.type] || c.type)}</span>`,
        `<span class="mono-sm">${esc(c.base_url)}</span>`,
        `<span class="mono-sm">${esc((c.model_list || []).join(', ') || '全部')}</span>`,
        esc(c.priority),
        c.enabled ? '<span class="badge ok">启用</span>' : '<span class="badge">停用</span>',
      ]), '暂无渠道，请先到「渠道」页面添加');

    $('#overviewModels').innerHTML = table(
      ['模型', '请求数', 'Tokens'],
      (d.models || []).map(m => [esc(m.model || '-'), fmtNum(m.requests), fmtNum(m.tokens)]),
      '暂无数据');
    $('#overviewRecent').innerHTML = table(
      ['时间', '格式', '模型', '状态', '耗时'],
      (d.recent || []).map(l => [
        `<span class="mono-sm">${esc((l.created_at || '').slice(11))}</span>`,
        `<span class="badge type">${esc(FORMAT_LABEL[l.client_format] || l.client_format)}</span>`,
        esc(l.request_model || '-'),
        statusBadge(l.status, l.error),
        fmtDuration(l.duration_ms),
      ]), '暂无请求');
  } catch (e) {
    toast('加载概览失败: ' + e.message, 'err');
  }
}

function card(label, value, accent) {
  return `<div class="card${accent ? ' accent' : ''}"><div class="label">${esc(label)}</div>
    <div class="value">${esc(value)}</div></div>`;
}

function table(headers, rows, emptyText) {
  if (!rows || !rows.length) return `<div class="empty">${esc(emptyText || '暂无数据')}</div>`;
  return `<table><thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map((c, i) => `<td${i === 0 ? ' class="nowrap"' : ''}>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}

/* ------------------------------------------------------------------ */
/* 渠道                                                                */
/* ------------------------------------------------------------------ */
async function loadChannels() {
  try {
    const d = await api('/channels');
    state.channels = d.items || [];
    state.channelTypes = d.types || [];
    $('#channelTable').innerHTML = state.channels.length ? `
      <table><thead><tr>
        <th>名称</th><th>类型</th><th>Base URL</th><th>模型</th><th>优先级</th>
        <th>状态</th><th class="right">操作</th></tr></thead><tbody>
      ${state.channels.map(c => `
        <tr>
          <td><b>${esc(c.name)}</b>${c.remark ? `<div class="mono-sm">${esc(c.remark)}</div>` : ''}</td>
          <td><span class="badge type">${esc(FORMAT_LABEL[c.type] || c.type)}</span></td>
          <td class="mono">${esc(c.base_url)}</td>
          <td class="mono">${esc((c.model_list || []).join(', ') || '全部模型')}
            ${Object.keys(c.model_map || {}).length ? `<div class="mono-sm">映射: ${esc(JSON.stringify(c.model_map))}</div>` : ''}</td>
          <td>${esc(c.priority)}</td>
          <td>${c.enabled ? '<span class="badge ok">启用</span>' : '<span class="badge">停用</span>'}
            ${c.mask_mode && c.mask_mode !== 'inherit'
              ? `<div><span class="badge mask">脱敏:${esc(c.mask_mode)}</span></div>` : ''}</td>
          <td class="right nowrap">
            <button class="btn mini" data-act="test" data-id="${c.id}">测试</button>
            <button class="btn mini" data-act="edit" data-id="${c.id}">编辑</button>
            <button class="btn mini danger" data-act="del" data-id="${c.id}">删除</button>
          </td>
        </tr>`).join('')}
      </tbody></table>` : '<div class="empty">暂无渠道，点击右上角新增</div>';

    $$('#channelTable button[data-act]').forEach(btn => {
      btn.onclick = () => {
        const id = Number(btn.dataset.id);
        if (btn.dataset.act === 'edit') editChannel(id);
        if (btn.dataset.act === 'del') deleteChannel(id);
        if (btn.dataset.act === 'test') testChannel(id, btn);
      };
    });
  } catch (e) {
    toast('加载渠道失败: ' + e.message, 'err');
  }
}

function channelForm(c) {
  c = c || { type: 'openai', priority: 0, timeout: 300, stream_usage: 1, enabled: 1 };
  const types = state.channelTypes.map(t =>
    `<option value="${t.value}"${c.type === t.value ? ' selected' : ''}>${esc(t.label)}</option>`).join('');
  return `
    <div class="row">
      <label class="field"><span>名称 *</span><input type="text" id="chName" value="${esc(c.name || '')}" placeholder="例如 OpenAI 官方"></label>
      <label class="field"><span>类型 *</span><select id="chType">${types}</select></label>
    </div>
    <label class="field"><span>Base URL *</span>
      <input type="text" id="chBaseUrl" value="${esc(c.base_url || '')}" placeholder="https://api.openai.com/v1">
      <div class="hint">填到 /v1 一级即可；若已包含 /v1 会自动识别。</div>
    </label>
    <label class="field"><span>API Key</span><input type="text" id="chApiKey" value="${esc(c.api_key || '')}" placeholder="sk-..."></label>
    <label class="field"><span>支持的模型</span>
      <input type="text" id="chModels" value="${esc((c.model_list || []).join(', '))}" placeholder="gpt-4o, gpt-4o-mini（留空表示全部）">
      <div class="hint">逗号分隔，支持通配符前缀，如 claude-*。</div>
    </label>
    <label class="field"><span>模型映射 (JSON)</span>
      <textarea id="chModelMap" rows="2" placeholder='{"gpt-4o": "gpt-4o-2024-11-20", "*": "gpt-4o-mini"}'>${esc(JSON.stringify(c.model_map || {}, null, 0))}</textarea>
      <div class="hint">左侧为客户端请求的模型名，右侧为转发给上游的模型名。</div>
    </label>
    <label class="field"><span>额外请求头 (JSON)</span>
      <textarea id="chHeaders" rows="2" placeholder='{"X-Custom": "value"}'>${esc(JSON.stringify(c.extra_headers || {}, null, 0))}</textarea>
    </label>
    <div class="row">
      <label class="field"><span>优先级（越大越优先）</span><input type="number" id="chPriority" value="${Number(c.priority || 0)}"></label>
      <label class="field"><span>超时（秒）</span><input type="number" id="chTimeout" value="${Number(c.timeout || 300)}"></label>
    </div>
    <label class="field"><span>本地上下文脱敏</span>
      <select id="chMaskMode">
        <option value="inherit"${(c.mask_mode || 'inherit') === 'inherit' ? ' selected' : ''}>inherit — 跟随全局设置</option>
        <option value="off"${c.mask_mode === 'off' ? ' selected' : ''}>off — 该渠道不脱敏</option>
        <option value="enforce"${c.mask_mode === 'enforce' ? ' selected' : ''}>enforce — 该渠道强制脱敏</option>
      </select>
      <div class="hint">上游是本地模型（Ollama 等）时可设为 off；是外部厂商时建议 enforce。</div>
    </label>
    <div class="check"><input type="checkbox" id="chStreamUsage" ${c.stream_usage ? 'checked' : ''}>
      <label for="chStreamUsage">流式请求向上游索取 usage 统计（OpenAI 兼容渠道）</label></div>
    <div class="check"><input type="checkbox" id="chEnabled" ${c.enabled ? 'checked' : ''}>
      <label for="chEnabled">启用该渠道</label></div>`;
}

function addChannel() {
  openModal('新增渠道', channelForm(null), async () => {
    const payload = collectChannel();
    await api('/channels', { method: 'POST', body: JSON.stringify(payload) });
    toast('渠道已创建', 'ok');
    closeModal();
    loadChannels();
  });
}

function editChannel(id) {
  const c = state.channels.find(x => x.id === id);
  if (!c) return;
  openModal('编辑渠道', channelForm(c), async () => {
    const payload = collectChannel();
    await api('/channels/' + id, { method: 'PUT', body: JSON.stringify(payload) });
    toast('渠道已保存', 'ok');
    closeModal();
    loadChannels();
  });
}

function collectChannel() {
  const parseJson = (id, fallback) => {
    const raw = $(id).value.trim();
    if (!raw) return fallback;
    try { return JSON.parse(raw); } catch (e) { throw new Error('JSON 格式错误: ' + raw); }
  };
  return {
    name: $('#chName').value.trim(),
    type: $('#chType').value,
    base_url: $('#chBaseUrl').value.trim(),
    api_key: $('#chApiKey').value.trim(),
    models: $('#chModels').value.trim(),
    model_map: parseJson('#chModelMap', {}),
    extra_headers: parseJson('#chHeaders', {}),
    priority: Number($('#chPriority').value || 0),
    timeout: Number($('#chTimeout').value || 300),
    stream_usage: $('#chStreamUsage').checked ? 1 : 0,
    enabled: $('#chEnabled').checked ? 1 : 0,
    mask_mode: $('#chMaskMode').value,
  };
}

async function deleteChannel(id) {
  const c = state.channels.find(x => x.id === id);
  if (!confirm(`确定删除渠道「${c ? c.name : id}」？`)) return;
  try {
    await api('/channels/' + id, { method: 'DELETE' });
    toast('已删除', 'ok');
    loadChannels();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

async function testChannel(id, btn) {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = '测试中…';
  try {
    const r = await api('/channels/' + id + '/test', { method: 'POST' });
    const head = r.ok
      ? `成功 · HTTP ${r.status} · ${r.elapsed_ms}ms · ${r.model}`
      : `失败 · HTTP ${r.status} · ${r.elapsed_ms}ms`;
    openModal('渠道测试 · ' + r.url, `
      <div class="kv">
        <div class="k">结果</div><div class="v" style="color:${r.ok ? 'var(--ok)' : 'var(--err)'}">${esc(head)}</div>
        <div class="k">请求地址</div><div class="v">${esc(r.url)}</div>
        <div class="k">使用模型</div><div class="v">${esc(r.model)}</div>
      </div>
      <pre class="code">${esc(r.body || '')}</pre>`, null, '关闭');
  } catch (e) {
    toast('测试失败: ' + e.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = old;
  }
}

/* ------------------------------------------------------------------ */
/* 密钥                                                                */
/* ------------------------------------------------------------------ */
async function loadKeys() {
  try {
    const d = await api('/keys');
    state.keys = d.items || [];
    $('#keySub').textContent = d.effective_require
      ? '已开启鉴权：请使用下方密钥访问 /v1 接口'
      : '当前未开启鉴权（创建任意启用密钥后自动开启）';
    $('#keyTable').innerHTML = state.keys.length ? `
      <table><thead><tr><th>名称</th><th>密钥</th><th>状态</th><th>累计调用</th>
      <th>最近使用</th><th class="right">操作</th></tr></thead><tbody>
      ${state.keys.map(k => `
        <tr>
          <td><b>${esc(k.name)}</b>${k.remark ? `<div class="mono-sm">${esc(k.remark)}</div>` : ''}</td>
          <td class="mono">${esc(k.key)}<span class="copy" data-copy="${esc(k.key)}" title="复制到剪贴板">复制</span></td>
          <td>${k.enabled ? '<span class="badge ok">启用</span>' : '<span class="badge">停用</span>'}</td>
          <td>${fmtNum(k.used)}</td>
          <td class="mono-sm">${esc(k.last_used || '从未')}</td>
          <td class="right nowrap">
            <button class="btn mini" data-act="edit" data-id="${k.id}">编辑</button>
            <button class="btn mini danger" data-act="del" data-id="${k.id}">删除</button>
          </td>
        </tr>`).join('')}
      </tbody></table>` : '<div class="empty">暂无密钥</div>';

    $$('#keyTable .copy').forEach(el => {
      el.onclick = () => copyText(el.dataset.copy);
    });
    $$('#keyTable button[data-act]').forEach(btn => {
      btn.onclick = () => {
        const id = Number(btn.dataset.id);
        if (btn.dataset.act === 'edit') editKey(id);
        if (btn.dataset.act === 'del') deleteKey(id);
      };
    });
  } catch (e) {
    toast('加载密钥失败: ' + e.message, 'err');
  }
}

function keyForm(k) {
  k = k || { enabled: 1 };
  return `
    <label class="field"><span>名称</span><input type="text" id="kName" value="${esc(k.name || '')}" placeholder="例如 我的客户端"></label>
    <label class="field"><span>密钥</span><input type="text" id="kKey" value="${esc(k.key || '')}" placeholder="留空自动生成 sk-jai-..."></label>
    <label class="field"><span>备注</span><input type="text" id="kRemark" value="${esc(k.remark || '')}"></label>
    <div class="check"><input type="checkbox" id="kEnabled" ${k.enabled ? 'checked' : ''}><label for="kEnabled">启用</label></div>`;
}

function addKey() {
  openModal('新增密钥', keyForm(null), async () => {
    await api('/keys', {
      method: 'POST',
      body: JSON.stringify({
        name: $('#kName').value.trim(), key: $('#kKey').value.trim(),
        remark: $('#kRemark').value.trim(), enabled: $('#kEnabled').checked ? 1 : 0,
      }),
    });
    toast('密钥已创建', 'ok');
    closeModal();
    loadKeys();
  });
}

function editKey(id) {
  const k = state.keys.find(x => x.id === id);
  if (!k) return;
  openModal('编辑密钥', keyForm(k), async () => {
    await api('/keys/' + id, {
      method: 'PUT',
      body: JSON.stringify({
        name: $('#kName').value.trim(), key: $('#kKey').value.trim(),
        remark: $('#kRemark').value.trim(), enabled: $('#kEnabled').checked ? 1 : 0,
      }),
    });
    toast('已保存', 'ok');
    closeModal();
    loadKeys();
  });
}

async function deleteKey(id) {
  const k = state.keys.find(x => x.id === id);
  if (!confirm(`确定删除密钥「${k ? k.name : id}」？`)) return;
  try {
    await api('/keys/' + id, { method: 'DELETE' });
    toast('已删除', 'ok');
    loadKeys();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

/* ------------------------------------------------------------------ */
/* 日志                                                                */
/* ------------------------------------------------------------------ */
async function loadLogs(offset) {
  if (offset !== undefined) state.logOffset = offset;
  const params = new URLSearchParams({
    limit: state.logLimit,
    offset: state.logOffset,
    keyword: $('#logKeyword').value.trim(),
    status: $('#logStatus').value,
  });
  try {
    const d = await api('/logs?' + params.toString());
    state.logTotal = d.total || 0;
    const items = d.items || [];
    $('#logTable').innerHTML = items.length ? `
      <table><thead><tr>
        <th>时间</th><th>接口</th><th>模型</th><th>渠道</th><th>格式转换</th>
        <th>状态</th><th>耗时</th><th>Tokens</th><th></th></tr></thead><tbody>
      ${items.map(l => `
        <tr data-log="${l.id}" style="cursor:pointer">
          <td class="nowrap mono-sm">${esc(l.created_at || '')}</td>
          <td class="nowrap">${esc((l.path || '').replace('/v1/', ''))}${l.stream ? ' <span class="badge stream">流</span>' : ''}${logMaskBadge(l)}</td>
          <td class="mono">${esc(l.request_model || '-')}</td>
          <td>${esc(l.channel_name || '-')}</td>
          <td class="mono-sm">${esc(FORMAT_LABEL[l.client_format] || l.client_format)} → ${esc(FORMAT_LABEL[l.upstream_format] || l.upstream_format || '-')}${l.retries ? ` <span class="badge">重试${l.retries}</span>` : ''}</td>
          <td>${statusBadge(l.status, l.error)}</td>
          <td class="nowrap">${fmtDuration(l.duration_ms)}</td>
          <td class="nowrap mono-sm">${fmtNum(l.total_tokens)}</td>
          <td class="right"><button class="btn mini" data-open="${l.id}">详情</button></td>
        </tr>`).join('')}
      </tbody></table>` : '<div class="empty">暂无日志</div>';

    const from = state.logTotal ? state.logOffset + 1 : 0;
    const to = Math.min(state.logOffset + state.logLimit, state.logTotal);
    $('#logPager').innerHTML = `
      <span class="muted" style="margin-right:12px">共 ${fmtNum(state.logTotal)} 条 · ${from}-${to}</span>
      <button class="btn mini" id="logPrev" ${state.logOffset <= 0 ? 'disabled' : ''}>上一页</button>
      <button class="btn mini" id="logNext" ${to >= state.logTotal ? 'disabled' : ''}>下一页</button>`;
    const prev = $('#logPrev'), next = $('#logNext');
    if (prev) prev.onclick = () => loadLogs(Math.max(0, state.logOffset - state.logLimit));
    if (next) next.onclick = () => loadLogs(state.logOffset + state.logLimit);

    $$('#logTable tr[data-log]').forEach(tr => {
      tr.onclick = (ev) => {
        if (ev.target.tagName === 'BUTTON') return;
        showLogDetail(Number(tr.dataset.log));
      };
    });
    $$('#logTable button[data-open]').forEach(b => {
      b.onclick = (ev) => { ev.stopPropagation(); showLogDetail(Number(b.dataset.open)); };
    });
  } catch (e) {
    toast('加载日志失败: ' + e.message, 'err');
  }
}

function renderMaskTab(l) {
  let summary = {};
  try { summary = JSON.parse(l.mask_summary || '{}'); } catch (e) { summary = {}; }
  let mapping = [];
  try { mapping = JSON.parse(l.mask_map || '[]'); } catch (e) { mapping = []; }
  const mode = MASK_MODE_LABEL[l.mask_mode] || l.mask_mode;
  const byType = Object.entries(summary.by_type || {})
    .map(([k, v]) => `<span class="badge type">${esc(k)} × ${v}</span>`).join(' ');

  const previewBlock = l.mask_preview
    ? `<div style="margin-top:16px"><h4 class="muted" style="font-size:12px;font-weight:500;margin-bottom:6px">干跑预览（未实际发送，上游收到的是原文）</h4>
       <pre class="code" style="max-height:22vh">${esc(pretty(l.mask_preview))}</pre></div>`
    : '';

  const mapBlock = mapping.length
    ? `<div style="margin-top:16px">
        <h4 class="muted" style="font-size:12px;font-weight:500;margin-bottom:6px">本次请求的句柄映射（点击遮罩显示明文）</h4>
        <table><thead><tr><th>句柄（发送给上游）</th><th>真值（本机）</th><th>类型</th></tr></thead><tbody>
        ${mapping.map(m => `<tr>
          <td class="mono">${esc(m.handle)}</td>
          <td><span class="secret masked" onclick="this.classList.toggle('shown')">${esc(m.raw)}</span></td>
          <td><span class="badge type">${esc(m.slot_type || m.kind)}</span></td>
        </tr>`).join('')}</tbody></table></div>`
    : '<div class="muted" style="margin-top:16px">本次请求未产生新的句柄，或未开启映射记录。</div>';

  const lookup = `
    <div style="margin-top:16px">
      <h4 class="muted" style="font-size:12px;font-weight:500;margin-bottom:6px">按句柄反查真值</h4>
      <div class="toolbar">
        <input type="text" id="maskLookupInput" placeholder="粘贴一个句柄，如 [WORKSPACE_ROOT_1]">
        <button class="btn mini" id="maskLookupBtn">反查</button>
      </div>
      <pre class="code" id="maskLookupOut" style="max-height:12vh;margin-top:8px">—</pre>
    </div>`;

  setTimeout(() => {
    const btn = $('#maskLookupBtn');
    if (!btn) return;
    btn.onclick = async () => {
      const handle = $('#maskLookupInput').value.trim();
      if (!handle) return;
      try {
        const q = new URLSearchParams({ handle });
        if (l.mask_session) q.set('session_id', l.mask_session);
        const d = await api('/mask/lookup?' + q.toString());
        $('#maskLookupOut').textContent = d.raw
          ? `${d.raw}\n\n（会话 ${d.session_id}）` : '未找到该句柄';
      } catch (e) { $('#maskLookupOut').textContent = String(e.message || e); }
    };
  }, 0);

  return `
    <div class="kv" style="margin-bottom:0">
      <div class="k">模式</div><div class="v">${esc(mode)}</div>
      <div class="k">脱敏会话</div><div class="v">${esc(l.mask_session || '-')}</div>
      <div class="k">命中合计</div><div class="v">${fmtNum(summary.count)} 处
        （路径 ${fmtNum(summary.paths)} · 凭证 ${fmtNum(summary.credentials)}）</div>
      <div class="k">按类型</div><div class="v">${byType || '-'}</div>
    </div>
    <div style="margin-top:16px"><h4 class="muted" style="font-size:12px;font-weight:500;margin-bottom:6px">客户端原文与实际发送内容</h4>
      <div class="diff">
        <div><h4>客户端原文（本机）</h4><pre class="code" style="max-height:26vh">${esc(pretty(l.request_body))}</pre></div>
        <div><h4>实际发送给上游</h4><pre class="code" style="max-height:26vh">${esc(pretty(l.upstream_request))}</pre></div>
      </div>
    </div>
    ${mapBlock}
    ${previewBlock}
    ${lookup}`;
}

async function showLogDetail(id) {
  try {
    const l = await api('/logs/' + id);
    const tabs = [
      ['概要', `<div class="kv">
          <div class="k">时间</div><div class="v">${esc(l.created_at)}</div>
          <div class="k">接口</div><div class="v">${esc(l.method)} ${esc(l.path)}</div>
          <div class="k">客户端格式</div><div class="v">${esc(FORMAT_LABEL[l.client_format] || l.client_format)}</div>
          <div class="k">上游格式</div><div class="v">${esc(FORMAT_LABEL[l.upstream_format] || l.upstream_format || '-')}</div>
          <div class="k">渠道</div><div class="v">${esc(l.channel_name || '-')} (#${esc(l.channel_id)})</div>
          <div class="k">上游地址</div><div class="v">${esc(l.upstream_url || '-')}</div>
          <div class="k">模型</div><div class="v">${esc(l.request_model || '-')} → ${esc(l.upstream_model || '-')}</div>
          <div class="k">API Key</div><div class="v">${esc(l.api_key_name || '-')}</div>
          <div class="k">客户端 IP</div><div class="v">${esc(l.client_ip || '-')}</div>
          <div class="k">状态码</div><div class="v">${esc(l.status)}</div>
          <div class="k">耗时</div><div class="v">${fmtDuration(l.duration_ms)}</div>
          <div class="k">重试次数</div><div class="v">${esc(l.retries)}</div>
          <div class="k">Tokens</div><div class="v">输入 ${fmtNum(l.prompt_tokens)} / 输出 ${fmtNum(l.completion_tokens)} / 合计 ${fmtNum(l.total_tokens)}</div>
          ${l.error ? `<div class="k">错误</div><div class="v" style="color:var(--err)">${esc(l.error)}</div>` : ''}
        </div>
        <div style="margin-top:8px"><div class="k muted" style="margin-bottom:4px">请求头</div>
        <pre class="code" style="max-height:22vh">${esc(pretty(l.request_headers))}</pre></div>`],
      ['客户端请求体', `<pre class="code">${esc(pretty(l.request_body))}</pre>`],
      ['上游请求体', `<pre class="code">${esc(pretty(l.upstream_request))}</pre>`],
      ['响应内容', `<pre class="code">${esc(pretty(l.response_body))}</pre>`],
      ['原始 SSE', `<pre class="code">${esc(l.stream_raw || '（非流式请求）')}</pre>`],
      ['上游响应头', `<pre class="code">${esc(pretty(l.response_headers))}</pre>`],
    ];
    if (l.mask_mode && l.mask_mode !== 'off') {
      tabs.splice(1, 0, ['脱敏', renderMaskTab(l)]);
    }
    const body = `
      <div class="tabs">${tabs.map((t, i) =>
        `<button data-tab="${i}"${i === 0 ? ' class="active"' : ''}>${esc(t[0])}</button>`).join('')}</div>
      <div id="logTabBody">${tabs[0][1]}</div>`;
    openModal('请求详情 #' + id, body, null, '关闭', true);
    $$('#modalRoot .tabs button').forEach((b, i) => {
      b.onclick = () => {
        $$('#modalRoot .tabs button').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        $('#logTabBody').innerHTML = tabs[i][1];
      };
    });
  } catch (e) {
    toast('加载详情失败: ' + e.message, 'err');
  }
}

function toggleAutoRefresh() {
  const btn = $('#logRefreshBtn');
  if (state.logTimer) {
    clearInterval(state.logTimer);
    state.logTimer = null;
    btn.textContent = '自动刷新：关';
  } else {
    state.logTimer = setInterval(() => loadLogs(), 3000);
    btn.textContent = '自动刷新：开';
  }
}

/* ------------------------------------------------------------------ */
/* 设置                                                                */
/* ------------------------------------------------------------------ */
async function loadSettings() {
  try {
    const s = await api('/settings');
    $('#setSiteName').value = s.site_name || '';
    $('#setRetention').value = s.log_retention_days || '0';
    $('#setMaxBody').value = s.max_body_log || '262144';
    $('#setRequireKey').checked = s.require_api_key === '1';
    $('#setLogReq').checked = s.log_request_body !== '0';
    $('#setLogResp').checked = s.log_response_body !== '0';
    renderEndpoints();
  } catch (e) {
    toast('加载设置失败: ' + e.message, 'err');
  }
}

function renderEndpoints() {
  const origin = location.origin;
  const rows = [
    ['OpenAI Chat', `POST ${origin}/v1/chat/completions`],
    ['Anthropic Messages', `POST ${origin}/v1/messages`],
    ['OpenAI Responses', `POST ${origin}/v1/responses`],
    ['OpenAI Completions', `POST ${origin}/v1/completions`],
    ['Embeddings', `POST ${origin}/v1/embeddings`],
    ['模型列表', `GET  ${origin}/v1/models`],
    ['健康检查', `GET  ${origin}/health`],
  ];
  $('#endpointList').innerHTML = rows.map(([k, v]) =>
    `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join('');
}

async function saveSettings() {
  try {
    await api('/settings', {
      method: 'POST',
      body: JSON.stringify({
        site_name: $('#setSiteName').value.trim(),
        log_retention_days: $('#setRetention').value || '0',
        max_body_log: $('#setMaxBody').value || '262144',
        require_api_key: $('#setRequireKey').checked ? '1' : '0',
        log_request_body: $('#setLogReq').checked ? '1' : '0',
        log_response_body: $('#setLogResp').checked ? '1' : '0',
      }),
    });
    toast('设置已保存', 'ok');
    state.siteName = $('#setSiteName').value.trim() || state.siteName;
    $('#brand').innerHTML = `${esc(state.siteName)}<small>LLM API 中转</small>`;
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function savePassword() {
  try {
    await api('/password', {
      method: 'POST',
      body: JSON.stringify({
        old_password: $('#pwOld').value, new_password: $('#pwNew').value,
      }),
    });
    toast('密码已修改，请重新登录', 'ok');
    $('#pwOld').value = ''; $('#pwNew').value = '';
    setTimeout(showLogin, 800);
  } catch (e) { toast('修改失败: ' + e.message, 'err'); }
}

/* ------------------------------------------------------------------ */
/* 脱敏                                                                */
/* ------------------------------------------------------------------ */
const MASK_MODE_LABEL = { off: '关闭', dry_run: '干跑', enforce: '强制' };

async function loadMask() {
  try {
    const d = await api('/mask');
    const s = d.settings || {};
    $('#maskMode').value = s.mask_mode || 'off';
    $('#maskSessionSource').value = s.mask_session_source || 'auto';
    $('#maskPaths').checked = s.mask_paths !== '0';
    $('#maskCreds').checked = s.mask_credentials !== '0';
    $('#maskIncludeHome').checked = s.mask_include_home !== '0';
    $('#maskOutside').checked = s.mask_outside_roots === '1';
    $('#maskPropagate').checked = s.mask_propagate !== '0';
    $('#maskStoreMap').checked = s.mask_store_map !== '0';
    $('#maskCredMode').value = s.mask_credential_mode || 'fps';
    $('#maskWorkspaceRoots').value = s.mask_workspace_roots || '';
    $('#maskRepoRoots').value = s.mask_repo_roots || '';
    $('#maskPreserveSegments').value = s.mask_preserve_segments || '3';
    $('#maskKeywords').value = s.mask_sensitive_keywords || '';
    $('#maskSuffixes').value = s.mask_internal_suffixes || '';
    $('#maskRetention').value = s.mask_retention_days || '7';
    renderMaskWarn(s.mask_mode, d.policy || {}, d.summary || {});
    renderMaskSessions(d.sessions || []);
  } catch (e) {
    toast('加载脱敏设置失败: ' + e.message, 'err');
  }
}

function renderMaskWarn(mode, policy, summary) {
  const el = $('#maskWarn');
  if (mode === 'off') {
    el.className = 'note';
    el.textContent = '未启用。请求原样发送给上游，本机路径与凭证都会出现在上游侧。';
    return;
  }
  if (mode === 'dry_run') {
    el.className = 'note warn';
    el.textContent = '干跑模式。请求仍原样发送，仅在日志中记录本应脱敏的内容。'
      + '可用于先确认规则是否误伤，再切换到强制模式。';
    return;
  }
  const roots = [].concat(policy.repo_roots || [], policy.workspace_roots || []);
  const home = policy.include_home ? (policy.home || '未识别到主目录') : '未启用';
  el.className = 'note danger';
  el.innerHTML = '强制模式已开启。'
    + `<br>生效根目录：<code>${esc(roots.join('   ') || '未配置')}</code>`
    + `　主目录：<code>${esc(home)}</code>`
    + `<br>已保存 ${fmtNum(summary.slots)} 条句柄映射，分布于 ${fmtNum(summary.sessions)} 个会话。`
    + '<br>为使响应可以还原，这些映射会以明文保存在本机数据库中。'
    + '请控制 data/gateway.db 的访问权限，并设置合理的保留天数。';
}

function renderMaskSessions(items) {
  $('#maskSessions').innerHTML = items.length ? `
    <table><thead><tr><th>会话</th><th>句柄数</th><th>创建时间</th><th>最近使用</th>
    <th class="right">操作</th></tr></thead><tbody>
    ${items.map(s => `
      <tr>
        <td class="mono">${esc(s.session_id)}</td>
        <td>${fmtNum(s.slots)}</td>
        <td class="mono-sm">${esc(s.created_at || '')}</td>
        <td class="mono-sm">${esc(s.updated_at || '')}</td>
        <td class="right nowrap">
          <button class="btn mini" data-view="${esc(s.session_id)}">映射</button>
          <button class="btn mini danger" data-clear="${esc(s.session_id)}">清除</button>
        </td>
      </tr>`).join('')}
    </tbody></table>` : '<div class="empty">还没有任何脱敏会话</div>';

  $$('#maskSessions button[data-view]').forEach(b => {
    b.onclick = () => showMaskMappings(b.dataset.view);
  });
  $$('#maskSessions button[data-clear]').forEach(b => {
    b.onclick = async () => {
      if (!confirm(`清除会话 ${b.dataset.clear} 的全部映射？其旧句柄将无法还原。`)) return;
      try {
        await api('/mask/sessions/' + encodeURIComponent(b.dataset.clear), { method: 'DELETE' });
        toast('已清除', 'ok');
        loadMask();
      } catch (e) { toast(e.message, 'err'); }
    };
  });
}

async function showMaskMappings(sessionId) {
  try {
    const d = await api('/mask/sessions/' + encodeURIComponent(sessionId));
    openModal(`映射 · ${sessionId}`, mappingTable(d.mappings || []), null, '关闭', true);
  } catch (e) { toast(e.message, 'err'); }
}

function mappingTable(rows) {
  if (!rows.length) return '<div class="empty">该会话没有映射</div>';
  return `
    <div class="muted" style="margin-bottom:10px;font-size:12px">
      点击遮罩可显示明文。这些真值不会发送给上游。</div>
    <table><thead><tr><th>句柄（发送给上游）</th><th>真值（本机）</th><th>类型</th></tr></thead><tbody>
    ${rows.map(m => `
      <tr>
        <td class="mono">${esc(m.handle)}</td>
        <td><span class="secret masked" onclick="this.classList.toggle('shown')">${esc(m.raw)}</span></td>
        <td><span class="badge type">${esc(m.slot_type)}</span></td>
      </tr>`).join('')}
    </tbody></table>`;
}

async function saveMaskSettings() {
  try {
    await api('/mask/settings', {
      method: 'POST',
      body: JSON.stringify({
        mask_mode: $('#maskMode').value,
        mask_session_source: $('#maskSessionSource').value,
        mask_paths: $('#maskPaths').checked ? '1' : '0',
        mask_credentials: $('#maskCreds').checked ? '1' : '0',
        mask_include_home: $('#maskIncludeHome').checked ? '1' : '0',
        mask_outside_roots: $('#maskOutside').checked ? '1' : '0',
        mask_propagate: $('#maskPropagate').checked ? '1' : '0',
        mask_store_map: $('#maskStoreMap').checked ? '1' : '0',
        mask_credential_mode: $('#maskCredMode').value,
        mask_workspace_roots: $('#maskWorkspaceRoots').value,
        mask_repo_roots: $('#maskRepoRoots').value,
        mask_preserve_segments: $('#maskPreserveSegments').value || '3',
        mask_sensitive_keywords: $('#maskKeywords').value,
        mask_internal_suffixes: $('#maskSuffixes').value,
        mask_retention_days: $('#maskRetention').value || '7',
      }),
    });
    toast('脱敏设置已保存', 'ok');
    loadMask();
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function previewMask() {
  const text = $('#maskPreviewInput').value;
  if (!text.trim()) { toast('请先输入文本', 'err'); return; }
  try {
    const d = await api('/mask/preview', { method: 'POST', body: JSON.stringify({ text }) });
    if (d.error) { toast(d.error, 'err'); return; }
    const hits = d.hits || [];
    $('#maskPreviewResult').innerHTML = `
      <div class="diff">
        <div><h4>客户端原文（本机）</h4><pre class="code">${esc(text)}</pre></div>
        <div><h4>实际发送给上游</h4><pre class="code">${esc(d.masked)}</pre></div>
      </div>
      <div style="margin-top:14px">
        <div class="muted" style="font-size:12px;margin-bottom:8px">
          命中 ${hits.length} 处${d.changed ? '' : '，内容将原样发送给上游'}
        </div>
        ${hits.length ? `<table><thead><tr><th>类型</th><th>句柄</th><th>被替换的真值</th></tr></thead><tbody>
          ${hits.map(h => `<tr>
            <td><span class="badge type">${esc(h.slot_type)}</span></td>
            <td class="mono">${esc(h.handle)}</td>
            <td><span class="secret masked" onclick="this.classList.toggle('shown')">${esc(h.raw)}</span></td>
          </tr>`).join('')}</tbody></table>` : ''}
      </div>
      <div style="margin-top:14px"><h4 class="muted" style="font-size:12px;font-weight:500;margin-bottom:6px">还原校验（应与原文一致）</h4>
        <pre class="code" style="max-height:14vh">${esc(d.unmasked || '')}</pre></div>`;
  } catch (e) { toast('试跑失败: ' + e.message, 'err'); }
}

function logMaskBadge(l) {
  if (!l || !l.mask_mode || l.mask_mode === 'off') return '';
  let n = 0;
  try { n = (JSON.parse(l.mask_summary || '{}').count) || 0; } catch (e) { n = 0; }
  const label = MASK_MODE_LABEL[l.mask_mode] || l.mask_mode;
  return ` <span class="badge mask" title="脱敏模式 ${esc(label)}，命中 ${n} 处">脱敏 ${n}</span>`;
}


/* ------------------------------------------------------------------ */
/* 调试台                                                              */
/* ------------------------------------------------------------------ */
async function fillPlaygroundDefaults() {
  if (!state.channels.length) {
    try { state.channels = (await api('/overview')).channels || []; } catch (e) { /* ignore */ }
  }
  if (!state.keys.length) {
    try { state.keys = (await api('/keys')).items || []; } catch (e) { /* ignore */ }
  }
  if (!$('#pgModel').value && state.channels.length) {
    const c = state.channels.find(x => x.enabled && (x.model_list || []).length);
    if (c) $('#pgModel').value = c.model_list[0];
  }
  if (!$('#pgKey').value && state.keys.length) {
    const k = state.keys.find(x => x.enabled);
    if (k) $('#pgKey').value = k.key;
  }
}

function buildPlaygroundRequest() {
  const fmt = $('#pgFormat').value;
  const model = $('#pgModel').value.trim() || 'gpt-4o-mini';
  const input = $('#pgInput').value;
  const system = $('#pgSystem').value.trim();
  const maxTokens = Number($('#pgMaxTokens').value || 256);
  const stream = $('#pgStream').checked;
  const endpoint = {
    openai: '/v1/chat/completions',
    anthropic: '/v1/messages',
    openai_responses: '/v1/responses',
    openai_completions: '/v1/completions',
  }[fmt];
  let body;
  if (fmt === 'openai') {
    body = { model, stream, messages: [] };
    if (system) body.messages.push({ role: 'system', content: system });
    body.messages.push({ role: 'user', content: input });
    body.max_tokens = maxTokens;
  } else if (fmt === 'anthropic') {
    body = { model, stream, max_tokens: maxTokens, messages: [{ role: 'user', content: input }] };
    if (system) body.system = system;
  } else if (fmt === 'openai_responses') {
    body = { model, stream, input, max_output_tokens: maxTokens };
    if (system) body.instructions = system;
  } else {
    body = { model, stream, prompt: input, max_tokens: maxTokens };
  }
  return { endpoint, body };
}

async function sendPlayground() {
  const { endpoint, body } = buildPlaygroundRequest();
  const out = $('#pgOutput');
  const statusEl = $('#pgStatus');
  const key = $('#pgKey').value.trim();
  const headers = { 'Content-Type': 'application/json' };
  if (key) headers['Authorization'] = 'Bearer ' + key;
  statusEl.textContent = '请求中…';
  statusEl.className = 'badge';
  out.textContent = '';
  const t0 = performance.now();
  try {
    const res = await fetch(endpoint, { method: 'POST', headers, body: JSON.stringify(body) });
    statusEl.textContent = res.status + ' · ' + Math.round(performance.now() - t0) + 'ms';
    statusEl.className = 'badge ' + (res.ok ? 'ok' : 'err');
    if (body.stream && res.ok) {
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let acc = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += dec.decode(value, { stream: true });
        out.textContent = acc;
        out.scrollTop = out.scrollHeight;
      }
    } else {
      const text = await res.text();
      out.textContent = pretty(text);
    }
  } catch (e) {
    statusEl.textContent = '失败';
    statusEl.className = 'badge err';
    out.textContent = String(e);
  }
}

function genCurl() {
  const { endpoint, body } = buildPlaygroundRequest();
  const key = $('#pgKey').value.trim() || 'sk-jai-xxxx';
  const cmd = `curl -N ${location.origin}${endpoint} \\\n  -H "Content-Type: application/json" \\\n  -H "Authorization: Bearer ${key}" \\\n  -d '${JSON.stringify(body)}'`;
  openModal('curl 命令', `<pre class="code">${esc(cmd)}</pre>`,
    () => copyText(cmd), '复制并关闭');
}

/* ------------------------------------------------------------------ */
/* 模态框                                                              */
/* ------------------------------------------------------------------ */
let modalConfirm = null;

function openModal(title, bodyHtml, onConfirm, cancelText, wide) {
  modalConfirm = onConfirm;
  $('#modalRoot').innerHTML = `
    <div class="modal-mask">
      <div class="modal${wide ? ' wide' : ''}">
        <div class="modal-head"><span>${esc(title)}</span><button class="x" id="modalX">✕</button></div>
        <div class="modal-body">${bodyHtml}</div>
        <div class="modal-foot">
          <button class="btn" id="modalCancel">${esc(cancelText || '取消')}</button>
          ${onConfirm ? '<button class="btn primary" id="modalOk">确定</button>' : ''}
        </div>
      </div>
    </div>`;
  $('#modalX').onclick = closeModal;
  $('#modalCancel').onclick = closeModal;
  const ok = $('#modalOk');
  if (ok) {
    ok.onclick = async () => {
      ok.disabled = true;
      try { await modalConfirm(); }
      catch (e) { toast(e.message, 'err'); }
      finally { ok.disabled = false; }
    };
  }
  $('.modal-mask').onclick = (e) => { if (e.target.classList.contains('modal-mask')) closeModal(); };
}

function closeModal() {
  $('#modalRoot').innerHTML = '';
  modalConfirm = null;
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
async function boot() {
  try {
    const ov = await api('/overview');
    state.channels = ov.channels || [];
  } catch (e) { /* ignore */ }
}

async function init() {
  $('#loginBtn').onclick = doLogin;
  $('#loginPassword').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  $('#logoutBtn').onclick = doLogout;
  $$('#nav button').forEach(b => { b.onclick = () => switchPage(b.dataset.page); });

  $('#refreshOverview').onclick = loadOverview;
  $('#addChannelBtn').onclick = addChannel;
  $('#addKeyBtn').onclick = addKey;
  $('#logSearchBtn').onclick = () => loadLogs(0);
  $('#logRefreshBtn').onclick = toggleAutoRefresh;
  $('#logClearBtn').onclick = async () => {
    if (!confirm('确定清空所有日志？')) return;
    try { await api('/logs', { method: 'DELETE' }); toast('日志已清空', 'ok'); loadLogs(0); }
    catch (e) { toast(e.message, 'err'); }
  };
  $('#logKeyword').addEventListener('keydown', e => { if (e.key === 'Enter') loadLogs(0); });
  $('#saveSettings').onclick = saveSettings;
  $('#savePassword').onclick = savePassword;
  $('#maskRefresh').onclick = loadMask;
  $('#maskSave').onclick = saveMaskSettings;
  $('#maskPreviewBtn').onclick = previewMask;
  $('#maskClearAll').onclick = async () => {
    if (!confirm('清空全部脱敏映射？所有历史句柄都将无法还原（不影响新请求）。')) return;
    try {
      const d = await api('/mask/sessions', { method: 'DELETE' });
      toast(`已清空 ${d.removed || 0} 条映射`, 'ok');
      loadMask();
    } catch (e) { toast(e.message, 'err'); }
  };
  $('#pgSend').onclick = sendPlayground;
  $('#pgCurl').onclick = genCurl;
  $('#pgClear').onclick = () => { $('#pgOutput').textContent = '—'; $('#pgStatus').textContent = '待发送'; $('#pgStatus').className = 'badge'; };
  $('#pgFormat').onchange = () => {
    const m = $('#pgModel');
    if (m.value) return;
    fillPlaygroundDefaults();
  };
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  try {
    const s = await fetch('/admin/api/session', { credentials: 'same-origin' }).then(r => r.json());
    state.siteName = s.site_name || state.siteName;
    state.channelTypes = s.channel_types || [];
    $('#brand').innerHTML = `${esc(state.siteName)}<small>LLM API 中转</small>`;
    $('#loginTitle').textContent = state.siteName;
    if (s.authenticated) {
      showApp();
      await boot();
      switchPage(location.hash.replace('#', '') || 'overview');
    } else {
      showLogin();
    }
  } catch (e) {
    showLogin();
  }
  window.addEventListener('hashchange', () => switchPage(location.hash.replace('#', ''), false));
}

document.addEventListener('DOMContentLoaded', init);
