import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import check_odyssey as bot


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.tomorrow = datetime.now(bot.PACIFIC).date() + timedelta(days=1)
        self.day = self.tomorrow.isoformat()
        self.item = {
            "date": self.day,
            "time": "7:00p",
            "status": "available",
            "url": "https://tickets.fandango.com/example",
            "source": "Fandango",
        }

    def write_state(self, **overrides):
        state = bot.default_state()
        state.update({"initialized": True})
        state.update(overrides)
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def run_bot(self, current=None, scan_error=None, send_error=None):
        current = current or {}

        def scan(_session, _today):
            if scan_error:
                raise RuntimeError(scan_error)
            return current

        with (
            patch.object(bot, "STATE_PATH", self.path),
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
        self.path.write_text(
            json.dumps({"version": 6, "initialized": False}),
            encoding="utf-8",
        )
        code, calls, state = self.run_bot({self.day: [self.item]})
        self.assertEqual(code, 0)
        self.assertFalse(calls)
        self.assertIn(self.day, state["ever_seen_dates"])
        self.assertTrue(state["initialized"])
        self.assertEqual(state["version"], 7)

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
            time="10:00p",
            url="https://tickets.fandango.com/example2",
        )
        self.assertFalse(
            self.run_bot({self.day: [self.item, second]})[1]
        )

    def test_disappear_and_reappear_is_silent(self):
        self.write_state(ever_seen_dates=[self.day])
        code, calls, state = self.run_bot({})
        self.assertFalse(calls)
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(
            self.run_bot({self.day: [self.item]})[1]
        )

    def test_failed_scan_does_not_change_baseline(self):
        self.write_state(ever_seen_dates=["2099-01-01"])
        code, calls, state = self.run_bot(scan_error="blocked")
        self.assertEqual(code, 2)
        self.assertFalse(calls)
        self.assertEqual(state["ever_seen_dates"], ["2099-01-01"])

    def test_delivery_failure_retries(self):
        self.write_state()
        code, calls, state = self.run_bot(
            {self.day: [self.item]},
            send_error="ntfy down",
        )
        self.assertEqual(code, 2)
        self.assertIn(self.day, state["pending_dates"])
        self.assertNotIn(self.day, state["ever_seen_dates"])
        self.path.write_text(json.dumps(state), encoding="utf-8")
        code, calls, state = self.run_bot({self.day: [self.item]})
        self.assertEqual(calls[0].args[0], [self.day])

    def test_extract_requires_explicit_imax_70mm_film_format(self):
        vm = {
            "movies": [{
                "title": "The Odyssey (2026)",
                "variants": [{
                    "amenityGroups": [{
                        "amenities": [{"name": "IMAX"}],
                        "showtimes": [{
                            "date": "7:00p",
                            "ticketingDate": f"{self.day}+19:00",
                            "filmFormat": [{"filterName": "IMAX"}],
                        }],
                    }],
                }],
            }],
        }
        self.assertEqual(
            bot.extract_matching_showtimes(vm, self.tomorrow),
            [],
        )

        vm["movies"][0]["variants"][0]["amenityGroups"][0]["showtimes"][0][
            "filmFormat"
        ] = [{"filterName": "IMAX 70MM"}]
        self.assertEqual(
            len(bot.extract_matching_showtimes(vm, self.tomorrow)),
            1,
        )

    def test_standard_70mm_does_not_match(self):
        vm = {
            "movies": [{
                "title": "The Odyssey",
                "variants": [{
                    "amenityGroups": [{
                        "amenities": [{"name": "70MM Film"}],
                        "showtimes": [{
                            "date": "7:00p",
                            "ticketingDate": f"{self.day}+19:00",
                            "filmFormat": [{"filterName": "70MM"}],
                        }],
                    }],
                }],
            }],
        }
        self.assertEqual(
            bot.extract_matching_showtimes(vm, self.tomorrow),
            [],
        )

    def test_sold_out_imax_70mm_still_makes_date_qualify(self):
        vm = {
            "movies": [{
                "title": "The Odyssey",
                "variants": [{
                    "amenityGroups": [{
                        "amenities": [],
                        "showtimes": [{
                            "date": "7:00p",
                            "ticketingDate": f"{self.day}+19:00",
                            "type": "soldout",
                            "expired": False,
                            "filmFormat": [{"filterName": "IMAX 70MM"}],
                            "ticketingJumpPageURL": "https://tickets.fandango.com/x",
                        }],
                    }],
                }],
            }],
        }
        items = bot.extract_matching_showtimes(vm, self.tomorrow)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "soldout")

    def test_amenity_fallback_requires_imax_and_70mm_together(self):
        vm = {
            "movies": [{
                "title": "The Odyssey",
                "variants": [{
                    "amenityGroups": [{
                        "amenities": [{"name": "IMAX 70MM Film"}],
                        "showtimes": [{
                            "date": "10:00a",
                            "ticketingDate": f"{self.day}+10:00",
                        }],
                    }],
                }],
            }],
        }
        self.assertEqual(
            len(bot.extract_matching_showtimes(vm, self.tomorrow)),
            1,
        )

    def test_fetch_day_validates_citywalk_theater(self):
        session = Mock()
        response = Mock()
        response.json.return_value = {
            "viewModel": {
                "theater": {
                    "details": {
                        "id": "AAAWX",
                        "chainCode": "AMC",
                        "name": "Universal Cinema AMC at CityWalk Hollywood",
                    }
                },
                "movies": [],
            }
        }
        session.get.return_value = response

        vm = bot.fetch_day(session, self.tomorrow)
        self.assertEqual(vm["movies"], [])
        response.raise_for_status.assert_called_once()
        kwargs = session.get.call_args.kwargs
        self.assertIn("fandango.com", kwargs["headers"]["Referer"])
        self.assertEqual(kwargs["params"]["startDate"], self.day)


if __name__ == "__main__":
    unittest.main()
