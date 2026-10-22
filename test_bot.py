#!/usr/bin/env python3
"""Test offline cho bot on-chain + installer.

Chạy:
    python3 test_bot.py              # test offline (không cần mạng, ~5 giây)
    RUN_LIVE=1 python3 test_bot.py   # thêm 1 test tải dữ liệu thật (cần mạng)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import onchain_bot as ob  # noqa: E402

DAY = 86400
BASE_TS = 1_700_000_000  # mốc cố định để test tất định


def make_snapshots(balances):
    """balances: list các dict token -> số lượng, mỗi phần tử là 1 ngày."""
    return [
        {"date": BASE_TS + idx * DAY, "tokens": dict(tokens)}
        for idx, tokens in enumerate(balances)
    ]


def rising_btc_snapshots(days=31, start=1000.0, step=10.0):
    return make_snapshots([{"BTC": start + idx * step} for idx in range(days)])


class FakeTelegram:
    """Bot API giả: ghi lại request, trả về ok (hỗ trợ cả GET cho installer)."""

    def __init__(self, chat_id=555):
        self.requests = []
        self.chat_id = chat_id
        self.updates_served = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode()
                payload = json.loads(raw) if raw else {}
                outer.requests.append({"path": self.path, "payload": payload})
                self._respond({"ok": True, "result": {"message_id": len(outer.requests)}})

            def do_GET(self):
                outer.requests.append({"path": self.path, "payload": None})
                if "getMe" in self.path:
                    self._respond({"ok": True, "result": {"username": "mockbot"}})
                elif "getUpdates" in self.path:
                    if not outer.updates_served:
                        outer.updates_served = True
                        result = [{"update_id": 1,
                                   "message": {"chat": {"id": outer.chat_id, "type": "private"}}}]
                    else:
                        result = []
                    self._respond({"ok": True, "result": result})
                else:
                    self._respond({"ok": True, "result": {}})

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = "http://127.0.0.1:%d" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_cfg(tmpdir, telegram, allowed=("42",)):
    return {
        "token": "TESTTOKEN",
        "chat_ids": ["42"],
        "allowed": set(allowed),
        "api_base": telegram.base_url,
        "tg_timeout": 10,
        "cache_dir": Path(tmpdir),
        "ttl_seconds": 6 * 3600,
        "http_timeout": 30,
        "report_times": [(8, 0), (20, 0)],
        "report_enabled": False,
        "coin": "BTC",
        "tf": "24h",
        "exchanges": [{"slug": "binance-cex", "name": "Binance"}],
    }


class ScheduleTests(unittest.TestCase):
    def test_parse_report_times(self):
        self.assertEqual(ob.parse_report_times("08:00,20:00"), [(8, 0), (20, 0)])
        self.assertEqual(ob.parse_report_times("8, 20"), [(8, 0), (20, 0)])
        self.assertEqual(ob.parse_report_times("20:30"), [(20, 30)])
        self.assertEqual(ob.parse_report_times("rác"), [(8, 0), (20, 0)])
        self.assertEqual(ob.parse_report_times("25:00"), [(8, 0), (20, 0)])

    def test_next_run_default_twice_a_day(self):
        import datetime
        cfg = {"report_times": [(8, 0), (20, 0)]}
        tz = ob.local_tz()

        def at(hour, minute):
            return datetime.datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)

        cases = [(7, 0, 8, 0, 0), (9, 0, 20, 0, 0), (20, 0, 8, 0, 1), (21, 30, 8, 0, 1)]
        for hour, minute, expect_h, expect_m, plus_days in cases:
            now = at(hour, minute)
            nxt = ob.next_run(cfg, now=now)
            self.assertEqual((nxt.hour, nxt.minute), (expect_h, expect_m), (hour, minute))
            self.assertEqual((nxt.date() - now.date()).days, plus_days, (hour, minute))


class MathTests(unittest.TestCase):
    def test_coin_qty_groups_wrapped_tokens(self):
        tokens = {"BTC": 1, "WBTC": 2, "cbBTC": 3, "ETH": 5, "WETH": 1}
        self.assertAlmostEqual(ob.coin_qty(tokens, "BTC"), 6.0)
        self.assertAlmostEqual(ob.coin_qty(tokens, "ETH"), 6.0)
        self.assertTrue(ob.coin_tracked(tokens, "BTC"))
        self.assertFalse(ob.coin_tracked(tokens, "SOL"))

    def test_flows_and_aggregate(self):
        records = {"binance-cex": rising_btc_snapshots()}
        rows = ob.coin_rows(records, "BTC")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["flow"]["24h"], 10.0)
        self.assertAlmostEqual(row["flow"]["7d"], 70.0)
        self.assertAlmostEqual(row["flow"]["30d"], 300.0)
        self.assertAlmostEqual(row["hold"], 1300.0)
        self.assertEqual(len(row["daily"]), 7)  # keep_days=7 -> 7 delta giữa 8 ngày

        agg = ob.aggregate(rows, "24h")
        self.assertAlmostEqual(agg["holdings"], 1300.0)
        self.assertAlmostEqual(agg["rise"], 10.0)
        self.assertAlmostEqual(agg["fall"], 0.0)
        self.assertAlmostEqual(agg["net"], 10.0)

    def test_short_history_marks_30d_missing(self):
        records = {"binance-cex": rising_btc_snapshots(days=5)}
        row = ob.coin_rows(records, "BTC")[0]
        self.assertIsNone(row["flow"]["30d"])
        self.assertEqual(len(row["daily"]), 4)

    def test_tiny_flow_is_not_significant(self):
        row = {"hold": 140852.0, "flow": {"24h": 0.02}}
        self.assertFalse(ob.is_significant(row, "24h"))
        row_big = {"hold": 140852.0, "flow": {"24h": 200.0}}
        self.assertTrue(ob.is_significant(row_big, "24h"))

    def test_window_hours_from_snapshots(self):
        records = {"binance-cex": rising_btc_snapshots()}
        rows = ob.coin_rows(records, "BTC")
        self.assertAlmostEqual(ob.window_hours(rows), 24.0)


MIDNIGHT = (BASE_TS // DAY) * DAY  # mốc 00:00 UTC cố định


class AnchorTests(unittest.TestCase):
    """Mốc 24h/7d/30d phải neo vào snapshot chốt ngày 00:00 UTC."""

    def test_24h_uses_day_close_not_last_intraday_snapshot(self):
        snapshots = [
            {"date": MIDNIGHT, "tokens": {"BTC": 1000.0}},
            {"date": MIDNIGHT + DAY, "tokens": {"BTC": 1010.0}},
            {"date": MIDNIGHT + 2 * DAY, "tokens": {"BTC": 1020.0}},
            # snapshot lẻ trong ngày: dự trữ nhảy mạnh, không được tính vào mốc 24h
            {"date": MIDNIGHT + 2 * DAY + 12 * 3600, "tokens": {"BTC": 1500.0}},
        ]
        row = ob.coin_rows({"binance-cex": snapshots}, "BTC")[0]
        self.assertAlmostEqual(row["hold"], 1500.0)          # dự trữ = snapshot mới nhất
        self.assertAlmostEqual(row["flow"]["24h"], 10.0)    # 1020 - 1010 (chốt ngày)
        self.assertAlmostEqual(ob.window_hours([row]), 24.0)

    def test_short_history_leaves_30d_empty(self):
        snapshots = [{"date": MIDNIGHT + idx * DAY, "tokens": {"BTC": 500.0 + idx}}
                     for idx in range(5)]
        row = ob.coin_rows({"binance-cex": snapshots}, "BTC")[0]
        self.assertAlmostEqual(row["flow"]["24h"], 1.0)
        self.assertIsNone(row["flow"]["30d"])

    def test_daily_series_one_bar_per_calendar_day(self):
        snapshots = [
            {"date": MIDNIGHT, "tokens": {"BTC": 100.0}},
            {"date": MIDNIGHT + DAY, "tokens": {"BTC": 110.0}},
            {"date": MIDNIGHT + DAY + 6 * 3600, "tokens": {"BTC": 90.0}},
            {"date": MIDNIGHT + 2 * DAY, "tokens": {"BTC": 120.0}},
        ]
        row = ob.coin_rows({"binance-cex": snapshots}, "BTC", keep_days=7)[0]
        diffs = [round(item["diff"], 2) for item in row["daily"]]
        # ngày 1: 90 (snapshot cuối ngày) - 100; ngày 2: 120 - 90
        self.assertEqual(diffs, [-10.0, 30.0])

    def test_exchange_list_uses_real_cex_slugs(self):
        slugs = [ex["slug"] for ex in ob.EXCHANGES]
        self.assertIn("kucoin", slugs)
        self.assertNotIn("coinbase-bridge", slugs)
        self.assertEqual(len(slugs), 8)


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.records = {"binance-cex": rising_btc_snapshots()}
        self.prices = {"BTC": 80000.0, "USDT": 1.0}

    def test_report_contains_expected_numbers(self):
        blocks = ob.build_report(self.records, "BTC", "24h", self.prices)
        text = "\n\n".join(blocks)
        self.assertIn("DỰ TRỮ ON-CHAIN", text)
        self.assertIn("+10.0 BTC", text)
        self.assertIn("1,300 BTC", text)
        self.assertIn("Binance", text)
        self.assertLessEqual(len(ob.pack_blocks(blocks)[0]), 4096)

    def test_html_tags_balanced(self):
        text = "\n\n".join(ob.build_report(self.records, "BTC", "24h", self.prices))
        for open_tag, close_tag in [("<b>", "</b>"), ("<i>", "</i>"),
                                    ("<pre>", "</pre>"), ("<code>", "</code>")]:
            self.assertEqual(text.count(open_tag), text.count(close_tag), open_tag)

    def test_pack_blocks_keeps_limit_and_content(self):
        blocks = ["x" * 1500 for _ in range(4)] + ["y" * 900]
        chunks = ob.pack_blocks(blocks, limit=3900)
        self.assertTrue(all(len(chunk) <= 3900 for chunk in chunks))
        self.assertEqual(len(chunks), 3)  # 1500+2+1500 = 3002 mỗi tin, còn 1 tin cuối
        for block in blocks:
            self.assertIn(block, "\n\n".join(chunks))

    def test_number_formatting(self):
        self.assertEqual(ob.fmt_coin(1234.5, "BTC", True), "+1,234 BTC")
        self.assertEqual(ob.fmt_coin(-1.2345, "ETH"), "-1.23 ETH")
        self.assertEqual(ob.fmt_coin(1.5e9, "USDT"), "1.50B USDT")
        self.assertEqual(ob.fmt_usd(2.5e6), "$2.50M")
        self.assertEqual(ob.fmt_short(2500000, "SOL"), "+2.50M")
        self.assertEqual(ob.fmt_short(1500, "BTC", show_sign=False), "1.5K")

    def test_tf_variants_build(self):
        for tf in ("24h", "7d", "30d"):
            blocks = ob.build_report(self.records, "BTC", tf, self.prices)
            self.assertTrue(blocks)
        self.assertTrue(ob.build_top(self.records, "BTC", "24h"))
        self.assertTrue(ob.build_days(self.records, "BTC"))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.snaps = rising_btc_snapshots()

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_read_expiry_force(self):
        ob.write_cache(self.dir, "binance-cex", self.snaps)
        fresh = ob.read_cache(self.dir, "binance-cex", 3600)
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh[0], self.snaps)
        self.assertIsNone(ob.read_cache(self.dir, "binance-cex", 0))
        self.assertIsNone(ob.read_cache(self.dir, "binance-cex", 3600, force_refresh=True))
        stale = ob.read_cache(self.dir, "binance-cex", None)
        self.assertIsNotNone(stale)

    def test_load_all_uses_cache(self):
        ob.write_cache(self.dir, "binance-cex", self.snaps)
        cfg = {"cache_dir": self.dir, "ttl_seconds": 3600,
               "http_timeout": 1, "exchanges": [{"slug": "binance-cex", "name": "Binance"}]}
        records = ob.load_all(cfg)
        self.assertIn("binance-cex", records)
        self.assertEqual(records["binance-cex"], self.snaps)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        ob.write_cache(self.dir, "binance-cex", rising_btc_snapshots())
        self.telegram = FakeTelegram()
        self.cfg = make_cfg(self.dir, self.telegram)
        self.prices = {"BTC": 80000.0, "USDT": 1.0}

    def tearDown(self):
        self.telegram.close()
        self.tmp.cleanup()

    def send(self, text, chat_id=42):
        ob.handle_command(self.cfg, {"chat": {"id": chat_id}, "text": text}, self.prices)

    def test_any_message_returns_report(self):
        self.send("xem giúp anh cái BTC")
        self.assertEqual(len(self.telegram.requests), 1)
        payload = self.telegram.requests[0]["payload"]
        self.assertEqual(payload["chat_id"], 42)
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("DỰ TRỮ ON-CHAIN", payload["text"])

    def test_unknown_chat_is_ignored(self):
        self.send("hello", chat_id=999)
        self.assertEqual(self.telegram.requests, [])

    def test_id_works_for_any_chat(self):
        self.send("/id", chat_id=999)
        self.assertEqual(len(self.telegram.requests), 1)
        self.assertIn("999", self.telegram.requests[0]["payload"]["text"])

    def test_help_and_unknown_command(self):
        self.send("/help")
        self.send("/khongco")
        texts = [req["payload"]["text"] for req in self.telegram.requests]
        self.assertIn("🤖 Bot dự trữ on-chain CEX", texts[0])
        self.assertIn("Lệnh không hợp lệ", texts[1])

    def test_set_timeframe_persists(self):
        self.send("/tf 7d")
        self.assertEqual(self.cfg["tf"], "7d")
        state = json.loads((self.dir / "state.json").read_text())
        self.assertEqual(state["tf"], "7d")

    def test_bad_coin_hint(self):
        self.send("/coin DOGE")
        self.assertIn("Coin hợp lệ", self.telegram.requests[0]["payload"]["text"])

    def test_coin_switch_builds_report(self):
        self.send("/coin ETH")
        self.assertEqual(self.cfg["coin"], "ETH")
        self.assertTrue(self.telegram.requests)

    def test_now_command_uses_cache(self):
        self.send("/now")
        self.assertEqual(len(self.telegram.requests), 1)
        self.assertIn("DỰ TRỮ ON-CHAIN", self.telegram.requests[0]["payload"]["text"])


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name in ("install.sh", "onchain_bot.py", ".env.example"):
            shutil.copy2(Path(__file__).resolve().parent / name, self.dir / name)
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("không có bash")

    def tearDown(self):
        self.tmp.cleanup()

    def run_installer(self, args, stdin=subprocess.DEVNULL, extra_env=None):
        env = dict(os.environ)
        env.update({
            "SKIP_TOKEN_CHECK": "1",
            "SYSTEMCTL": "echo",
            "UNIT_DIR": str(self.dir / "units"),
            "CHAT_WAIT_SECONDS": "0",
        })
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [self.bash, "install.sh", *args],
            cwd=str(self.dir), env=env, stdin=stdin,
            capture_output=True, text=True, timeout=120,
        )

    def test_install_writes_env_and_unit(self):
        result = self.run_installer(["--token", "123456789:AAfake", "--chat", "4242"])
        self.assertEqual(result.returncode, 0, result.stderr)
        env_text = (self.dir / ".env").read_text()
        self.assertIn("TELEGRAM_BOT_TOKEN=123456789:AAfake", env_text)
        self.assertIn("TELEGRAM_CHAT_ID=4242", env_text)
        self.assertIn("ALLOWED_CHAT_IDS=4242", env_text)
        self.assertEqual(oct((self.dir / ".env").stat().st_mode)[-3:], "600")
        unit = (self.dir / "units" / "onchain-bot.service").read_text()
        workdir = next(line.split("=", 1)[1] for line in unit.splitlines()
                       if line.startswith("WorkingDirectory="))
        execstart = next(line.split("=", 1)[1] for line in unit.splitlines()
                         if line.startswith("ExecStart="))
        # macOS có symlink /var -> /private/var nên so sánh theo đường dẫn thực
        self.assertEqual(Path(workdir).resolve(), self.dir.resolve())
        self.assertTrue(Path(execstart.split()[-1]).resolve().name == "onchain_bot.py")

    def test_install_is_idempotent(self):
        args = ["--token", "123456789:AAfake", "--chat", "4242"]
        self.assertEqual(self.run_installer(args).returncode, 0)
        self.assertEqual(self.run_installer(args).returncode, 0)
        env_text = (self.dir / ".env").read_text()
        self.assertEqual(env_text.count("TELEGRAM_BOT_TOKEN="), 1)
        self.assertEqual(env_text.count("TELEGRAM_CHAT_ID="), 1)

    def test_missing_token_fails_clearly(self):
        result = self.run_installer([])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Chưa có token", result.stdout + result.stderr)

    def test_bad_token_format_rejected(self):
        result = self.run_installer(["--token", "khong-phai-token"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("sai định dạng", result.stdout + result.stderr)

    def test_auto_detects_chat_id_from_message(self):
        import pty
        telegram = FakeTelegram(chat_id=777)
        self.addCleanup(telegram.close)
        master, slave = pty.openpty()
        try:
            result = self.run_installer(
                ["--token", "123456789:AAfake"],
                stdin=slave,
                extra_env={"TELEGRAM_API_BASE": telegram.base_url, "CHAT_WAIT_SECONDS": "5"},
            )
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(result.returncode, 0, result.stderr)
        env_text = (self.dir / ".env").read_text()
        self.assertIn("TELEGRAM_CHAT_ID=777", env_text)
        self.assertIn("ALLOWED_CHAT_IDS=777", env_text)


@unittest.skipUnless(os.environ.get("RUN_LIVE") == "1", "đặt RUN_LIVE=1 để test dữ liệu thật")
class LiveTests(unittest.TestCase):
    def test_fetch_real_exchange(self):
        snapshots = ob.fetch_snapshots("kucoin", timeout=60, keep=90)
        self.assertGreaterEqual(len(snapshots), 2)
        self.assertIn("tokens", snapshots[-1])
        rows = ob.coin_rows({"kucoin": snapshots}, "BTC")
        self.assertTrue(rows and rows[0]["hold"] > 0)

    def test_fetch_live_prices(self):
        prices = ob.fetch_prices(30)
        self.assertGreater(prices.get("BTC", 0), 100)
        self.assertEqual(prices.get("USDT"), 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
