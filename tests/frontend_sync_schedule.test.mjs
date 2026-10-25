// ZhiZhang V1.1 Phase1 前端行为测试：自动同步状态卡 + 托盘常驻更新检查重查。
// 从 web/v1/app.js 提取真实函数对象（syncStatusMeta / syncIntervalButtons，
// marker 之间），并以静态接线契约覆盖 HTTP/动作/启动加载（与
// frontend_update_notice.test.mjs 同一 Node 直驱风格）。
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const src = fs.readFileSync(new URL('../web/v1/app.js', import.meta.url), 'utf-8');
const pick = (marker) => {
  const begin = src.indexOf(`/* ${marker}:begin`);
  const end = src.indexOf(`/* ${marker}:end */`);
  assert.ok(begin >= 0 && end > begin, `missing marker ${marker}`);
  return src.slice(begin, end);
};

// ---- syncStatusMeta：状态语义（维护静默/互斥/顺延/失败/正常） ----
const ctx = { };
vm.createContext(ctx);
const { syncStatusMeta, syncIntervalButtons } = vm.runInContext(
  pick('syncStatus') + '\n;({ syncStatusMeta, syncIntervalButtons });', ctx);

assert.equal(syncStatusMeta(null).label, '状态不可用');
assert.equal(syncStatusMeta({}).label, '未开启');
assert.equal(syncStatusMeta({ enabled: true }).label, '运行中');
assert.equal(syncStatusMeta({ enabled: true }).type, 'complete');
assert.equal(
  syncStatusMeta({ enabled: true, maintenance_quiesced: true }).label,
  '维护静默中，暂停运行');
assert.equal(syncStatusMeta({ enabled: true, scan_running: true }).label, '正在同步');
assert.equal(
  syncStatusMeta({ enabled: true, last_status: 'postponed_busy' }).type, 'partial');
assert.equal(syncStatusMeta({ enabled: true, last_status: 'failed' }).type, 'error');
// 未启用时任何后端字段都不产生"运行中"误报
assert.equal(syncStatusMeta({ enabled: false, scan_running: true }).label, '未开启');

// ---- syncIntervalButtons：档位与选中态 ----
{
  const h = syncIntervalButtons({ interval_minutes: 30 });
  assert.ok(h.includes('data-interval="30"'), '必须含 30 分钟档');
  assert.ok(h.includes('data-interval="120"'), '必须含 120 分钟档');
  assert.ok(/data-interval="30"[^>]*class="active"/.test(h), '当前间隔应选中');
  const h2 = syncIntervalButtons(null);
  assert.ok(h2.includes('30'), '无配置时默认 30 分钟档展示');
}

// ---- 自动同步接线契约（静态） ----
assert.ok(src.includes("api('/api/v1/sync-schedule'"),
  '必须通过 GET /api/v1/sync-schedule 读取状态');
assert.ok(src.includes("post('/api/v1/sync-schedule'"),
  '必须通过 POST /api/v1/sync-schedule 写开关/间隔');
assert.ok(src.includes("'toggle-auto-sync'"), '设置页必须有开关动作');
assert.ok(src.includes("'set-sync-interval'"), '设置页必须有间隔动作');
assert.ok(src.includes('下次自动更新'), '状态卡必须显示下次预计运行');
assert.ok(src.includes('最近一次自动结果'), '状态卡必须显示最近结果');
assert.ok(src.includes('上次未成功的原因'), '状态卡必须显示失败原因位');
assert.ok(src.includes('autoSyncGroup()'), 'scan 设置页必须挂自动同步卡');
assert.ok(!src.includes('当前版本尚未开放桌面设置'),
  'V1.0.0 的自动更新占位文案必须移除');
assert.ok(/Promise\.all\(\[loadIdentity\(\),loadHealth\(\),loadDiscover\(\),loadDesktop\(\),loadSync\(\)\]\)/.test(src),
  'boot 必须加载同步状态');

// ---- 托盘常驻更新检查重查（V1.0.0 实测缺陷：12 天只查过一次） ----
{
  const block = pick('autoCheckTicker');
  assert.ok(block.includes('maybeAutoCheckUpdate'),
    '重查必须复用节流入口（24h 节流与开关判断不旁路）');
  assert.ok(/60\s*\*\s*60\s*\*\s*1000/.test(block), '复查周期必须为 1 小时');
  // 启动路径仍保留首查
  assert.ok(src.includes('maybeAutoCheckUpdate();   // 启动时后台检查'));
}

