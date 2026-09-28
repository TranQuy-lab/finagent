# Báo cáo cách hoạt động của FinAgent

Tài liệu này giải thích **hệ thống ra quyết định giao dịch dựa trên những yếu tố
nào**, và **các thành phần được triển khai ở đâu**. Viết cho người đọc cần hiểu
hoặc bảo vệ đề tài, không cần đọc mã nguồn trước.

---

## 1. Tổng quan

FinAgent là hệ thống **đa tác nhân** phân tích thị trường tài chính và đề xuất
giao dịch. Hệ thống gồm hai nửa:

| Nửa | Nhiệm vụ | Chạy ở đâu |
|---|---|---|
| **Phân tán** | Cào giá và tin tức từ nhiều nguồn | Nhiều máy con (worker) |
| **Tập trung** | Phân tích, ra quyết định, báo cáo, đặt lệnh | Một máy chủ (server) |

Nguyên tắc xuyên suốt: **máy chủ không bao giờ tự tiêu tiền.** Mọi lệnh vượt
ngưỡng đều phải chờ người dùng bấm nút duyệt.

---

## 2. Kiến trúc triển khai

### 2.1. Sơ đồ

```
                    MÁY THẬT (máy bạn đang dùng)
                    VMware Workstation, mạng NAT vmnet8
                              172.16.142.0/24
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
   ┌──────┴───────┐    ┌─────────┴────────┐   ┌─────────┴────────┐
   │  MÁY CHỦ     │    │   MÁY CON 1      │   │   MÁY CON 2/3    │
   │ .131         │    │   .132           │   │   .133, .134     │
   │              │    │                  │   │                  │
   │ Redis        │◄───┤ Celery worker    │   │ Celery worker    │
   │ Celery worker│    │  (10 slot)       │   │  (10 slot)       │
   │ Web/API      │    │                  │   │                  │
   │ Bot Telegram │    │ Cào giá + tin    │   │ Cào giá + tin    │
   └──────────────┘    └──────────────────┘   └──────────────────┘
        ▲
        │ đọc/ghi SQLite + Redis
        ▼
   Người dùng ◄──── Telegram ──── Bot
```

### 2.2. Vì sao máy chủ cũng chạy một worker

Máy chủ vừa điều phối vừa tham gia cào dữ liệu. Nếu chỉ có máy con làm, lúc cả 3
máy con bận thì máy chủ ngồi không trong khi vẫn còn 2 nhân CPU rảnh. Cho máy chủ
một slot là tận dụng tài nguyên và giúp cụm không chết khi mọi máy con mất kết nối.

### 2.3. Thông số từng máy

| Máy | IP | Vai trò | CPU | RAM | Đĩa | Dịch vụ |
|---|---|---|---|---|---|---|
| `finagent-server` | 172.16.142.131 | Máy chủ | 2 nhân | 1,5 GB | 3,3 GB | `redis-server`, `finagent-server`, `finagent-bot`, `finagent-worker` |
| `finagent-worker1` | 172.16.142.132 | Máy con 1 | 2 nhân | 1,5 GB | 3,3 GB | `finagent-worker` |
| `finagent-worker2` | 172.16.142.133 | Máy con 2 | 2 nhân | 1,5 GB | 3,1 GB | `finagent-worker` |
| `finagent-worker3` | 172.16.142.134 | Máy con 3 | 2 nhân | 1,5 GB | 3,3 GB | `finagent-worker` |

Tất cả chạy Ubuntu Server, cài tại `/opt/finagent`, tài khoản dịch vụ `finagent`,
card mạng `e1000` nối kiểu NAT.

> **Vì sao dùng card `e1000` chứ không phải `vmxnet3`:** bản VMware này lỗi khi cấp
> khe PCI cho `vmxnet3`, máy ảo báo `failed to reserve slot for vmxnet3 PCIe device`
> rồi không khởi động được. `e1000` tuy chậm hơn nhưng chạy ổn định, và với lưu
> lượng của đề tài thì tốc độ mạng không phải điểm nghẽn.

### 2.4. Máy ảo chạy ẩn — vì sao không thấy cửa sổ

