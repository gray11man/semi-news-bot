import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ci_state import snapshot,restore,recover,manifest_at,NAMES

class CheckpointTests(unittest.TestCase):
    def make_data(self,root,value):
        root.mkdir(parents=True,exist_ok=True)
        connections=[]
        for name in NAMES:
            db=sqlite3.connect(root/name)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS example(value TEXT)')
            db.execute('DELETE FROM example');db.execute('INSERT INTO example VALUES(?)',(value,));db.commit()
            connections.append(db)
        return connections
    def test_snapshot_restores_wal_commits_and_both_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);connections=self.make_data(p/'source','kept')
            snapshot(p/'source',p/'state')
            self.assertIsNotNone(manifest_at(p/'state'))
            restore(p/'state',p/'restored')
            for name in NAMES:
                with sqlite3.connect(p/'restored'/name) as db:
                    self.assertEqual(db.execute('SELECT value FROM example').fetchone()[0],'kept')
                db.close()
            for db in connections:db.close()
    def test_incomplete_state_cannot_overwrite_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);p.mkdir(exist_ok=True)
            sqlite3.connect(p/'radar.sqlite3').close()
            with self.assertRaises(RuntimeError):snapshot(p,p/'state')
    def test_tampered_checkpoint_blocks_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);connections=self.make_data(p/'source','old')
            snapshot(p/'source',p/'state')
            (p/'state'/'usage.sqlite3.gz').write_bytes(b'bad')
            with self.assertRaises(RuntimeError):restore(p/'state',p/'target')
            for db in connections:db.close()
    def test_newer_failed_push_artifact_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);connections=self.make_data(p/'source','kept')
            with patch.dict(os.environ,{'GITHUB_RUN_ID':'200','GITHUB_RUN_ATTEMPT':'1'}):snapshot(p/'source',p/'new')
            blob=io.BytesIO()
            with zipfile.ZipFile(blob,'w') as z:
                for f in (p/'new').iterdir():z.write(f,f.name)
            artifacts={'artifacts':[{'id':8,'expired':False,'workflow_run':{'head_branch':'main','id':200}}]}
            details={'path':'.github/workflows/bottleneck.yml','event':'schedule'}
            with patch.dict(os.environ,{'TARGET_BRANCH':'main'}),patch('ci_state.api',side_effect=[artifacts,details,blob.getvalue()]):recover(p/'state')
            self.assertEqual(manifest_at(p/'state')['run_id'],'200')
            for db in connections:db.close()
    def test_expired_newer_artifact_blocks_silent_reset(self):
        artifacts={'artifacts':[{'id':8,'expired':True,'workflow_run':{'head_branch':'main','id':200}}]}
        details={'path':'.github/workflows/bottleneck.yml','event':'schedule'}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'TARGET_BRANCH':'main'}),patch('ci_state.api',side_effect=[artifacts,details]):
            with self.assertRaises(RuntimeError):recover(Path(tmp)/'state')
