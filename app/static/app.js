/* ===== 微博签到管理面板 - 前端逻辑 ===== */
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];

// 未登录时跳转登录页
function gotoLogin() {
  if (!location.pathname.endsWith('/login.html')) location.href = '/login.html';
}

function handleResp(r, path) {
  if (r.status === 401) { gotoLogin(); throw new Error('未登录'); }
  if (!r.ok) throw new Error('请求失败 ' + r.status);
  return r;
}

const api = {
  get: (p) => fetch(p).then(r => handleResp(r, p)).then(r => r.json()),
  post: (p, b) => fetch(p, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b||{})}).then(r => handleResp(r, p)).then(r => r.json()),
  put: (p, b) => fetch(p, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b)}).then(r => handleResp(r, p)).then(r => r.json()),
  del: (p) => fetch(p, {method:'DELETE'}).then(r => handleResp(r, p)).then(r => r.json()),
};

/* ===== Toast ===== */
let toastTimer;
function toast(msg, type='') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=> t.className = 'toast', 2600);
}

/* ===== 主题 ===== */
const themeSel = $('#theme');
themeSel.value = document.documentElement.dataset.theme || 'system';
themeSel.onchange = () => {
  const t = themeSel.value;
  document.documentElement.dataset.theme = t;
  try{ localStorage.setItem('wcm-theme', t); }catch(e){}
};

/* ===== 导航 ===== */
const VIEW_TITLES = {dashboard:'仪表盘', accounts:'账号管理', proxies:'代理', logs:'签到日志', settings:'设置'};
$$('.nav-item').forEach(btn => {
  btn.onclick = () => {
    $$('.nav-item').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    $$('.view').forEach(v=>v.hidden = true);
    $('#view-' + btn.dataset.view).hidden = false;
    $('#view-title').textContent = VIEW_TITLES[btn.dataset.view];
    if (btn.dataset.view === 'dashboard') loadDashboard();
    if (btn.dataset.view === 'accounts') loadAccounts();
    if (btn.dataset.view === 'proxies') loadProxies();
    if (btn.dataset.view === 'logs') loadLogs();
    if (btn.dataset.view === 'settings') loadSettings();
  };
});

/* ===== 仪表盘 ===== */
async function loadDashboard() {
  try {
    const [stats, tasks, accounts] = await Promise.all([
      api.get('/api/logs/stats'),
      api.get('/api/tasks?limit=6'),
      api.get('/api/accounts'),
    ]);
    renderStats(stats, accounts);
    renderRecentTasks(tasks);
    renderDashAccounts(accounts);
    loadTrend();
    loadQuote();
    loadNextRun();
  } catch(e) { toast('加载仪表盘失败', 'err'); }
}

/* ===== 下次定时签到倒计时（v7.1） ===== */
let schedTimer = null;
let schedLeft = null;
function fmtLeft(sec) {
  if (sec == null) return '';
  if (sec <= 0) return '即将开始';
  const d = Math.floor(sec/86400), h = Math.floor(sec%86400/3600),
        m = Math.floor(sec%3600/60), s = sec%60;
  if (d) return `${d} 天 ${h} 小时后`;
  if (h) return `${h} 小时 ${m} 分后`;
  if (m) return `${m} 分 ${s} 秒后`;
  return `${s} 秒后`;
}
function paintCountdown() {
  const el = $('#schedCountdown');
  if (!el) return;
  el.textContent = schedLeft == null ? '' : fmtLeft(schedLeft);
  if (schedLeft != null && schedLeft > 0) schedLeft -= 1;
}
async function loadNextRun() {
  const strip = $('#schedStrip');
  if (!strip) return;
  try {
    const info = await api.get('/api/schedule/next');
    if (!info.enabled) {
      strip.hidden = false;
      $('#schedText').textContent = '定时签到已关闭（可到设置里开启）';
      schedLeft = null; paintCountdown();
      return;
    }
    strip.hidden = false;
    $('#schedText').textContent = info.next_run
      ? `下次定时签到：${info.next_run}（cron ${info.cron}）`
      : `定时已开启（cron ${info.cron}），尚未排程`;
    schedLeft = info.seconds_left;
    paintCountdown();
    if (schedTimer) clearInterval(schedTimer);
    schedTimer = setInterval(paintCountdown, 1000);
  } catch(e) { strip.hidden = true; }
}

async function loadTrend() {
  const box = $('#trendChart');
  if (!box) return;
  try {
    const data = await api.get('/api/logs/trend?days=7');
    const max = Math.max(1, ...data.map(d=>d.success + d.fail));
    box.innerHTML = data.map(d=>{
      const totalH = Math.round((d.success + d.fail) / max * 100);
      const okH = (d.success + d.fail) ? Math.round(d.success / (d.success + d.fail) * totalH) : 0;
      const failH = Math.max(0, totalH - okH);
      const title = `${d.day}：成功 ${d.success}，失败 ${d.fail}`;
      return `<div class="trend-col" title="${esc(title)}">
        <div class="trend-bars">
          <i class="tb-fail" style="height:${failH}%"></i>
          <i class="tb-ok" style="height:${okH}%"></i>
        </div>
        <span class="trend-lbl">${esc(d.label)}</span>
      </div>`;
    }).join('');
    const sum = data.reduce((a,d)=>a + d.success, 0);
    const hint = $('#trendHint');
    if (hint) hint.textContent = `近 7 天成功 ${sum} 次`;
  } catch(e) { box.innerHTML = '<div class="hint">趋势加载失败</div>'; }
}

