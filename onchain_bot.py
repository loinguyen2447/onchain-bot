#!/usr/bin/env python3
"""Bot Telegram: dòng tiền on-chain của 8 sàn CEX (DefiLlama Proof-of-Reserves).

Cơ chế tính giống section "Lượng Coin chuyển lên sàn & rút khỏi sàn" của web:
    flow = reserve(mới nhất) - reserve(mốc cũ)
      > 0  -> coin chuyển LÊN sàn (inflow)
      < 0  -> coin RÚT khỏi sàn (outflow)
Dữ liệu là chênh lệch số dư ví do DefiLlama tổng hợp, KHÔNG phải số lệnh chuyển.

Chỉ dùng thư viện chuẩn của Python 3.9+ (không cần pip install), chạy tốt trên
laptop Linux Mint.

Cách dùng nhanh:
    cp .env.example .env                # điền TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    python3 onchain_bot.py --dry-run    # in báo cáo ra terminal, chưa gửi
    python3 onchain_bot.py              # chạy bot: trả lời lệnh + báo cáo hằng ngày
    python3 onchain_bot.py --once       # gửi 1 lần rồi thoát (dùng cho cron)
"""

import argparse
import collections
import datetime
import gzip
import html
import json
import logging
import os
import re
import resource
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("onchain-bot")

# ---------------------------------------------------------------------------
# Cấu hình dữ liệu (khớp với assets/js/onchain.js của web)
# ---------------------------------------------------------------------------

# Gộp wrapped/pegged token về một coin chuẩn, y như extractTokenQtyFromMap().
COIN_GROUPS = {
    "BTC": ("BTC", "WBTC", "BTCB", "CBBTC", "cbBTC"),
    "ETH": ("ETH", "WETH", "STETH", "WBETH", "BETH"),
    "SOL": ("SOL", "BNSOL", "JITOSOL"),
    "USDT": ("USDT", "USDC", "USDT0", "FDUSD"),
    "XRP": ("XRP", "WXRP"),
    "BNB": ("BNB", "WBNB"),
}
COINS = ("BTC", "ETH", "SOL", "USDT", "XRP", "BNB")

# 8 sàn lớn, dùng đúng slug DefiLlama như web.
EXCHANGES = [
    {"slug": "binance-cex", "name": "Binance"},
    {"slug": "okx", "name": "OKX"},
    {"slug": "coinbase-bridge", "name": "Coinbase"},
    {"slug": "bybit", "name": "Bybit"},
    {"slug": "bitfinex", "name": "Bitfinex"},
    {"slug": "robinhood", "name": "Robinhood"},
    {"slug": "bitget", "name": "Bitget"},
    {"slug": "gate", "name": "Gate.io"},
]

COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "BNB": "binancecoin",
}
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "BNB": "BNBUSDT",
}

TF_LABEL = {"24h": "24h", "7d": "7 ngày", "30d": "30 ngày"}
TF_DAYS = {"24h": 1, "7d": 7, "30d": 30}
KEEP_SNAPSHOTS = 40   # đủ cho mốc 30 ngày + 7 cột ngày
DAILY_POINTS = 8      # 8 snapshot -> 7 cột ngày
MAX_MESSAGE = 3900    # Telegram giới hạn 4096 ký tự, chừa biên an toàn
USER_AGENT = "wedtaichinh-onchain-bot/1.1 (+linux mint)"


# ---------------------------------------------------------------------------
# Cấu hình runtime
# ---------------------------------------------------------------------------

