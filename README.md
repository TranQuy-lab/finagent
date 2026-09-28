# FinAgent — Hệ thống đa tác nhân thu thập tin tức và ra quyết định tài chính

> Đồ án: dùng VMware cài **Ubuntu Server làm máy chủ**, kết nối các **máy con** để
> thu thập tin tức giá vàng, bất động sản, chứng khoán; đưa vào mô hình LLM/ML nhỏ
> để ra quyết định tài chính; máy chủ **báo cáo qua Telegram**, người dùng duyệt
> mua/không, máy chủ **tự động đặt lệnh và giám sát thị trường**.

📄 **[Báo cáo cách hoạt động của mô hình](docs/BAO-CAO-CACH-HOAT-DONG.md)** — giải
thích hệ thống dựa vào yếu tố nào để quyết định giao dịch, kiến trúc triển khai máy
ảo (máy chủ ở đâu, máy con ở đâu, vì sao không thấy cửa sổ VMware), và những hạn
chế đã biết. **Đọc tài liệu này trước** nếu bạn cần hiểu tổng thể.

---

## 1. Tổng quan

Hệ thống chia làm hai tầng:

- **Máy chủ (Ubuntu Server trên VMware)** — điều phối công việc, lưu trữ dữ liệu,
  chạy mô hình ra quyết định, giao tiếp Telegram, đặt lệnh và giám sát.
- **Các máy con** — dùng tài nguyên rảnh để cào giá và tin tức, gửi kết quả về máy chủ.

```
                          ┌──────────────────────────────────────────┐
                          │        MÁY CHỦ (Ubuntu Server)           │
                          │                                          │
                          │  ┌────────────────┐  ┌────────────────┐  │
                          │  │ Bộ giám sát    │  │ Bot Telegram   │  │
                          │  │ (APScheduler)  │  │ duyệt mua/bán  │  │
                          │  └───────┬────────┘  └───────┬────────┘  │
                          │          │                   │           │
                          │  ┌───────▼───────────────────▼────────┐  │
                          │  │   Bộ máy ra quyết định             │  │
                          │  │   ML nhỏ + TradingAgents (LLM)     │  │
                          │  └───────┬────────────────────────────┘  │
                          │          │                               │
                          │  ┌───────▼────────┐   ┌───────────────┐  │
                          │  │  SQLite        │   │  Redis        │  │
                          │  │  giá/tin/lệnh  │   │  hàng đợi     │  │
                          │  └────────────────┘   └───────┬───────┘  │
                          └───────────────────────────────┼──────────┘
                                                          │ mạng NAT
                       ┌──────────────────┬───────────────┼──────────────┐
                       │                  │               │              │
                 ┌─────▼─────┐      ┌─────▼─────┐   ┌─────▼─────┐  ┌─────▼─────┐
                 │ Máy con 1 │      │ Máy con 2 │   │ Máy con 3 │… │ Máy con N │
                 │ cào giá,  │      │ cào giá,  │   │ cào giá,  │  │ (tối đa   │
                 │ tin tức   │      │ tin tức   │   │ tin tức   │  │  10 slot) │
                 └───────────┘      └───────────┘   └───────────┘  └───────────┘
```

### Nguyên tắc thiết kế