async function loadQuote(refresh=false) {
  const box = $('#quoteBox');
  if (!box) return;
  try {
    const q = await api.get('/api/quote' + (refresh ? '?refresh=true' : ''));
    box.textContent = q.text || '—';
    const from = $('#quoteFrom');
    if (from) from.textContent = q.source ? '—— ' + q.source : '';
  } catch(e) { box.textContent = '每日一言加载失败'; }
}

function renderStats(stats, accounts) {
  const active = accounts.filter(a=>a.enabled);
  const okAcc = accounts.filter(a=>a.last_status==='success').length;
  $('#statGrid').innerHTML = [
    card('账号总数', accounts.length, 'acc', '个'),
    card('启用中', active.length, '', '个'),
    card('签到正常', okAcc, 'green', '个'),
    card('累计超话签到', stats.topics_signed || 0, 'green', '个'),
    card('成功率', stats.success_rate || 0, '', '%'),
    card('今日记录', stats.today || 0, '', '条'),
  ].join('');
}
function card(lbl, num, cls, suffix='') {
  return `<div class="stat ${cls}"><div class="num">${num}<span style="font-size:14px">${suffix}</span></div><div class="lbl">${lbl}</div></div>`;
}
function statusBadge(s) {
  const map = {success:['ok','✅'], partial:['warn','⚠️'], failed:['bad','❌'], running:['acc','🔄'], unknown:['gray','未知']};
  const [cls, lab] = map[s] || map.unknown;
  return `<span class="badge ${cls}">${lab} ${s==='success'?'成功':s==='partial'?'部分':s==='failed'?'失败':s==='running'?'运行中':'未知'}</span>`;
}
function renderRecentTasks(tasks) {
  const tb = $('#recentTasks tbody');
  tb.innerHTML = tasks.length ? tasks.map(t=>`
    <tr>
      <td><code>${t.task_id}</code></td>
      <td>${t.trigger_type==='schedule'?'⏰ 定时':'👆 手动'}</td>
      <td>${statusBadge(t.status)}</td>
      <td>${t.started_at}</td>
      <td>${t.finished_at||'—'}</td>
    </tr>`).join('') : '<tr><td colspan="5" style="color:var(--muted)">暂无任务记录</td></tr>';
}
function renderDashAccounts(accounts) {
  const tb = $('#dashAccounts tbody');
  tb.innerHTML = accounts.length ? accounts.map(a=>`
    <tr>
      <td>${a.name}</td>
      <td>${statusBadge(a.last_status)}</td>
      <td>${a.last_checkin||'从未'}</td>
      <td style="white-space:normal;color:var(--muted)">${a.last_message||''}</td>
    </tr>`).join('') : '<tr><td colspan="4" style="color:var(--muted)">暂无账号，请到「账号管理」添加</td></tr>';
}
$('#btn-refresh-acc').onclick = loadDashboard;
const btnQuoteRefresh = $('#btn-quote-refresh');
if (btnQuoteRefresh) btnQuoteRefresh.onclick = ()=>loadQuote(true);
const btnQuotePush = $('#btn-quote-push');
if (btnQuotePush) btnQuotePush.onclick = async ()=>{
  toast('推送中…');
  try {
    const r = await api.post('/api/notify/quote', {});
    toast(r.ok ? '✅ 已推送到 TG' : '❌ 推送失败（检查 TG 配置）', r.ok?'good':'err');
  } catch(e){ toast('推送失败','err'); }
};

