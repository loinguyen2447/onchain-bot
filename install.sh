#!/usr/bin/env bash
# Cài bot Telegram dòng tiền on-chain: tạo .env, kiểm tra token, cài service systemd.
#
#   bash install.sh                          # hỏi token / chat id rồi cài
#   bash install.sh --token 123:abc --chat 42 # không hỏi gì
#   bash install.sh --no-service              # chỉ tạo .env, không cài service
#
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
UNIT_DIR="${UNIT_DIR:-$HOME/.config/systemd/user}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
API_BASE="${TELEGRAM_API_BASE:-https://api.telegram.org}"
API_BASE="${API_BASE%/}"
SERVICE_NAME="onchain-bot.service"
NO_SERVICE=0
TOKEN_ARG=""
CHAT_ARG=""

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Tùy chọn:
  --token <token>   token từ @BotFather
  --chat <chat_id>  chat id nhận báo cáo (nhắn /id cho bot để lấy)
  --no-service      chỉ tạo .env, không cài systemd
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN_ARG="${2:-}"; shift 2 ;;
    --chat) CHAT_ARG="${2:-}"; shift 2 ;;
    --no-service) NO_SERVICE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "❌ Tham số không hợp lệ: $1"; usage; exit 1 ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "❌ Không tìm thấy python3. Cài bằng: sudo apt install python3"
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "❌ Cần Python 3.8 trở lên (máy đang có $("$PYTHON" -V 2>&1))."
  exit 1
fi
PYTHON_BIN="$(command -v "$PYTHON")"

get_env() {
  grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true
}

set_env() {
  local key="$1" value="$2"
  "$PYTHON" - ".env" "$key" "$value" <<'PY'
import pathlib, sys
path, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text().splitlines() if path.exists() else []
result, replaced = [], False
for line in lines:
    if line.startswith(key + "="):
        result.append("%s=%s" % (key, value))
        replaced = True
    else:
        result.append(line)
if not replaced:
    result.append("%s=%s" % (key, value))
path.write_text("\n".join(result) + "\n")
path.chmod(0o600)
PY
}

PLACEHOLDER_TOKEN="123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
PLACEHOLDER_CHAT="123456789"

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "📝 Đã tạo bot/.env"
fi

# ---------- Token ----------
TOKEN="$(get_env TELEGRAM_BOT_TOKEN)"
if [ -n "$TOKEN_ARG" ]; then
  TOKEN="$TOKEN_ARG"
elif [ -z "$TOKEN" ] || [ "$TOKEN" = "$PLACEHOLDER_TOKEN" ]; then
  if [ -t 0 ]; then
    echo "Mở Telegram → @BotFather → /newbot → copy token."
    read -r -s -p "Dán TELEGRAM_BOT_TOKEN (không hiện lên màn hình): " TOKEN
    echo
  fi
fi

TOKEN="$(echo "$TOKEN" | tr -d '[:space:]')"
if [ -z "$TOKEN" ] || [ "$TOKEN" = "$PLACEHOLDER_TOKEN" ]; then
  echo "❌ Chưa có token. Chạy lại và dán token, hoặc: bash install.sh --token <token> --chat <chat_id>"
  exit 1
fi
if ! echo "$TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
  echo "❌ Token sai định dạng (phải dạng 123456789:AA...)."
  exit 1
fi
set_env TELEGRAM_BOT_TOKEN "$TOKEN"
echo "✅ Đã lưu token vào bot/.env"

# ---------- Kiểm tra token với Telegram ----------
if [ "${SKIP_TOKEN_CHECK:-0}" != "1" ]; then
  BOT_NAME="$("$PYTHON" - "$TOKEN" "$API_BASE" <<'PY'
import json, sys, urllib.request
token, api_base = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen("%s/bot%s/getMe" % (api_base, token), timeout=25) as resp:
        data = json.load(resp)
    print(data["result"]["username"] if data.get("ok") else "")
except Exception:
    print("")
PY
)"
  if [ -z "$BOT_NAME" ]; then
    echo "❌ Không xác thực được token (sai token hoặc không có mạng). Kiểm tra lại rồi chạy lại."
    exit 1
  fi
  echo "✅ Token hợp lệ — bot @$BOT_NAME"
else
  BOT_NAME="your_bot"
fi

