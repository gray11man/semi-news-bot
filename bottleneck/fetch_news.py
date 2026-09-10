import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import fetch_news as f
import pick_headlines as p
import telegram_send as t
import main

ARTICLE = {'title': '핵심 공급사 생산능력 30% 영구 폐쇄 확정', 'summary': '업계 공급의 10% 감소',
           'link': 'https://example.org/news', 'source': '테스트', '_seen_key': 'key'}
PICK = {'index': 0, 'headline': ARTICLE['title'], 'reason': '영구 폐쇄 → 산업 공급 감소',
        'evidence': '생산능력 30% 영구 폐쇄 확정'}

class BotTests(unittest.TestCase):
    def test_bad_indices_fail(self):
        for idx in (True, '0', -1, 10, 0.0):
            with self.assertRaises(ValueError):
                p._validate([{**PICK, 'index': idx}], [ARTICLE], 3)

    def test_duplicate_indices_fail(self):
        with self.assertRaises(ValueError):
            p._validate([PICK, PICK], [ARTICLE], 3)

    def test_invented_evidence_fails(self):
        with self.assertRaises(ValueError):
            p._validate([{**PICK, 'evidence': '존재하지 않는 사실'}], [ARTICLE], 3)

    def test_original_fields_preserved(self):
        result = p._validate([PICK], [ARTICLE], 3)[0]
        self.assertEqual(result['_seen_key'], 'key')
        self.assertEqual(result['title'], ARTICLE['title'])

    def test_final_review_runs_for_single_batch(self):
        with patch.object(f, 'recent_sent', return_value=[]), patch.object(p, '_pick', side_effect=[[ARTICLE], []]) as call:
            self.assertEqual(p.pick_critical([ARTICLE]), [])
            self.assertTrue(call.call_args.kwargs['review'])

    def test_failed_batch_prevents_partial_picks(self):
        with patch.object(f, 'recent_sent', return_value=[]), patch.object(p, '_pick', side_effect=[[ARTICLE], ValueError('실패')]):
            self.assertIsNone(p.pick_critical([ARTICLE] * 61))

    def test_zero_limit(self):
        with patch.object(p, '_pick') as call:
            self.assertEqual(p.pick_critical([ARTICLE], max_pick=0), [])
            call.assert_not_called()

    def test_html_escaped(self):
        msg = t.format_message({**ARTICLE, 'title': '<b>& 테스트'})
        self.assertIn('&lt;b&gt;&amp;', msg)

    def test_only_confirmed_delivery_saved(self):
        saved = Mock()
        with patch.object(t, 'send_message', side_effect=[True, False]), patch.object(t.time, 'sleep'):
            result = t.send_results([ARTICLE, {**ARTICLE, 'title': '두 번째'}], on_sent=saved)
        self.assertEqual(result, [ARTICLE])
        saved.assert_called_once_with([ARTICLE])

    def test_empty_selection_is_silent(self):
        with patch.object(t, 'send_message') as call:
            self.assertEqual(t.send_results([]), [])
            call.assert_not_called()

    def test_dry_run_never_saves(self):
        with patch.dict(os.environ, {'DRY_RUN': '1'}), patch.object(main, 'pick_critical', return_value=[{**ARTICLE, 'reason': '테스트'}]), patch.object(main, 'send_results') as send:
            main.run([ARTICLE])
            send.assert_not_called()

    def test_atomic_history_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'seen.json')
            now = f._now_utc().timestamp()
            f._save_json(path, {'legacy': now})
            with patch.object(f, 'SEEN_FILE', path):
                f.mark_sent([ARTICLE])
                data = json.loads(Path(path).read_text())
                self.assertEqual(data['key']['title'], ARTICLE['title'])
                self.assertEqual(data['legacy']['ts'], now)

    def test_corrupt_history_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'seen.json'
            path.write_text('{broken')
            with self.assertRaises(RuntimeError):
                f._load_json(path, {})

    def test_distinct_updates_not_string_filtered(self):
        self.assertFalse(f._is_similar('산업 생산능력 30 감소', '산업 생산능력 50 증가'))

    def test_all_feeds_failed_is_error(self):
        with patch.object(f, 'FEEDS', ['https://example.org/rss']), patch.object(f, '_load_json', return_value={}), patch.object(f.requests, 'get', side_effect=f.requests.Timeout):
            with self.assertRaises(RuntimeError):
                f.fetch_news()

    def test_ok_false_not_delivery(self):
        response = Mock(status_code=200)
        response.json.return_value = {'ok': False}
        with patch.dict(os.environ, {'DRY_RUN': '0', 'BOTTLENECK_TOKEN': 'test', 'BOTTLENECK_CHAT_ID': 'test'}), patch.object(t.requests, 'post', return_value=response):
            self.assertFalse(t.send_message('test'))

if __name__ == '__main__':
    unittest.main()