/* ===== 账号管理 ===== */
let editingId = null;
async function loadAccounts() {
  try {
    const accounts = await api.get('/api/accounts');
    window.__accounts = accounts;
    const tb = $('#accTable tbody');
    tb.innerHTML = accounts.length ? accounts.map(a=>`
      <tr data-id="${a.id}">
        <td><input type="checkbox" class="acc-check" data-id="${a.id}" /></td>
        <td><strong>${esc(a.name)}</strong></td>
        <td>${a.enabled?statusBadge('success')+' 启用':'<span class="badge gray">已停用</span>'} <span style="color:var(--muted)">·</span> ${statusBadge(a.last_status)}</td>
        <td style="color:var(--muted)">${a.cookie_length} 字符</td>
        <td>${a.proxy_label ? '<span class="badge">'+esc(a.proxy_label)+'</span>' : (a.proxy ? '<span class="badge">'+esc(a.proxy)+'</span>' : '<span class="badge gray">直连</span>')}</td>
        <td>${a.last_checkin||'从未'}</td>
        <td>
          <button class="btn btn-ghost btn-sm" onclick="toggleAccEnabled(${a.id})">${a.enabled?'停用':'启用'}</button>
          <button class="btn btn-ghost btn-sm" onclick="openAccModal(${a.id})">编辑</button>
          <button class="btn btn-ghost btn-sm" onclick="verifyAcc(${a.id})">校验</button>
          <button class="btn btn-danger btn-sm" onclick="delAcc(${a.id})">删除</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="7" style="color:var(--muted)">暂无账号</td></tr>';
    $('#accCheckAll').checked = false;
    updateSelCount();
    $('#btn-checkin-selected').disabled = true;
  } catch(e) { toast('加载账号失败', 'err'); }
}

// ---------- 账号多选 + 批量操作 ----------
function getSelectedAccountIds() {
  return Array.from(document.querySelectorAll('.acc-check:checked')).map(c=>+c.dataset.id);
}
function updateSelCount() {
  const n = getSelectedAccountIds().length;
  $('#batchBar').hidden = n === 0;
  $('#selCount').textContent = n;
  $('#btn-checkin-selected').disabled = n === 0;
  const cbs = document.querySelectorAll('.acc-check');
  if (cbs.length) $('#accCheckAll').checked = cbs.length === Array.from(cbs).filter(c=>c.checked).length;
}
const accCheckAllEl = $('#accCheckAll');
const btnCheckinSel = $('#btn-checkin-selected');
if (accCheckAllEl) accCheckAllEl.addEventListener('change', e=>{
  document.querySelectorAll('.acc-check').forEach(c=>{ if(!c.disabled) c.checked = e.target.checked; });
  updateSelCount();
});
document.addEventListener('change', e=>{
  if (e.target.classList && e.target.classList.contains('acc-check')) updateSelCount();
});
const btnSelClear = $('#btn-sel-clear');
if (btnSelClear) btnSelClear.onclick = ()=>{ document.querySelectorAll('.acc-check').forEach(c=>c.checked=false); updateSelCount(); };

function batchSetEnabled(enabled) {
  const ids = getSelectedAccountIds();
  if (!ids.length) return;
  Promise.all(ids.map(id=>api.put('/api/accounts/'+id, {enabled}))).then(()=>{
    toast((enabled?'已启用':'已停用')+' '+ids.length+' 个账号', 'good');
    loadAccounts();
  }).catch(()=>toast('批量操作失败','err'));
}
const btnSelEnable = $('#btn-sel-enable'); if (btnSelEnable) btnSelEnable.onclick = ()=>batchSetEnabled(true);
const btnSelDisable = $('#btn-sel-disable'); if (btnSelDisable) btnSelDisable.onclick = ()=>batchSetEnabled(false);
if (btnCheckinSel) btnCheckinSel.onclick = ()=>{
  const ids = getSelectedAccountIds();
  if (!ids.length) { toast('请先勾选账号','err'); return; }
  const enabledIds = ids.filter(id => {
    const a = (window.__accounts || []).find(x => x.id === id);
    return a && !!a.enabled;
  });
  const skipped = ids.length - enabledIds.length;
  if (!enabledIds.length) {
    toast('选中的账号都已停用，请先点击「启用选中」', 'err');
    return;
  }
  api.post('/api/checkin/run-accounts', {account_ids: enabledIds}).then(r=>{
    toast((r.message || '已启动手动签到') + (skipped ? `，已跳过 ${skipped} 个停用账号` : ''), 'good');
  }).catch(()=>toast('启动失败','err'));
};
async function toggleAccEnabled(id) {
  const a = (window.__accounts||[]).find(x=>x.id===id);
  if (!a) return;
  try {
    await api.put('/api/accounts/'+id, {enabled: !a.enabled});
    toast(a.enabled?'已停用自动签到':'已启用自动签到', 'good');
    loadAccounts();
  } catch(e){ toast('操作失败','err'); }
}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

let accModal = (()=>{})();
async function populateProxySelect(sel, selectedId=null) {
  // v7.1：下拉用代理 id 作为值，不再把含密码的完整链接塞进 DOM
  let proxies = [];
  try {
    const list = await api.get('/api/proxies');
    proxies = list.filter(p=>p.enabled!==false).map(p => {
      const cc = p.geo_country_code || '';
      const flag = cc ? (String.fromCodePoint(0x1F1E6+cc.charCodeAt(0)-65, 0x1F1E6+cc.charCodeAt(1)-65)) : '🌐';
      const name = p.label || (p.geo_country ? p.geo_country+' '+(p.geo_region||'') : (p.ip||'节点'));
      return { id: p.id, label: `${flag} ${name}${p.ip?' ('+p.ip+':'+p.port+')':''}` };
    });
  } catch(e) { /* 忽略 */ }
  const cur = selectedId == null ? '' : String(selectedId);
  const opt = (v, t) => `<option value="${esc(v)}"${String(v)===cur?' selected':''}>${esc(t)}</option>`;
  sel.innerHTML = opt('', '自动 / 直连') + proxies.map(p => opt(p.id, p.label)).join('');
}

function openAccModal(id=null) {
  editingId = id;
  $('#accModalTitle').textContent = id ? '编辑账号' : '添加账号';
  $('#m-name').value = ''; $('#m-cookie').value = ''; $('#m-remark').value='';
  $('#m-enabled').checked = true;
  populateProxySelect($('#m-proxy'), null);
  if (id) {
    api.get('/api/accounts/'+id).then(a=>{
      $('#m-name').value = a.name;
      $('#m-cookie').value = a.cookie_raw || a.cookie || '';
      $('#m-remark').value = a.remark||'';
      $('#m-enabled').checked = !!a.enabled;
      populateProxySelect($('#m-proxy'), a.proxy_id ?? null);
    });
  }
  $('#accModal').hidden = false;
}
$('#btn-add-acc').onclick = () => openAccModal();
$('#accModalClose').onclick = $('#accModalCancel').onclick = ()=> $('#accModal').hidden = true;
$('#accModalSave').onclick = async () => {
  const body = {
    name: $('#m-name').value.trim() || '未命名账号',
    cookie_raw: $('#m-cookie').value.trim(),
    remark: $('#m-remark').value.trim(),
    enabled: $('#m-enabled').checked,
    proxy_id: $('#m-proxy').value ? +$('#m-proxy').value : 0,
  };
  try {
    if (editingId) await api.put('/api/accounts/'+editingId, body);
    else await api.post('/api/accounts', body);
    toast('保存成功', 'good');
    $('#accModal').hidden = true;
    loadAccounts(); loadDashboard();
  } catch(e){ toast('保存失败', 'err'); }
};
async function delAcc(id) {
  if (!confirm('确定删除该账号？其签到日志会保留。')) return;
  try { await api.del('/api/accounts/'+id); toast('已删除','good'); loadAccounts(); loadDashboard(); }
  catch(e){ toast('删除失败','err'); }
}
async function verifyAcc(id) {
  toast('校验中…');
  try {
    const r = await api.post(`/api/accounts/${id}/verify`, {});
    toast(r.valid ? '✅ Cookie 有效' : '❌ ' + r.message, r.valid?'good':'err');
  } catch(e){ toast('校验失败','err'); }
}

/* ===== 代理管理 ===== */
let editingProxyId = null;
function flagEmoji(cc) {
  if (!cc || cc.length !== 2) return '🌐';
  const ccU = cc.toUpperCase();
  return String.fromCodePoint(0x1F1E6 + ccU.charCodeAt(0) - 65, 0x1F1E6 + ccU.charCodeAt(1) - 65);
}
function proxyTestHtml(p) {
  if (!p.last_test) return '<span class="ptest-idle">尚未测速</span>';
  const cls = p.last_test === 'ok' ? 't-ok' : 't-bad';
  const icon = p.last_test === 'ok' ? '●' : '●';
  const latency = Number(p.last_latency_ms || 0);
  const detail = p.last_test_message || (p.last_test === 'ok' ? '测试成功' : '测试失败');
  const tested = p.last_test_at ? ` · ${esc(p.last_test_at.slice(5,16))}` : '';
  return `<span class="${cls}">${icon} ${esc(detail)}${latency && !detail.includes('ms') ? ` · ${latency} ms` : ''}${tested}</span>`;
}

async function loadProxies() {
  try {
    const list = await api.get('/api/proxies');
    $('#proxyListHint').textContent = list.length ? '' : '添加后，账号管理里可为每个账号指定对应代理（不同节点并行签到）。';
    const box = $('#proxyList');
    if (!list.length) {
      box.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center">尚未添加代理节点，点击「＋ 添加代理」</div>';
      return;
    }
    box.innerHTML = list.map(p => {
      const flag = flagEmoji(p.geo_country_code);
      const host = p.ip || ((p.url||'').replace(/socks5.*@/,'').replace(/^socks5:\/\//,''));
      const has = p.ip || (p.url||'').includes('socks5');
      const geoHtml = p.geo_country ? `<div class="proxy-geo-line">${flag} ${esc(p.geo_country)} ${esc(p.geo_region||'')}${p.geo_ip?' · '+esc(p.geo_ip):''}</div>` : (has ? '<div class="proxy-geo-line"><span class="badge gray">归属地未识别</span></div>' : '');
      return `<div class="proxy-card ${p.enabled===false?'proxy-disabled':''}">
        <div class="proxy-top">
          <div class="proxy-flag">${flag}</div>
          <div class="proxy-main">
            <div class="proxy-name">${esc(p.label || (p.geo_country? p.geo_country+' '+p.geo_region : host))} ${p.enabled===false?'<span class="badge gray">停用</span>':''}</div>
            <div class="proxy-meta">${esc(has? host : '请编辑补全')}${p.port?':'+p.port:''}</div>
          </div>
          <div class="proxy-actions">
            <button class="btn btn-ghost btn-sm" onclick="testProxy(${p.id})">测试</button>
            <button class="btn btn-ghost btn-sm" onclick="openProxyModal(${p.id})">编辑</button>
            <button class="btn btn-danger btn-sm" onclick="delProxy(${p.id})">删除</button>
          </div>
        </div>
        ${geoHtml}
        <div class="proxy-test" id="ptest-${p.id}">${proxyTestHtml(p)}</div>
      </div>`;
    }).join('');
  } catch(e){ toast('加载代理失败','err'); }
}

function openProxyModal(id=null) {
  editingProxyId = id;
  $('#proxyModalTitle').textContent = id ? '编辑代理' : '添加代理';
  ['p-url','p-ip','p-port','p-user','p-pwd','p-label'].forEach(i=> $('#'+i).value='');
  $('#p-geo').innerHTML=''; $('#p-geo-status').textContent='';
  if (id) {
    api.get('/api/proxies').then(list=>{
      const p = list.find(x=>x.id===id);
      if (!p) return;
      if (p.ip) $('#p-ip').value = p.ip;
      if (p.port) $('#p-port').value = p.port;
      if (p.username) $('#p-user').value = p.username;
      if (p.label) $('#p-label').value = p.label;
      $('#p-geo').innerHTML = p.geo_country ? `<div class="geo-preview-item">${flagEmoji(p.geo_country_code)} ${esc(p.geo_country)} ${esc(p.geo_region||'')}</div>` : '';
    });
  }
  $('#proxyModal').hidden = false;
}
$('#btn-proxy-add').onclick = () => openProxyModal();
$('#proxyModalClose').onclick = $('#proxyModalCancel').onclick = ()=> $('#proxyModal').hidden = true;

$('#p-url').oninput = () => {
  const url = $('#p-url').value.trim();
  if (!url.includes('socks5://')) return;
  const m = url.match(/socks5:\/\/(?:([^:@\/]+):([^@\/]*))?@?([^:\/\s]+):(\d+)/);
  if (m) {
    $('#p-ip').value = m[3]; $('#p-port').value = m[4];
    if (m[1]) $('#p-user').value = decodeURIComponent(m[1]);
    if (m[2]) $('#p-pwd').value = decodeURIComponent(m[2]);
    $('#p-geo-status').textContent = '已解析，点「识别归属地」…';
  }
};
$('#btn-p-detect').onclick = async () => {
  const url = buildProxyUrlFromForm();
  if (!url) { toast('请填写 IP 或链接', 'err'); return; }
  const st = $('#p-geo-status'); st.textContent = '识别中…';
  try {
    const r = await api.post('/api/proxies/detect', { url });
    if (r.ok) {
      $('#p-geo').innerHTML = `<div class="geo-preview-item">${flagEmoji(r.country_code)} ${esc(r.country)} ${esc(r.region||'')}（${esc(r.ip)}）</div>`;
      st.textContent = '✅ 识别成功';
    } else { $('#p-geo').innerHTML=''; st.textContent = '❌ ' + (r.message||'识别失败'); }
  } catch(e){ st.textContent = '❌ '+e.message; }
};
function buildProxyUrlFromForm() {
  const ip=$('#p-ip').value.trim(), port=$('#p-port').value.trim();
  if (!ip || !port) return '';
  const u=$('#p-user').value.trim(), pw=$('#p-pwd').value.trim();
  return 'socks5://'+(u?encodeURIComponent(u)+(pw?':'+encodeURIComponent(pw):'')+'@':'')+ip+':'+port;
}
$('#proxyModalSave').onclick = async () => {
  const url = buildProxyUrlFromForm() || $('#p-url').value.trim();
  if (!url) { toast('请填写 IP/端口 或 完整链接', 'err'); return; }
  try {
    if (editingProxyId) await api.put('/api/proxies/'+editingProxyId, { url, label: $('#p-label').value.trim() });
    else await api.post('/api/proxies', { url, label: $('#p-label').value.trim() });
    $('#proxyModal').hidden = true;
    toast('保存成功', 'good');
    loadProxies();
  } catch(e){ toast('保存失败：'+e.message, 'err'); }
};
async function delProxy(id) {
  if (!confirm('确定删除该代理？')) return;
  try { await api.del('/api/proxies/'+id); toast('已删除','good'); loadProxies(); }
  catch(e){ toast('删除失败','err'); }
}
async function testProxy(id) {
  const el = $('#ptest-'+id);
  const btn = document.querySelector(`button[onclick="testProxy(${id})"]`);
  if (el) el.innerHTML = '<span class="ptest-running"><i></i> 正在测速…</span>';
  if (btn) { btn.disabled = true; btn.textContent = '测速中'; }
  try {
    const r = await api.post('/api/proxies/'+id+'/test', {});
    if (el) el.innerHTML = `<span class="${r.ok?'t-ok':'t-bad'}">${r.ok?'●':'●'} ${esc(r.message)} · 已保存</span>`;
  } catch(e) {
    if(el) el.innerHTML = `<span class="t-bad">● 测试失败：${esc(e.message || '网络错误')}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '测试'; }
  }
}
$('#btn-proxy-test-all').onclick = async () => {
  const btn = $('#btn-proxy-test-all');
  try {
    const list = await api.get('/api/proxies');
    btn.disabled = true; btn.textContent = `测速中 0/${list.length}`;
    for (let i=0; i<list.length; i++) {
      btn.textContent = `测速中 ${i+1}/${list.length}`;
      await testProxy(list[i].id);
    }
    btn.textContent = '🧪 测试全部';
    btn.disabled = false;
  } catch(e) { btn.textContent = '🧪 测试全部'; btn.disabled = false; toast('批量测速失败','err'); }
};

