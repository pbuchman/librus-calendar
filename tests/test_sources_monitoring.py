"""Only finite source aggregates can leave SQLite; legacy evidence remains unknown."""
from test_support import TEST_CONFIG
from test_monitoring import NOW, state, snapshot, stamp, load_collector
from app.health_state import read_source_health
from app.monitoring import sanitize_state
from app import monitoring
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


def sources():
    return {'mode':'preview','preview_count':3,'kinds':{kind:{'mode':'active' if kind=='message' else 'preview',
        'last_successful_fetch':stamp(10),'pending':2,'failed':1,'links':4,'last_result':'partial',
        'last_error':'source_collection_incomplete'} for kind in ('message','announcement','school_event','substitution')}}


class SourceMonitoringTests(unittest.TestCase):
    def test_legacy_state_and_collector_keep_source_gap(self):
        self.assertIsNone(sanitize_state(state(),NOW)['sources'])
        data=load_collector().HealthMetrics().calculate(snapshot(),NOW.timestamp())
        self.assertFalse(any(key.startswith('source_') for key in data))
        db=sqlite3.connect(':memory:')
        try: self.assertIsNone(read_source_health(db,set()))
        finally: db.close()
    def test_source_aggregate_whitelist_drops_extra_fields(self):
        record=sources()
        record['learner_id']='private_identity'
        record['kinds']['announcement']['text']='private_content'
        data=state();data['sources']=record
        clean=sanitize_state(data,NOW)
        self.assertNotIn('private',json.dumps(clean))
        self.assertEqual(clean['sources']['preview_count'],3)
        sample=snapshot();sample['state']=clean
        metrics=load_collector().HealthMetrics().calculate(sample,NOW.timestamp())
        self.assertEqual(metrics['source_announcement_mode'],1)
        self.assertEqual(metrics['source_message_mode'],2)
        self.assertEqual(metrics['source_announcement_success_age'],10)
        self.assertEqual(metrics['source_school_event_links'],4)
        self.assertEqual(metrics['source_preview_count'],3)
        self.assertEqual(metrics['success_age'],10)
    def test_unrecognized_error_or_source_kind_is_refused(self):
        for mutate in (lambda r:r['kinds']['announcement'].update(last_error='private_lowercase_secret'),
                       lambda r:r['kinds'].update(private_learner={}),
                       lambda r:r['kinds']['announcement'].update(pending=True)):
            data=state();data['sources']=sources();mutate(data['sources'])
            with self.assertRaises(ValueError): sanitize_state(data,NOW)
            sample=snapshot();sample['state']=data
            with self.assertRaises(ValueError): load_collector().HealthMetrics().calculate(sample,NOW.timestamp())

    def test_preview_budget_reasons_survive_export_and_standalone_collector(self):
        for code in ('source_preview_time_budget_exhausted',
                     'source_preview_call_budget_exhausted', 'source_preview_worker_required'):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                data=state();data['sources']=sources()
                data['sources']['kinds']['announcement'].update(last_error=code,text='private_content')
                with patch.object(monitoring,'unit_state',return_value={
                        'ActiveState':'active','UnitFileState':'enabled','Result':'success'}), \
                     patch.object(monitoring,'web_available',return_value=True):
                    exported=monitoring.collect_snapshot(now=NOW,reader=lambda *args,**kwargs:data,config=TEST_CONFIG)
                self.assertTrue(exported['db_ok'])
                path=Path(directory)/'status.json'
                monitoring.atomic_write(exported,path)
                sample=json.loads(path.read_text())
                self.assertNotIn('private_content',json.dumps(sample))
                self.assertEqual(sample['state']['sources']['kinds']['announcement']['last_error'],code)
                metrics=load_collector().HealthMetrics().calculate(sample,NOW.timestamp())
                self.assertEqual(metrics['source_announcement_pending'],2)
                self.assertEqual(metrics['source_announcement_failed'],1)
                self.assertEqual(metrics['source_preview_count'],3)
    def test_current_preview_queue_counts_ready_failed_and_stale_separately(self):
        db=sqlite3.connect(':memory:')
        try:
            from app.source_store import SOURCE_SCHEMA
            from app.source_preview import SOURCE_PREVIEW_PLANNER_VERSION
            db.execute('CREATE TABLE settings(key TEXT,value TEXT)')
            db.executescript(SOURCE_SCHEMA)
            db.execute("INSERT INTO settings VALUES ('multi_source_mode','preview')")
            for sid,status,hashed in [('ready','ready','v1'),('failed','failed','v1'),('stale','ready','v0')]:
                db.execute("INSERT INTO sources(id,kind,upstream_id,learner_id,payload,content_hash,status,fetched_at) VALUES (?,'announcement',?,'fictional','{}','v1','pending',?)",(sid,sid,stamp()))
                db.execute('INSERT INTO source_previews VALUES (?,?,?,?,?,?,?)',(sid,sid,hashed,json.dumps({'planner_version':SOURCE_PREVIEW_PLANNER_VERSION}),status,stamp(),None))
            tables={'settings','sources','source_health','action_sources','source_previews'}
            result=read_source_health(db,tables)['kinds']['announcement']
            self.assertEqual((result['pending'],result['failed'],result['preview_ready']),(1,1,1))
            self.assertEqual(read_source_health(db,tables)['preview_count'],1)
            data=state();data['sources']=read_source_health(db,tables)
            sample=snapshot();sample['state']=sanitize_state(data,NOW)
            metrics=load_collector().HealthMetrics().calculate(sample,NOW.timestamp())
            self.assertEqual(metrics['source_announcement_pending'],1)
            self.assertEqual(metrics['source_announcement_failed'],1)
            self.assertEqual(metrics['source_announcement_preview_ready'],1)
            db.execute("UPDATE settings SET value='active' WHERE key='multi_source_mode'")
            result=read_source_health(db,tables)['kinds']['announcement']
            self.assertEqual((result['pending'],result['failed'],result['preview_ready']),(3,0,None))
        finally: db.close()

    def test_reader_selects_only_aggregates_and_maps_private_error(self):
        db=sqlite3.connect(':memory:')
        try:
            db.executescript('''CREATE TABLE settings(key TEXT,value TEXT); CREATE TABLE sources(id TEXT,kind TEXT,status TEXT,payload TEXT,content_hash TEXT,deleted INTEGER DEFAULT 0);
                CREATE TABLE source_health(kind TEXT,last_successful_fetch TEXT,last_result TEXT,last_error TEXT);
                CREATE TABLE action_sources(action_id TEXT,source_id TEXT); CREATE TABLE source_previews(status TEXT,source_id TEXT,source_hash TEXT);''')
            db.execute("INSERT INTO settings VALUES ('multi_source_mode','preview')")
            db.execute('INSERT INTO sources(id,kind,status,payload) VALUES (?,?,?,?)',('private_source','announcement','pending','private_content'))
            db.execute('INSERT INTO source_health VALUES (?,?,?,?)',('announcement',stamp(2),'failed','private_lowercase_secret'))
            db.execute("INSERT INTO action_sources VALUES ('private_action','private_source')")
            db.execute("UPDATE sources SET content_hash='v1'")
            db.execute("INSERT INTO source_previews VALUES ('ready','private_source','v1')")
            tables={'settings','sources','source_health','action_sources','source_previews'}
            result=read_source_health(db,tables)
            self.assertNotIn('private',json.dumps(result))
            self.assertEqual(result['kinds']['announcement']['last_error'],'source_operation_failed')
            self.assertEqual(result['kinds']['announcement']['links'],1)
            self.assertEqual(result['kinds']['announcement']['pending'],1)
            self.assertEqual(result['kinds']['announcement']['preview_ready'],0)
            self.assertEqual(result['preview_count'],0)
        finally: db.close()

if __name__ == '__main__': unittest.main()
