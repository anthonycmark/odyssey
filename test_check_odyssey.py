import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock
import check_odyssey as bot


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.tomorrow = datetime.now(bot.PACIFIC).date() + timedelta(days=1)
        self.day = self.tomorrow.isoformat()
        self.item = {'date': self.day, 'time': '7:00PM', 'url': 'https://example.com', 'sold_out': False}
        self.path.write_text(json.dumps({'version': 5, 'initialized': True, 'seen_dates': [], 'checked_dates': []}))
        self.control = '<h1>Universal Cinema</h1>' + ''.join(f'<a href="/showtimes/{i}">7:00PM</a>' for i in range(5))

    def run_bot(self, fail=False, send_fail=False):
        def fetch(session, d):
            if fail and d != self.tomorrow:
                raise RuntimeError('unavailable')
            return ({'new': self.item} if d == self.tomorrow else {}, 5, False)
        response = Mock(text=self.control, status_code=200)
        with patch.object(bot, 'STATE_PATH', self.path), patch.object(bot.requests, 'Session') as session, patch.object(bot, 'fetch_listing', side_effect=fetch), patch.object(bot, 'send_new_date_notification') as send, contextlib.redirect_stdout(io.StringIO()):
            session.return_value.get.return_value = response
            if send_fail:
                send.side_effect = RuntimeError('secret URL must not be logged')
            code = bot.main()
            return code, send.call_args_list, json.loads(self.path.read_text())

    def test_first_appearance_then_no_repeat(self):
        code, calls, state = self.run_bot()
        self.assertEqual(code, 0)
        self.assertEqual(calls[0].args[0], [self.day])
        self.assertFalse(state['pending_showings'])
        self.assertEqual(self.run_bot()[1], [])

    def test_partial_failure_retains_alert_for_retry(self):
        code, calls, state = self.run_bot(fail=True)
        self.assertEqual(code, 2)
        self.assertFalse(calls)
        self.assertTrue(state['pending_showings'])
        self.assertEqual(self.run_bot()[1][0].args[0], [self.day])

    def test_delivery_failure_retries(self):
        self.assertTrue(self.run_bot(send_fail=True)[2]['pending_showings'])
        self.assertEqual(self.run_bot()[1][0].args[0], [self.day])

    def test_sold_out_date_does_not_alert_on_restock(self):
        self.item['sold_out'] = True
        self.assertFalse(self.run_bot()[1])
        self.item['sold_out'] = False
        self.assertFalse(self.run_bot()[1])

    def test_failed_baseline_is_not_initialized(self):
        self.path.unlink()
        self.assertFalse(self.run_bot(fail=True)[2]['initialized'])
        self.assertFalse(self.run_bot()[1])

    def test_full_scan_window(self):
        state = self.run_bot()[2]
        self.assertEqual(len(state['checked_dates']), bot.DAYS_AHEAD)

    def test_corrupt_state_is_not_silently_replaced(self):
        self.path.write_text('{')
        with patch.object(bot, 'STATE_PATH', self.path), self.assertRaises(json.JSONDecodeError):
            bot.load_state()

    def test_format_does_not_leak_between_movies(self):
        html = '<main><section><h2>The Odyssey</h2><p>IMAX with Laser</p><a href="/showtimes/1">7:00PM</a></section><section><h2>Other movie</h2><p>IMAX 70mm</p><a href="/showtimes/2">8:00PM</a></section></main>'
        self.assertFalse(bot.parse_listing_page(html, self.tomorrow)[0])

    def test_exact_format(self):
        html = '<section><h2>The Odyssey – IMAX 70mm Event</h2><a href="/showtimes/1">7:00PM</a></section>'
        self.assertEqual(len(bot.parse_listing_page(html, self.tomorrow)[0]), 1)


if __name__ == '__main__':
    unittest.main()
