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

console.log('frontend_sync_schedule: all assertions passed');
