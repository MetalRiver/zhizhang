"""Release gates: independent facts, failure atomicity, new rescan events.

All writable ledgers are claimed through _safety; production is never opened.
"""
import contextlib
import datetime
import io
import json
import os
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

import _safety
import ledger
import serve


class ReleaseAudit(unittest.TestCase):
    def setUp(self):
        import shutil
        self.tmp = _safety.sandbox_dir('ul-release-audit-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, 'usage.db')
        self.enterContext(patch.object(ledger, 'DB_PATH', self.db_path))
        self.enterContext(patch.object(ledger, 'BOARD_PATH', os.path.join(self.tmp, 'usage-board.json')))
        self.conn = ledger.connect(create=True)
        self.addCleanup(self.conn.close)
        self.now = int(datetime.datetime.now().timestamp() * 1000)
        self.pricing = {'priced': {'input': 1, 'output': 10, 'cache_read': 2,
                                   'cache_write': 3, 'currency': 'USD'}}
        # V1.1：_query 的定价注入点已切到分层溯源（load_pricing_layers）；
        # 合成价格全部进 user_channel 层，语义与旧 manual_verified 等价。
        self.enterContext(patch.object(
            ledger, 'load_pricing_layers',
            return_value={'user': self.pricing, 'official': {},
                          'reference': {}, 'ref_meta': {}}))

    def event(self, eid='e', project='A', sid='s', client='zcode', i=100, o=0,
              cr=0, cw=0, model='priced', ts=None):
        ev = dict(client=client, session_id=sid, event_id=eid, project_key=project,
                  project_kind='project', project_display=project)
        self.conn.execute(ledger.UPSERT, (client, sid, eid, ts or self.now, model, '',
            i, o, 0, cr, cw, 'src', 'first', 'last', 0, None, None, project))
        ledger._upsert_project_and_session(self.conn, ev, 'last')
        self.conn.commit()
        return ev

    def query(self, query='range=all'):
        handler = SimpleNamespace(_ledger_db_ro=lambda: sqlite3.connect(self.db_path),
            _json=lambda status, data: data,
            _err=lambda status, code, message: {'status': status, 'error': code})
        return serve._query(handler, parse_qs(query))

    def test_event_price_uses_components_and_is_filter_invariant(self):
        self.event('input', i=1000, o=0, cr=500, cw=200)
        self.event('output', sid='other', i=0, o=1000)
        all_rows = {e['event_id']: e for e in self.query()['items']}
        isolated = self.query('range=all&session=zcode%7Cs')['items'][0]
        self.assertAlmostEqual(all_rows['input']['cost'], .0026, places=6)
        self.assertEqual(all_rows['input']['cost'], isolated['cost'])
        q = self.query()
        self.assertAlmostEqual(q['aggregate']['costs']['USD'], .0126, places=6)
        self.assertAlmostEqual(sum(s['estimated_cost_by_currency']['USD'] for s in q['sessions']), .0126, places=6)

    def test_missing_component_is_partial_not_free_and_coverage_is_exact(self):
        self.pricing['priced']['output'] = None
        self.event('mixed',i=100,o=100)
        q = self.query()
        self.assertEqual(q['items'][0]['cost_status'],'partial')
        self.assertEqual(q['aggregate']['priced_tokens'],100)
        self.assertEqual(q['aggregate']['unpriced_events'],1)
        self.event('known-input', i=100, o=0)
        q = self.query()
        self.assertEqual(q['aggregate']['unpriced_events'], 1)
        self.assertEqual(q['by_project'][0]['unpriced_events'], 1)
        self.assertEqual(q['sessions'][0]['unpriced_events'], 1)
        self.event('output',sid='out',i=0,o=100)
        q = self.query('range=all&session=zcode%7Cout')
        self.assertIsNone(q['items'][0]['cost'])
        self.assertEqual(q['aggregate']['costs'],{})
        board = ledger.build_board()
        # Mixed/input records retain their known cost; an isolated unknown
        # output component must never become a zero-cost model in the board.
        self.assertTrue(board['models'][0]['cost_partial'])

    def test_cache_only_is_token_record_with_cost_and_missing_rate_notice(self):
        self.event('cached', i=0, o=0, cr=1000)
        q = self.query()
        self.assertEqual(q['aggregate']['total'], 0)
        self.assertEqual(q['aggregate']['token_records'], 1)
        for group in ('by_project', 'sessions', 'by_client'):
            self.assertEqual(q[group][0]['token_records'], 1)
        self.assertEqual(q['items'][0]['record_kind'], 'token')
        self.assertAlmostEqual(q['items'][0]['cost'], .002)
        self.pricing['priced']['cache_read'] = None
        q = self.query()
        self.assertEqual(q['aggregate']['unpriced_events'], 1)
        self.assertIsNone(q['items'][0]['cost'])
        self.assertEqual(q['aggregate']['costs'], {})
        board = ledger.build_board()
        self.assertIsNone(board['models'][0]['cost'])
        self.assertFalse(board['models'][0]['priced'])

    def test_failed_board_rebuild_rolls_back_assignment(self):
        self.event()
        target=ledger.project_alias_add('target')['project_key']
        with patch.object(ledger,'build_board',side_effect=OSError('injected failure')):
            with self.assertRaises(OSError):
                ledger.project_assign_sessions(target,[dict(source='zcode',session_id='s')])
        self.assertEqual(self.conn.execute('SELECT project_key FROM usage_event').fetchone()[0],'A')

    def test_batch_assignment_invalid_tail_rolls_back_valid_prefix(self):
        self.event()
        target=ledger.project_alias_add('target')['project_key']
        with self.assertRaises(ValueError):
            ledger.project_assign_sessions(target,[dict(source='zcode',session_id='s'),
                dict(source='zcode',session_id='missing')])
        self.assertEqual(self.conn.execute('SELECT project_key FROM usage_event').fetchone()[0],'A')

    def test_http_origin_payload_and_same_process_write_lock(self):
        import threading
        import urllib.request
        import urllib.error
        self.event()
        self.enterContext(patch.object(serve,'HERE',self.tmp))
        self.enterContext(patch.object(ledger,'SCAN_LOCK_PATH',os.path.join(self.tmp,'usage-ledger-scan.lock')))
        server=serve.make_server(0)
        self.addCleanup(server.server_close)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.shutdown)
        base='http://127.0.0.1:%s'%server.server_address[1]
        def request(route,payload,origin=None):
            headers={'Content-Type':'application/json'}
            if origin is not None:headers['Origin']=origin
            req=urllib.request.Request(base+route,json.dumps(payload).encode(),headers,method='POST')
            try:
                with urllib.request.urlopen(req,timeout=10) as r:return r.status
            except urllib.error.HTTPError as e:return e.code
        for origin in ('https://evil.example','null','http://localhost','http://localhost:bad'):
            self.assertEqual(request('/api/v1/projects/alias',{'alias':'x','project_key':'A'},origin),403)
        self.assertEqual(request('/api/v1/projects/assign',{'project_key':'A','sessions':[{'source':3,'session_id':'s'}]}),400)
        self.assertEqual(request('/api/v1/projects/assign',{'project_key':'A','sessions':[{'source':'zcode','session_id':'missing'}]}),400)
        self.assertEqual(request('/api/v1/projects/alias',{'alias':'x','project_key':'missing'}),400)
        self.assertEqual(request('/api/v1/projects/merge',{'from':'A','to':'A'}),400)
        self.assertEqual(request('/api/v1/projects/alias',{'alias':'x','unexpected':True}),400)
        self.assertEqual(request('/api/v1/projects/alias',[]),400)
        entered=threading.Event();release=threading.Event();result=[]
        commit=ledger._commit_project_write
        def delayed(conn):
            entered.set()
            if not release.wait(10):raise RuntimeError('test gate timed out')
            return commit(conn)
        with patch.object(ledger,'_commit_project_write',side_effect=delayed):
            writer=threading.Thread(target=lambda:result.append(request('/api/v1/projects/alias',{'alias':'x','project_key':'A'},base)))
            writer.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(request('/api/v1/projects/alias',{'alias':'y','project_key':'A'}),409)
                self.assertEqual(request('/api/v1/scan',{}),409)
            finally:
                release.set();writer.join(10)
        self.assertEqual(result,[200])

    def test_session_and_client_filters_intersect(self):
        self.event()
        self.assertEqual(self.query('range=all&session=zcode%7Cs&client=dsh')['total'], 0)
        self.assertEqual(self.query('range=all&session=invalid')['status'], 400)

    def test_injection_and_extreme_pagination_do_not_change_ledger(self):
        self.event()
        self.assertEqual(self.query('range=all&project=x%27%20OR%201%3D1--')['total'],0)
        self.assertEqual(self.query('range=all&page=999999999999999999999999999')['items'],[])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_event').fetchone()[0],1)

    def test_activity_is_visible_but_has_no_token_or_cost(self):
        self.conn.execute("INSERT INTO activity_event VALUES ('catpaw','act','a',?,'message','src','t','t')", (self.now,))
        ledger._upsert_project_and_session(self.conn, dict(client='catpaw',session_key='act',
            event_id='a',project_key='A',project_display='A'), 't')
        self.conn.commit()
        q = self.query()
        self.assertEqual(q['total'], 1)
        self.assertEqual(q['aggregate']['sessions'], 1)
        self.assertEqual(q['aggregate']['total'], 0)
        self.assertIsNone(q['items'][0]['total'])
        self.assertIsNone(q['items'][0]['cost'])

    def test_new_events_inherit_manual_session_and_keep_auto_key(self):
        self.event()
        target = ledger.project_alias_add('manual')['project_key']
        ledger.project_assign_sessions(target, [dict(source='zcode',session_id='s')])
        self.event('new', project='AUTO2')
        row = self.conn.execute("SELECT project_key,project_key_auto,project_key_manual FROM usage_event WHERE event_id='new'").fetchone()
        self.assertEqual(tuple(row), (target, 'AUTO2', 1))

    def test_merge_rescan_new_session_and_chain(self):
        self.event()
        self.event('b', project='B', sid='b')
        self.event('c', project='C', sid='c')
        ledger.project_merge('A','B')
        ledger.project_merge('B','C')
        self.event('new', project='A', sid='new')
        row = self.conn.execute("SELECT project_key,project_key_auto FROM usage_event WHERE event_id='new'").fetchone()
        self.assertEqual(tuple(row), ('C','A'))
        self.assertEqual(self.conn.execute("SELECT display_name FROM project_registry WHERE project_key='C'").fetchone()[0], 'C')

    def test_merge_cycle_rejected_without_changes(self):
        self.event()
        self.event('b', project='B', sid='b')
        ledger.project_merge('A','B')
        before = self.conn.execute('SELECT project_key,metadata_json FROM project_registry ORDER BY project_key').fetchall()
        with self.assertRaises(ValueError):
            ledger.project_merge('B','A')
        after = self.conn.execute('SELECT project_key,metadata_json FROM project_registry ORDER BY project_key').fetchall()
        self.assertEqual([tuple(x) for x in before], [tuple(x) for x in after])

    def test_nonexistent_session_does_not_create_registry(self):
        self.event()
        before = self.conn.execute('SELECT COUNT(*) FROM session_registry').fetchone()[0]
        with self.assertRaises(ValueError):
            ledger.project_assign_sessions('A', [dict(source='zcode',session_id='missing')])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_registry').fetchone()[0], before)

    def test_unknown_project_rejected(self):
        self.event()
        with self.assertRaises(ValueError):
            ledger.project_alias_add('new',project_key='missing')
        with self.assertRaises(ValueError):
            ledger.project_assign_sessions('missing',[dict(source='zcode',session_id='s')])

    def test_invalid_session_types_rejected(self):
        self.event()
        with self.assertRaises(ValueError):
            ledger.project_assign_sessions('A',[dict(source=42,session_id=['s'])])

    def test_attribution_preserves_raw_facts_and_original_key(self):
        self.event(i=700,o=9,cr=33,cw=4)
        columns = 'client,session_id,event_id,ts_ms,model,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,source_file'
        before = tuple(self.conn.execute('SELECT '+columns+' FROM usage_event').fetchone())
        target = ledger.project_alias_add('manual')['project_key']
        ledger.project_assign_sessions(target,[dict(source='zcode',session_id='s')])
        self.assertEqual(tuple(self.conn.execute('SELECT '+columns+' FROM usage_event').fetchone()),before)
        self.assertEqual(self.conn.execute('SELECT project_key_auto FROM usage_event').fetchone()[0],'A')

    def test_more_than_400_sessions_are_not_silently_truncated(self):
        for n in range(401):
            self.event(str(n),sid=str(n))
        self.assertEqual(len(self.query()['sessions']),401)

    def test_board_counts_session_identity_with_source(self):
        self.event()
        self.event('other',client='dsh')
        self.assertEqual(ledger.build_board()['by_project'][0]['sessions'],2)

    def test_backfill_resolves_merged_key_and_preserves_manual(self):
        self.event(client='dsh')
        self.event('b',project='B',sid='b')
        ledger.project_merge('A','B')
        # A previously unseen session auto-attributed to merged A.
        self.conn.execute("INSERT INTO usage_event (client,session_id,event_id,ts_ms,first_seen,last_seen) VALUES ('dsh','new','new',?,'t','t')",(self.now,))
        self.conn.commit()
        self.enterContext(patch.object(ledger,'_dsh_session_head',return_value={'cwd':'A'}))
        self.enterContext(patch.object(ledger,'project_key_from_path',return_value=('A','a')))
        self.enterContext(patch.dict(ledger.SOURCES,{'zcode':dict(path=os.path.join(self.tmp,'missing'))}))
        with contextlib.redirect_stdout(io.StringIO()):
            ledger.cmd_attribution_backfill(SimpleNamespace())
        keys = {r[0] for r in self.conn.execute("SELECT project_key FROM usage_event WHERE client='dsh'")}
        self.assertEqual(keys,{'B'})