/* ===== 扫码登录添加账号 ===== */
let qrId = null, qrTimer = null, qrImporting = false;
const qrModal = $('#qrModal');

function qrOpen() {
  qrModal.hidden = false;
  qrTimer && clearInterval(qrTimer);
  loadQr();
}
function qrClose() {
  qrModal.hidden = true;
  qrTimer && clearInterval(qrTimer);
  qrId = null;
  qrImporting = false;
}
async function loadQr() {
  $('#qrStatus').textContent = '正在获取二维码…';
  $('#qrStatus').className = 'qr-status';
  $('#qrImportBtn').disabled = true;
  $('#qrName').value = '';
  $('#qrImage').src = '';
  qrImporting = false;
  try {
    const d = await api.get('/api/auth/qrcode');
    if (!d.qrid) throw new Error('no qrid');
    qrId = d.qrid;
    // 优先用后端渲染的 base64 二维码（无防盗链、100% 可显示），否则用外链图片
    if (d.image_b64) {
      $('#qrImage').src = 'data:image/png;base64,' + d.image_b64;
    } else if (d.image) {
      $('#qrImage').src = d.image;
    } else {
      throw new Error('二维码数据为空');
    }
    $('#qrStatus').textContent = '等待扫码…';
    startQrPoll();
  } catch(e) {
    $('#qrStatus').textContent = '获取二维码失败：' + e.message;
    $('#qrStatus').className = 'qr-status bad';
    toast('获取二维码失败', 'err');
  }
}
function startQrPoll() {
  qrTimer && clearInterval(qrTimer);
  let status_speed = 2000;   // pending 轮询间隔
  const tick = async () => {
    if (!qrId || qrImporting) { return; }
    try {
      const st = await api.get('/api/auth/qrcode/check?qrid=' + encodeURIComponent(qrId));
      if (st.status === 'pending') {
        $('#qrStatus').textContent = '等待扫码…';
        status_speed = 2000;
      } else if (st.status === 'scanned') {
        $('#qrStatus').textContent = '✅ 已扫码，请在手机上确认';
        $('#qrStatus').className = 'qr-status success';
        // 已扫码→加速轮询，捕捉确认瞬间（20000000），降低错过概率
        status_speed = 400;
        $('#qrImportBtn').disabled = false;
      } else if (st.status === 'success') {
        qrTimer && clearInterval(qrTimer);
        $('#qrStatus').textContent = '✅ 扫码成功，正在自动导入…';
        $('#qrStatus').className = 'qr-status success';
        $('#qrImportBtn').disabled = false;
        await doQrImport();
        return;
      } else if (st.status === 'expired') {
        $('#qrStatus').textContent = '二维码已过期，请刷新';
        $('#qrStatus').className = 'qr-status bad';
        $('#qrImportBtn').disabled = true;
        qrTimer && clearInterval(qrTimer);
        return;
      } else if (st.status === 'error') {
        $('#qrStatus').textContent = '查询失败：' + st.message;
        $('#qrStatus').className = 'qr-status bad';
      } else if (st.status === 'unknown') {
        $('#qrStatus').textContent = '正在确认扫码状态…';
        $('#qrStatus').className = 'qr-status';
      }
    } catch(e) { /* 轮询错误忽略，继续 */ }
    // 无条件递归调度下一次（不能依赖 if(qrTimer)，首次时 qrTimer 尚为 null）
    qrTimer = setTimeout(tick, status_speed);
  };
  tick();
}
async function doQrImport() {
  if (!qrId || qrImporting) return;
  qrImporting = true;
  const btn = $('#qrImportBtn');
  btn.disabled = true; const old = btn.textContent; btn.textContent = '导入中…';
  try {
    const r = await api.post('/api/auth/qrcode/import', { qrid: qrId, name: $('#qrName').value.trim() });
    toast('✅ 账号已自动导入：' + r.name, 'good');
    qrClose();
    loadAccounts(); loadDashboard();
  } catch(e) {
    toast('自动导入失败：' + e.message, 'err');
    $('#qrStatus').textContent = '导入失败:' + e.message;
    $('#qrStatus').className = 'qr-status bad';
    btn.disabled = false; btn.textContent = old;
  } finally {
    qrImporting = false;
  }
}
$('#btn-qr-add').onclick = qrOpen;
$('#qrModalClose').onclick = qrClose;
$('#qrRefresh').onclick = () => { qrTimer && clearInterval(qrTimer); loadQr(); };
$('#qrImportBtn').onclick = doQrImport;