def load_dotenv(path):
    """Đọc file .env đơn giản (KEY=VALUE). Biến môi trường đã có thì giữ nguyên."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


def parse_report_times(raw):
    """Đọc danh sách giờ gửi báo cáo: "08:00,20:00" (hoặc "8,20")."""
    times = set()
    for chunk in re.split(r"[,;\s]+", (raw or "").strip()):
        if not chunk:
            continue
        match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", chunk)
        if not match:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.add((hour, minute))
    return sorted(times) or [(8, 0), (20, 0)]


def select_exchanges(raw):
    """Cho phép giới hạn số sàn qua biến EXCHANGES (mặc định: cả 8)."""
    if not raw or not raw.strip():
        return list(EXCHANGES)
    wanted = {item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()}
    chosen = [ex for ex in EXCHANGES
              if ex["slug"].lower() in wanted or ex["name"].lower() in wanted]
    return chosen or list(EXCHANGES)


def load_config():
    load_dotenv(BASE_DIR / ".env")

    cache_dir = Path(os.environ.get("CACHE_DIR") or (BASE_DIR / "cache"))
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    raw_chats = (os.environ.get("TELEGRAM_CHAT_ID") or "").replace(";", ",")
    chat_ids = [c.strip() for c in raw_chats.split(",") if c.strip()]

    raw_allowed = (os.environ.get("ALLOWED_CHAT_IDS") or "").replace(";", ",")
    allowed = [c.strip() for c in raw_allowed.split(",") if c.strip()]
    if not allowed:
        allowed = list(chat_ids)

    return {
        "token": token,
        "chat_ids": chat_ids,
        "allowed": set(allowed),
        "api_base": (os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/"),
        "tg_timeout": env_int("TELEGRAM_TIMEOUT", 75),
        "cache_dir": cache_dir,
        "ttl_seconds": max(60, env_int("CACHE_TTL_HOURS", 6) * 3600),
        "http_timeout": env_int("HTTP_TIMEOUT", 300),
        # Mặc định 8:00 sáng và 20:00 tối (giờ Việt Nam).
        "report_times": parse_report_times(
            os.environ.get("REPORT_TIMES")
            or ("%02d:%02d" % (env_int("REPORT_HOUR", 8), env_int("REPORT_MINUTE", 0))
                if os.environ.get("REPORT_HOUR") else "08:00,20:00")
        ),
        "report_enabled": (os.environ.get("REPORT_ENABLED", "1").strip() not in ("0", "false", "no")),
        "coin": (os.environ.get("DEFAULT_COIN") or "BTC").upper(),
        "tf": (os.environ.get("DEFAULT_TF") or "24h").lower(),
        "exchanges": select_exchanges(os.environ.get("EXCHANGES")),
    }


# ---------------------------------------------------------------------------
# HTTP + lấy dữ liệu DefiLlama
# ---------------------------------------------------------------------------

def http_get(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        if "gzip" in (response.headers.get("Content-Encoding") or "").lower():
            raw = gzip.decompress(raw)
    return raw


def http_get_json(url, timeout, attempts=2):
    last_error = None
    for attempt in range(attempts):
        try:
            return json.loads(http_get(url, timeout).decode("utf-8"))
        except Exception as exc:  # mạng chập chờn thì thử lại 1 lần
            last_error = exc
            if attempt + 1 < attempts:
                LOG.warning("lỗi tải (%s) — thử lại sau 3s", exc)
                time.sleep(3)
    raise last_error


def fetch_snapshots(slug, timeout, keep=KEEP_SNAPSHOTS):
    """Tải lịch sử dự trữ của một sàn, chỉ giữ `keep` snapshot cuối."""
    data = http_get_json("https://api.llama.fi/protocol/%s" % slug, timeout)
    tokens = data.get("tokens")
    if not isinstance(tokens, list) or len(tokens) < 2:
        raise RuntimeError("phản hồi không có mảng 'tokens'")
    snapshots = [item for item in tokens[-keep:]
                 if isinstance(item, dict) and "date" in item and "tokens" in item]
    if len(snapshots) < 2:
        raise RuntimeError("không đủ snapshot để tính flow")
    LOG.info("  %-14s %d snapshot", slug, len(snapshots))
    return snapshots


# ---------------------------------------------------------------------------
# Cache trên đĩa
# ---------------------------------------------------------------------------

def cache_path(cache_dir, slug):
    return cache_dir / ("%s.json" % slug)


def read_cache(cache_dir, slug, ttl_seconds, force_refresh=False):
    path = cache_path(cache_dir, slug)
    if force_refresh or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    snapshots = payload.get("snapshots") or []
    fetched_at = float(payload.get("fetched_at") or 0)
    if len(snapshots) < 2:
        return None
    if ttl_seconds is not None and time.time() - fetched_at > ttl_seconds:
        return None
    return snapshots, fetched_at


def write_cache(cache_dir, slug, snapshots):
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(cache_dir, slug)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "slug": slug,
        "fetched_at": time.time(),
        "snapshots": snapshots,
    }), encoding="utf-8")
    os.replace(tmp, path)


FETCH_LOCK = threading.Lock()


def load_all(cfg, force_refresh=False, allow_stale=False):
    """Trả về {slug: [snapshots...]} theo thứ tự thời gian.

    allow_stale=True: dùng cache cũ thay vì bắt người dùng chờ tải lại.
    """
    with FETCH_LOCK:
        cfg["cache_dir"].mkdir(parents=True, exist_ok=True)
        records = {}
        for ex in cfg["exchanges"]:
            slug = ex["slug"]
            cached = None if force_refresh else read_cache(cfg["cache_dir"], slug, cfg["ttl_seconds"])
            if cached:
                snapshots, fetched_at = cached
                LOG.info("  %-14s cache (%.1f giờ trước)", slug, (time.time() - fetched_at) / 3600.0)
                records[slug] = snapshots
                continue

            stale = read_cache(cfg["cache_dir"], slug, None)
            if stale and allow_stale and not force_refresh:
                LOG.info("  %-14s cache cũ (sẽ làm mới ở nền)", slug)
                records[slug] = stale[0]
                continue

            LOG.info("  %-14s đang tải từ DefiLlama…", slug)
            try:
                snapshots = fetch_snapshots(slug, cfg["http_timeout"])
            except Exception as exc:
                if stale:
                    LOG.warning("  %-14s lỗi (%s) — dùng cache cũ", slug, exc)
                    records[slug] = stale[0]
                    continue
                LOG.error("  %-14s lỗi và không có cache: %s", slug, exc)
                continue
            write_cache(cfg["cache_dir"], slug, snapshots)
            records[slug] = snapshots

        if not records:
            raise RuntimeError("không tải được dữ liệu on-chain cho sàn nào")
        return records


# ---------------------------------------------------------------------------
# Tính toán
# ---------------------------------------------------------------------------

def coin_qty(tokens_map, coin):
    return sum(float(tokens_map.get(key) or 0.0) for key in COIN_GROUPS.get(coin, (coin,)))


def coin_tracked(tokens_map, coin):
    return any(key in tokens_map for key in COIN_GROUPS.get(coin, (coin,)))


def coin_rows(records, coin, keep_days=7, exchanges=None):
    """Mỗi sàn một dòng: dự trữ hiện tại + flow 24h/7d/30d + chuỗi ngày."""
    rows = []
    for ex in (exchanges or EXCHANGES):
        snapshots = records.get(ex["slug"])
        if not snapshots or len(snapshots) < 2:
            continue
        latest = snapshots[-1]
        if not coin_tracked(latest.get("tokens") or {}, coin):
            continue
        now_qty = coin_qty(latest["tokens"], coin)
        row = {
            "slug": ex["slug"],
            "name": ex["name"],
            "hold": now_qty,
            "flow": {},
            "daily": [],
            "ts": latest.get("date"),
            "prev_ts": snapshots[-2].get("date"),
        }
        for days, key in ((1, "24h"), (7, "7d"), (30, "30d")):
            if len(snapshots) >= days + 1:
                row["flow"][key] = now_qty - coin_qty(snapshots[-1 - days]["tokens"], coin)
            else:
                row["flow"][key] = None
        tail = snapshots[-keep_days:]
        for idx in range(1, len(tail)):
            row["daily"].append({
                "date": tail[idx].get("date"),
                "diff": coin_qty(tail[idx]["tokens"], coin) - coin_qty(tail[idx - 1]["tokens"], coin),
            })
        rows.append(row)
    rows.sort(key=lambda r: (r["flow"].get("24h") or 0), reverse=True)
    return rows


def aggregate(rows, tf):
    inflow = 0.0
    outflow = 0.0
    holdings = 0.0
    for row in rows:
        flow = row["flow"].get(tf)
        holdings += row["hold"]
        if flow is None:
            continue
        if flow > 0:
            inflow += flow
        elif flow < 0:
            outflow += -flow
    return {
        "holdings": holdings,
        "inflow": inflow,
        "outflow": outflow,
        "net": inflow - outflow,
        "tracked": len(rows),
    }


def window_hours(rows):
    """Số giờ thực tế mà cột '24h' bao phủ (snapshot đóng ngày -> snapshot mới nhất)."""
    spans = []
    for row in rows:
        try:
            spans.append((float(row["ts"]) - float(row["prev_ts"])) / 3600.0)
        except (TypeError, ValueError):
            continue
    return max(spans) if spans else None


def snapshot_time(rows):
    stamps = [row["ts"] for row in rows if row.get("ts")]
    return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# Giá coin (USD)
# ---------------------------------------------------------------------------

def fetch_prices(timeout):
    """Giá spot cho mọi coin trừ USDT (USDT = 1). Lỗi -> {} (không bịa giá)."""
    prices = {}
    ids = ",".join("coingecko:%s" % cid for cid in COINGECKO_IDS.values())
    try:
        payload = http_get_json("https://coins.llama.fi/prices/current/%s" % ids, timeout)
        for coin, cid in COINGECKO_IDS.items():
            price = ((payload.get("coins") or {}).get("coingecko:%s" % cid) or {}).get("price")
            if price:
                prices[coin] = float(price)
    except Exception as exc:
        LOG.warning("không lấy được giá từ DefiLlama: %s", exc)

    missing = [coin for coin in COINGECKO_IDS if coin not in prices]
    if missing:
        try:
            symbols = [BINANCE_SYMBOLS[coin] for coin in missing]
            query = urllib.request.quote(json.dumps(symbols))
            url = "https://api.binance.com/api/v3/ticker/24hr?symbols=%s" % query
            rows = http_get_json(url, timeout)
            by_symbol = {row["symbol"]: float(row["lastPrice"]) for row in rows}
            for coin in missing:
                price = by_symbol.get(BINANCE_SYMBOLS[coin])
                if price:
                    prices[coin] = price
        except Exception as exc:
            LOG.warning("không lấy được giá từ Binance: %s", exc)

    prices["USDT"] = 1.0
    return prices


# ---------------------------------------------------------------------------
# Định dạng số
# ---------------------------------------------------------------------------

def fmt_coin(value, coin, show_sign=False):
    if value is None:
        return "—"
    if show_sign:
        sign = "+" if value > 0 else ("-" if value < 0 else "")
    else:
        sign = "-" if value < 0 else ""
    amount = abs(value)
    if coin == "USDT":
        if amount >= 1e9:
            return "%s%.2fB USDT" % (sign, amount / 1e9)
        if amount >= 1e6:
            return "%s%.2fM USDT" % (sign, amount / 1e6)
        return "%s%s USDT" % (sign, format(round(amount), ","))
    if amount >= 1e6:
        return "%s%.2fM %s" % (sign, amount / 1e6, coin)
    if amount >= 1000:
        return "%s%s %s" % (sign, format(round(amount), ","), coin)
    if amount >= 10:
        return "%s%.1f %s" % (sign, amount, coin)
    return "%s%.2f %s" % (sign, amount, coin)


def fmt_short(value, coin, show_sign=True):
    """Số gọn không kèm đơn vị, dùng cho bảng nhiều coin."""
    if value is None:
        return "—"
    if show_sign:
        sign = "+" if value > 0 else ("-" if value < 0 else "")
    else:
        sign = "-" if value < 0 else ""
    amount = abs(value)
    if coin == "USDT":
        if amount >= 1e9:
            return "%s%.2fB" % (sign, amount / 1e9)
        if amount >= 1e6:
            return "%s%.1fM" % (sign, amount / 1e6)
        if amount >= 1e3:
            return "%s%.0fK" % (sign, amount / 1e3)
        return "%s%.0f" % (sign, amount)
    if amount >= 1e6:
        return "%s%.2fM" % (sign, amount / 1e6)
    if amount >= 1e3:
        return "%s%.1fK" % (sign, amount / 1e3)
    if amount >= 10:
        return "%s%.0f" % (sign, amount)
    return "%s%.2f" % (sign, amount)


def fmt_usd(value):
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1e9:
        return "%s$%.2fB" % (sign, amount / 1e9)
    if amount >= 1e6:
        return "%s$%.2fM" % (sign, amount / 1e6)
    if amount >= 1e3:
        return "%s$%.1fK" % (sign, amount / 1e3)
    return "%s$%s" % (sign, format(round(amount), ","))


def fmt_price(value):
    if value is None:
        return "—"
    if value >= 1000:
        return "$%s" % format(round(value, 2), ",")
    if value >= 10:
        return "$%.2f" % value
    return "$%.4f" % value


# ---------------------------------------------------------------------------
# Thời gian
# ---------------------------------------------------------------------------

def local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("TZ_NAME") or "Asia/Ho_Chi_Minh")
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=7))


def vn_time(timestamp):
    if timestamp:
        moment = datetime.datetime.fromtimestamp(float(timestamp), local_tz())
    else:
        moment = datetime.datetime.now(local_tz())
    return moment.strftime("%d/%m/%Y %H:%M")


def vn_day(timestamp):
    try:
        return datetime.datetime.fromtimestamp(float(timestamp), local_tz()).strftime("%d/%m")
    except (TypeError, ValueError):
        return "  ??  "


# ---------------------------------------------------------------------------
# Nội dung tin nhắn
# ---------------------------------------------------------------------------

def is_significant(row, tf):
    """Biến động đủ lớn để hiển thị (tránh 0.02 BTC được tính là "nạp ròng")."""
    flow = row["flow"].get(tf)
    if flow is None:
        return False
    return abs(flow) >= max(0.01, row["hold"] * 1e-6)


def signal_line(coin, net):
    if coin == "USDT":
        if net >= 0:
            return "🟢 Nạp ròng stablecoin → sức mua chuẩn bị vào sàn tăng"
        return "🟠 Rút ròng stablecoin khỏi sàn → bớt sức mua tại chỗ"
    if net <= 0:
        return "🟢 Rút ròng khỏi sàn → dấu hiệu cá voi gom tích lũy"
    return "🔴 Nạp ròng lên sàn → cảnh báo áp lực xả"


def price_of(prices, coin):
    if coin == "USDT":
        return 1.0
    return prices.get(coin)


def usd_note(value, coin, prices):
    price = price_of(prices, coin)
    if not price or value is None:
        return ""
    return " (~%s)" % fmt_usd(value * price)


def build_report(records, coin, tf, prices, exchanges=None):
    """Trả về danh sách block HTML (mỗi block tự chứa thẻ, ghép vừa 4096 ký tự)."""
    rows = coin_rows(records, coin, exchanges=exchanges)
    stats = aggregate(rows, tf)
    net = stats["net"]
    latest = snapshot_time(rows)
    hours = window_hours(rows)
    price = price_of(prices, coin)

    blocks = []
    header = [
        "🏦 <b>DÒNG TIỀN ON-CHAIN · %d SÀN CEX</b>" % len(exchanges or EXCHANGES),
        "<i>DefiLlama Proof-of-Reserves · khung %s</i>" % TF_LABEL[tf],
        "🕒 Snapshot: <b>%s</b> (giờ VN)" % vn_time(latest),
    ]
    if tf == "24h" and hours:
        header.append("<i>Cửa sổ thực tế ≈ %.1f giờ (snapshot đóng ngày → mới nhất)</i>" % hours)
    blocks.append("\n".join(header))

    overview = [
        "<b>💰 %s — tổng hợp %d sàn</b>" % (coin, stats["tracked"]),
        "• Dự trữ: <b>%s</b>%s" % (fmt_coin(stats["holdings"], coin), usd_note(stats["holdings"], coin, prices)),
        "• ▲ Nạp lên sàn: <b>%s</b>%s" % (fmt_coin(stats["inflow"], coin, True), usd_note(stats["inflow"], coin, prices)),
        "• ▼ Rút khỏi sàn: <b>%s</b>%s" % (fmt_coin(-stats["outflow"], coin, True), usd_note(stats["outflow"], coin, prices)),
        "• 🧭 Netflow: <b>%s</b>%s" % (fmt_coin(net, coin, True), usd_note(net, coin, prices)),
        signal_line(coin, net),
    ]
    if price and coin != "USDT":
        overview.append("<i>Giá quy đổi: %s</i>" % fmt_price(price))
    blocks.append("\n".join(overview))

    # Bảng từng sàn, canh cột bằng <pre>.
    name_width = max([len(row["name"]) for row in rows] + [6])
    body = []
    for row in rows:
        flow = row["flow"].get(tf)
        significant = is_significant(row, tf)
        arrow = "—" if not is_significant(row, tf) else ("▲" if flow > 0 else "▼")
        body.append("%s  %s %10s  %12s" % (
            row["name"].ljust(name_width),
            arrow,
            fmt_short(flow, coin, show_sign=significant),
            fmt_short(row["hold"], coin, show_sign=False),
        ))
    table = "<b>🏦 Theo sàn (%s)</b>\n<pre>%s\n%s</pre>" % (
        TF_LABEL[tf],
        "Sàn".ljust(name_width) + "    " + "Flow".rjust(10) + "  " + "Dự trữ".rjust(12),
        "\n".join(body),
    )
    blocks.append(table)

    meaningful = [r for r in rows if is_significant(r, tf)]
    if meaningful:
        top_in = max(meaningful, key=lambda r: r["flow"][tf])
        top_out = min(meaningful, key=lambda r: r["flow"][tf])
        movers = ["<b>⚡️ Biến động mạnh nhất (%s)</b>" % TF_LABEL[tf]]
        if (top_in["flow"].get(tf) or 0) > 0:
            movers.append("▲ %s: <b>%s</b>" % (html.escape(top_in["name"]),
                                               fmt_coin(top_in["flow"][tf], coin, True)))
        if (top_out["flow"].get(tf) or 0) < 0:
            movers.append("▼ %s: <b>%s</b>" % (html.escape(top_out["name"]),
                                               fmt_coin(top_out["flow"][tf], coin, True)))
        if len(movers) > 1:
            blocks.append("\n".join(movers))

    # Netflow 6 coin để nhìn bức tranh tổng thể.
    other_lines = ["<b>🪙 Netflow các coin (%s)</b>" % TF_LABEL[tf]]
    for sym in COINS:
        sym_stats = aggregate(coin_rows(records, sym, exchanges=exchanges), tf)
        marker = " (đang xem)" if sym == coin else ""
        other_lines.append("%s: %s %s%s" % (sym, fmt_short(sym_stats["net"], sym), sym, marker))
    blocks.append("\n".join(other_lines))

    blocks.append(
        "<i>Inflow/Outflow = chênh lệch số dư ví on-chain, không phải số lệnh chuyển. "
        "Khung 24h tính từ snapshot đóng ngày gần nhất đến snapshot mới nhất nên có thể chưa đủ 24 giờ.</i>\n"
        "Gõ /help để xem lệnh."
    )
    return blocks


def build_days(records, coin, exchanges=None):
    rows = coin_rows(records, coin, keep_days=DAILY_POINTS, exchanges=exchanges)
    if not rows:
        return ["Chưa có dữ liệu cho %s." % coin]
    length = min(len(row["daily"]) for row in rows)
    if length < 1:
        return ["Chưa đủ dữ liệu ngày cho %s." % coin]
    totals = []
    for idx in range(-length, 0):
        totals.append({
            "ts": rows[0]["daily"][idx]["date"],
            "diff": sum(row["daily"][idx]["diff"] for row in rows),
        })
    peak = max(abs(item["diff"]) for item in totals) or 1.0
    lines = []
    previous_day = None
    for item in totals:
        bar_len = max(1, int(round(abs(item["diff"]) / peak * 12)))
        bar = ("█" if item["diff"] >= 0 else "▒") * bar_len
        label = vn_day(item["ts"])
        if label == previous_day:  # snapshot nội ngày trùng ngày với mốc trước
            label = "%s %02dh" % (label, datetime.datetime.fromtimestamp(
                float(item["ts"]), local_tz()).hour)
        previous_day = vn_day(item["ts"])
        lines.append("%s  %9s  %s" % (label.ljust(11), fmt_short(item["diff"], coin), bar))
    return [
        "<b>📅 %s · %d ngày gần nhất (tổng các sàn)</b>" % (coin, length),
        "<pre>%s</pre>" % "\n".join(lines),
        "<i>█ = nạp lên sàn, ▒ = rút khỏi sàn.</i>",
    ]


def build_top(records, coin, tf, exchanges=None):
    rows = coin_rows(records, coin, exchanges=exchanges)
    if not rows:
        return ["Chưa có dữ liệu cho %s." % coin]
    inflow = sorted([r for r in rows if (r["flow"].get(tf) or 0) > 0 and is_significant(r, tf)],
                    key=lambda r: r["flow"][tf], reverse=True)
    outflow = sorted([r for r in rows if (r["flow"].get(tf) or 0) < 0 and is_significant(r, tf)],
                     key=lambda r: r["flow"][tf])
    blocks = ["<b>🏆 Xếp hạng %s · %s</b>" % (coin, TF_LABEL[tf])]
    blocks.append("<b>▲ Nạp lên sàn nhiều nhất</b>\n" + (
        "\n".join("• %s: <b>%s</b>" % (html.escape(r["name"]), fmt_coin(r["flow"][tf], coin, True))
                  for r in inflow[:3]) or "<i>Không sàn nào nạp ròng.</i>"
    ))
    blocks.append("<b>▼ Rút khỏi sàn nhiều nhất</b>\n" + (
        "\n".join("• %s: <b>%s</b>" % (html.escape(r["name"]), fmt_coin(r["flow"][tf], coin, True))
                  for r in outflow[:3]) or "<i>Không sàn nào rút ròng.</i>"
    ))
    return blocks


HELP_TEXT = """<b>🤖 Bot dòng tiền on-chain CEX</b>
Theo dõi 8 sàn: Binance, OKX, Coinbase, Bybit, Bitfinex, Robinhood, Bitget, Gate.io — dữ liệu DefiLlama (Proof-of-Reserves).