// ---- toolStatusMeta：三态不误报（V1.1 验收修复的回归锁） ----
{
  const ctx2 = {};
  vm.createContext(ctx2);
  const { toolStatusMeta } = vm.runInContext(
    pick('toolStatus') + '\n;({ toolStatusMeta });', ctx2);
  // 适配器存在但本机无数据 → 必须显示 未找到（不得误报已连接）
  assert.equal(
    toolStatusMeta({ status: 'SUPPORTED', capability: 'TOKEN', data_sources_count: 0 }).b,
    '未找到');
  assert.equal(
    toolStatusMeta({ status: 'SUPPORTED', capability: 'ACTIVITY', data_sources_count: 0 }).b,
    '未找到');
  // 有数据 → 已连接（TOKEN/ACTIVITY 语义区分保持）
  assert.equal(
    toolStatusMeta({ status: 'SUPPORTED', capability: 'TOKEN', data_sources_count: 2 }).b,
    '已连接');
  assert.equal(
    toolStatusMeta({ status: 'SUPPORTED', capability: 'ACTIVITY', data_sources_count: 1 }).p,
    '只能确认使用过，暂无具体用量');
  // Codex 历史缺口口径保持
  assert.equal(
    toolStatusMeta({ status: 'SUPPORTED', capability: 'TOKEN', data_sources_count: 3,
                     completeness: 'PARTIAL' }).b,
    '已连接 · 部分历史缺口');
  // 其余状态不受影响
  assert.equal(toolStatusMeta({ status: 'DETECTED_UNSUPPORTED' }).b, '新发现 · 暂未支持');
  assert.equal(toolStatusMeta({ status: 'UNKNOWN' }).b, '待识别');
}

// ---- V1.1.1 statusPoll：可见性驱动轻量轮询（绝不触发扫描） ----
{
  const makePollCtx = (vis = 'visible', overlay = null) => {
    const ctx = {
      state: { overlay, health: { last_run: { finished_at: '2026-10-25T17:14:33' } } },
      calls: [], refreshes: 0, renders: 0, listeners: 0,
      async api(p) {
        ctx.calls.push(p);
        if (p === '/api/v1/health') {
          if (ctx._healthDown) throw new Error('本地服务不可达');
          return { last_run: { finished_at: ctx._nextFinished || '2026-10-25T17:29:40' } };
        }
        return { next_run_at: '2026-10-25T17:44:40', last_run_at: '2026-10-25T17:29:40', last_status: 'success' };
      },
      async refreshAndRender() { ctx.refreshes++; },
      render() { ctx.renders++; },
      document: { visibilityState: vis, addEventListener() { ctx.listeners++; } },
      window: { addEventListener() { ctx.listeners++; } },
      setInterval: () => 1,
    };
    return ctx;
  };
  const load = (ctx) => {
    vm.createContext(ctx);
    return vm.runInContext(
      pick('statusPoll') + '\n;({ statusCheck, startStatusPolling, statusPoll })', ctx);
  };

  // T1 隐藏（托盘/后台）→ 不发任何请求
  {
    const ctx = makePollCtx('hidden'); load(ctx);
    await vm.runInContext('statusCheck()', ctx);
    assert.equal(ctx.calls.length, 0, '隐藏态不得轮询');
  }
  // T2 新一轮真实完成 → 只重取总览（refresh），仅两个 GET、无 POST
  {
    const ctx = makePollCtx(); load(ctx);
    await vm.runInContext('statusCheck()', ctx);
    assert.deepEqual(ctx.calls, ['/api/v1/health', '/api/v1/sync-schedule']);
    assert.ok(!ctx.calls.some(c => c.includes('scan')), '轮询不得触发扫描');
    assert.equal(ctx.refreshes, 1, '新一轮扫描完成才重取总览');
    assert.equal(ctx.renders, 0, 'refreshAndRender 自带渲染，不重复 render');
    // T3 同数据再查 → 无新请求副作用、不重复刷新/渲染
    await vm.runInContext('statusCheck()', ctx);
    assert.equal(ctx.refreshes, 1);
    assert.equal(ctx.renders, 0);
  }
  // T4 overlay 打开（用户交互中）→ 只记待补不渲染；关闭后补一次刷新
  {
    const ctx = makePollCtx('visible', 'attribution'); load(ctx);
    await vm.runInContext('statusCheck()', ctx);
    assert.equal(ctx.refreshes, 0, '交互中不得刷新打断');
    assert.equal(ctx.renders, 0);
    ctx.state.overlay = null;                 // 交互结束（sig 不变，走 pendingReload 分支）
    await vm.runInContext('statusCheck()', ctx);
    assert.equal(ctx.refreshes, 1, '交互结束后补一次刷新');
  }
  // T5 后端断开 → health 置 null（未知态），不沿用旧成功冒充
  {
    const ctx = makePollCtx(); load(ctx);
    ctx._healthDown = true;
    await vm.runInContext('statusCheck()', ctx);
    assert.equal(ctx.state.health, null, '断连后 state.health 必须置空（UI 显示未知）');
    assert.equal(ctx.refreshes, 0);
  }
  // T6 startStatusPolling 幂等（重复调用不重复挂监听）
  {
    const ctx = makePollCtx(); const h = load(ctx);
    vm.runInContext('startStatusPolling();startStatusPolling();', ctx);
    assert.equal(ctx.listeners, 2, 'visibilitychange + focus 各一次');
  }
  // 静态契约：轮询块内不得出现任何 POST；boot 必须启动轮询
  assert.ok(!pick('statusPoll').includes('post('), 'statusPoll 块内禁止 POST');
  assert.ok(src.includes('startStatusPolling();          // V1.1.1'),
    'boot 必须启动状态轮询');
}