/* ===== 日志 ===== */
let logCache = [];
async function loadLogs() {
  try {
    logCache = await api.get('/api/logs?limit=200');
    renderLogs();
  } catch(e){ toast('加载日志失败','err'); }
}
function renderLogs() {
  const list = $('#logList');
  if (!list) return;
  const kw = (($('#logSearch')||{}).value || '').trim().toLowerCase();
  const st = ($('#logFilter')||{}).value || '';
  const rows = (logCache||[]).filter(l => {
    if (st && l.status !== st) return false;
    if (!kw) return true;
    return ((l.account_name||'') + ' ' + (l.message||'')).toLowerCase().includes(kw);
  });
  const countBox = $('#logCount');
  if (countBox) countBox.textContent = '共 ' + (logCache||[]).length + ' 条，当前显示 ' + rows.length + ' 条';
  if (!rows.length) {
    list.innerHTML = '<div style="color:var(--muted);padding:20px">' + ((logCache||[]).length ? '没有匹配的日志' : '暂无签到日志') + '</div>';
    return;
  }
  list.innerHTML = rows.map(l => {
    const t = (l.created_at||'').slice(5,16);
    const badge = l.status==='success' ? '<span class="badge ok">成功</span>'
            : l.status==='failed' ? '<span class="badge bad">失败</span>'
            : '<span class="badge warn">部分</span>';
    const cnt = (l.success!=null && l.total!=null) ? '<span class="log-count">'+l.success+'/'+l.total+'</span>' : '';
    const ch = l.channel ? '<span class="log-chan">'+esc(l.channel)+'</span>' : '';
    const msg = l.message ? '<span class="log-msg">'+esc(l.message)+'</span>' : '';
    return '<div class="log-simple">' +
      '<span class="log-simple-time">'+esc(t)+'</span>' +
      '<span class="log-simple-name">'+esc(l.account_name||'')+'</span>' +
      badge + cnt + ch + msg +
    '</div>';
  }).join('');
}
const btnRefreshLogs = $('#btn-refresh-logs');
if (btnRefreshLogs) btnRefreshLogs.onclick = loadLogs;
const logSearchEl = $('#logSearch');
if (logSearchEl) logSearchEl.addEventListener('input', renderLogs);
const logFilterEl = $('#logFilter');
if (logFilterEl) logFilterEl.addEventListener('change', renderLogs);
const btnClearLogs = $('#btn-clear-logs');
if (btnClearLogs) btnClearLogs.onclick = async () => {
  if (!confirm('确定清空全部签到日志？此操作不可恢复。')) return;
  try {
    const r = await api.del('/api/logs');
    toast('已清空 ' + (r.removed||0) + ' 条日志', 'good');
    loadLogs(); loadDashboard();
  } catch(e){ toast('清空失败','err'); }
};

