import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, UTC, alert_key, chunks, evaluate, source_counts, validate_evidence

TODAY=dt.datetime(2026,9,16,tzinfo=UTC)

def assessment(): return dict(structural=True,economic_path=True,thesis_broken=False)
def sample(i=0,axis='demand',stance='supports'):
    return dict(id=str(i),fact_key='fact'+str(i),origin_key='reporter'+str(i),
                text_hash='body'+str(i),publisher=f'publisher{i}.com',event_date='2026-09-15',
                axis=axis,stance=stance,primary=i==0)
def five():
    return [sample(i,['new_use','demand','supply','commitment','pricing'][i]) for i in range(5)]

class EvidenceTests(unittest.TestCase):
    def test_five_unique_sources_promote(self):
        g=evaluate(assessment(),five(),today=TODAY)
        self.assertEqual(g['state'],'변화 강화')
    def test_four_sources_stay_watch(self):
        self.assertEqual(evaluate(assessment(),five()[:4],today=TODAY)['state'],'관찰')
    def test_five_reprints_count_once(self):
        evidence=five()
        for e in evidence: e['origin_key']='same press release'
        self.assertEqual(source_counts(evidence),1)
        self.assertEqual(evaluate(assessment(),evidence,today=TODAY)['state'],'관찰')
    def test_same_publisher_and_copy_transitively_merge(self):
        ev=five(); ev[1]['publisher']=ev[0]['publisher']; ev[2]['origin_key']=ev[1]['origin_key']
        ev[3]['text_hash']=ev[2]['text_hash']
        self.assertEqual(source_counts(ev),2)
    def test_no_primary_means_watch(self):
        ev=five()
        for e in ev:e['primary']=False
        self.assertEqual(evaluate(assessment(),ev,today=TODAY)['state'],'관찰')
    def test_old_story_does_not_become_new(self):
        ev=five()
        for e in ev:e['event_date']='2025-09-01'
        self.assertEqual(evaluate(assessment(),ev,today=TODAY)['state'],'관찰')
    def test_recent_reprint_of_older_event_is_not_alert(self):
        ev=five()
        for e in ev:e['event_date']='2026-07-01'
        self.assertEqual(evaluate(assessment(),ev,today=TODAY)['state'],'관찰')
    def test_hallucinated_quote_and_unknown_doc_rejected(self):
        doc=dict(id='a',status='body',text='A verified original statement about new electricity storage demand.',
                 url='https://source.example/article',fetched='2026-09-16',primary=True)
        base=dict(doc_id='a',quote=doc['text'],event_date='2026-09-15',axis='demand',stance='supports',
                  origin_key='independent original research',fact_key='new demand',fact='새 수요')
        self.assertEqual(len(validate_evidence([base],[doc],TODAY.date())),1)
        for changes in [dict(quote='Invented statement about a 900% demand increase.'),dict(doc_id='b'),
                        dict(event_date='2026-10-01'),dict(origin_key='unknown')]:
            self.assertEqual(validate_evidence([dict(base,**changes)],[doc],TODAY.date()),[])
    def test_rss_never_counts_as_body(self):
        doc=dict(id='a',status='snippet',text='A long piece of RSS text pretending to be full source evidence.')
        e=dict(doc_id='a',quote=doc['text'],event_date='2026-09-15')
        self.assertEqual(validate_evidence([e],[doc],TODAY.date()),[])
    def test_counterevidence_can_reverse(self):
        ev=five()+[sample(i,stance='contradicts') for i in range(5,10)]
        self.assertEqual(evaluate(dict(assessment(),thesis_broken=True),ev,today=TODAY)['state'],'반증 경고')
    def test_reprint_does_not_change_alert_key(self):
        ev=five(); g=evaluate(assessment(),ev,today=TODAY)
        duplicate=dict(ev[0],id='reprint',publisher='newspaper.com')
        self.assertEqual(alert_key('theme',g,ev),alert_key('theme',g,ev+[duplicate]))
    def test_changed_numbers_change_event_key(self):
        ev=five(); g=evaluate(assessment(),ev,today=TODAY)
        self.assertNotEqual(alert_key('theme',g,ev),alert_key('theme',g,ev+[dict(ev[0],fact_key='demand 200 instead of 100')]))
    def test_telegram_utf16_limits(self):
        content='😀산업\n'*3000
        parts=chunks(content)
        self.assertEqual(''.join(parts),content)
        self.assertTrue(all(len(p.encode('utf-16-le'))//2<=3500 for p in parts))

class MemoryTests(unittest.TestCase):
    def setUp(self): self.store=Store(':memory:')
    def tearDown(self): self.store.db.close()
    def test_backlog_preserved_sector_balanced(self):
        self.store.add_articles([dict(id=str(i),sector='A' if i<9 else 'B',published=str(i)) for i in range(10)])
        a=self.store.pending_articles(2)
        self.assertEqual({x['sector'] for x in a},{'A','B'})
        self.store.screened([x['id'] for x in a])
        self.assertEqual(len(self.store.pending_articles(100)),8)
    def test_theme_merge_keeps_seeds(self):
        c=dict(chain_key='x|y|z',seed_ids=['a'])
        tid=self.store.upsert_theme(c)
        self.store.upsert_theme(dict(c,existing_id=tid,seed_ids=['b']))
        self.assertEqual(self.store.themes()[0]['seed_ids'],['a','b'])
    def test_unknown_theme_id_rejected(self):
        with self.assertRaises(ValueError): self.store.upsert_theme(dict(existing_id='wrong',chain_key='x'))
    def test_outbox_idempotent(self):
        self.store.enqueue('same','hello'); self.store.enqueue('same','hello')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
    def test_preview_memory_does_not_change_live(self):
        preview=Store(':memory:'); self.store.db.backup(preview.db)
        preview.enqueue('preview','should not be sent')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        preview.db.close()
    def test_evidence_reclassification_replaces_current_but_keeps_history(self):
        tid=self.store.upsert_theme(dict(chain_key='x'))
        ev=sample()
        self.store.record(tid,{'old':True},[ev])
        self.store.record(tid,{'new':True},[dict(ev,stance='contradicts')])
        self.assertEqual(self.store.evidence(tid)[0]['stance'],'contradicts')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],2)

