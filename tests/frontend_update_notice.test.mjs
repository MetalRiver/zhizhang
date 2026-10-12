// ZhiZhang V1.0.0 前端行为测试：软件更新提醒状态（Node 直驱生产代码）。
// 从 web/v1/app.js 提取 cmpSemver + shouldAutoCheck + updateNoticeState（marker 之间），
// 以真实函数对象断言六类状态：无更新 / 有更新 / 网络失败 / 开关关闭 / 24h 限制 /
// 升级后清除。另附处理器与 Tauri 命令的静态接线契约。
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
const ctx = { console };
vm.createContext(ctx);
const { cmpSemver, shouldAutoCheck, updateNoticeState } = vm.runInContext(
  pick('updateNotice') + '\n;({ cmpSemver, shouldAutoCheck, updateNoticeState });', ctx);

const HOUR = 60 * 60 * 1000;
const NOW = 1_800_000_000_000;

// ---- 版本比较（1.0.0 与现有 rc 语义兼容） ----
assert.equal(cmpSemver('1.0.0', '0.11.0-rc.7'), 1, '1.0.0 应高于 0.11.0-rc.7');
assert.equal(cmpSemver('0.11.0-rc.7', '0.11.0-rc.6'), 1, 'rc.7 应高于 rc.6');
assert.equal(cmpSemver('0.11.0', '0.11.0-rc.7'), 1, '正式版应高于同号预发布');
assert.equal(cmpSemver('1.0.0', '1.0.0'), 0);
assert.equal(cmpSemver('0.11.0-rc.6', '0.11.0-rc.7'), -1);

// ---- 1. 有更新：发现比当前更新的版本 → 显示提醒 ----
{
  const s = updateNoticeState('0.11.0-rc.7', '', { status: 'available', version: '1.0.0' });
  assert.equal(s.notice, '1.0.0');
  assert.equal(s.clear, false);
}
// ---- 2. 无更新（已是最新）→ 清除提醒 ----
{
  const s = updateNoticeState('1.0.0', '1.0.0', { status: 'up_to_date' });
  assert.equal(s.notice, null);
  assert.equal(s.clear, true);
}
// 2b) 检查返回 available 但版本不高于当前（异常/回退）→ 不得误报，清除
{
  const s = updateNoticeState('1.0.0', '', { status: 'available', version: '0.11.0-rc.7' });
  assert.equal(s.notice, null);
  assert.equal(s.clear, true);
}
// 2c) 更新源未配置 → 清除
assert.equal(updateNoticeState('1.0.0', '1.0.0', { status: 'not_configured' }).notice, null);

// ---- 3. 网络失败：本次没有结果（null）→ 沿用已存提醒，不清除、不改动 ----
{
  const s = updateNoticeState('0.11.0-rc.7', '1.0.0', null);
  assert.equal(s.notice, '1.0.0', '网络失败不得清除已有提醒');
  assert.equal(s.clear, false);
}
// 3b) 网络失败且无已存提醒 → 保持无提醒
assert.equal(updateNoticeState('0.11.0-rc.7', '', null).notice, null);

// ---- 4. 开关关闭：不自动检查 ----
assert.equal(shouldAutoCheck({ auto_check_update: false, last_update_check_ms: 0 }, NOW), false);
assert.equal(shouldAutoCheck(null, NOW), false);
assert.equal(shouldAutoCheck({ auto_check_update: 'yes' }, NOW), false, '仅接受布尔 true');
// 4b) 默认开启（Rust 缺字段 → true）在启动时会触发自动检查
assert.equal(shouldAutoCheck({ auto_check_update: true, last_update_check_ms: 0 }, NOW), true);

// ---- 5. 24 小时限制 ----
assert.equal(shouldAutoCheck({ auto_check_update: true, last_update_check_ms: NOW - 23 * HOUR }, NOW), false);
assert.equal(shouldAutoCheck({ auto_check_update: true, last_update_check_ms: NOW - 24 * HOUR }, NOW), true);
assert.equal(shouldAutoCheck({ auto_check_update: true, last_update_check_ms: NOW - 25 * HOUR }, NOW), true);
assert.equal(shouldAutoCheck({ auto_check_update: true, last_update_check_ms: 0 }, NOW), true, '从未检查过 → 允许');

// ---- 6. 升级成功后状态清除（当前版本已达到/超过已存提醒版本） ----
{
  const s = updateNoticeState('1.0.0', '0.11.0-rc.7', null);
  assert.equal(s.notice, null);
  assert.equal(s.clear, true);
}
{
  const s = updateNoticeState('1.0.0', '1.0.0', null);
  assert.equal(s.notice, null);
  assert.equal(s.clear, true);
}
// 6b) 已存提醒确实更新 → 保留
assert.equal(updateNoticeState('0.11.0-rc.7', '1.0.0', null).notice, '1.0.0');

// ---- 静态接线契约 ----
const must = [
  'data-action="open-about-update"',            // 侧边栏/设置入口直达设置→关于
  "state.settingTab='about'",                    // 直达「关于」
  "act==='toggle-auto-check-update'",            // 开关处理器
  "tauriInvoke('set_auto_check_update'",         // 持久化开关
  "tauriInvoke('record_update_check'",           // 持久化检查结果（时间+版本）
  "'clear_update_notice'",                        // 升级后清除
  'function sidebarUpdate(',                     // 侧边栏底部入口
  'state.updateAvailable',                       // 设置入口圆点来源
  'maybeAutoCheckUpdate',                        // 启动时自动检查
  'shouldAutoCheck(state.desktop,Date.now())',   // 24h 节流接入
  '自动检查软件更新',                             // 设置项文案（V1.0.0 定稿）
  'if(on)maybeAutoCheckUpdate()',                 // 关闭→开启立即做一次（受节流）
];
for (const m of must) assert.ok(src.includes(m), `app.js 缺少接线：${m}`);

// 网络失败必须静默：自动检查里不得设置全局错误、不得自动下载
const autoBlock = src.slice(src.indexOf('async function maybeAutoCheckUpdate'),
                            src.indexOf('async function maybeAutoCheckUpdate') + 600);
assert.ok(autoBlock.includes('catch'), 'maybeAutoCheckUpdate 必须有 catch');
assert.ok(!autoBlock.includes('state.error'), '自动检查失败不得写入 state.error');
assert.ok(!autoBlock.includes('download_and_install_update'), '自动检查绝不自动下载/安装');
// 检查更新（手动/自动）都不得触发下载安装；下载仍是独立的用户显式动作
assert.ok(src.includes("act==='download-update'"), '下载仍是独立动作');
{
  const start = src.indexOf("act==='check-update'");
  const blk = src.slice(start, start + 900);
  assert.ok(!blk.includes('download_and_install_update'), '检查更新不得触发下载安装');
}

console.log('FRONTEND-UPDATE-NOTICE-OK: assertions passed');