**Máy ảo được khởi động bằng `vmrun start <file.vmx> nogui`.** Tham số `nogui`
nghĩa là VMware không mở cửa sổ giao diện; máy ảo vẫn chạy đầy đủ dưới dạng tiến
trình nền `vmware-vmx`.

Đây là chủ đích, không phải lỗi:

- Máy chủ chỉ cần SSH và dịch vụ systemd, không cần màn hình đồ họa.
- Mỗi cửa sổ VMware tốn thêm RAM và CPU để vẽ giao diện — tài nguyên đó nên dành
  cho việc cào dữ liệu.
- Không có cửa sổ thì máy ảo không tự tắt khi đóng giao diện.

**Cách xem máy ảo đang chạy:**

```bash
vmrun list                      # liệt kê máy ảo đang chạy
ps aux | grep vmware-vmx        # xem tiến trình nền
```

**Cách mở màn hình máy ảo:** mở VMware Workstation — cả 4 máy đã có trong thư viện
bên trái. Bấm vào một máy đang chạy, VMware sẽ **gắn vào máy ảo đang chạy** và
hiện màn hình console, không cần khởi động lại.

Đăng nhập console: tài khoản `finagent`, mật khẩu `finagent` (chỉ dùng khi cần
cứu hộ; bình thường nên dùng SSH).

**Muốn máy ảo hiện cửa sổ ngay từ lúc khởi động:** đổi `nogui` thành `gui` trong
`deploy/provision_vms.sh`, hoặc chạy tay:

```bash
vmrun -T ws start /home/noble-tran/vmware/finagent/finagent-server/finagent-server.vmx gui
```

---

## 3. Hoạt động phân tán

### 3.1. Chia việc giữa máy chủ và máy con

Máy chủ đẩy việc vào hàng đợi **Redis**; các máy con nhận việc qua **Celery**.
Redis chạy trên máy chủ tại `redis://127.0.0.1:6379/0`, các máy con kết nối qua
mạng nội bộ VMware.

### 3.2. Cơ chế "slot" — chống quá tải

Mỗi máy con chỉ nhận tối đa một số việc cùng lúc (mặc định **10 slot**). Máy chủ
giữ một bộ đếm trên Redis; hết slot thì việc nằm chờ chứ không dồn hết vào một
máy. Mỗi slot có thời gian hết hạn riêng, nên máy con bị treo hoặc tắt đột ngột
sẽ tự nhả slot thay vì giữ mãi.

### 3.3. Máy con chết thì sao

Máy chủ định kỳ dọn danh sách máy con. Máy nào không báo còn sống trong 600 giây
sẽ bị xoá khỏi danh sách, và việc của nó được chia lại cho máy khác.

---

## 4. Hệ thống ra quyết định dựa trên yếu tố gì

Quyết định đi qua **5 tầng lọc nối tiếp**. Chỉ khi vượt hết mới thành lệnh chờ duyệt.

### Tầng 1 — Dữ liệu đầu vào

| Loại | Nguồn | Tần suất |
|---|---|---|
| Giá crypto | Binance API | 5 phút |
| Giá chứng khoán | VNDirect, dự phòng SSI | 5 phút |
| Giá vàng | vang.today | 5 phút |
| Tin tức | 9 nguồn RSS: VnExpress, CafeF, Vietstock | 15 phút |
| Vĩ mô Mỹ | FRED *(tuỳ chọn)* | theo lượt |
| Xác suất sự kiện | Polymarket *(tuỳ chọn)* | theo lượt |

Mỗi tin tức được băm một mã định danh, nên cùng một bài xuất hiện ở nhiều nguồn
chỉ được tính một lần.

### Tầng 2 — Mô hình ML nhỏ

Một mô hình hồi quy logistic viết thuần bằng numpy, dùng **7 đặc trưng kỹ thuật**:

| Đặc trưng | Ý nghĩa |
|---|---|
| `return_1d` | Tăng/giảm 1 phiên |
| `return_5d` | Tăng/giảm 5 phiên |
| `return_20d` | Tăng/giảm 20 phiên |
| `rsi_14` | Quá mua / quá bán |
| `price_vs_sma20` | Giá so với trung bình 20 phiên |
| `volatility_20` | Độ biến động |
| `volume_ratio` | Thanh khoản bất thường |

