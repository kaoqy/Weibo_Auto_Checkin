/* ===== 微博签到管理面板 - 前端逻辑 ===== */
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];

const api = {
  get: (p) => fetch(p).then(r => r.ok ? r.json() : Promise.reject(r)),
  post: (p, b) => fetch(p, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b||{})}).then(r => r.ok ? r.json() : Promise.reject(r)),
  put: (p, b) => fetch(p, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(b)}).then(r => r.ok ? r.json() : Promise.reject(r)),
  del: (p) => fetch(p, {method:'DELETE'}).then(r => r.ok ? r.json() : Promise.reject(r)),
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
const VIEW_TITLES = {dashboard:'仪表盘', accounts:'账号管理', logs:'签到日志', settings:'设置'};
$$('.nav-item').forEach(btn => {
  btn.onclick = () => {
    $$('.nav-item').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    $$('.view').forEach(v=>v.hidden = true);
    $('#view-' + btn.dataset.view).hidden = false;
    $('#view-title').textContent = VIEW_TITLES[btn.dataset.view];
    if (btn.dataset.view === 'dashboard') loadDashboard();
    if (btn.dataset.view === 'accounts') loadAccounts();
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
        <td>${a.last_checkin||'从未'}</td>
        <td>
          <button class="btn btn-ghost btn-sm" onclick="openAccModal(${a.id})">编辑</button>
          <button class="btn btn-ghost btn-sm" onclick="verifyAcc(${a.id})">校验</button>
          <button class="btn btn-danger btn-sm" onclick="delAcc(${a.id})">删除</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="6" style="color:var(--muted)">暂无账号</td></tr>';
  } catch(e) { toast('加载账号失败', 'err'); }
}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

let accModal = (()=>{})();
function openAccModal(id=null) {
  editingId = id;
  $('#accModalTitle').textContent = id ? '编辑账号' : '添加账号';
  $('#m-name').value = ''; $('#m-cookie').value = ''; $('#m-remark').value='';
  $('#m-enabled').checked = true; $('#m-proxy_round').checked = false;
  if (id) {
    api.get('/api/accounts/'+id).then(a=>{
      $('#m-name').value = a.name;
      $('#m-cookie').value = a.cookie_raw || a.cookie || '';
      $('#m-remark').value = a.remark||'';
      $('#m-enabled').checked = !!a.enabled;
      $('#m-proxy_round').checked = a.proxy_index > 0;
    });
  }
  $('#accModal').hidden = false;
}
$('#btn-add-acc').onclick = () => openAccModal();

/* ===== 微博扫码登录（B 方案：纯 requests，无浏览器内核） ===== */
let qrSessionId = '';
let qrPollTimer = null;

function stopQrPolling() {
  if (qrPollTimer) clearTimeout(qrPollTimer);
  qrPollTimer = null;
}

function closeQrModal() {
  stopQrPolling();
  $('#qrModal').hidden = true;
}

async function startQrLogin() {
  stopQrPolling();
  qrSessionId = '';
  $('#qrModal').hidden = false;
  $('#qrImage').hidden = true;
  $('#qrLoading').hidden = false;
  $('#qrLoading').textContent = '正在获取二维码…';
  $('#qrStatus').textContent = '请稍候';
  $('#qrSave').disabled = true;
  try {
    const result = await api.post('/api/accounts/qr/start', {});
    qrSessionId = result.session_id;
    $('#qrImage').src = result.image;
    $('#qrImage').hidden = false;
    $('#qrLoading').hidden = true;
    $('#qrStatus').textContent = result.message || '请使用微博客户端扫码';
    pollQrStatus();
  } catch (e) {
    $('#qrLoading').textContent = '二维码获取失败';
    $('#qrStatus').textContent = e.message || '请稍后重试';
    toast('获取二维码失败', 'err');
  }
}

async function pollQrStatus() {
  if (!qrSessionId || $('#qrModal').hidden) return;
  try {
    const result = await api.get(`/api/accounts/qr/${encodeURIComponent(qrSessionId)}/status`);
    $('#qrStatus').textContent = result.message || '等待扫码';
    if (result.status === 'confirmed') {
      $('#qrSave').disabled = false;
      toast('扫码登录成功，请保存账号', 'good');
      return;
    }
    if (result.status === 'expired' || result.status === 'failed') {
      $('#qrSave').disabled = true;
      return;
    }
    qrPollTimer = setTimeout(pollQrStatus, 1800);
  } catch (e) {
    $('#qrStatus').textContent = e.message || '查询扫码状态失败';
    qrPollTimer = setTimeout(pollQrStatus, 3000);
  }
}

$('#btn-qr-login').onclick = startQrLogin;
$('#qrRefresh').onclick = startQrLogin;
$('#qrModalClose').onclick = closeQrModal;
$('#qrSave').onclick = async () => {
  if (!qrSessionId) return;
  const name = $('#qrAccountName').value.trim() || '扫码登录账号';
  $('#qrSave').disabled = true;
  try {
    await api.post('/api/accounts/qr/finish', {
      session_id: qrSessionId,
      name,
      enabled: true,
      proxy_index: 0,
      remark: '扫码登录',
    });
    closeQrModal();
    toast('账号已通过扫码登录添加', 'good');
    loadAccounts();
    loadDashboard();
  } catch (e) {
    $('#qrSave').disabled = false;
    toast(e.message || '保存扫码账号失败', 'err');
  }
};

$('#accModalClose').onclick = $('#accModalCancel').onclick = ()=> $('#accModal').hidden = true;
$('#accModalSave').onclick = async () => {
  const body = {
    name: $('#m-name').value.trim() || '未命名账号',
    cookie_raw: $('#m-cookie').value.trim(),
    remark: $('#m-remark').value.trim(),
    enabled: $('#m-enabled').checked,
    proxy_index: $('#m-proxy_round').checked ? 1 : 0,
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

/* ===== 日志 ===== */
async function loadLogs() {
  try {
    const logs = await api.get('/api/logs?limit=100');
    const list = $('#logList');
    list.innerHTML = logs.length ? logs.map(l=>{
      const det = (l.detail||[]).map(d=>`<span class="tag ${d.success?'ok':'bad'}">${esc(d.name)} ${d.success?'✓':'✗'}</span>`).join('');
      return `<div class="log-item">
        <div class="lt">
          <span class="acc">${esc(l.account_name)}</span>
          ${statusBadge(l.status)}
          <span class="badge gray">${l.channel}</span>
          <span class="badge gray">${l.success}/${l.total} 成功</span>
          <span class="time">${l.created_at}</span>
          <span class="meta">#${l.task_id}</span>
        </div>
        ${det ? `<div class="log-detail">${det}</div>` : `<div class="log-detail"><span style="color:var(--muted);font-size:12px">${esc(l.message)}</span></div>`}
      </div>`;
    }).join('') : '<div style="color:var(--muted);padding:20px">暂无签到日志</div>';
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
    $('#s-proxies').value = settingsCache.proxies||'';
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
    proxies: $('#s-proxies').value,
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

/* ===== 初始化 ===== */
loadDashboard();
setInterval(()=>{ if (!$('#view-dashboard').hidden) loadDashboard(); }, 30000);
