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
  } catch(e) { toast('加载仪表盘失败', 'err'); }
}

function renderStats(stats, accounts) {
  const active = accounts.filter(a=>a.enabled);
  const okAcc = accounts.filter(a=>a.last_status==='success').length;
  $('#statGrid').innerHTML = [
    card('账号总数', accounts.length, 'acc', '个'),
    card('启用中', active.length, '', '个'),
    card('签到正常', okAcc, 'green', '个'),
    card('累计签到', stats.total || 0, '', '次'),
    card('成功', stats.success || 0, 'green', '次'),
    card('失败', stats.fail || 0, 'red', '次'),
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

/* ===== 账号管理 ===== */
let editingId = null;
async function loadAccounts() {
  try {
    const accounts = await api.get('/api/accounts');
    const tb = $('#accTable tbody');
    tb.innerHTML = accounts.length ? accounts.map(a=>`
      <tr>
        <td>#${a.id}</td>
        <td><strong>${esc(a.name)}</strong></td>
        <td>${a.enabled?statusBadge('success'):'<span class="badge gray">已停用</span>'} · ${statusBadge(a.last_status)}</td>
        <td style="color:var(--muted)">${a.cookie_length} 字符</td>
        <td>${a.proxy? '<span class="badge">'+esc(a.proxy.replace(/socks5.*@/,'socks5://***@').slice(0,28))+'</span>' : '<span class="badge gray">直连</span>'}</td>
        <td>${a.last_checkin||'从未'}</td>
        <td>
          <button class="btn btn-ghost btn-sm" onclick="openAccModal(${a.id})">编辑</button>
          <button class="btn btn-ghost btn-sm" onclick="verifyAcc(${a.id})">校验</button>
          <button class="btn btn-danger btn-sm" onclick="delAcc(${a.id})">删除</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="7" style="color:var(--muted)">暂无账号</td></tr>';
  } catch(e) { toast('加载账号失败', 'err'); }
}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

let accModal = (()=>{})();
async function populateProxySelect(sel, selected='') {
  // 从管理的代理列表读取（含归属地），账号可选指定
  let proxies = [];
  try {
    const list = await api.get('/api/proxies');
    proxies = list.filter(p=>p.enabled!==false).map(p => {
      const cc = p.geo_country_code || '';
      const flag = cc ? (String.fromCodePoint(0x1F1E6+cc.charCodeAt(0)-65, 0x1F1E6+cc.charCodeAt(1)-65)) : '🌐';
      const name = p.label || (p.geo_country ? p.geo_country+' '+(p.geo_region||'') : (p.ip||'节点'));
      return { url: p.url || `${p.ip}:${p.port}`, label: `${flag} ${name}${p.ip?' ('+p.ip+':'+p.port+')':''}` };
    });
  } catch(e) { /* 忽略 */ }
  if (!proxies.length && selected) {
    proxies = [{ url: selected, label: selected }];
  }
  const opt = (v, t) => `<option value="${esc(v)}"${v===selected?' selected':''}>${esc(t)}</option>`;
  sel.innerHTML = opt('', '自动 / 直连') + proxies.map(p => opt(p.url, p.label)).join('');
}

function openAccModal(id=null) {
  editingId = id;
  $('#accModalTitle').textContent = id ? '编辑账号' : '添加账号';
  $('#m-name').value = ''; $('#m-cookie').value = ''; $('#m-remark').value='';
  $('#m-enabled').checked = true;
  populateProxySelect($('#m-proxy'), '');
  if (id) {
    api.get('/api/accounts/'+id).then(a=>{
      $('#m-name').value = a.name;
      $('#m-cookie').value = a.cookie_raw || a.cookie || '';
      $('#m-remark').value = a.remark||'';
      $('#m-enabled').checked = !!a.enabled;
      populateProxySelect($('#m-proxy'), a.proxy || '');
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
    proxy: $('#m-proxy').value,
  };
  try {
    if (editingId) await api.put('/api/accounts/'+editingId, body);
    else await api.post('/api/accounts', body);
    $('#accModal').hidden = false;
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
        <div class="proxy-test" id="ptest-${p.id}"></div>
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
  const el = $('#ptest-'+id); if (el) el.innerHTML = '测试中…';
  try {
    const r = await api.post('/api/proxies/'+id+'/test', {});
    if (el) el.innerHTML = '<span class="'+(r.ok?'t-ok':'t-bad')+'">'+esc(r.message)+'</span>';
    loadProxies();
  } catch(e){ if(el) el.innerHTML = '<span class="t-bad">失败</span>'; }
}
$('#btn-proxy-test-all').onclick = async () => {
  try { const list = await api.get('/api/proxies'); for (const p of list) await testProxy(p.id); } catch(e){}
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
    $('#qrImage').src = d.image;
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
  qrTimer = setInterval(async () => {
    if (!qrId || qrImporting) return;
    try {
      const st = await api.get('/api/auth/qrcode/check?qrid=' + encodeURIComponent(qrId));
      if (st.status === 'pending') {
        $('#qrStatus').textContent = '等待扫码…';
      } else if (st.status === 'scanned') {
        $('#qrStatus').textContent = '✅ 已扫码，请在手机上确认';
        $('#qrStatus').className = 'qr-status success';
        // 已扫码即允许手动导入（后端会补全 Cookie），避免回调卡住时无法继续
        $('#qrImportBtn').disabled = false;
      } else if (st.status === 'success') {
        // 成功：停止轮询并自动导入账号（无需用户手动点按钮）
        qrTimer && clearInterval(qrTimer);
        $('#qrStatus').textContent = '✅ 扫码成功，正在自动导入…';
        $('#qrStatus').className = 'qr-status success';
        $('#qrImportBtn').disabled = false;
        await doQrImport();
      } else if (st.status === 'expired') {
        $('#qrStatus').textContent = '二维码已过期，请刷新';
        $('#qrStatus').className = 'qr-status bad';
        $('#qrImportBtn').disabled = true;
        qrTimer && clearInterval(qrTimer);
      } else if (st.status === 'error') {
        $('#qrStatus').textContent = '查询失败：' + st.message;
        $('#qrStatus').className = 'qr-status bad';
      } else if (st.status === 'unknown') {
        // 未知状态（风控/网络抖动）：循环轮询即可，几分钟后 Qr 会过期
        $('#qrStatus').textContent = '正在确认扫码状态…';
        $('#qrStatus').className = 'qr-status';
      }
    } catch(e) { /* 轮询错误忽略，继续 */ }
  }, 2000);
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
async function loadLogs() {
  try {
    const data = await api.get('/api/logs/grouped?limit=20');
    const list = $('#logList');
    if (!data.length) {
      list.innerHTML = '<div style="color:var(--muted);padding:20px">暂无签到日志</div>';
      return;
    }
    let html = '';
    data.forEach((day, di) => {
      html += `<div class="log-date"><span>📅 ${esc(day.date)}</span><span class="day-count">${day.groups.length} 次执行</span></div>`;
      day.groups.forEach((g) => {
        // 聚合本次执行所有账号结果
        const rows = g.accounts.map(l => {
          const det = (l.detail||[]).map(d=>`<span class="tag ${d.success?'ok':'bad'}">${esc(d.name)} ${d.success?'✓':'✗'}</span>`).join('');
          return `<div class="log-item">
            <div class="lt">
              <span class="acc">${esc(l.account_name)}</span>
              ${statusBadge(l.status)}
              <span class="badge gray">${l.channel}</span>
              <span class="badge gray">${l.success}/${l.total} 成功</span>
              <span class="time">${esc(l.created_at||'')}</span>
            </div>
            ${det ? `<div class="log-detail">${det}</div>` : `<div class="log-detail"><span style="color:var(--muted);font-size:12px">${esc(l.message||'')}</span></div>`}
          </div>`;
        }).join('');
        const time = g.created_at ? g.created_at.slice(11,16) : '';
        html += `<div class="log-task">
          <div class="lt task-head">
            <span class="task-ico">🕘 ${esc(time)}</span>
            ${statusBadge(g.status)}
            <span class="badge gray">${g.accounts.length} 个账号</span>
            <span class="badge gray">成功 ${g.success} · 失败 ${g.fail}</span>
            <span class="meta">#${esc(g.task_id||'')}</span>
          </div>
          ${rows}
        </div>`;
      });
    });
    list.innerHTML = html;
  } catch(e){ toast('加载日志失败','err'); }
}
$('#btn-refresh-logs').onclick = loadLogs;

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
  } catch(e){ toast('加载设置失败','err'); }
}

function setVal(id, checked) { $('#'+id).checked = !!checked; }
function collectSettings() {
  return {
    tg_enabled: $('#s-tg_enabled').checked ? '1':'0',
    tg_bot_token: $('#s-tg_bot_token').value.trim(),
    tg_user_id: $('#s-tg_user_id').value.trim(),
    schedule_enabled: $('#s-schedule_enabled').checked ? '1':'0',
    schedule_cron: $('#s-schedule_cron').value.trim(),
    anti_ban_enabled: $('#s-anti_ban_enabled').checked ? '1':'0',
    anti_ban_wait_min: $('#s-anti_ban_wait_min').value,
    anti_ban_wait_max: $('#s-anti_ban_wait_max').value,
    anti_ban_window_hour: $('#s-anti_ban_window_hour').value,
    proxy_force: $('#s-proxy_force').checked ? '1':'0',
    proxy_fallback: $('#s-proxy_fallback').checked ? '1':'0',
    checkin_delay_min: $('#s-checkin_delay_min').value,
    checkin_delay_max: $('#s-checkin_delay_max').value,
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
loadMe();
loadDashboard();
setInterval(()=>{ if (!$('#view-dashboard').hidden) loadDashboard(); }, 30000);
