/* 前端渲染测试：用 jsdom 加载 index.html + app.js，mock fetch，验证 UI 正确渲染。
   运行：node tests/frontend-render.test.js
   若 jsdom 未安装，会自动装到本地临时目录（不污染项目）。 */
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');
const os = require('os');

let jsdom;
try {
  jsdom = require('jsdom');
} catch (e) {
  // 自动安装 jsdom 到临时目录并用 NODE_PATH 解析
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'jsdom-'));
  console.log('⏳ 正在临时安装 jsdom…');
  execSync('npm install jsdom --prefix ' + tmp, { stdio: 'inherit' });
  process.env.NODE_PATH = path.join(tmp, 'node_modules');
  delete require.cache[require.resolve('module')];
  require('module').Module._initPaths();
  jsdom = require('jsdom');
}
const { JSDOM } = jsdom;

const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'index.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');

// --- 构造 mock 数据 ---
const mockStats = { total: 42, success: 38, partial: 2, fail: 2, today: 5 };
const mockAccounts = [
  { id: 1, name: '小号A', enabled: 1, last_status: 'success', last_checkin: '2026-08-14 07:00:00', last_message: '签到完成', cookie_length: 40, cookie_preview: 'SUB=xxx…', remark: '', proxy_index: 0 },
  { id: 2, name: '小号B', enabled: 1, last_status: 'failed', last_checkin: null, last_message: 'Cookie 无效', cookie_length: 0, cookie_preview: '', remark: '', proxy_index: 0 },
];
const mockTasks = [
  { task_id: 'abc123', trigger_type: 'schedule', status: 'success', started_at: '2026-08-14 07:00:00', finished_at: '2026-08-14 07:00:05' },
];
const mockSettings = {
  tg_enabled: '1', tg_bot_token: 'tok', tg_user_id: '123', schedule_enabled: '1',
  schedule_cron: '0 7 * * *', anti_ban_enabled: '1', anti_ban_wait_min: '120',
  anti_ban_wait_max: '300', anti_ban_window_hour: '7',
  proxies: 'socks5://a@1:1', proxy_force: '0', proxy_fallback: '1',
  checkin_delay_min: '3', checkin_delay_max: '8',
};

// --- mock fetch 路由 ---
function mockFetch(url) {
  const u = url.split('?')[0];
  const table = {
    '/api/logs/stats': { ok: true, json: () => Promise.resolve(mockStats) },
    '/api/accounts': { ok: true, json: () => Promise.resolve(mockAccounts) },
    '/api/tasks': { ok: true, json: () => Promise.resolve(mockTasks) },
    '/api/settings': { ok: true, json: () => Promise.resolve(mockSettings) },
    '/api/logs': { ok: true, json: () => Promise.resolve([]) },
    '/api/checkin/status': { ok: true, json: () => Promise.resolve({ running: false }) },
    '/api/checkin/last': { ok: true, json: () => Promise.resolve({ summary: { status:'success', accounts:2, success:38, fail:2 } }) },
  };
  if (table[u]) return Promise.resolve(table[u]);
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
}

let failures = 0;
function check(name, cond) {
  console.log((cond ? '  ✅ ' : '  ❌ ') + name);
  if (!cond) failures++;
}

(async () => {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost:8000/' });
  const { window } = dom;
  window.fetch = mockFetch;
  window.confirm = () => true;
  window.localStorage = { getItem: () => null, setItem: () => {} };
  window.setInterval = () => {};  // 禁用自动刷新

  const errs = [];
  window.addEventListener('error', e => errs.push(e.message));

  // 执行 app.js（在 window 上下文中）
  window.eval(js);

  await new Promise(r => setTimeout(r, 300));  // 等待异步加载
  
  // 若还没渲染，再多等一会并检查 console 错误
  if (window.document.querySelectorAll('#statGrid .stat').length === 0) {
    await new Promise(r => setTimeout(r, 600));
  }

  console.log('— 仪表盘 —');
  check('页面标题正确', window.document.title.includes('微博超话签到'));
  check('6 张统计卡渲染', window.document.querySelectorAll('#statGrid .stat').length === 6);
  check('账号总数卡=2', window.document.querySelectorAll('#statGrid .stat .num')[0].textContent.includes('2'));
  check('累计签到卡=42', window.document.querySelectorAll('#statGrid .stat .num')[3].textContent.includes('42'));

  console.log('— 导航 —');
  check('4 个导航项', window.document.querySelectorAll('.nav-item').length === 4);

  console.log('— 账号管理 —');
  const navAccounts = window.document.querySelector('.nav-item[data-view="accounts"]');
  navAccounts.click();  // 切到账号页
  await new Promise(r => setTimeout(r, 200));
  check('账号表渲染 2 行', window.document.querySelectorAll('#accTable tbody tr').length === 2);
  check('账号名正确', window.document.querySelector('#accTable tbody tr').textContent.includes('小号A'));

  if (errs.length) { console.log('⚠️ window 错误:', errs); }
  console.log('— 设置页 —');
  window.document.querySelector('.nav-item[data-view="settings"]').click();
  await new Promise(r => setTimeout(r, 500));
  check('Cron 输入框有值', window.document.querySelector('#s-schedule_cron').value === '0 7 * * *');
  check('TG 开关为开', window.document.querySelector('#s-tg_enabled').checked === true);
  check('防封等待最小值 120', window.document.querySelector('#s-anti_ban_wait_min').value === '120');

  console.log('— 弹窗 —');
  window.document.querySelector('.nav-item[data-view="accounts"]').click();
  await new Promise(r => setTimeout(r, 100));
  window.document.querySelector('#btn-add-acc').click();
  check('添加账号弹窗打开', window.document.querySelector('#accModal').hidden === false);
  check('弹窗标题为"添加账号"', window.document.querySelector('#accModalTitle').textContent === '添加账号');
  // 填表并保存
  window.document.querySelector('#m-name').value = '新账号';
  window.document.querySelector('#m-cookie').value = 'SUB=new';
  window.document.querySelector('#accModalSave').click();
  await new Promise(r => setTimeout(r, 200));
  // 由于 mock 的 POST 返回空，至少不应报错；弹窗应关闭
  check('保存后弹窗关闭', window.document.querySelector('#accModal').hidden === true);

  console.log('');
  if (failures === 0) {
    console.log('🎉 全部前端渲染测试通过');
    process.exit(0);
  } else {
    console.log(`\n❌ ${failures} 项失败`);
    process.exit(1);
  }
})();
