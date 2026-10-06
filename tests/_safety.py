# -*- coding: utf-8 -*-
"""tests/_safety.py — 测试数据隔离保险（V3-00 起，fail-closed）。

背景：测试曾误写真实 production 账本。此后所有会写 DB / DATA_ROOT 的测试，
沙盒目录必须经 sandbox_dir() 领取；一旦目标路径解析到 production
（仓库根或 %LOCALAPPDATA%\\UsageLedger），立即 FAIL，绝不写入。

规则：
  1. 禁止测试直接调用 tempfile.mkdtemp() 充当数据沙盒；
  2. sandbox_dir() 正常情况下总是返回系统临时目录里的新建目录——
     guard 防的是路径拼接被改坏、环境变量劫持、或有人图省事直接传仓库根；
  3. 本模块在 frozen 环境（PyInstaller / Tauri sidecar）导入即失败：
     测试永远不该在安装形态里跑。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

if getattr(sys, 'frozen', False):
    raise AssertionError('_safety: 检测到 frozen 运行环境，测试禁止在此形态执行')


def _localappdata():
    return (os.environ.get('LOCALAPPDATA')
            or os.path.join(os.path.expanduser('~'), 'AppData', 'Local'))


def _production_roots():
    return (
        os.path.normpath(os.path.join(_localappdata(), 'UsageLedger')),
        os.path.normpath(REPO_ROOT),
    )


class ProductionDataRootError(AssertionError):
    """测试沙盒解析到了 production DATA_ROOT —— fail-closed，立即 FAIL。"""


def is_production_data_root(path):
    p = os.path.normpath(os.path.abspath(path)) if path else os.path.abspath(os.curdir)
    return any(p == root or p.startswith(root + os.sep) for root in _production_roots())


def assert_safe_data_root(path, context='sandbox'):
    if is_production_data_root(path):
        raise ProductionDataRootError(
            '%s 指向 production DATA_ROOT（%s）——已拒绝执行。'
            '测试只允许写系统临时目录（tests/_safety.sandbox_dir）。'
            % (context, path))


def sandbox_dir(prefix='ul-test-'):
    """领取一个经过 production 检查的测试沙盒目录（替代 tempfile.mkdtemp）。"""
    tmp = tempfile.mkdtemp(prefix=prefix)
    assert_safe_data_root(tmp, context='sandbox_dir(%r)' % prefix)
    return tmp


# 语义化别名
claim_sandbox = sandbox_dir
