#!/usr/bin/env python3
"""test_replay_analyzer.py — dsh 重放判定器合成测试（R3）。

全部使用合成 fixture（纯 jsonl，无需 zstandard 即可运行）。
涉及 zstd 多帧 / 截断尾部的用例在本机缺 zstandard 时自动跳过。
禁止使用任何真实 dsh 会话数据。

覆盖 Round 3 要求的用例：
    1  parent replay            父子重放判定
    2  no-parent normal         无父会话全为原始
    3  two sibling children     两个兄弟 fork 各自标记、父保留
    4  multi-level lineage      祖→父→子 递归血统（含父缺失时 ancestor 验证）
    5  broken parent            断链保留，绝不判定为重放
    6  ambiguous seed           同树同 seq 异指纹 → 保留
    7  independent same fingerprint  同毫秒/同模型/同 token 的独立调用不误删
    8  rerun idempotence        重跑结果逐字节一致
    9  Windows path             反斜杠/混合分隔符路径
    10 truncated zstd tail      截断 zstd 尾部容忍（需 zstandard）
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import unittest
from importlib.util import find_spec

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ANALYZER_PATH = os.path.join(ROOT, 'scripts', 'analyze_dsh_replay.py')

HAS_ZSTANDARD = find_spec('zstandard') is not None


def load_analyzer():
    spec = importlib.util.spec_from_file_location('analyze_dsh_replay', ANALYZER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def msg(seq, time, i, o, cr, model='model-x'):
    """合成 assistant/message usage 事件（结构与真实 dsh 一致，无任何正文）。"""
    return {'type': 'assistant/message', 'seq': seq, 'time': time,
            'data': {'usage': {'inputTokens': i, 'outputTokens': o,
                               'cacheReadTokens': cr},
                     'message': {'source': {'model': model}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-replay-test-')
        self.mod = load_analyzer()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_session(self, rel_dir, sid, events, parent=None, seed=None, depth=0,
                      created=1000):
        d = os.path.join(self.tmp, rel_dir, sid)
        os.makedirs(d, exist_ok=True)
        header = {'type': 'session', 'id': sid, 'createdAt': created,
                  'delegationDepth': depth, 'version': 0}
        if parent is not None:
            header['parentSession'] = parent
        if seed is not None:
            header['seedLength'] = seed
        lines = [json.dumps(header)] + [json.dumps(e) for e in events]
        with open(os.path.join(d, 'session.jsonl'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return d

    def scan(self):
        return self.mod.collect_sessions(self.tmp)

    def reasons_of(self, verdicts, sid):
        return {seq: r for seq, r in verdicts[sid]}


class TestParentReplay(Base):
    def test_parent_replay_marked_and_own_kept(self):
        # 1) 父子重放：child seed 区与父逐条一致 → parent_seed_exact；own 区保留
        parent_events = [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60)]
        child_events = [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60),
                        msg(9, 200, 5, 1, 0)]
        self.write_session('proj', 'session-p', parent_events)
        self.write_session('proj', 'session-c', child_events,
                           parent='session-p', seed=5)
        sessions, errs = self.scan()
        self.assertEqual(errs, 0)
        verdicts, _ = self.mod.classify(sessions)
        r = self.reasons_of(verdicts, 'session-c')
        self.assertEqual(r[1], 'parent_seed_exact')
        self.assertEqual(r[2], 'parent_seed_exact')
        self.assertEqual(r[9], 'normal_own')
        self.reasons_of(verdicts, 'session-p')  # 父侧全部 no_parent
        self.assertEqual(set(self.reasons_of(verdicts, 'session-p').values()),
                         {'no_parent'})


class TestNoParent(Base):
    def test_no_parent_all_normal(self):
        # 2) 无父会话：即使与别人指纹相同也全为原始
        self.write_session('projA', 'session-x',
                           [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60)])
        sessions, _ = self.scan()
        verdicts, _ = self.mod.classify(sessions)
        self.assertEqual(set(self.reasons_of(verdicts, 'session-x').values()),
                         {'no_parent'})


class TestSiblings(Base):
    def test_two_sibling_children_both_marked_parent_kept(self):
        # 3) 兄弟 fork：两个子会话各自标记，父会话保留
        pe = [msg(1, 100, 10, 2, 30)]
        self.write_session('proj', 'session-p', pe)
        self.write_session('proj', 'session-c1', pe + [msg(9, 300, 1, 1, 1)],
                           parent='session-p', seed=5)
        self.write_session('proj', 'session-c2', pe + [msg(9, 301, 1, 1, 1)],
                           parent='session-p', seed=5)
        sessions, _ = self.scan()
        verdicts, lineage = self.mod.classify(sessions)
        self.assertEqual(self.reasons_of(verdicts, 'session-c1')[1], 'parent_seed_exact')
        self.assertEqual(self.reasons_of(verdicts, 'session-c2')[1], 'parent_seed_exact')
        self.assertEqual(set(self.reasons_of(verdicts, 'session-p').values()),
                         {'no_parent'})
        self.assertEqual(lineage['roots']['session-c1'],
                         lineage['roots']['session-c2'])


class TestMultiLevel(Base):
    def test_multi_level_direct_parent(self):
        # 4a) 祖→父→子：leaf 的种子区经直接父逐层验证
        gp = [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60)]
        mid = gp + [msg(9, 200, 5, 1, 0)]
        leaf = gp + [msg(9, 200, 5, 1, 0), msg(12, 300, 7, 7, 7)]
        self.write_session('proj', 'session-gp', gp)
        self.write_session('proj', 'session-mid', mid,
                           parent='session-gp', seed=5)
        self.write_session('proj', 'session-leaf', leaf,
                           parent='session-mid', seed=10)
        sessions, _ = self.scan()
        verdicts, _ = self.mod.classify(sessions)
        r = self.reasons_of(verdicts, 'session-leaf')
        self.assertEqual(r[1], 'parent_seed_exact')
        self.assertEqual(r[2], 'parent_seed_exact')
        self.assertEqual(r[9], 'parent_seed_exact')
        self.assertEqual(r[12], 'normal_own')

    def test_multi_level_ancestor_when_mid_purged(self):
        # 4b) 直接父只剩文件头（内容被清退）→ 沿链到祖父验证 ancestor_seed_exact
        gp = [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60)]
        self.write_session('proj', 'session-gp2', gp)
        # mid-missing：文件在但只剩头（一半被清退），无 usage 事件
        self.write_session('proj', 'session-mid-missing', [],
                           parent='session-gp2', seed=10)
        self.write_session('proj', 'session-leaf3',
                           [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60),
                            msg(3, 400, 8, 8, 8)],
                           parent='session-mid-missing', seed=15)
        sessions, _ = self.scan()
        verdicts, _ = self.mod.classify(sessions)
        r = self.reasons_of(verdicts, 'session-leaf3')
        self.assertEqual(r[1], 'ancestor_seed_exact')
        self.assertEqual(r[2], 'ancestor_seed_exact')

    def test_parent_link_to_missing_file_is_broken(self):
        # 4c) parentSession 指向的文件完全不存在 → broken_parent 保留
        self.write_session('proj', 'session-gp3',
                           [msg(1, 100, 10, 2, 30)])
        self.write_session('proj', 'session-leaf4',
                           [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60)],
                           parent='session-totally-gone', seed=15)
        sessions, _ = self.scan()
        verdicts, _ = self.mod.classify(sessions)
        self.assertEqual(set(self.reasons_of(verdicts, 'session-leaf4').values()),
                         {'broken_parent'})


class TestBrokenParent(Base):
    def test_broken_parent_kept_not_deduped(self):
        # 5) 断链：父不存在 → 保留（宁可漏杀不可误删）
        self.write_session('proj', 'session-orphan',
                           [msg(1, 100, 10, 2, 30)],
                           parent='session-vanished', seed=5)
        sessions, _ = self.scan()
        verdicts, lineage = self.mod.classify(sessions)
        self.assertEqual(set(self.reasons_of(verdicts, 'session-orphan').values()),
                         {'broken_parent'})


class TestAmbiguous(Base):
    def test_same_seq_different_fingerprint_kept(self):
        # 6) 模糊：同树同 seq 但指纹不一致 → 保留，不判定为重放
        self.write_session('proj', 'session-p', [msg(3, 100, 10, 2, 30)])
        self.write_session('proj', 'session-c',
                           [msg(3, 999, 77, 7, 7)],
                           parent='session-p', seed=10)
        sessions, _ = self.scan()
        verdicts, _ = self.mod.classify(sessions)
        self.assertEqual(set(self.reasons_of(verdicts, 'session-c').values()),
                         {'ambiguous'})


class TestIndependentSameFingerprint(Base):
    def test_independent_calls_never_deduped(self):
        # 7) 误伤检测：同毫秒/同模型/同 token 的独立调用绝不能被去重
        #    a) 跨树同 seq 同指纹 → 各自 no_parent
        #    b) 同树内 own 事件与父事件指纹完全相同但 seq 不同 → normal_own
        same = dict(time=1700000000000, i=1234, o=56, cr=789)
        self.write_session('projA', 'session-a1',
                           [msg(5, same['time'], same['i'], same['o'], same['cr'])])
        self.write_session('projB', 'session-b1',
                           [msg(5, same['time'], same['i'], same['o'], same['cr'])])
        self.write_session('projA', 'session-a2',
                           [msg(1, same['time'], same['i'], same['o'], same['cr'])])
        self.write_session('projA', 'session-a3',
                           [msg(1, same['time'], same['i'], same['o'], same['cr']),
                            msg(7, same['time'], same['i'], same['o'], same['cr'])],
                           parent='session-a2', seed=3)
        sessions, _ = self.scan()
        verdicts, _ = self.mod.classify(sessions)
        self.assertEqual(set(self.reasons_of(verdicts, 'session-a1').values()),
                         {'no_parent'})
        self.assertEqual(set(self.reasons_of(verdicts, 'session-b1').values()),
                         {'no_parent'})
        # a3 的 seq=7 在种子边界之外：即使与父 seq=1 指纹完全相同也是独立事件
        self.assertEqual(self.reasons_of(verdicts, 'session-a3')[7], 'normal_own')
        self.assertEqual(self.reasons_of(verdicts, 'session-a3')[1], 'parent_seed_exact')


class TestIdempotence(Base):
    def test_rerun_identical(self):
        # 8) 幂等：同输入两次分类（含 JSON 序列化）结果逐字节一致
        self.write_session('proj', 'session-p', [msg(1, 100, 10, 2, 30)])
        self.write_session('proj', 'session-c', [msg(1, 100, 10, 2, 30)],
                           parent='session-p', seed=5)
        s1, e1 = self.scan()
        v1, l1 = self.mod.classify(s1)
        s2, e2 = self.scan()
        v2, l2 = self.mod.classify(s2)
        self.assertEqual(e1, e2)
        self.assertEqual(json.dumps(v1, sort_keys=True),
                         json.dumps(v2, sort_keys=True))
        self.assertEqual(json.dumps(l1, sort_keys=True),
                         json.dumps(l2, sort_keys=True))


class TestWindowsPath(Base):
    def test_backslash_and_mixed_separators(self):
        # 9) Windows 路径：反斜杠与正斜杠混用均可扫描
        self.write_session('proj dir', 'session-w',
                           [msg(1, 100, 10, 2, 30)])
        mixed = self.tmp + '\\proj dir'       # 反斜杠结尾路径
        sessions, errs = self.mod.collect_sessions(mixed)
        self.assertEqual(errs, 0)
        self.assertIn('session-w', sessions)


@unittest.skipUnless(HAS_ZSTANDARD, '需要 zstandard（.venv）')
class TestZstd(Base):
    def test_truncated_tail_and_multiframe(self):
        # 10) 截断 zstd 尾部容忍 + 追加写多帧
        import zstandard
        events = [msg(i, 1000 + i, 10 + i, 2, 30) for i in range(1, 21)]
        lines = [json.dumps({'type': 'session', 'id': 'session-z',
                             'createdAt': 1, 'delegationDepth': 0,
                             'version': 0})]
        lines += [json.dumps(e) for e in events]
        payload = ('\n'.join(lines) + '\n').encode('utf-8')
        cctx = zstandard.ZstdCompressor()
        frame1 = cctx.compress(payload[:len(payload) // 2])
        frame2 = cctx.compress(payload[len(payload) // 2:])
        d = os.path.join(self.tmp, 'proj', 'session-z')
        os.makedirs(d)
        # 多帧追加写
        with open(os.path.join(d, 'session.jsonl.zstd'), 'wb') as f:
            f.write(frame1 + frame2)
        sessions, errs = self.mod.collect_sessions(self.tmp)
        self.assertEqual(errs, 0)
        self.assertEqual(len(sessions['session-z']['usage']), 20)
        # 截断尾部：保留 frame1 完整 + frame2 前半 —— 已解出的行必须保留
        with open(os.path.join(d, 'session.jsonl.zstd'), 'wb') as f:
            f.write(frame1 + frame2[:len(frame2) // 2])
        sessions2, errs2 = self.mod.collect_sessions(self.tmp)
        self.assertEqual(errs2, 0)
        self.assertGreater(len(sessions2['session-z']['usage']), 0)
        self.assertLess(len(sessions2['session-z']['usage']), 20)


if __name__ == '__main__':
    unittest.main()
