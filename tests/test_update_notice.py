# -*- coding: utf-8 -*-
"""test_update_notice.py — V1.0.0 软件更新提醒（前端行为 + 接线契约）。

- 行为测试：Node 直驱生产 cmpSemver / shouldAutoCheck / updateNoticeState。
- 静态契约：app.js 处理器与元素、main.rs 命令及注册、desktop_state.rs 字段。
不读写任何账本或真实数据。
"""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


class UpdateNotice(unittest.TestCase):
    def test_frontend_update_notice_behaviour(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('环境没有 node，跳过前端行为测试')
        r = subprocess.run(
            [node, os.path.join(HERE, 'frontend_update_notice.test.mjs')],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', cwd=ROOT, timeout=60)
        self.assertEqual(r.returncode, 0,
                         '前端更新提醒行为测试失败： | %s | %s'
                         % (r.stdout[-800:], r.stderr[-800:]))
        self.assertIn('FRONTEND-UPDATE-NOTICE-OK', r.stdout)

    def test_frontend_wiring(self):
        js = read('web', 'v1', 'app.js')
        for token in (
            'updateNotice:begin', 'updateNotice:end',
            'async function maybeAutoCheckUpdate', 'function initUpdateNotice',
            'function sidebarUpdate', 'async function applyCheckResult',
            "act==='open-about-update'", "act==='toggle-auto-check-update'",
            "tauriInvoke('set_auto_check_update'",
            "tauriInvoke('record_update_check'",
            "tauriInvoke('clear_update_notice'",
            'class="update-dot"', 'class="update-entry"',
            'sidebarUpdate()',
        ):
            self.assertIn(token, js, 'app.js 缺少：%s' % token)

    def test_rust_desktop_state_fields(self):
        rs = read('src-tauri', 'src', 'desktop_state.rs')
        for token in ('auto_check_update', 'last_update_check_ms',
                      'update_available_version', 'update_available_notes',
                      'v1_update_fields_backcompat_and_roundtrip',
                      'v1_auto_check_defaults_true_and_respects_explicit_false'):
            self.assertIn(token, rs, 'desktop_state.rs 缺少：%s' % token)
        # 关键：缺字段必须默认 true，必须用自定义 default 函数而非普通 serde default
        self.assertIn('#[serde(default = "auto_check_true")]', rs,
                      'auto_check_update 必须用 auto_check_true 默认（否则旧配置得到 false）')
        self.assertIn('fn auto_check_true() -> bool', rs)
        self.assertIn('auto_check_update: true', rs, 'Default 必须为 true')

    def test_rust_commands_registered(self):
        rs = read('src-tauri', 'src', 'main.rs')
        for token in ('fn set_auto_check_update', 'fn record_update_check',
                      'fn clear_update_notice', 'fn now_epoch_ms'):
            self.assertIn(token, rs, 'main.rs 缺少：%s' % token)
        for token in ('set_auto_check_update,', 'record_update_check,',
                      'clear_update_notice,'):
            self.assertIn(token, rs, 'main.rs 未注册命令：%s' % token)
        for token in ('"auto_check_update": st.auto_check_update',
                      '"last_update_check_ms": st.last_update_check_ms',
                      '"update_available_version": st.update_available_version'):
            self.assertIn(token, rs, 'main.rs get_desktop_state 缺少：%s' % token)

    def test_visual_contracts(self):
        """V1.0.0 视觉精修契约：开关 48×26 / 滑块左右一致 / 更新弹窗 700px / 侧边栏不跳动。"""
        css = read('web', 'v1', 'app.css')
        for token in (
            '.switch{position:relative;width:48px;height:26px;min-width:48px;min-height:26px;border-radius:13px',
            '.settings-row>button.switch{min-width:48px;min-height:26px}',
            '.switch .knob{position:absolute;top:2px;left:2px;width:20px;height:20px',
            '.switch.on .knob{left:24px}',          # 开=滑块在右
            '.modal.update-modal{width:min(700px,calc(100vw - 90px))}',
            'min-height:58px',                      # 侧边栏底部高度预留，切换不跳动
        ):
            self.assertIn(token, css, 'app.css 缺少视觉契约：%s' % token)
        js = read('web', 'v1', 'app.js')
        for label in ('发现新版本', '更新失败'):
            self.assertIn('class="modal update-modal" role="dialog" aria-modal="true" aria-label="%s"'
                          % label, js, '更新弹窗缺少 update-modal 类：%s' % label)


if __name__ == '__main__':
    unittest.main()
