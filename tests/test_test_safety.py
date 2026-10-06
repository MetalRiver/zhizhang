# -*- coding: utf-8 -*-
"""测试安全保险自身的测试（V3-00 §14 强制项）。

验证：
  - production root（%LOCALAPPDATA%\\UsageLedger、仓库根）写入被拒绝；
  - 系统 temp root 可以写；
  - 沙盒清理不触碰 production。

注意：本文件只验证 guard 函数本身，绝不修改真实 production DB。
"""
import os
import shutil
import sqlite3
import unittest

import _safety
from _safety import (ProductionDataRootError, assert_safe_data_root,
                     is_production_data_root, sandbox_dir)


def _localappdata():
    return (os.environ.get('LOCALAPPDATA')
            or os.path.join(os.path.expanduser('~'), 'AppData', 'Local'))


class TestProductionRootRejected(unittest.TestCase):
    def test_localappdata_production_root_rejected(self):
        root = os.path.join(_localappdata(), 'UsageLedger')
        self.assertTrue(is_production_data_root(root))
        self.assertTrue(is_production_data_root(os.path.join(root, 'usage.db')))
        self.assertTrue(is_production_data_root(os.path.join(root, 'sub', 'x.db')))
        with self.assertRaises(ProductionDataRootError):
            assert_safe_data_root(root, context='meta-test')

    def test_repo_root_rejected(self):
        self.assertTrue(is_production_data_root(_safety.REPO_ROOT))
        self.assertTrue(is_production_data_root(os.path.join(_safety.REPO_ROOT, 'web')))
        with self.assertRaises(ProductionDataRootError):
            assert_safe_data_root(_safety.REPO_ROOT, context='meta-test')

    def test_sandbox_dir_never_returns_production(self):
        for _ in range(5):
            d = sandbox_dir(prefix='ul-guard-')
            try:
                self.assertFalse(is_production_data_root(d))
            finally:
                shutil.rmtree(d, ignore_errors=True)


class TestTempRootWritable(unittest.TestCase):
    def test_temp_root_sqlite_write_ok(self):
        d = sandbox_dir(prefix='ul-guard-w-')
        try:
            db = os.path.join(d, 'usage.db')
            c = sqlite3.connect(db)
            c.execute('CREATE TABLE t (x INTEGER)')
            c.execute('INSERT INTO t VALUES (1)')
            c.commit()
            self.assertEqual(c.execute('SELECT COUNT(*) FROM t').fetchone()[0], 1)
            c.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertFalse(os.path.exists(d), '沙盒清理后必须消失')


class TestCleanupSafety(unittest.TestCase):
    def test_sandbox_cleanup_leaves_production_untouched(self):
        prod = os.path.join(_safety.REPO_ROOT, 'usage.db')
        before = os.path.getsize(prod) if os.path.isfile(prod) else None
        d = sandbox_dir(prefix='ul-guard-c-')
        with open(os.path.join(d, 'x'), 'w') as f:
            f.write('x')
        shutil.rmtree(d, ignore_errors=True)
        self.assertFalse(os.path.exists(d))
        after = os.path.getsize(prod) if os.path.isfile(prod) else None
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
