// ZhiZhang RC.7-r2 前端行为测试：firstScanVerdict 判定（Node 直驱生产代码）。
// 从 web/v1/app.js 提取 scanOutcome + firstScanVerdict（marker 之间），
// 在 Node vm 中以真实函数对象运行五类后端响应的行为断言。
// 由 tests/test_v1_runtime.py::test_frontend_first_scan_verdict 以 node 子进程驱动。
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const src = fs.readFileSync(new URL('../web/v1/app.js', import.meta.url),
                            'utf-8');
const pick = (marker) => {
  const begin = src.indexOf(`/* ${marker}:begin`);
  const end = src.indexOf(`/* ${marker}:end */`);
  assert.ok(begin >= 0 && end > begin, `missing marker ${marker}`);
  return src.slice(begin, end);
};
const scanLine = src.match(/const scanOutcome=[^;]+;/);
assert.ok(scanLine, 'missing scanOutcome');
const ctx = { console };
vm.createContext(ctx);
const fns = vm.runInContext(
  scanLine[0] + '\n' + pick('firstScanVerdict') +
  '\n;({ scanOutcome, firstScanVerdict });', ctx);
const { firstScanVerdict } = fns;

// 1) 成功扫描且有记录 → complete（进入主界面）
assert.equal(firstScanVerdict({ status: 'success' }, true), 'complete');
// 2) 成功扫描但零记录（工具存在但无解析记录）→ complete（账本已建立，
//    overview 空态如实呈现；不得弹回引导死循环）
assert.equal(firstScanVerdict({ status: 'success' }, false), 'complete');
// 3) 真零工具（sources_found=0，无读取错误）→ 空账本合法完成
assert.equal(firstScanVerdict({
  status: 'no_supported_sources', sources_found: 0,
  health: 'failed', errors: [] }), 'complete-empty');
// 4) 来源读取失败（sources_found≥1 + 明确 errors）→ 不得伪装成空账本成功
assert.equal(firstScanVerdict({
  status: 'no_supported_sources', sources_found: 1, health: 'failed',
  errors: ['所有已发现的数据源都读取失败（1 个）—— 等价于没有可用数据源',
           '数据源读取失败 → zcode | bad-zcode.sqlite | file is not a database']
}), 'retry-error');
// 4b) 发现了来源但读取全失败时，即便 health 未标 failed 也必须报错
assert.equal(firstScanVerdict({
  status: 'no_supported_sources', sources_found: 2, errors: ['x'] }), 'retry-error');
// 5) 其他失败/未知状态 → 如实报错
assert.equal(firstScanVerdict({ status: 'failed', health: 'failed' }), 'retry-error');
assert.equal(firstScanVerdict({ status: null }), 'retry-error');
// 6) partial（部分来源失败但有入库）→ complete（健康面板展示细节）
assert.equal(firstScanVerdict({ status: 'partial_failure' }, true), 'complete');

// 处理器接线与失败原因透出（静态契约）
assert.ok(src.includes('const v=firstScanVerdict(result)'),
          'handler 必须使用 firstScanVerdict');
assert.ok(src.includes("(result.errors&&result.errors[0])"),
          '失败 toast 必须透出首个来源错误');
// 重启状态保持：boot 守卫必须尊重 first_run_complete
assert.ok(src.includes('state.desktop.first_run_complete'),
          'boot 不得把已完成引导的空账本用户弹回欢迎页');

console.log('FRONTEND-FIRST-SCAN-OK: 8 assertions passed');
