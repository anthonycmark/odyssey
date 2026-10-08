import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import check_odyssey as bot


class FakeBrowser:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.tomorrow = datetime.now(bot.PACIFIC).date() + timedelta(days=1)
        self.day = self.tomorrow.isoformat()
        self.item = {
            "date": self.day,
            "time": "7:00PM",
            "url": "https://www.amctheatres.com/showtimes/123",
            "showtime_id": "123",
        }

    def write_state(self, **overrides):
        state = bot.default_state()
        state.update({"initialized": True})
        state.update(overrides)
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def run_bot(self, current=None, scan_error=None, send_error=None):
        current = current or {}

        def scan(_browser, _today):
            if scan_error:
                raise RuntimeError(scan_error)
            return current

        with (
            patch.object(bot, "STATE_PATH", self.path),
            patch.object(bot, "AMCBrowser", return_value=FakeBrowser()),
            patch.object(bot, "scan_future_dates", side_effect=scan),
            patch.object(bot, "send_new_date_notification") as send,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            if send_error:
                send.side_effect = RuntimeError(send_error)
            code = bot.main()
            state = json.loads(self.path.read_text())
            return code, send.call_args_list, state

    def test_first_success_after_rewrite_is_silent_baseline(self):
        self.path.write_text(json.dumps({"version": 5, "initialized": True}), encoding="utf-8")
        code, calls, state = self.run_bot({self.day: [self.item]})
        self.assertEqual(code, 0)
        self.assertFalse(calls)
        self.assertIn(self.day, state["ever_seen_dates"])
        self.assertTrue(state["initialized"])

    def test_new_date_alerts_once(self):
        self.write_state()
        code, calls, state = self.run_bot({self.day: [self.item]})
        self.assertEqual(code, 0)
        self.assertEqual(calls[0].args[0], [self.day])
        self.assertIn(self.day, state["ever_seen_dates"])
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(self.run_bot({self.day: [self.item]})[1])

    def test_extra_showtime_on_known_date_is_silent(self):
        self.write_state(ever_seen_dates=[self.day])
        second = dict(
            self.item,
            showtime_id="456",
            time="10:00PM",
            url="https://www.amctheatres.com/showtimes/456",
        )
        self.assertFalse(self.run_bot({self.day: [self.item, second]})[1])

    def test_disappear_and_reappear_is_silent(self):
        self.write_state(ever_seen_dates=[self.day])
        code, calls, state = self.run_bot({})
        self.assertFalse(calls)
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(self.run_bot({self.day: [self.item]})[1])

    def test_failed_scan_does_not_change_baseline(self):
        self.write_state(ever_seen_dates=["2099-01-01"])
        code, calls, state = self.run_bot(scan_error="blocked")
        self.assertEqual(code, 2)
        self.assertFalse(calls)
        self.assertEqual(state["ever_seen_dates"], ["2099-01-01"])

    def test_delivery_failure_retries(self):
        self.write_state()
        code, calls, state = self.run_bot({self.day: [self.item]}, send_error="ntfy down")
        self.assertEqual(code, 2)
        self.assertIn(self.day, state["pending_dates"])
        self.assertNotIn(self.day, state["ever_seen_dates"])
        self.path.write_text(json.dumps(state), encoding="utf-8")
        code, calls, state = self.run_bot({self.day: [self.item]})
        self.assertEqual(calls[0].args[0], [self.day])

    def test_parser_requires_same_nearby_odyssey_imax70_context(self):
        html = """
        <main>
          <section><h2>The Odyssey</h2><p>IMAX with Laser</p><a href='/showtimes/1'>7:00 PM</a></section>
          <section><h2>Other Movie</h2><p>IMAX 70MM</p><a href='/showtimes/2'>8:00 PM</a></section>
        </main>
        """
        items, _, _ = bot.parse_listing_page(html, self.tomorrow)
        self.assertEqual(items, [])

    def test_parser_accepts_explicit_imax70_event(self):
        html = """
        <main><h1>Universal Cinema AMC at CityWalk Hollywood</h1>
          <section><h2>The Odyssey – IMAX 70MM Event</h2>
            <a href='/showtimes/123'>7:00 PM</a>
          </section>
        </main>
        """
        items, _, signal = bot.parse_listing_page(html, self.tomorrow)
        self.assertEqual(len(items), 1)
        self.assertTrue(signal)


if __name__ == "__main__":
    unittest.main()