Kết quả: xác suất giá tăng. Mô hình **chỉ huấn luyện trên dữ liệu trước ngày phân
tích** — không nhìn về tương lai, nếu không kết quả sẽ đẹp một cách giả tạo.

### Tầng 2.5 — Tổng hợp thông tin vi mô

Giá chỉ cho biết chuyện **đã xảy ra**. Tầng này đọc những thứ đi trước giá, lấy từ
sổ lệnh và dòng tiền của chính sàn:

| Tín hiệu | Nguồn | Ý nghĩa |
|---|---|---|
| Mất cân bằng thanh khoản | Binance (crypto), SSI (chứng khoán) | Bên mua hay bên bán đang dày hơn |
| Khối ngoại ròng | SSI | Nhà đầu tư nước ngoài mua ròng hay bán ròng |
| Dòng tiền chủ động | SSI | Lệnh nào đang sốt ruột hơn |
| Phí funding, open interest, long/short | Binance Futures | Thị trường đang nghiêng về bên nào |
| Giá trần / giá sàn | SSI | Còn bao xa tới biên độ |

**Đối chiếu chéo hai nguồn giá.** Chứng khoán Việt Nam có hai nguồn độc lập
(VNDirect và SSI), nên hệ thống hỏi cả hai và so với nhau. Một con số từ một nguồn
là *dữ liệu*; cùng con số từ hai nguồn khớp nhau mới là *thông tin*. Lệch quá 0,5%
thì báo động.

**Vì sao phải đo độ ổn định trước khi tin một tín hiệu.** Cách đo sổ lệnh hiển
nhiên nhất là cộng khối lượng của N mức gần giá khớp. Đo thực tế 8 lần liên tiếp:

| Cách đo | Độ lệch chuẩn | Kết luận |
|---|---|---|
| 5 mức đầu | 31% | ❌ nhiễu, đảo dấu giữa các lần gọi |
| 50 mức đầu | 45% | ❌ nhiễu |
| 100 mức đầu | 38% | ❌ nhiễu |
| 500 mức đầu | 5,5% | ✅ ổn định |
| **Thanh khoản trong ±0,5% giá khớp** | **2,2%** | ✅ **dùng cách này** |
| SSI sổ lệnh 3 mức | 0,07% | ✅ rất ổn định |

Đưa một tín hiệu nhiễu cho mô hình còn tệ hơn không đưa gì, vì nó tạo ra tự tin
giả. Nên hệ thống dùng thanh khoản quanh giá khớp trên sổ lệnh sâu (1000 mức), và
ngưỡng báo động khác nhau theo nhóm tài sản vì cách đo khác nhau.

### Tầng 3 — 12 tác nhân LLM

Đây là phần "suy nghĩ" của hệ thống, mô phỏng một công ty giao dịch:

| Nhóm | Số | Nhiệm vụ |
|---|---|---|
| **Analyst** | 4 | Cơ bản doanh nghiệp · Tâm lý thị trường · Tin tức vĩ mô · Kỹ thuật |
| **Researcher** | 2 | Một bò (lạc quan) và một gấu (bi quan) **tranh luận** |
| **Trader** | 1 | Tổng hợp báo cáo, quyết định thời điểm và khối lượng |
| **Risk** | 3 | Hiếu chiến / Thận trọng / Trung lập — tranh luận về rủi ro |
| **Manager** | 2 | Research Manager và Portfolio Manager ra quyết định cuối |

Các tác nhân đọc 6 nhóm dữ liệu: giá, chỉ báo kỹ thuật, cơ bản doanh nghiệp, tin
tức, vĩ mô, thị trường dự đoán.

### Tầng 4 — Hợp nhất hai nguồn

Mỗi tín hiệu được gán độ tin cậy:

| Tín hiệu LLM | Tin cậy |
|---|---|
| Buy | 0,90 |
| Overweight | 0,72 |
| Hold | 0,50 |
| Underweight | 0,35 |
| Sell | 0,20 |

