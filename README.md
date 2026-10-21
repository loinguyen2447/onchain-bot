# Bot Telegram — dòng tiền on-chain 8 sàn CEX

Bot gửi cho bạn đúng thông tin của section **“Lượng Coin chuyển lên sàn & rút khỏi sàn”** trên web:
Binance, OKX, Coinbase, Bybit, Bitfinex, Robinhood, Bitget, Gate.io — dữ liệu Proof-of-Reserves từ DefiLlama.

- Không cần `pip install`: chỉ dùng thư viện chuẩn của Python 3.8+ (Linux Mint có sẵn).
- **Tự gửi báo cáo 8:00 sáng và 8:00 tối** mỗi ngày; **nhắn tin bất kỳ cho bot là nhận báo cáo ngay**.
- Chạy dạng service systemd, tự khởi động lại khi lỗi hoặc reboot.
- Cache dữ liệu 6 giờ (dữ liệu on-chain chỉ đổi theo ngày) để không phải tải lại 200MB mỗi lần.

## 1. Cài bằng một lệnh (khuyến nghị)

```bash
git clone https://github.com/loinguyen2447/wedtaichinh.git
cd wedtaichinh/bot
bash install.sh
```

Script sẽ hỏi **token** (lấy từ [@BotFather](https://t.me/BotFather): `/newbot`) — token nhập ẩn,
không hiện lên màn hình. Sau đó nó chờ bạn mở Telegram bấm **Start** với bot và **tự lấy chat id**,
rồi cài + bật service systemd. Chạy lại lần nữa cũng an toàn.

Nếu không muốn chờ tự dò: `bash install.sh --chat <chat_id>` (lấy id bằng lệnh `/id`).

> ⚠️ Token là mật khẩu của bot. Không dán vào chat, không commit `.env`. Nếu lộ, vào
> @BotFather gõ `/revoke` để đổi token rồi sửa lại `.env`.

Không muốn bị hỏi gì:

```bash
bash install.sh --token 123456789:AA... --chat 123456789
```

Chỉ muốn tạo `.env` để chạy thử: `bash install.sh --no-service`.

## 2. Kiểm tra

```bash
python3 onchain_bot.py --dry-run     # in báo cáo ra terminal, chưa gửi Telegram
python3 test_bot.py                  # 29 test offline (~7 giây, không cần mạng)
RUN_LIVE=1 python3 test_bot.py       # thêm test tải dữ liệu thật từ DefiLlama
journalctl --user -u onchain-bot -f  # xem log bot đang chạy nền
```

Muốn bot vẫn chạy sau khi đăng xuất / khởi động lại máy:

```bash
sudo loginctl enable-linger "$USER"
```

Lệnh quản lý service:

```bash
systemctl --user restart onchain-bot
systemctl --user stop onchain-bot
systemctl --user status onchain-bot
```

## 3. Cách khác: cron (không cần chạy thường trực)

```bash
crontab -e
# 8h sáng và 8h tối mỗi ngày
0 8,20 * * * cd $HOME/wedtaichinh/bot && /usr/bin/python3 onchain_bot.py --once >> bot.log 2>&1
```

## 4. Kết nối Telegram của bạn

1. Mở Telegram → [@BotFather](https://t.me/BotFather) → `/newbot` → đặt tên → nhận **token**
   dạng `123456789:AA...`.
2. Chạy `bash install.sh`, dán token khi được hỏi.
3. Mở chat với bot vừa tạo, bấm **Start** (hoặc nhắn bất kỳ). Installer tự nhận chat id.
4. Xong: bot trả báo cáo ngay cho tin nhắn đó, và tự gửi lúc **08:00** và **20:00** mỗi ngày.

Kiểm tra nhanh: nhắn `hello` cho bot — nếu nhận báo cáo là kết nối thành công.

## 5. Lệnh trên Telegram

Gửi tin nhắn bất kỳ (không cần lệnh) → bot trả báo cáo mới nhất.

| Lệnh | Tác dụng |
|---|---|
| `/now` | Báo cáo đầy đủ coin đang xem (dùng cache) |
| `/now refresh` | Tải lại DefiLlama rồi báo cáo (chậm vài phút) |
| `/btc` `/eth` `/sol` `/usdt` `/xrp` `/bnb` | Xem nhanh một coin |
| `/coin ETH` | Đổi coin đang xem |
| `/tf 24h` `/tf 7d` `/tf 30d` | Đổi khung thời gian |
| `/top` | 3 sàn nạp ròng / rút ròng mạnh nhất |
| `/days` | 7 ngày gần nhất, biểu đồ thanh |
| `/id` | Lấy chat id |
| `/help` | Trợ giúp |

## 6. Cơ chế tính (giống web)

```
flow = reserve(snapshot mới nhất) − reserve(snapshot mốc)
  > 0  → coin chuyển LÊN sàn (inflow)
  < 0  → coin rút KHỎI sàn (outflow)
```

- Mốc: `24h` = snapshot liền trước, `7d` = 8 snapshot trước, `30d` = 31 snapshot trước.
- Dự trữ được gộp theo coin gốc: `BTC + WBTC + BTCB + cbBTC`, `ETH + WETH + stETH…`,
  `USDT + USDC + FDUSD`, `SOL + BNSOL + jitoSOL`, `XRP + WXRP`, `BNB + WBNB`.
- Tổng hợp 8 sàn: inflow = tổng các flow dương, outflow = tổng các flow âm,
  netflow = inflow − outflow. Đây là **tổng gộp**, không phải net của từng sàn.
- Giá USD: `coins.llama.fi` (dự phòng: Binance ticker). Không lấy được giá thì ẩn phần USD.
- Không có số liệu giả: sàn nào DefiLlama không có dữ liệu cho coin đó thì bỏ khỏi bảng.

**Lưu ý:** đây là chênh lệch số dư ví, không phải số lệnh chuyển. Khung “24h” thực chất là
từ snapshot đóng ngày gần nhất đến snapshot mới nhất (thường 6–12 giờ), nên con số nhỏ hơn
24 giờ trọn vẹn.

## 7. Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| `401 Unauthorized` | Token sai hoặc đã thu hồi — tạo token mới ở BotFather |
| Bot không trả lời | Chưa nhắn Start cho bot; hoặc chat id không nằm trong `ALLOWED_CHAT_IDS` |
| Token lộ | @BotFather → `/revoke` → dán token mới, chạy lại `bash install.sh` |
| `⛔️ Chat id ... chưa được cấp quyền` | Thêm id đó vào `ALLOWED_CHAT_IDS` trong `.env`, restart service |
| Tải dữ liệu lỗi/timeout | Kiểm tra mạng; tăng `HTTP_TIMEOUT`; nếu cần proxy thì đặt `HTTPS_PROXY` |
| Muốn chạy nhẹ hơn | Đặt `EXCHANGES=binance-cex,okx,bybit` để theo dõi ít sàn hơn |
| Xem log | `journalctl --user -u onchain-bot -f` |
| Đổi giờ gửi | Sửa `REPORT_TIMES=08:00,20:00` trong `.env`, rồi `systemctl --user restart onchain-bot` |

Dữ liệu tải mỗi lần làm mới khoảng 200MB (chủ yếu là lịch sử của Gate và Binance).
Cache 6 giờ nên bình thường mỗi ngày chỉ tải vài lần; có thể tăng `CACHE_TTL_HOURS`.