| Nguyên tắc | Cách thực hiện |
|---|---|
| **Không viết lại từ đầu** | Kế thừa [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (108k★, Apache-2.0) làm lõi suy luận đa tác nhân |
| **Thêm máy con không gây quá tải** | Semaphore Redis chặn tổng số việc chạy song song ở `FINAGENT_MAX_CONCURRENCY` (mặc định **10 slot**) |
| **Máy con chết không làm kẹt hệ thống** | Mỗi slot có TTL riêng, tự giải phóng; Celery bật `acks_late` để giao lại việc |
| **Mất mạng vẫn ra được quyết định** | Mô hình ML nhỏ chạy độc lập, không cần API key |
| **Không rủi ro tiền thật khi chưa sẵn sàng** | Mặc định `paper` — mô phỏng; cắm broker thật chỉ cần đổi một adapter |

---

## 2. Kết nối máy con — chọn cách nào?

Đề tài yêu cầu chọn cách **dễ, miễn phí và phổ biến nhất**. So sánh các phương án:

| Phương án | Miễn phí | Phổ biến | Nhận xét |
|---|---|---|---|
| **Redis + Celery** ✅ | Có | Rất cao (28,9k★) | Hàng đợi công việc chuẩn công nghiệp, tự động thử lại, tự cân bằng tải |
| SSH + script | Có | Cao | Không có hàng đợi, không cân bằng tải, khó giám sát |
| Kafka | Có | Cao | Mạnh nhưng nặng, thừa cho quy mô này |
| Kubernetes | Có | Cao | Rất nặng cho vài máy ảo |
| Docker Swarm | Có | Trung bình | Cần Docker trên mọi máy |

**Chọn: Redis + Celery, chạy trên mạng NAT của VMware.**

Lý do cụ thể với bối cảnh đề tài:

1. **Không cần cấu hình mạng phức tạp.** Cả máy chủ lẫn máy con đều để chế độ NAT
   (`vmnet8`), tự động cùng dải `172.16.x.x` nên thấy nhau, mà **vẫn ra được internet**
   để cào dữ liệu. Không phải đụng tới bridged network — vốn rất hay lỗi với Wi-Fi.
2. **Máy con chỉ cần biết một địa chỉ.** Cài xong chỉ cần khai `REDIS_HOST=<IP máy chủ>`.
3. **Thêm máy con không phải sửa gì trên máy chủ.** Máy con tự đăng ký khi chạy việc đầu tiên.
4. **Giới hạn 10 slot độc lập với số máy.** Dù có 3 hay 30 máy con, tổng tải lên
   nguồn dữ liệu vẫn không vượt ngưỡng cấu hình.

---

## 3. Cấu trúc mã nguồn

```
finagent/
├── finagent/
│   ├── config.py            Cấu hình trung tâm (đọc từ .env)
│   ├── celery_app.py        Celery app + semaphore giới hạn slot toàn cục
│   ├── tasks.py              Tác vụ chạy trên máy con (crypto/vàng/VN/tin tức)
│   ├── collectors/
│   │   ├── base.py           Kiểu dữ liệu chung (PricePoint, NewsItem)
│   │   ├── crypto.py         Giá crypto — API công khai Binance
│   │   ├── vnstock.py        Giá chứng khoán VN — VNDirect dchart + SSI dự phòng
│   │   ├── gold.py           Giá vàng — SJC/DOJI/PNJ/Bảo Tín + XAU/USD
│   │   └── news.py           Tin tức RSS + phân loại chủ đề tự động
│   ├── broker/
│   │   ├── base.py           Giao diện Broker (để cắm broker thật)
│   │   └── paper.py          Broker mô phỏng, khớp lệnh tức thì
│   ├── decision/
│   │   ├── ml_model.py       Mô hình ML nhỏ (hồi quy logistic, thuần numpy)
│   │   ├── vendor.py         Cầu nối dữ liệu Việt Nam vào TradingAgents
│   │   └── engine.py         Hợp nhất tín hiệu, tính khối lượng, tạo đề xuất
│   ├── storage.py            SQLite: giá, tin, đề xuất, lệnh, vị thế
│   ├── telegram_bot.py       Báo cáo + nút duyệt mua/bán
│   ├── scheduler.py          Chia việc, tổng hợp, quét thị trường định kỳ
│   └── cli.py                Giao diện dòng lệnh
├── vendor/
│   └── TradingAgents/        Bản TradingAgents đã kiểm thử (0.5.1) — xem mục 4
├── deploy/
│   ├── provision_vms.sh      Tạo máy ảo VMware (máy chủ + N máy con)
│   ├── test_vm_qemu.sh       Kiểm chứng máy ảo bằng QEMU/KVM khi VMware chưa chạy
│   ├── make_seed_iso.py      Sinh đĩa cloud-init (không cần root)
│   ├── setup_server.sh       Cài đặt máy chủ (Redis + dịch vụ systemd)
│   ├── setup_worker.sh       Cài đặt máy con
│   └── *.service             Đơn vị systemd
└── tests/
    ├── mock_llm.py           LLM giả lập (kiểm chứng đường đi LLM, không cần API key)
    ├── mock_telegram.py      Telegram giả lập (kiểm chứng luồng duyệt lệnh)
    └── ...                   153 test
```

---

## 4. Cài đặt

Hệ thống có **hai tập phụ thuộc khác nhau**, vì máy con và máy chủ làm hai việc
khác hẳn nhau:

| | Máy chủ | Máy con (client) |
|---|---|---|
| Cài bằng | `pip install -e ".[server]"` | `pip install -e ".[worker]"` |
| TradingAgents | ✅ cần (khung đa tác nhân) | ❌ **không cần** |
| Telegram, pandas, APScheduler | ✅ | ❌ |
| Mã nguồn | toàn bộ | 6 tệp + thư mục `collectors/` |
| Kích thước gói | 8,5 MB | **32 KB** |

Máy con chỉ cào dữ liệu rồi gửi về máy chủ, nên không cần khung đa tác nhân, phần
ra quyết định, broker hay bot Telegram. Cài thừa chỉ tốn dung lượng và thời gian —
mà máy con thường là máy ảo ít tài nguyên.

### 4.1. Đóng gói máy con để cài lên nhiều máy

```bash
bash deploy/make_worker_bundle.sh
# → dist/finagent-worker-0.1.0-<ngày>.tar.gz   (32 KB, 20 tệp)
```

Cài lên một máy mới:

```bash
scp dist/finagent-worker-*.tar.gz user@<ip-máy-mới>:/tmp/
ssh user@<ip-máy-mới>
tar xzf /tmp/finagent-worker-*.tar.gz
cd finagent-worker-0.1.0
sudo REDIS_HOST=<ip-máy-chủ> bash install.sh
```

Script tự cài Python, tạo tài khoản dịch vụ, ghi `.env`, đăng ký systemd và khởi
động máy con. Xong thì trên máy chủ chạy `finagent status` sẽ thấy máy mới.

**Thêm bao nhiêu máy con cũng được** — chỉ cần chạy lại đúng một lệnh trên mỗi máy.
Máy chủ tự phát hiện và chia việc.

> Đã kiểm chứng: gói 32 KB cài trong môi trường Python sạch, kết nối tới máy chủ
> thật, nạp đủ 5 tác vụ và đăng ký thành công. Gói **không** kéo theo telegram,
> pandas, langchain, langgraph hay numpy.

### 4.2. Vì sao TradingAgents nằm trong `vendor/`

PyPI **có** gói tên `tradingagents`, nhưng ở đó là phiên bản khác (0.7.x) so với bản
đồ án này đã kiểm thử và vá (0.5.1). Nếu để pip tự lấy từ PyPI, các bản vá dữ liệu
Việt Nam trong `finagent/decision/vendor.py` sẽ không khớp và hỏng âm thầm.

Vì vậy bản đã kiểm thử nằm ngay trong dự án và **chỉ máy chủ** cài:

```bash
pip install -e ".[server]"
pip install -e vendor/TradingAgents
```

Các script trong `deploy/` đã làm sẵn đúng thứ tự này. Khi chạy, hệ thống còn tự
kiểm tra phiên bản và cảnh báo nếu không khớp.

### 4.3. Cài nhanh trên một máy (để thử)

```bash
# 1) Môi trường Python
python3 -m venv .venv
.venv/bin/pip install -e ".[server]"
.venv/bin/pip install -e vendor/TradingAgents

# 2) Cấu hình
cp .env.example .env
nano .env
#    • Chọn nhà cung cấp LLM và điền khoá tương ứng. Đang dùng opencode zen:
#         TRADINGAGENTS_LLM_PROVIDER=openai_compatible
#         TRADINGAGENTS_LLM_BACKEND_URL=https://opencode.ai/zen/v1
#         OPENAI_COMPATIBLE_API_KEY=...
#         TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4.1-flash
#    • Điền TELEGRAM_BOT_TOKEN (xin qua @BotFather)

# 3) Redis (dùng Docker cho nhanh)
docker run -d --name finagent-redis -p 6379:6379 redis:7-alpine

# 4) Khởi tạo và kiểm tra
.venv/bin/finagent init

# 5) Chạy máy con (mở terminal riêng, có thể chạy nhiều cái)
FINAGENT_WORKER_NAME=may-con-1 .venv/bin/finagent worker

# 6) Chạy máy chủ
.venv/bin/finagent collect      # một lượt thu thập
.venv/bin/finagent scan         # quét thị trường, gửi đề xuất Telegram
.venv/bin/finagent run          # bộ giám sát định kỳ
.venv/bin/finagent bot          # bot Telegram
```

### 4.3. Chạy thử khi chưa có khoá API

Hệ thống vẫn ra quyết định bằng mô hình ML nhỏ mà không cần khoá nào. Muốn thử cả
đường đi LLM mà không tốn tiền, dùng LLM giả lập kèm theo:

```bash
# Terminal 1 — LLM giả lập
.venv/bin/python tests/mock_llm.py

# Terminal 2 — trỏ FinAgent vào đó
export DEEPSEEK_API_KEY=dummy
export TRADINGAGENTS_LLM_BACKEND_URL=http://127.0.0.1:8123/v1
.venv/bin/finagent scan
```

Cách này cũng dùng được với endpoint tự lưu trữ thật (vLLM, LM Studio, Ollama) —
chỉ cần đổi `TRADINGAGENTS_LLM_BACKEND_URL`.

### 4.4. Lấy `TELEGRAM_CHAT_ID`

Bot chỉ trả lời đúng một cuộc trò chuyện đã cấu hình, nên cần biết `chat_id`:

1. Nhắn `@BotFather` → `/newbot` → nhận `TELEGRAM_BOT_TOKEN`.
2. **Mở Telegram, tìm bot vừa tạo và bấm Start** (bắt buộc — bot không đọc được
   tin nhắn nào cho tới khi bạn nhắn trước).
3. Lấy `chat_id`:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | python3 -c "import json,sys; [print(u['message']['chat']['id']) for u in json.load(sys.stdin)['result'] if 'message' in u]"
```

Kết quả rỗng nghĩa là bot chưa nhận được tin nhắn nào — hãy bấm Start trước.

### 4.5. Lưu ý khi xoá cơ sở dữ liệu

Các dịch vụ đang chạy giữ kết nối tới file SQLite. Nếu bạn xoá `data/finagent.db`
**trong lúc dịch vụ đang chạy**, tiến trình vẫn trỏ vào file đã bị xoá và sẽ không
thấy dữ liệu mới — biểu hiện là bấm nút Duyệt trên Telegram báo "không tìm thấy
đề xuất" dù đề xuất hiển thị bình thường trong `finagent pending`.

Luôn khởi động lại dịch vụ sau khi xoá cơ sở dữ liệu:

```bash
sudo -u finagent .venv/bin/finagent init
sudo systemctl restart finagent-server finagent-bot
```

---

## 5. Triển khai trên VMware (theo đề tài)

Xem hướng dẫn đầy đủ từng bước tại **[deploy/README.md](deploy/README.md)**.

> **Đã dựng và kiểm chứng thành công:** 1 máy chủ + 3 máy con Ubuntu Server 24.04.5 LTS
> trên VMware Workstation. Máy con kết nối Redis qua mạng NAT, thu về **12 mẫu giá +
> 288 bài tin với 0 lỗi**; ra 7 đề xuất; duyệt → đặt lệnh chạy đúng.
> Cụm đã dựng: máy chủ `.131`, máy con `.132` `.133` `.134` (dải NAT `172.16.142.0/24`).

Tóm tắt:

```bash
# Tạo máy chủ + 3 máy con (tải ~600MB, dùng ~8GB đĩa)
bash deploy/provision_vms.sh --workers 3 --start

# Trên MÁY CHỦ
sudo bash deploy/setup_server.sh
sudo nano /opt/finagent/.env        # điền khoá API
sudo -u finagent /opt/finagent/.venv/bin/finagent init
sudo systemctl start finagent-server finagent-bot

# Trên TỪNG MÁY CON (thay IP máy chủ)
sudo REDIS_HOST=172.16.x.x bash deploy/setup_worker.sh
```

Hai điều kiện tiên quyết hay gặp nhất, cả hai đều đã gặp thật khi dựng cụm:

1. **Module `vmmon`/`vmnet`** — thiếu thì không máy ảo nào bật được.
   `sudo vmware-modconfig --console --install-all`
2. **PCI bridge trong VMX** — thiếu thì báo `failed to reserve slot for vmxnet3`.
   Mẫu trong `provision_vms.sh` đã có sẵn; xem `deploy/README.md` mục 7.1.

---

## 6. Lệnh thường dùng

| Lệnh | Chức năng |
|---|---|
| `finagent init` | Khởi tạo cơ sở dữ liệu, kiểm tra cấu hình |
| `finagent status` | Trạng thái cụm máy con, kho dữ liệu, danh mục |
| `finagent collect` | Một lượt thu thập phân tán |
| `finagent scan` | Quét thị trường, tạo đề xuất (`--no-llm` để chỉ dùng ML) |
| `finagent pending` | Liệt kê đề xuất đang chờ duyệt |
| `finagent positions` | Xem danh mục mô phỏng |
| `finagent run` | Bộ giám sát định kỳ (máy chủ) |
| `finagent bot` | Bot Telegram |
| `finagent worker` | Chạy máy con |
| `finagent ping` | Kiểm tra kết nối tới máy con |
| `finagent broker-check` | Kiểm tra nền tảng giao dịch và tài khoản sàn |

Qua Telegram: `/status`, `/positions`, `/pending`, `/scan`, `/help`.

---

## 7. Luồng ra quyết định

```
Máy con thu thập  ──►  Máy chủ lưu trữ  ──►  Phân tích  ──►  Telegram  ──►  Người dùng
   giá + tin tức        SQLite (khử trùng)      │            xin duyệt      bấm nút
                                                 │                              │
                     ┌───────────────────────────┴──────────┐                   │
                     │                                      │                   ▼
              Mô hình ML nhỏ                        TradingAgents        Đặt lệnh / Từ chối
        (hồi quy logistic trên nến ngày)      (đa tác nhân LLM, DeepSeek)         │
                     │                                      │                   ▼
                     └──────────────►  Hợp nhất tín hiệu  ◄─┘            Giám sát vị thế
```

**Hợp nhất tín hiệu** (`engine.combine_signals`):

- Cả hai cùng chiều **Mua** → tăng độ tin cậy, hành động mua.
- Cả hai cùng chiều **Bán** → tăng độ tin cậy, hành động bán (nếu đang có vị thế).
- **Mâu thuẫn** (LLM mua, ML bán) → hạ về **Hold**, đứng ngoài. Nguyên tắc: thà bỏ lỡ còn hơn vào lệnh khi hai nguồn không thống nhất.
- Chỉ có một nguồn → dùng nguồn đó.

**Kiểm soát rủi ro:**

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `FINAGENT_MAX_POSITION_PCT` | 10% | Tối đa một lệnh chiếm bao nhiêu phần danh mục |
| `FINAGENT_MIN_CONFIDENCE` | 0.55 | Dưới ngưỡng này không đề xuất |
| `FINAGENT_APPROVAL_THRESHOLD` | 5.000.000 ₫ | Lệnh lớn phải người dùng duyệt |
| `FINAGENT_APPROVAL_TIMEOUT` | 900s | Quá hạn thì đề xuất tự hết hiệu lực |
| `FINAGENT_MAX_CONCURRENCY` | 10 | Tổng slot thu thập toàn cụm |

---

## 8. Nguồn dữ liệu (đều miễn phí, không cần API key)

| Nguồn | Dữ liệu | Ghi chú kỹ thuật |
|---|---|---|
| Binance `/api/v3/klines`, `/ticker/24hr` | Giá crypto | Ổn định nhất |
| VNDirect `dchart-api` | Nến ngày chứng khoán VN | **Giá theo nghìn đồng**, phải nhân 1000. Cần UA giả trình duyệt, `Accept: */*` (gửi `application/json` bị trả 406) |
| SSI `iboard-query` | Giá tham chiếu VN (dự phòng) | Đã là VND |
| `vang.today/api/prices` | Giá vàng SJC/DOJI/PNJ/Bảo Tín | Giá bán ra |
| RSS VnExpress / CafeF / Vietstock | Tin tức | Tự phân loại chủ đề: vàng, BĐS, chứng khoán, crypto, vĩ mô |

---

## 9. Kiểm thử

```bash
.venv/bin/python -m pytest -m "not integration"   # 83 test nhanh, không cần mạng/Redis
.venv/bin/python -m pytest                        # 153 test đầy đủ
```

**153 test**, chia ba nhóm theo thứ tự giá trị:

| Nhóm | Số test | Kiểm chứng điều gì |
|---|---|---|
| Đơn vị | 83 | Đặc trưng ML, hợp nhất tín hiệu, tính khối lượng, lưu trữ, broker đa tiền tệ |
| Tích hợp dữ liệu | 41 | Giá Binance/VNDirect/vàng, RSS, vendor cắm vào TradingAgents, semaphore Redis |
| Đường đi LLM | 29 | Chạy **trọn đồ thị đa tác nhân** với LLM giả lập, khẳng định dữ liệu Việt Nam tới được mô hình |
| Luồng Telegram | 14 | Gửi đề xuất kèm nút bấm, duyệt thì đặt lệnh, từ chối thì không, chống bấm hai lần |

### Vì sao có LLM và Telegram giả lập

Hai đường đi này chỉ chạy được khi có khoá API thật, nên nếu không có gì thay thế
thì chúng **hoàn toàn không được kiểm chứng** — mà đây lại đúng là phần cốt lõi của
đề tài. Hai máy chủ giả lập trong `tests/` giải quyết việc đó:

* `mock_llm.py` — LLM tương thích OpenAI, có gọi tool thật và trả structured output
  đúng schema, đồng thời **ghi lại mọi prompt**. Nhờ vậy khẳng định được bảng OHLCV,
  giá VND, chỉ báo RSI, tin tức Việt Nam và tên doanh nghiệp thật sự tới được mô hình.
* `mock_telegram.py` — Bot API giả lập, ghi lại tin nhắn và bàn phím inline, nhờ đó
  kiểm chứng được cả nội dung báo cáo lẫn hệ quả của việc người dùng bấm nút.

Cả hai đều dùng được ngoài môi trường test: `mock_llm.py` chạy độc lập giúp thử
đường đi LLM mà không tốn tiền (xem mục 4.3).

### Lỗi thật đã phát hiện nhờ test

* **Bấm Duyệt hai lần đặt lệnh hai lần** — rủi ro tài chính trực tiếp. Đã sửa bằng
  câu `UPDATE … WHERE status = 'pending'` có tính nguyên tử, chỉ một lần thắng.
* **Trộn tiền tệ** — mua ETH (niêm yết USDT) bằng ví VND. Đã tách ví đa tiền tệ.
* **Vòng chờ máy ảo thoát quá sớm** — SSH thông nhưng cloud-init chưa cài xong gói,
  khiến lệnh cài đặt ngay sau đó thất bại.

---

## 10. Giới hạn và hướng phát triển

**Giới hạn hiện tại:**

- **Chưa đặt lệnh thật.** Mặc định chạy mô phỏng. Muốn giao dịch thật phải viết một
  lớp con của `Broker` cho công ty chứng khoán (SSI/TCBS/VPS) và đổi
  `FINAGENT_TRADING_MODE=live`.
- **Không có dữ liệu cơ bản** (P/E, EPS, báo cáo tài chính) cho cổ phiếu Việt Nam —
  các nguồn miễn phí không cung cấp. Vendor đã được lập trình để **báo rõ thiếu dữ liệu**
  thay vì để mô hình bịa số.
- **Vàng không có chuỗi nến ngày** từ nguồn miễn phí, nên không đưa vào mô hình ML;
  vàng được giám sát qua tin tức và giá hiện tại.
- Chứng khoán Việt Nam khó bán khống, nên tín hiệu Bán khi không có vị thế sẽ thành Hold.
- **Đường đi LLM đã được kiểm chứng bằng LLM giả lập, chưa chạy bằng DeepSeek thật**
  (cần khoá API). Cơ chế đã đúng: đồ thị chạy trọn, dữ liệu Việt Nam tới được mô
  hình, structured output trả về đúng. Chất lượng câu trả lời thật còn phụ thuộc mô hình.
- **Máy ảo trên VMware cần một lệnh `sudo` của bạn** để build lại module `vmmon`/`vmnet`
  (xem `deploy/README.md` mục 0.1). Phần cấu hình máy chủ đã được kiểm chứng độc lập
  bằng QEMU/KVM với đúng cloud image và đĩa seed đó.

### 10.1. Nhà cung cấp LLM

Hệ thống dùng endpoint **OpenAI-compatible**, nên đổi nhà cung cấp chỉ là đổi ba
biến trong `.env`:

```bash
TRADINGAGENTS_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_LLM_BACKEND_URL=https://opencode.ai/zen/v1
OPENAI_COMPATIBLE_API_KEY=...
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4.1-flash
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-v4.1-flash
```

Cấu hình này cũng dùng được cho vLLM, LM Studio, llama.cpp, hoặc gateway nội bộ.

**Vì sao không dùng gói miễn phí của Google:** gói miễn phí giới hạn 20 request
mỗi ngày cho **mỗi** model, mà một lượt phân tích đa tác nhân tốn khoảng 20 lời
gọi — vừa đủ một lượt rồi hết. Không đủ dùng thực tế. `deepseek-v4.1-flash` qua
opencode zen không gặp giới hạn đó.

**`deepseek-v4.1-flash` là model suy luận.** Phần lớn token đầu được dùng cho
`reasoning` trước khi sinh câu trả lời, nên đừng giới hạn `max_tokens` chặt —
để trống (mặc định) là an toàn nhất.

### 10.2. Nền tảng giao dịch

| Tài sản | Nền tảng | Trạng thái |
|---|---|---|
| Crypto (BTC/ETH) | **Binance Spot** | ✅ Đã có adapter — `finagent/broker/binance.py` |
| Chứng khoán VN | SSI / TCBS / VPS | ❌ Chưa có, cần đăng ký API |
| Vàng (SJC/DOJI) | — | ❌ Không có API đặt lệnh |

Chọn Binance vì API công khai, tài liệu rõ, và có **Testnet** — API giống hệt bản
thật nhưng dùng tiền giả, nên kiểm chứng được trọn luồng mà không mất tiền.

**Đổi sang giao dịch thật:**

```bash
# 1) Lấy khoá Testnet miễn phí: https://testnet.binance.vision (đăng nhập GitHub)
# 2) Điền vào .env
FINAGENT_TRADING_MODE=live
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_TESTNET=true          # tiền giả

# 3) Kiểm tra kết nối trước khi chạy
finagent broker-check
```

**Hai điều khác biệt so với mô phỏng:**

1. **Kích thước lệnh do sàn quy định.** Binance chỉ nhận bội số `stepSize` và từ
   chối lệnh dưới `minNotional`. Hệ thống hỏi sàn trước khi đặt chứ không đoán —
   BTC bước `0.00001` nhưng ETH bước `0.0001`, đoán là sai.
2. **Sàn không biết giá vốn.** Binance chỉ cho biết đang giữ bao nhiêu. Nên số
   lượng lấy từ sàn (nguồn sự thật), giá vốn bình quân giữ trong SQLite cục bộ.

**Cửa chặn tiền thật:** muốn dùng tiền thật phải đặt **cả hai** `BINANCE_TESTNET=false`
**và** `BINANCE_LIVE_CONFIRM=YES`. Hai điều kiện, để không ai vô tình giao dịch tiền
thật chỉ vì sửa nhầm một dòng.

**Hướng phát triển:**

1. Cắm broker thật (SSI FastConnect / TCBS API).
2. Thêm dữ liệu cơ bản qua nguồn trả phí (Fiin, Vietstock Pro).
3. Thay mô hình ML nhỏ bằng mô hình chuỗi thời gian (LSTM/Temporal Fusion Transformer).
4. Đo hiệu quả bằng backtest trên dữ liệu lịch sử trước khi chạy thật.
5. Thêm chấm điểm tin cậy của nguồn tin (chống tin giả).

---

## 11. Ghi công

- Lõi suy luận đa tác nhân: [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) — Apache-2.0.
  Phần tuỳ biến của đồ án: vendor dữ liệu Việt Nam (`finagent/decision/vendor.py`),
  mô hình ML nhỏ, và toàn bộ hạ tầng phân tán máy chủ–máy con.
- Hàng đợi phân tán: [Celery](https://github.com/celery/celery) + Redis.