Mô hình ML quy về cùng thang: `P=0,5 → 0,50` (không tin), `P=1,0 → 0,95` (rất tin).

**Luật hợp nhất — điểm quan trọng nhất:**

| Trường hợp | Kết quả |
|---|---|
| Hai bên cùng chiều mua | Giữ tín hiệu, tin cậy = trung bình **+0,1 thưởng đồng thuận** |
| Hai bên cùng chiều bán | Tương tự |
| LLM nói Hold | Hold, tin cậy 0,50 |
| **Hai bên mâu thuẫn** | **Hold, tin cậy 0,40 — đứng ngoài** |

Mâu thuẫn thì **không giao dịch**, chứ không bình quân hai bên. Hệ thống thiên về
thận trọng: thà bỏ lỡ cơ hội còn hơn hành động trên tín hiệu trái chiều.

### Tầng 5 — Cổng chặn và khối lượng

Một đề xuất chỉ **đáng chờ duyệt** khi thoả **cả ba** điều kiện:

```
1. action là "buy" hoặc "sell"     ← có hướng rõ ràng
2. quantity > 0                    ← khối lượng tính ra dương
3. confidence >= 0,55              ← đủ tin cậy
```

Không đạt thì ghi trạng thái `observed` — **chỉ để đối chiếu, không báo Telegram**,
để không làm phiền người dùng.

Khối lượng lệnh:

```
ngân sách = min(giá trị danh mục × 10%,  tiền mặt × 98%)
```

Không bao giờ dùng quá **10% danh mục** cho một lệnh, và luôn chừa **2% tiền mặt**
để tránh lệnh bị từ chối vì thiếu phí.

### Chốt cuối — người dùng duyệt

Lệnh vượt **5.000.000 VND** phải chờ người dùng bấm **✅ DUYỆT** trên Telegram.
Duyệt xong máy chủ mới đặt lệnh. Bấm hai lần không đặt hai lệnh — thao tác duyệt
có tính nguyên tử ở tầng cơ sở dữ liệu.

---

## 5. Vòng tự học

Đây là phần khiến hệ thống khác một công cụ tính chỉ báo thông thường.

1. Mỗi quyết định được ghi vào **nhật ký** kèm ngày và giá lúc đó.
2. Sau khi đủ số phiên nắm giữ (mặc định 5), hệ thống lấy **lợi nhuận thực tế** và
   **alpha** so với chỉ số chuẩn (VN-Index cho cổ phiếu Việt Nam, Bitcoin cho crypto).
3. Nó sinh một đoạn **tự rút kinh nghiệm** rồi bơm vào prompt của Portfolio Manager
   ở lần phân tích sau.

Nghĩa là hệ thống **học từ chính quyết định sai của mình**, không chỉ chạy lại
cùng một công thức.

Ngoài ra **checkpoint** lưu trạng thái sau mỗi bước, nên lượt phân tích bị hỏng
(mất mạng, hết hạn mức API) sẽ chạy tiếp từ bước cuối thay vì làm lại từ đầu — mỗi
lượt tốn khoảng 8 phút nên điều này quan trọng.

---

## 6. Nền tảng giao dịch

| Tài sản | Nền tảng | Trạng thái |
|---|---|---|
| Crypto (BTC/ETH) | Binance Spot | Đã có adapter, **mặc định chạy Testnet** (tiền giả) |
| Chứng khoán VN | SSI / TCBS / VPS | Chưa có, cần đăng ký API với công ty chứng khoán |
| Vàng (SJC/DOJI) | — | Không có API đặt lệnh; chỉ mua bán vật lý |

**Cửa chặn tiền thật:** muốn giao dịch tiền thật phải đặt **cả hai**
`BINANCE_TESTNET=false` **và** `BINANCE_LIVE_CONFIRM=YES`. Cố ý khó, để không ai
vô tình bật tiền thật chỉ vì sửa nhầm một dòng.

---

## 7. Kết quả kiểm thử thực tế

### Đã kiểm chứng chạy được