// ---- V1.1.1 lastUpdateText：移除「刚刚更新」长驻 + trigger 区分 + 未知态 ----
{
  // 旧版样张（v1.1.0 原文，缺陷对照）：手动更新后 state.scan==='done' 长驻
  // 「刚刚更新」，与账本真实时间脱钩 —— 先重现，再验证新版修复。
  const legacySrc =
    "function lastUpdateText(){const lr=state.health&&(state.health.last_run||{});" +
    "if(state.scan==='done')return '刚刚更新';" +
    "if(lr&&lr.finished_at)return relative(Date.parse(lr.finished_at))+'更新';" +
    "return '尚未更新';}";
  const mkState = () => ({
    state: { scan: 'done', health: { last_run: { finished_at: '2026-10-25T17:14:33+08:00', trigger: 'scheduled' } } },
    relative: t => (t ? '3 分钟前' : '暂无活动'),
  });
  const legacyCtx = mkState(); vm.createContext(legacyCtx);
  assert.equal(vm.runInContext(legacySrc + ';lastUpdateText()', legacyCtx),
    '刚刚更新', '旧缺陷重现：手动更新后长驻「刚刚更新」');
  const newCtx = mkState(); vm.createContext(newCtx);
  assert.equal(vm.runInContext(pick('lastUpdate') + ';lastUpdateText()', newCtx),
    '自动 · 3 分钟前更新', '新版：真实时间 + 自动/手动区分');
  const manualCtx = mkState(); manualCtx.state.health.last_run.trigger = 'api';
  vm.createContext(manualCtx);
  assert.equal(vm.runInContext(pick('lastUpdate') + ';lastUpdateText()', manualCtx),
    '手动 · 3 分钟前更新');
  const unknownCtx = { state: { scan: 'idle', health: null }, relative: () => '' };
  vm.createContext(unknownCtx);
  assert.equal(vm.runInContext(pick('lastUpdate') + ';lastUpdateText()', unknownCtx),
    '状态未知 · 待刷新', 'health 不可读 → 明示未知，不用旧成功冒充');
}

// ---- V1.1.1 状态卡静态契约：实测执行行 + 「已开启」语义注 ----
assert.ok(src.includes('最近一次执行（含手动）'), '自动同步卡必须含最近实际执行行');
assert.ok(src.includes('成功 · 无新记录'), '零新增须可如实显示（有计数佐证）');
assert.ok(src.includes('「已开启」表示自动更新计划已启用'), '「已开启」不得暗示线程健康');

console.log('frontend_sync_schedule: all assertions passed');