/* ===== 设置 ===== */
let settingsCache = {};
async function loadSettings() {
  try {
    settingsCache = await api.get('/api/settings');
    setVal('s-tg_enabled', settingsCache.tg_enabled==='1');
    $('#s-tg_bot_token').value = settingsCache.tg_bot_token||'';
    $('#s-tg_user_id').value = settingsCache.tg_user_id||'';
    setVal('s-schedule_enabled', settingsCache.schedule_enabled==='1');
    $('#s-schedule_cron').value = settingsCache.schedule_cron||'0 7 * * *';
    setVal('s-anti_ban_enabled', settingsCache.anti_ban_enabled==='1');
    $('#s-anti_ban_wait_min').value = settingsCache.anti_ban_wait_min||'120';
    $('#s-anti_ban_wait_max').value = settingsCache.anti_ban_wait_max||'300';
    $('#s-anti_ban_window_hour').value = settingsCache.anti_ban_window_hour||'7';
    setVal('s-proxy_force', settingsCache.proxy_force==='1');
    setVal('s-proxy_fallback', settingsCache.proxy_fallback!=='0');
    $('#s-checkin_delay_min').value = settingsCache.checkin_delay_min||'3';
    $('#s-checkin_delay_max').value = settingsCache.checkin_delay_max||'8';
    setVal('s-tg_quote_enabled', settingsCache.tg_quote_enabled!=='0');
    setVal('s-tg_only_on_change', settingsCache.tg_only_on_change==='1');
    setVal('s-tg_silent', settingsCache.tg_silent==='1');
    const retEl = $('#s-log_retention_days');
    if (retEl) retEl.value = settingsCache.log_retention_days||'30';
  } catch(e){ toast('加载设置失败','err'); }
}