| Hạng mục | Kết quả |
|---|---|
| Bộ kiểm thử | **278 bài, đạt toàn bộ** |
| Máy con tham gia | 3/3 |
| Một lượt cào | 12 mức giá, 288 tin tức, 0 lỗi |
| Chống trùng tin | Lượt hai thêm 0 tin mới (đúng) |
| Phân tích LLM thật | 468 giây, ra quyết định cho FPT |

Bản quyết định thật do hệ thống viết có trích đúng số tiền mặt và việc danh mục
đang không giữ FPT — chứng minh **ngữ cảnh danh mục đã được truyền tới tác nhân**.

### Lỗi thật do đối chiếu chéo phát hiện

Khi mới thêm phần đối chiếu hai nguồn giá, kết quả báo **4/5 mã lệch nhau ~2,2%** —
lệch có hệ thống trên gần như mọi mã, một mức quá lớn để là sai số bình thường.

Truy vết ra: hàm dự phòng SSI đọc trường ``refPrice``, nhưng đó là **giá tham
chiếu, tức giá đóng cửa hôm trước**, không phải giá hiện tại. Giá khớp thật nằm ở
``matchedPrice``. Nghĩa là mỗi khi nguồn chính lỗi và hệ thống chuyển sang SSI, nó
ghi **giá cũ** như thể giá hiện tại — sai tới mức bằng cả biên độ một phiên.

Sau khi sửa: **5/5 mã khớp nhau**, trong đó 4 mã khớp chính xác 0,000%.

Đây là ví dụ rõ nhất cho giá trị của việc tổng hợp nhiều nguồn: một nguồn thì không
có cách nào biết mình đang đọc sai trường.

### Kết quả backtest — đọc kỹ trước khi nghĩ tới tiền thật

Chạy 46 ô (FPT, VNM, ETH, BTC), đo alpha so với chỉ số chuẩn:

| Tín hiệu | Số ô | Đúng hướng | Alpha |
|---|---|---|---|
| Buy | 3 | **0%** | −1,22% |
| Hold | 25 | không dự đoán hướng | +0,07% |
| Sell | 18 | 33% | −1,17% |

**Mô hình ML chỉ phát 3 tín hiệu Mua, và không tín hiệu nào đúng.** Tín hiệu Bán
đúng 33% — kém hơn cả đoán ngẫu nhiên.

Phần LLM đa tác nhân chưa đo được vì mới chạy một lượt; mỗi lượt tốn khoảng 8 phút.

---

## 8. Hạn chế đã biết

Nói rõ để không ai kỳ vọng sai.

1. **Mô hình ML chưa chứng minh được năng lực.** Kết quả backtest cho thấy nó chưa
   tốt hơn đoán ngẫu nhiên. Chưa nên dùng tiền thật.

2. **Bảng độ tin cậy là quy ước, không phải đo lường.** Các con số 0,90 / 0,72 /
   0,50 do người viết đặt ra, chưa có bằng chứng thống kê rằng tín hiệu Buy thực sự
   đúng 90% số lần. Muốn biết thật phải chạy backtest lớn.

3. **Chứng khoán Việt Nam chưa đặt lệnh thật được**, và ràng buộc T+2 (mua xong hai
   ngày mới bán được) chưa được mô hình hoá — bán ngay sau khi mua sẽ bị sàn từ chối.

4. **Vàng không tự động giao dịch được.** Trong nước chỉ mua bán vật lý tại cửa
   hàng, không có API đặt lệnh. Hệ thống chỉ giám sát giá và tin tức.

5. **Dữ liệu lịch sử miễn phí còn ngắn** (khoảng 270–320 phiên), nên backtest dài
   hạn bị giới hạn.

6. **Gói miễn phí của Google không đủ dùng** — 20 request/ngày cho mỗi model, mà
   một lượt phân tích tốn khoảng 20 lời gọi. Hệ thống đang dùng
   `deepseek-v4.1-flash` qua endpoint OpenAI-compatible.

---

## 9. Tóm tắt một câu

Hệ thống **lọc khá tốt việc gì KHÔNG nên làm** — mâu thuẫn thì đứng ngoài, giới
hạn 10% mỗi lệnh, luôn chờ người duyệt — nhưng **chưa chứng minh được việc nó chọn
là đúng**. Đó là việc của backtest quy mô lớn tiếp theo.