class MigrationAudit(unittest.TestCase):
    def test_versions_and_reconnect_preserve_business_rows(self):
        import test_v1_runtime as fixtures
        import shutil
        for version in (1,2,3):
            with self.subTest(version=version):
                tmp = _safety.sandbox_dir('ul-migration-audit-')
                try:
                    path = os.path.join(tmp,'usage.db')
                    fixtures._seed_db(path,[('zcode','s','e',123,'m',100,9,'t','t','A')])
                    db = sqlite3.connect(path)
                    if version == 1:
                        db.execute('ALTER TABLE usage_event DROP COLUMN project_key')
                        db.execute('DROP TABLE session_registry')
                        db.execute('DROP TABLE project_registry')
                    db.execute('PRAGMA user_version='+str(version)); db.commit(); db.close()
                    with patch.object(ledger,'DB_PATH',path):
                        for _ in range(2):
                            db = ledger.connect(); self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
                            self.assertEqual(tuple(db.execute('SELECT event_id,ts_ms,input_tokens,output_tokens FROM usage_event').fetchone()),('e',123,100,9)); db.close()
                finally:
                    shutil.rmtree(tmp)
        tmp = _safety.sandbox_dir('ul-fresh-audit-')
        try:
            with patch.object(ledger,'DB_PATH',os.path.join(tmp,'usage.db')):
                db = ledger.connect(create=True)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
                db.close()
        finally:
            shutil.rmtree(tmp)

    def test_injected_alter_failure_rolls_back_every_schema_change(self):
        db = sqlite3.connect(':memory:')
        try:
            db.executescript(ledger.SCHEMA)
            db.execute('PRAGMA user_version=2')
            before = list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
            def authorizer(action,arg1,arg2,*rest):
                if action == sqlite3.SQLITE_ALTER_TABLE and arg2 == 'session_registry':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorizer)
            with self.assertRaises(sqlite3.DatabaseError):
                ledger.migrate_schema(db)
            db.set_authorizer(None)
            self.assertEqual(list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")),before)
            ledger.migrate_schema(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
        finally:
            db.close()


class RealBrowserAudit(unittest.TestCase):
    def test_cache_details_session_move_and_serial_merge(self):
        """Real DOM and HTTP writes; isolated fixtures, no route interception."""
        import pathlib
        import shutil
        import subprocess
        import time
        import test_v1_runtime as runtime
        import test_attribution as fixtures
        root = pathlib.Path(__file__).resolve().parents[1]
        deps = pathlib.Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        node = shutil.which('node') or str(deps/'bin/node.exe')
        playwright = os.environ.get('AUDIT_PLAYWRIGHT_PATH', str(deps/'node_modules/playwright'))
        edge = os.environ.get('AUDIT_EDGE_PATH', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        if not pathlib.Path(node).is_file() or not pathlib.Path(playwright).exists() or not pathlib.Path(edge).is_file():
            self.skipTest('Real browser audit requires Node, Playwright and Edge')
        servers = []
        config = {}
        try:
            for name in ('cache','writes'):
                tmp = pathlib.Path(runtime._sandbox_serve('ul-browser-release-'))
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                sources = {c:str(tmp/'missing'/c) for c in ('zcode','dsh','workbuddy','catpaw','traecn','codex')}
                if name == 'cache':
                    sources['zcode'] = str(tmp/'cache-source.db')
                    fixtures._make_zcode(sources['zcode'], [('cached-session','cache-project',r'D:\ReleaseFixture\Cache','Cache session', [('audit-model',0,0,1000,0,'agent')])])
                    src = sqlite3.connect(sources['zcode'])
                    src.execute('UPDATE model_usage SET started_at=?',(int(time.time()*1000),))
                    src.commit(); src.close()
                else:
                    now = int(time.time()*1000)
                    runtime._seed_db(str(tmp/'usage.db'), [('zcode','s'+k,'e'+k,now,'audit-model',100,10,'t','t',k) for k in ('A','B','C','D')])
                    db = sqlite3.connect(tmp/'usage.db')
                    db.executemany('INSERT INTO project_registry(project_key,project_kind,display_name) VALUES (?,\'project\',?)', [(k,k) for k in ('A','B','C','D')])
                    db.executemany('INSERT INTO session_registry(source,session_id,project_key,display_name) VALUES (\'zcode\',?,?,?)', [('s'+k,k,'Session '+k) for k in ('A','B','C','D')])
                    db.commit(); db.close()
                (tmp/'sources.json').write_text(json.dumps(sources),encoding='utf-8')
                (tmp/'pricing.json').write_text(json.dumps({'audit-model':dict(input=1,output=10,cache_read=2,cache_write=3,currency='USD')}),encoding='utf-8')
                proc, base = runtime._start_serve(str(tmp))
                servers.append(proc)
                config[name] = {'base':base}
            output = pathlib.Path(_safety.sandbox_dir('ul-browser-evidence-'))
            self.addCleanup(shutil.rmtree, output, ignore_errors=True)
            config['output'] = str(output)
            manifest = output/'config.json'
            manifest.write_text(json.dumps(config),encoding='utf-8')
            env = dict(os.environ,AUDIT_PLAYWRIGHT_PATH=playwright,AUDIT_EDGE_PATH=edge)
            result = subprocess.run([node,str(root/'tests/release_ui_smoke.cjs'),str(manifest)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads((output/'result.json').read_text())['status'],'PASS')
        finally:
            for proc in servers:
                proc.terminate(); proc.wait(timeout=10)
