# Funding Scanner — Dashboard công khai

Quét funding rate 4 sàn (Binance, Bybit, OKX, BingX), lọc cơ hội **săn phí delta-neutral**
(funding dương → SHORT perp + LONG spot), tự cập nhật mỗi giờ và hiển thị trên một trang web
ai cũng xem được, kèm **đếm ngược tới giờ chốt phí**.

## Cấu trúc

```
funding_scanner_v2.py          # scanner: quét + lọc + ghi docs/data.json
requirements.txt
docs/
  index.html                   # dashboard (GitHub Pages phục vụ từ đây)
  data.json                    # dữ liệu, do Actions ghi mỗi giờ
.github/workflows/update.yml   # cron chạy mỗi giờ, commit data.json
```

## Cách deploy (một lần)

1. Tạo repo **public** trên GitHub, đẩy toàn bộ thư mục này lên nhánh `main`.
2. Vào **Settings → Pages**: mục *Source* chọn **Deploy from a branch**, Branch = `main`, thư mục = **`/docs`**, Save.
3. Vào tab **Actions**, nếu được hỏi thì bật workflow. Bấm **Run workflow** một lần cho `Update funding data`
   để tạo `docs/data.json` ngay (không phải đợi tới đầu giờ).
4. Sau ~1 phút, mở địa chỉ Pages: `https://<tên-user>.github.io/<tên-repo>/`.

Từ đó workflow tự chạy **mỗi giờ** (cron `0 * * * *`, giờ UTC — GitHub có thể trễ vài phút khi tải cao).
Dashboard cũng tự fetch lại `data.json` mỗi 5 phút và đếm ngược cập nhật mỗi giây.

## ⚠️ Lưu ý quan trọng: Binance có thể bị chặn trên GitHub Actions

Runner của GitHub Actions dùng IP cloud/Mỹ, mà **Binance API thường trả lỗi 451** cho các IP này.
Khi đó Binance sẽ **vắng mặt** trong dữ liệu — scanner tự bỏ qua sàn lỗi và vẫn chạy với 3 sàn còn lại
(log sẽ ghi `[!] binance lỗi (bỏ qua): ...`). Các cách xử lý:

- **Chấp nhận 3 sàn** (Bybit/OKX/BingX) — đơn giản nhất, vẫn đủ nhiều cơ hội.
- **Chạy trên VPS/máy riêng của anh** thay cho Actions: đặt cron hệ thống
  `0 * * * * cd /path/repo && python funding_scanner_v2.py && git add docs/data.json && git commit -m auto && git push`
  rồi để GitHub Pages phục vụ file. IP của anh (VN) thường vào được Binance.
- **Dùng proxy**: đặt biến môi trường `HTTPS_PROXY` trong workflow trỏ tới proxy vào được Binance
  (lưu proxy trong *Settings → Secrets*), `requests` sẽ tự dùng.

## Chỉnh bộ lọc

Mọi tham số nằm ở đầu `funding_scanner_v2.py`:

- `NORMALIZE_HOURS = 4` và `THRESHOLD_PCT = 0.25` → lọc **rate quy về 4h > 0.25%**.
  (Cặp 8h phải đạt 0.5%/8h; cặp 4h cần rate thô > 0.25%.) Muốn quay lại chuẩn 8h thì đổi `NORMALIZE_HOURS = 8`.
- `REQUIRE_NET_POS` — chỉ giữ cặp có lời sau phí khi giữ `HOLD_PERIODS` kỳ.
- `MIN_SPOT_VOL_USDT`, `MIN_PCT_POSITIVE` — chặn thanh khoản Spot mỏng và funding không ổn định.
- Phí đã set theo thực tế: Spot 0.1/0.1, Perp 0.02/0.05, `FILL_MODE="taker"` → round-trip 0.24%.

## Chạy tay (kiểm thử cục bộ)

```bash
pip install -r requirements.txt
python funding_scanner_v2.py     # tạo docs/data.json + funding_v2.xlsx + funding_v2.csv
# xem thử dashboard:
python -m http.server -d docs 8000   # mở http://localhost:8000
```

> Dữ liệu chỉ để tham khảo, không phải khuyến nghị đầu tư. Funding có thể đảo dấu ngay trước giờ chốt;
> luôn tính phí, trượt giá và rủi ro thanh lý trước khi vào lệnh.
