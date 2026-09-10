import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import feedparser  # noqa: F401
except ModuleNotFoundError:
    sys.modules["feedparser"] = types.SimpleNamespace(parse=lambda content: {})

import fetch_news as f
import main
import pick_headlines as p
import telegram_send as t

ARTICLE = {
    "title": "핵심 공급사 생산능력 30% 영구 폐쇄 확정",
    "summary": "업계 공급의 10% 감소",
    "link": "https://example.org/news",
    "source": "테스트",
    "sector_hint": "철강·화학·소재",
    "_seen_key": "key",
    "_nt": "핵심 공급사 생산능력 30 영구 폐쇄 확정",
}
PICK = {
    "index": 0,
    "sector": "철강·화학·소재",
    "headline": ARTICLE["title"],
    "reason": "영구 폐쇄 → 산업 공급 감소",
    "evidence": "생산능력 30% 영구 폐쇄 확정",
}


class BotTests(unittest.TestCase):
    def test_bad_indices_are_dropped(self):
        for idx in (True, "0", -1, 10, 0.0):
            result = p._validate([{**PICK, "index": idx}], [ARTICLE], 3)
            self.assertEqual(result, [])

    def test_duplicate_indices_drop_only_duplicate(self):
        result = p._validate([PICK, PICK], [ARTICLE], 3)
        self.assertEqual(len(result), 1)

    def test_invented_evidence_is_dropped(self):
        result = p._validate([{**PICK, "evidence": "존재하지 않는 사실"}], [ARTICLE], 3)
        self.assertEqual(result, [])

    def test_original_fields_preserved(self):
        result = p._validate([PICK], [ARTICLE], 3)[0]
        self.assertEqual(result["_seen_key"], "key")
        self.assertEqual(result["title"], ARTICLE["title"])
        self.assertEqual(result["sector"], "철강·화학·소재")

    def test_distinct_numeric_update_not_filtered(self):
        self.assertFalse(f._is_similar("산업 생산능력 30% 감소", "산업 생산능력 50% 감소"))

    def test_opposite_direction_not_filtered(self):
        self.assertFalse(f._is_similar("산업 생산능력 감소", "산업 생산능력 증가"))

    def test_close_rewrite_is_duplicate(self):
        self.assertTrue(f._is_similar("abc company permanently closes plant", "abc company permanently closes plant"))

    def test_final_review_runs(self):
        first = [{**ARTICLE, "sector": "소재", "headline": ARTICLE["title"], "reason": "x", "evidence": ARTICLE["title"]}]
        with patch.object(f, "recent_sent", return_value=[]), patch.object(p, "_pick", side_effect=[first, first]) as call:
            result = p.pick_critical([ARTICLE], max_pick=3)
            self.assertEqual(len(result), 1)
            self.assertTrue(call.call_args.kwargs["review"])

    def test_zero_limit(self):
        with patch.object(p, "_pick") as call:
            self.assertEqual(p.pick_critical([ARTICLE], max_pick=0), [])
            call.assert_not_called()

    def test_diversity_soft_cap(self):
        rows = [
            {"title": "a", "sector": "반도체"},
            {"title": "b", "sector": "반도체"},
            {"title": "c", "sector": "반도체"},
            {"title": "d", "sector": "전력"},
            {"title": "e", "sector": "조선"},
        ]
        out = p._diversify(rows, 4, max_same_sector=2)
        self.assertEqual([x["title"] for x in out], ["a", "b", "d", "e"])

    def test_html_escaped(self):
        msg = t.format_message({**ARTICLE, "headline": "<b>& 테스트", "sector": "소재"})
        self.assertIn("&lt;b&gt;&amp;", msg)
        self.assertIn("소재", msg)

    def test_only_confirmed_delivery_saved(self):
        saved = Mock()
        with patch.object(t, "send_message", side_effect=[True, False]), patch.object(t.time, "sleep"):
            result = t.send_results([ARTICLE, {**ARTICLE, "title": "두 번째"}], on_sent=saved)
        self.assertEqual(result, [ARTICLE])
        saved.assert_called_once_with([ARTICLE])

    def test_empty_selection_is_silent(self):
        with patch.object(t, "send_message") as call:
            self.assertEqual(t.send_results([]), [])
            call.assert_not_called()

    def test_dry_run_never_sends(self):
        lock = Path(tempfile.gettempdir()) / f"newsbot-test-{os.getpid()}.lock"
        with patch.dict(os.environ, {"DRY_RUN": "1"}), \
             patch.object(main, "LOCK_FILE", lock), \
             patch.object(main, "pick_critical", return_value=[{**ARTICLE, "reason": "테스트"}]), \
             patch.object(main, "send_results") as send:
            main.run([ARTICLE])
            send.assert_not_called()
        lock.unlink(missing_ok=True)

    def test_atomic_history_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "seen.json")
            now = f._now_utc().timestamp()
            f._save_json(path, {"legacy": now})
            with patch.object(f, "SEEN_FILE", path):
                f.mark_sent([ARTICLE])
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                self.assertEqual(data["key"]["title"], ARTICLE["title"])
                self.assertEqual(data["legacy"]["ts"], now)

    def test_recent_history_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            now = f._now_utc().timestamp()
            data = {
                f"k{i}": {"ts": now - i, "title": f"t{i}", "nt": f"t{i}"}
                for i in range(20)
            }
            f._save_json(path, data)
            with patch.object(f, "SEEN_FILE", str(path)):
                rows = f.recent_sent(limit=5, days=10)
            self.assertEqual(len(rows), 5)
            self.assertEqual(rows[0]["title"], "t0")

    def test_corrupt_history_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                f._load_json(path, {})

    def test_all_feeds_failed_is_error(self):
        with patch.object(f, "FEEDS", ["https://example.org/rss"]), \
             patch.object(f, "_load_json", return_value={}), \
             patch.object(f.requests, "get", side_effect=f.requests.Timeout):
            with self.assertRaises(RuntimeError):
                f.fetch_news()

    def test_ok_false_not_delivery(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": False}
        with patch.dict(os.environ, {
            "DRY_RUN": "0", "BOTTLENECK_TOKEN": "test", "BOTTLENECK_CHAT_ID": "test"
        }), patch.object(t.requests, "post", return_value=response):
            self.assertFalse(t.send_message("test"))

    def test_lock_rejects_second_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "bot.lock"
            with patch.object(main, "LOCK_FILE", lock):
                with main.single_instance_lock():
                    with self.assertRaises(RuntimeError):
                        with main.single_instance_lock():
                            pass

    def test_stale_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "bot.lock"
            lock.write_text("old", encoding="utf-8")
            old = time.time() - 9999
            os.utime(lock, (old, old))
            with patch.object(main, "LOCK_FILE", lock), patch.object(main, "LOCK_STALE_SECONDS", 10):
                with main.single_instance_lock():
                    self.assertTrue(lock.exists())
                self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