function setVal(id, checked) { const el = $('#'+id); if (el) el.checked = !!checked; }
function collectSettings() {
  const val = (id, dflt='') => { const el = $('#'+id); return el ? el.value : dflt; };
  const chk = (id) => { const el = $('#'+id); return el && el.checked ? '1':'0'; };
  return {
    tg_enabled: chk('s-tg_enabled'),
    tg_bot_token: val('s-tg_bot_token').trim(),
    tg_user_id: val('s-tg_user_id').trim(),
    tg_quote_enabled: chk('s-tg_quote_enabled'),
    tg_only_on_change: chk('s-tg_only_on_change'),
    tg_silent: chk('s-tg_silent'),
    schedule_enabled: chk('s-schedule_enabled'),
    schedule_cron: val('s-schedule_cron').trim(),
    anti_ban_enabled: chk('s-anti_ban_enabled'),
    anti_ban_wait_min: val('s-anti_ban_wait_min'),
    anti_ban_wait_max: val('s-anti_ban_wait_max'),
    anti_ban_window_hour: val('s-anti_ban_window_hour'),
    proxy_force: chk('s-proxy_force'),
    proxy_fallback: chk('s-proxy_fallback'),
    checkin_delay_min: val('s-checkin_delay_min'),
    checkin_delay_max: val('s-checkin_delay_max'),
    log_retention_days: val('s-log_retention_days','30'),
  };
}
$('#btn-save-all').onclick = async () => {
  const status = $('#saveStatus');
  status.textContent = '保存中…'; status.className='save-status';
  try {
    settingsCache = await api.post('/api/settings', collectSettings());
    status.textContent = '✅ 已保存'; status.className='save-status ok';
    setTimeout(()=>status.textContent='',2500);
  } catch(e){ status.textContent='保存失败'; status.className='save-status'; }
};
$('#btn-save-schedule').onclick = async () => {
  try {
    await api.post('/api/settings', collectSettings());
    toast('定时配置已保存','good');
  } catch(e){ toast('保存失败','err'); }
};
$('#btn-test-tg').onclick = async () => {
  toast('发送测试中…');
  try {
    await api.post('/api/settings', collectSettings());
    const r = await api.post('/api/notify/test', {});
    toast(r.ok ? '✅ 测试消息已发送' : '❌ 发送失败，请检查配置', r.ok?'good':'err');
  } catch(e){ toast('发送失败','err'); }
};