# ---------- Chat id ----------
CHAT="$(get_env TELEGRAM_CHAT_ID)"
WAIT_SECONDS="${CHAT_WAIT_SECONDS:-90}"

if [ -n "$CHAT_ARG" ]; then
  CHAT="$CHAT_ARG"
elif [ -z "$CHAT" ] || [ "$CHAT" = "$PLACEHOLDER_CHAT" ]; then
  if [ -t 0 ] && [ "$WAIT_SECONDS" -gt 0 ] 2>/dev/null; then
    echo ""
    echo "👉 Mở Telegram, chat với @$BOT_NAME và bấm Start (hoặc nhắn /id)."
    echo "   Đang chờ tin nhắn đầu tiên từ bạn (tối đa ${WAIT_SECONDS}s)…"
    CHAT="$("$PYTHON" - "$TOKEN" "$WAIT_SECONDS" "$API_BASE" <<'PY'
import json, sys, time, urllib.request

token, wait_seconds, api_base = sys.argv[1], float(sys.argv[2]), sys.argv[3]
deadline = time.time() + wait_seconds
offset = None
chat_id = ""
while time.time() < deadline:
    url = "%s/bot%s/getUpdates?timeout=20" % (api_base, token)
    if offset:
        url += "&offset=%d" % offset
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
    except Exception:
        time.sleep(2)
        continue
    for update in data.get("result") or []:
        offset = (update.get("update_id") or 0) + 1
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("id") and chat.get("type", "private") == "private":
            chat_id = str(chat["id"])
    if chat_id:
        print(chat_id)
        sys.exit(0)
print("")
PY
)"
    if [ -n "$CHAT" ]; then
      echo "✅ Đã tự nhận chat id: $CHAT"
    else
      echo "⚠️  Chưa thấy tin nhắn nào. Cài tiếp, sau đó chạy: bash install.sh --chat <chat_id>"
    fi
  fi
fi
if [ -n "$CHAT" ] && [ "$CHAT" != "$PLACEHOLDER_CHAT" ]; then
  set_env TELEGRAM_CHAT_ID "$CHAT"
  set_env ALLOWED_CHAT_IDS "$CHAT"
  echo "✅ Đã lưu chat id: $CHAT"
  HAVE_CHAT=1
else
  HAVE_CHAT=0
fi

# ---------- Kiểm tra code ----------
"$PYTHON" -m py_compile onchain_bot.py
echo "✅ Code chạy được (python $("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# ---------- systemd user service ----------
if [ "$NO_SERVICE" -eq 0 ]; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$SERVICE_NAME" <<EOF
[Unit]
Description=Telegram on-chain CEX flow bot (DefiLlama)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$(pwd)
ExecStart=$PYTHON_BIN $(pwd)/onchain_bot.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF
  echo "✅ Đã tạo $UNIT_DIR/$SERVICE_NAME"

  if command -v "$SYSTEMCTL" >/dev/null 2>&1; then
    "$SYSTEMCTL" --user daemon-reload
    "$SYSTEMCTL" --user enable "$SERVICE_NAME"
    if [ "$HAVE_CHAT" -eq 1 ]; then
      "$SYSTEMCTL" --user restart "$SERVICE_NAME"
      echo "✅ Bot đã chạy nền (systemd user service)"
    else
      echo "⚠️  Chưa có chat id nên chưa khởi động bot."
    fi
  else
    echo "⚠️  Không thấy '$SYSTEMCTL' — bỏ qua bước chạy nền."
  fi
fi

cat <<EOF

──────────────────────────────────────────────
Xong. Việc còn lại:

1) Nếu chưa có chat id: nhắn /id cho @$BOT_NAME, rồi chạy:
      bash install.sh --chat <chat_id>
2) Xem log bot:
      journalctl --user -u onchain-bot -f
3) Muốn bot chạy cả khi đã logout/khởi động lại máy:
      sudo loginctl enable-linger "\$USER"
4) Báo cáo tự động: 08:00 và 20:00 mỗi ngày (đổi trong .env: REPORT_TIMES=08:00,20:00).
   Nhắn tin bất kỳ cho bot để nhận báo cáo ngay.
5) Thử không gửi Telegram:
      python3 onchain_bot.py --dry-run
──────────────────────────────────────────────
EOF