class DeliveryTests(unittest.TestCase):
    def test_uncertain_delivery_never_automatically_retries(self):
        from main import deliver
        s=Store(':memory:'); s.enqueue('x','hello')
        with patch('main.telegram',return_value=('uncertain',None)) as send:
            with self.assertRaises(RuntimeError): deliver(s,{'token':'x','chat':'y'})
            deliver(s,{'token':'x','chat':'y'})
            self.assertEqual(send.call_count,1)
        s.db.close()
    def test_crash_after_send_marked_uncertain(self):
        from main import deliver
        s=Store(':memory:'); s.enqueue('x','hello')
        s.db.execute("UPDATE outbox SET status='sending'");s.db.commit()
        with patch('main.telegram') as send:
            deliver(s,{'token':'x','chat':'y'});send.assert_not_called()
        self.assertEqual(s.db.execute('SELECT status FROM outbox').fetchone()[0],'uncertain');s.db.close()
    def test_success_receipt_recorded(self):
        from main import deliver
        s=Store(':memory:');s.enqueue('x','hello')
        with patch('main.telegram',return_value=('sent',123)),patch('main.time.sleep'):
            deliver(s,{'token':'x','chat':'y'})
        r=s.db.execute('SELECT status,message_id FROM outbox').fetchone()
        self.assertEqual(tuple(r),('sent',123));s.db.close()

if __name__=='__main__':unittest.main()

class IntegrationTests(unittest.TestCase):
    def test_rotation_includes_old_and_new(self):
        from main import review_queue
        themes=[dict(id='new',reviewed=''),dict(id='old',reviewed='2026-01-01')]
        self.assertEqual({t['id'] for t in review_queue(themes,2)},{'old','new'})

    def test_mocked_full_run_creates_verified_alert_and_persists_memory(self):
        import main
        from core import now
        date=now()[:10]
        articles=[dict(id=f'doc{i}',published=now(),sector='기타',title=f'New use report {i}',
                       url=f'https://publisher{i}.com/report',summary='New use evidence',publisher=f'Publisher{i}') for i in range(5)]
        evidence=[dict(doc_id=f'doc{i}',quote=f'This is an original factual statement number {i} about new use demand.',
                       fact=f'원문에 실린 새로운 수요 근거 {i}',fact_key=f'fact{i}',origin_key=f'origin{i}',
                       official_source=i==0,event_date=date,axis=['new_use','demand','supply','commitment','pricing'][i],stance='supports') for i in range(5)]
        candidate=dict(existing_id='',chain_key='buyer|new need|equipment',title='가상 검증 시나리오',
                       sector='기타',hypothesis='테스트용 가설',seed_ids=[a['id'] for a in articles],queries=['new demand'])
        response=dict(structural=True,economic_path=True,thesis_broken=False,change='새 용도가 관측됐습니다.',
                      chain='새 요구 → 장비 수요',beneficiaries='장비 산업이라는 가설입니다.',
                      profit_capture='증설 속도가 관건입니다.',countercase='수요 감소',next_check='계약 집행',evidence=evidence)
        class FakeAI:
            def __init__(self,*args):self.calls=0;self.max_calls=10;self.tokens=0
            def json(self,system,data,schema):
                self.calls+=1
                if 'articles' in data:return {'candidates':[candidate]}
                if 'assessment' in data:
                    return dict(narrative_supported=True,reason='테스트',checks=[dict(evidence_id=e['id'],supported=True,official_source=e['primary'],origin_key=e['origin_key']) for e in data['evidence']])
                return response
        def body(a,domains):
            i=int(a['id'][3:])
            return dict(id=a['id'],status='body',url=a['url'],fetched=now(),primary=i==0,
                        text=evidence[i]['quote'],title=a['title'],feed_date=now())
        with tempfile.TemporaryDirectory() as temp:
            c=dict(key='test-key',token='test-token',chat='test-chat',data=Path(temp),model='test',calls=10,
                   screen_batches=1,batch_size=80,research=1,docs=20,history_days=365,fresh_hours=72,
                   empty=True,primary=[],extra_feeds=[])
            health=dict(total=1,healthy=1,failed=[])
            with patch('main.Gemini',FakeAI),patch('main.collect',return_value=(articles,health)),patch('main.fetch_body',side_effect=body),patch('main.telegram',return_value=('sent',123)),patch('main.time.sleep'):
                main.run(c,send=True)
            store=Store(Path(temp)/'radar.sqlite3')
            self.assertEqual(len(store.themes()),1)
            self.assertEqual(len(store.evidence(store.themes()[0]['id'])),5)
            statuses=[r[0] for r in store.db.execute('SELECT status FROM outbox')]
            self.assertTrue(statuses and all(s=='sent' for s in statuses))
            self.assertIn('변화 강화',(Path(temp)/'latest.md').read_text())
            store.db.close()
            with patch('main.Gemini',FakeAI),patch('main.collect',return_value=(articles,health)),patch('main.fetch_body',side_effect=body),patch('main.telegram',return_value=('sent',124)),patch('main.time.sleep'):
                main.run(c,send=True)
            store=Store(Path(temp)/'radar.sqlite3')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],1)
            store.db.close()