<b>Lệnh:</b>
/now — báo cáo đầy đủ coin đang xem
/coin BTC|ETH|SOL|USDT|XRP|BNB — đổi coin
/btc /eth /sol /usdt /xrp /bnb — xem nhanh coin đó
/tf 24h|7d|30d — đổi khung thời gian
/top — xếp hạng nạp/rút ròng
/days — 7 ngày gần nhất (biểu đồ thanh)
/id — lấy chat id của bạn
/help — trợ giúp

<i>Gửi tin nhắn bất kỳ (không cần lệnh) để nhận báo cáo ngay.</i>
<i>Inflow/Outflow = chênh lệch số dư ví, không phải số lệnh chuyển.</i>"""


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def tg_api(cfg, method, payload):
    url = "%s/bot%s/%s" % (cfg["api_base"], cfg["token"], method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=cfg["tg_timeout"]) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError("Telegram %s lỗi: %s" % (method, body.get("description") or body))
    return body.get("result")


def pack_blocks(blocks, limit=MAX_MESSAGE):
    chunks = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return chunks


def send_blocks(cfg, chat_id, blocks):
    for chunk in pack_blocks(blocks):
        tg_api(cfg, "sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


# ---------------------------------------------------------------------------
# Trạng thái (coin/tf người dùng chọn)
# ---------------------------------------------------------------------------

def state_path(cfg):
    return cfg["cache_dir"] / "state.json"


def read_state(cfg):
    path = state_path(cfg)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("coin") in COINS:
        cfg["coin"] = data["coin"]
    if data.get("tf") in TF_LABEL:
        cfg["tf"] = data["tf"]


def save_state(cfg):
    cfg["cache_dir"].mkdir(parents=True, exist_ok=True)
    state_path(cfg).write_text(json.dumps({"coin": cfg["coin"], "tf": cfg["tf"]}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Bot: long polling
# ---------------------------------------------------------------------------

def is_allowed(cfg, chat_id):
    return str(chat_id) in cfg["allowed"]


def handle_command(cfg, message, prices):
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        # Người dùng gửi tin nhắn bất kỳ -> trả luôn báo cáo mới nhất.
        if not is_allowed(cfg, chat_id):
            LOG.warning("bỏ qua tin nhắn từ chat id lạ: %s", chat_id)
            return
        blocks = build_report(load_all(cfg, allow_stale=True), cfg["coin"], cfg["tf"], prices,
                              exchanges=cfg["exchanges"])
        send_blocks(cfg, chat_id, blocks)
        return

    parts = text.split()
    command = parts[0].split("@")[0].lower()
    args = parts[1:]

    # /id luôn trả lời để người dùng biết chat id lúc cài đặt.
    if command == "/id":
        tg_api(cfg, "sendMessage", {
            "chat_id": chat_id,
            "text": "Chat id của bạn: <code>%s</code>\nThêm vào TELEGRAM_CHAT_ID trong .env để nhận báo cáo." % chat_id,
            "parse_mode": "HTML",
        })
        return

    if not is_allowed(cfg, chat_id):
        LOG.warning("bỏ qua lệnh từ chat id lạ: %s", chat_id)
        tg_api(cfg, "sendMessage", {
            "chat_id": chat_id,
            "text": "⛔️ Chat id <code>%s</code> chưa được cấp quyền. Thêm id này vào ALLOWED_CHAT_IDS trong .env rồi khởi động lại bot." % chat_id,
            "parse_mode": "HTML",
        })
        return

    if command in ("/start", "/help"):
        tg_api(cfg, "sendMessage", {
            "chat_id": chat_id, "text": HELP_TEXT, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        return

    if command == "/tf":
        if args and args[0].lower() in TF_LABEL:
            cfg["tf"] = args[0].lower()
            save_state(cfg)
            tg_api(cfg, "sendMessage", {"chat_id": chat_id,
                                        "text": "Đã đổi khung: <b>%s</b>" % TF_LABEL[cfg["tf"]],
                                        "parse_mode": "HTML"})
        else:
            tg_api(cfg, "sendMessage", {"chat_id": chat_id,
                                        "text": "Dùng: /tf 24h | 7d | 30d", "parse_mode": "HTML"})
        return

    if command == "/coin":
        wanted = (args[0].upper() if args else "")
        if wanted in COINS:
            cfg["coin"] = wanted
            save_state(cfg)
            blocks = build_report(load_all(cfg, allow_stale=True), cfg["coin"], cfg["tf"], prices,
                                  exchanges=cfg["exchanges"])
            send_blocks(cfg, chat_id, blocks)
        else:
            tg_api(cfg, "sendMessage", {"chat_id": chat_id,
                                        "text": "Coin hợp lệ: " + ", ".join(COINS),
                                        "parse_mode": "HTML"})
        return

    shortcut = {
        "/btc": "BTC", "/eth": "ETH", "/sol": "SOL",
        "/usdt": "USDT", "/xrp": "XRP", "/bnb": "BNB",
    }
    coin = shortcut.get(command)
    force = False
    if command == "/now":
        coin = cfg["coin"]
        force = bool(args and args[0].lower() in ("refresh", "--refresh"))
    if coin:
        blocks = build_report(load_all(cfg, force_refresh=force, allow_stale=not force),
                              coin, cfg["tf"], prices, exchanges=cfg["exchanges"])
        send_blocks(cfg, chat_id, blocks)
        return
    if command == "/top":
        send_blocks(cfg, chat_id, build_top(load_all(cfg, allow_stale=True), cfg["coin"], cfg["tf"],
                                            exchanges=cfg["exchanges"]))
        return
    if command == "/days":
        send_blocks(cfg, chat_id, build_days(load_all(cfg, allow_stale=True), cfg["coin"],
                                             exchanges=cfg["exchanges"]))
        return

    tg_api(cfg, "sendMessage", {"chat_id": chat_id,
                                "text": "Lệnh không hợp lệ. Gõ /help.", "parse_mode": "HTML"})


def poll_loop(cfg):
    LOG.info("bot đang chạy (long polling) · chat được phép: %s",
             ", ".join(sorted(cfg["allowed"])) or "chưa cấu hình")
    try:
        me = tg_api(cfg, "getMe", {})
        LOG.info("đã đăng nhập bot @%s", me.get("username"))
    except Exception as exc:
        LOG.error("không gọi được getMe: %s", exc)

    offset = None
    prices_at = 0.0
    prices = {}
    while True:
        try:
            payload = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = tg_api(cfg, "getUpdates", payload) or []
            for update in updates:
                offset = update.get("update_id", 0) + 1
                message = update.get("message")
                if not message:
                    continue
                if time.time() - prices_at > 300:
                    prices = fetch_prices(cfg["http_timeout"])
                    prices_at = time.time()
                try:
                    handle_command(cfg, message, prices)
                except Exception:
                    LOG.exception("lỗi khi xử lý lệnh")
            if not updates:
                time.sleep(1)  # tránh vòng lặp nóng nếu server trả về ngay
        except urllib.error.HTTPError as exc:
            LOG.error("Telegram HTTP %s — thử lại sau 10s", exc.code)
            time.sleep(10)
        except Exception as exc:
            LOG.warning("lỗi polling: %s — thử lại sau 10s", exc)
            time.sleep(10)


# ---------------------------------------------------------------------------
# Lịch gửi báo cáo + làm mới cache nền
# ---------------------------------------------------------------------------

def next_run(cfg, now=None):
    """Mốc gửi kế tiếp trong ngày (mặc định 08:00 và 20:00)."""
    now = now or datetime.datetime.now(local_tz())
    candidates = []
    for hour, minute in cfg["report_times"]:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        candidates.append(target)
    return min(candidates)


def scheduler_loop(cfg):
    if not cfg["report_enabled"]:
        LOG.info("báo cáo tự động đang tắt (REPORT_ENABLED=0)")
        return
    times_label = ", ".join("%02d:%02d" % item for item in cfg["report_times"])
    LOG.info("lịch báo cáo tự động: %s mỗi ngày", times_label)
    while True:
        target = next_run(cfg)
        wait = max(5.0, (target - datetime.datetime.now(local_tz())).total_seconds())
        LOG.info("báo cáo tự động tiếp theo: %s", target.strftime("%d/%m/%Y %H:%M"))
        time.sleep(wait)
        if not cfg["chat_ids"]:
            LOG.error("chưa cấu hình TELEGRAM_CHAT_ID — không gửi được báo cáo")
            continue
        try:
            prices = fetch_prices(cfg["http_timeout"])
            records = load_all(cfg, force_refresh=True)
            blocks = build_report(records, cfg["coin"], "24h", prices, exchanges=cfg["exchanges"])
            for chat_id in cfg["chat_ids"]:
                send_blocks(cfg, chat_id, blocks)
            LOG.info("đã gửi báo cáo hằng ngày cho %d chat", len(cfg["chat_ids"]))
        except Exception:
            LOG.exception("gửi báo cáo hằng ngày thất bại")
        time.sleep(90)


def refresh_loop(cfg):
    """Làm mới cache ở nền để các lệnh /now không phải chờ tải lại."""
    while True:
        try:
            stale = any(
                read_cache(cfg["cache_dir"], ex["slug"], cfg["ttl_seconds"]) is None
                for ex in cfg["exchanges"]
            )
            if stale:
                LOG.info("làm mới dữ liệu on-chain ở nền…")
                load_all(cfg)
                LOG.info("đã làm mới xong cache")
        except Exception:
            LOG.exception("làm mới nền thất bại")
        time.sleep(300)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_blocks(blocks):
    for idx, chunk in enumerate(pack_blocks(blocks)):
        if idx:
            print("\n" + "-" * 40 + "\n")
        print(chunk)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bot Telegram dòng tiền on-chain 8 sàn CEX (DefiLlama).")
    parser.add_argument("--once", action="store_true", help="gửi 1 báo cáo rồi thoát (dùng cho cron)")
    parser.add_argument("--dry-run", action="store_true", help="in báo cáo ra terminal, không gửi Telegram")
    parser.add_argument("--coin", choices=COINS, help="coin muốn xem (mặc định BTC)")
    parser.add_argument("--tf", choices=tuple(TF_LABEL), help="khung thời gian (mặc định 24h)")
    parser.add_argument("--refresh", action="store_true", help="bỏ qua cache, tải lại DefiLlama")
    parser.add_argument("--stats", action="store_true", help="in thời gian chạy + RAM đỉnh ra stderr")
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    started = time.time()
    cfg = load_config()
    read_state(cfg)
    if args.coin:
        cfg["coin"] = args.coin
    if args.tf:
        cfg["tf"] = args.tf

    if args.dry_run:
        prices = fetch_prices(cfg["http_timeout"])
        records = load_all(cfg, force_refresh=args.refresh)
        print_blocks(build_report(records, cfg["coin"], cfg["tf"], prices, exchanges=cfg["exchanges"]))
        print("\n" + "-" * 40 + "\n[dry-run] coin=%s tf=%s sàn có dữ liệu=%d"
              % (cfg["coin"], cfg["tf"], len(records)))
    elif args.once:
        if not cfg["token"] or not cfg["chat_ids"]:
            parser.error("thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong bot/.env")
        prices = fetch_prices(cfg["http_timeout"])
        records = load_all(cfg, force_refresh=True)
        blocks = build_report(records, cfg["coin"], cfg["tf"], prices, exchanges=cfg["exchanges"])
        for chat_id in cfg["chat_ids"]:
            send_blocks(cfg, chat_id, blocks)
        LOG.info("đã gửi báo cáo cho %s", ", ".join(cfg["chat_ids"]))
    else:
        if not cfg["token"] or not cfg["chat_ids"]:
            parser.error("thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong bot/.env "
                         "(hoặc dùng --dry-run để xem trước nội dung)")
        threading.Thread(target=scheduler_loop, args=(cfg,), daemon=True).start()
        threading.Thread(target=refresh_loop, args=(cfg,), daemon=True).start()
        poll_loop(cfg)

    if args.stats:
        elapsed = time.time() - started
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
        LOG.info("thời gian: %.1fs · RAM đỉnh: %.1f MB", elapsed, peak_mb)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nđã dừng bot", file=sys.stderr)
        sys.exit(130)