/* ===== 立即签到 + 运行状态轮询 ===== */
$('#btn-run').onclick = async () => {
  try {
    $('#runModal').hidden = false;
    $('#runBar').style.width='0%';
    $('#runInfo').textContent='正在启动…';
    $('#runLines').innerHTML='';
    await api.post('/api/checkin/run', {});
    pollRun();
  } catch(e){ toast('启动失败','err'); $('#runModal').hidden=true; }
};
$('#runModalClose').onclick = ()=> $('#runModal').hidden=true;

let pollTimer;
function pollRun() {
  clearTimeout(pollTimer);
  api.get('/api/checkin/status').then(s=>{
    if (s.running && s.run) {
      const r = s.run;
      $('#runInfo').textContent = `账号 ${r.accounts_done||0}/${r.accounts_total||0}（${r.progress||0}%）· ${r.started_at}`;
      $('#runBar').style.width = (r.progress||0)+'%';
      addRunLine(`运行中… 已完成 ${r.accounts_done||0}/${r.accounts_total||0} 个账号`);
      pollTimer = setTimeout(pollRun, 1500);
    } else {
      // 结束
      $('#runBar').style.width='100%';
      api.get('/api/checkin/last').then(l=>{
        const sm = l.summary;
        if (sm) {
          $('#runInfo').textContent = `✅ 完成（${sm.status}）· 账号${sm.accounts||0} · 成功${sm.success||0} · 失败${sm.fail||0}`;
          addRunLine(`完成：${sm.message||''}`);
        }
        loadDashboard(); loadAccounts(); loadLogs();
      });
      clearTimeout(pollTimer);
    }
  }).catch(()=>{});
}
function addRunLine(txt){
  const box = $('#runLines');
  const div = document.createElement('div');
  div.className='ln'; div.textContent = txt;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

/* ===== 版本 ===== */
async function loadVersion() {
  try {
    const h = await api.get('/api/health');
    const el = document.getElementById('app-version');
    if (el && h && h.version) el.textContent = 'v' + h.version;
  } catch(e) { /* 忽略，保留占位 */ }
}

/* ===== 健康检查 ===== */
$('#btn-health').onclick = async () => {
  try {
    const h = await api.get('/api/health');
    toast(`服务正常 · v${h.version} · 上次签到 ${h.time}`, 'good');
  } catch(e){ toast('服务异常','err'); }
};

/* ===== 账户：当前用户 / 退出 / 改密 ===== */
async function loadMe() {
  try {
    const me = await api.get('/api/auth/me');
    $('#btn-user').title = me.username || '未登录';
    $('#btn-user').textContent = '👤 ' + (me.username || '');
  } catch(e) { /* 401 已处理 */ }
}
$('#btn-user').onclick = () => {
  document.querySelector('.nav-item[data-view="settings"]').click();
};
$('#btn-logout').onclick = async () => {
  try { await api.post('/api/auth/logout', {}); } catch(e){}
  location.href = '/login.html';
};
$('#btn-change-pwd').onclick = async () => {
  const oldP = $('#p-old').value, newP = $('#p-new').value;
  const st = $('#pwd-status');
  if (!oldP || !newP) { st.textContent = '请填写原密码和新密码'; return; }
  if (newP.length < 6) { st.textContent = '新密码至少 6 位'; return; }
  st.textContent = '提交中…';
  try {
    const r = await api.post('/api/auth/change-password', { old_password: oldP, new_password: newP });
    st.textContent = '✅ ' + (r.message||'已修改');
    setTimeout(()=> location.href='/login.html', 1200);
  } catch(e) {
    st.textContent = e.message || '修改失败';
  }
};

/* ===== 初始化 ===== */
loadVersion();
loadMe();
loadDashboard();
setInterval(()=>{ if (!$('#view-dashboard').hidden) loadDashboard(); }, 30000);
