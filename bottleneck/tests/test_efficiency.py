import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from efficiency import TokenBudget,BudgetExceeded,excerpt,compact_docs,audit_docs,relevant_catalog
from network import Gemini,ApiError,BudgetError
from main import discover_specs,review_queue
from sources import SECTORS
from core import Store

class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*a):self.close()

def response(obj):return Response(json.dumps(obj).encode())

class EfficiencyTests(unittest.TestCase):
    def test_eighty_distinct_sectors_and_compound_queries(self):
        self.assertEqual(len(SECTORS),80)
        self.assertEqual(len({x[0] for x in SECTORS}),80)
        from urllib.parse import urlparse,parse_qs
        specs=discover_specs(3)
        q=parse_qs(urlparse(specs[3]['url']).query)['q'][0]
        self.assertIn('"power grid"',q)
        self.assertNotIn('"power" OR "grid"',q)
    def test_excerpt_is_bounded_and_keeps_matching_evidence(self):
        text=('irrelevant filler '*1000)+'\nNew desalination contracts worth 500 million.\n'+('ending filler '*1000)
        result=excerpt(text,'desalination contracts',3000)
        self.assertLessEqual(len(result),3000)
        self.assertIn('New desalination contracts worth 500 million.',result)
    def test_audit_context_preserves_exact_quote(self):
        text='x'*10000+'Verified fact of industrial demand.'+'y'*10000
        d=dict(id='d',text=text)
        e=dict(doc_id='d',quote='Verified fact of industrial demand.')
        result=audit_docs([d],[e])[0]['text']
        self.assertIn(e['quote'],result);self.assertLess(len(result),1000)
    def test_catalog_keeps_relevant_themes_without_full_history(self):
        themes=[dict(id=str(i),title='metal',chain_key='metal',sector='a') for i in range(100)]
        themes.append(dict(id='target',title='aquaculture',chain_key='aquaculture demand',sector='fish'))
        result=relevant_catalog(themes,[dict(title='aquaculture',sector='fish')],40)
        self.assertEqual(len(result),40);self.assertEqual(result[0]['id'],'target')
    def test_daily_budget_survives_restarts_and_unknown_response(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'budget.sqlite3'
            a=TokenBudget(p,100,150);a.reserve(90);a.db.close()
            b=TokenBudget(p,100,150)
            with self.assertRaises(BudgetExceeded):b.reserve(70)
            self.assertEqual(b.total(),90);b.db.close()
    def test_measured_usage_releases_unused_reservation(self):
        b=TokenBudget(':memory:',100,150);r=b.reserve(90);b.settle(r,30)
        self.assertEqual(b.used,30);self.assertEqual(b.total(),30)
        b.reserve(70)
        with self.assertRaises(BudgetExceeded):b.reserve(1)
        b.db.close()
    def test_preview_cannot_reset_day_budget(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'u.sqlite3'
            live=TokenBudget(p,100,100);live.reserve(80)
            preview=TokenBudget(p,100,100)
            with self.assertRaises(BudgetExceeded):preview.reserve(30)
            live.db.close();preview.db.close()
    def test_backlog_can_select_newest_and_oldest(self):
        s=Store(':memory:');s.add_articles([dict(id=str(i),published=f'2026-09-{i+1:02}',sector='x') for i in range(5)])
        self.assertEqual(s.pending_articles(1,newest=True)[0]['id'],'4')
        self.assertEqual(s.pending_articles(1,newest=False)[0]['id'],'0');s.db.close()
    def test_single_review_slot_can_alternate(self):
        themes=[dict(id='n',reviewed=''),dict(id='o',reviewed='x')]
        self.assertEqual(review_queue(themes,1,True)[0]['id'],'n')
        self.assertEqual(review_queue(themes,1,False)[0]['id'],'o')

class BudgetApiTests(unittest.TestCase):
    schema={'type':'object','properties':{},'additionalProperties':False}
    def test_count_failure_blocks_generation(self):
        a=Gemini('x','gemini-3.8-flash')
        with patch('network.urllib.request.urlopen',side_effect=OSError()):
            with self.assertRaises(ApiError):a.json('system',{},self.schema)
        self.assertEqual(a.calls,0)
    def test_large_input_blocks_generation(self):
        a=Gemini('x','gemini-3.8-flash')
        with patch('network.urllib.request.urlopen',return_value=response({'totalTokens':40000})):
            with self.assertRaises(BudgetError):a.json('system',{},self.schema)
        self.assertEqual(a.calls,0)
    def test_daily_budget_blocks_generation_after_count(self):
        a=Gemini('x','gemini-3.8-flash');a.budget=TokenBudget(':memory:',50,50)
        with patch('network.urllib.request.urlopen',return_value=response({'totalTokens':10})):
            with self.assertRaises(BudgetExceeded):a.json('system',{},self.schema)
        self.assertEqual(a.calls,0);a.budget.db.close()
    def test_real_payload_shape_and_usage_accounting_with_mock_http(self):
        a=Gemini('x','gemini-3.8-flash');a.budget=TokenBudget(':memory:',50000,50000)
        data={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'{}'}]}}],
              'usageMetadata':{'totalTokenCount':130,'promptTokenCount':100,'candidatesTokenCount':20,'thoughtsTokenCount':10}}
        with patch('network.urllib.request.urlopen',side_effect=[response({'totalTokens':100}),response(data)]) as call:
            self.assertEqual(a.json('system',{},self.schema),{})
        self.assertEqual(a.budget.used,130);self.assertEqual(a.tokens,130)
        self.assertEqual(a.count_calls,1);self.assertEqual(a.calls,1)
        payload=json.loads(call.call_args_list[0].args[0].data)
        self.assertIn('systemInstruction',payload['generateContentRequest']);a.budget.db.close()
    def test_failed_generation_keeps_reservation(self):
        a=Gemini('x','gemini-3.8-flash');a.budget=TokenBudget(':memory:',50000,50000)
        with patch('network.urllib.request.urlopen',side_effect=[response({'totalTokens':100}),OSError()]):
            with self.assertRaises(ApiError):a.json('system',{},self.schema)
        self.assertGreater(a.budget.total(),100);a.budget.db.close()
