# Triển khai FinAgent trên VMware

Hướng dẫn từng bước dựng **máy chủ Ubuntu Server** và các **máy con** trên VMware
Workstation, đúng theo yêu cầu đề tài.

> **Đã kiểm chứng thực tế trên VMware.** Toàn bộ quy trình dưới đây đã chạy thành
> công: **1 máy chủ + 3 máy con** Ubuntu Server 24.04.5 LTS. Máy con kết nối Redis
> qua mạng NAT và thu về **12 mẫu giá + 288 bài tin với 0 lỗi**; bộ máy ra quyết
> định tạo 7 đề xuất; luồng duyệt → đặt lệnh chạy đúng (ví USDT trừ tiền, ví VND
> không bị đụng tới).
>
> Địa chỉ cụm đã dựng: máy chủ `172.16.142.131`, máy con `.132`, `.133`, `.134`.

---

## 0. Kiểm tra trước khi bắt đầu

### 0.1. VMware phải dùng được

Đây là bước hay bị bỏ qua nhất. Sau mỗi lần nâng cấp hạt nhân Linux, hai module
`vmmon` và `vmnet` của VMware bị mất và **không máy ảo nào bật lên được** — kể cả
máy ảo bạn tạo từ trước. Kiểm tra:

```bash
lsmod | grep -E 'vmmon|vmnet'
```

Nếu **không có kết quả**, sửa bằng:

```bash
sudo vmware-modconfig --console --install-all
sudo systemctl restart vmware
lsmod | grep -E 'vmmon|vmnet'     # phải thấy cả vmmon và vmnet
```

Triệu chứng khi chưa sửa: `vmrun start ...` báo `Error: The operation was canceled`,
hoặc `systemctl status vmware.service` báo `failed`.

**Nếu chưa sửa được ngay** vẫn có thể kiểm chứng hệ thống bằng QEMU — xem mục 9.

### 0.2. Dung lượng đĩa

Mỗi máy ảo chiếm khoảng **1,9GB** đĩa thật (đĩa ảo 12GB nhưng chỉ cấp phát phần dùng).
Cần tối thiểu:

| Số máy | Dung lượng cần |
|---|---|
| Máy chủ + 1 máy con | ~4,5GB |
| Máy chủ + 3 máy con | ~8,5GB |

Script tự kiểm tra và dừng lại nếu thiếu đĩa.

### 0.3. RAM

Máy chủ nên có **2GB**, mỗi máy con **1–1,5GB**. Đừng chạy quá nhiều máy ảo cùng lúc
nếu máy thật ít RAM — máy con có thể bật/tắt luân phiên mà hệ thống vẫn hoạt động đúng.

---

## 1. Tạo máy ảo

```bash
cd finagent
bash deploy/provision_vms.sh --workers 3 --start
```

Script sẽ:

1. Tải **Ubuntu Server 24.04 cloud image** (~600MB) — nhanh hơn nhiều so với ISO
   cài đặt 2,7GB, và không cần ngồi bấm qua trình cài đặt.
2. Tạo khoá SSH nếu máy bạn chưa có.
3. Sinh **đĩa seed cloud-init** riêng cho từng máy: đặt hostname, tạo tài khoản
   `finagent`, nạp khoá SSH, cài sẵn các gói cần thiết.
4. Tạo **1 máy chủ + 3 máy con**, mỗi máy một đĩa ảo thin-provision 12GB.
5. Khởi động cả cụm (nhờ cờ `--start`).

Tuỳ chọn:

```bash
# Ít RAM hơn cho máy yếu
bash deploy/provision_vms.sh --workers 3 --ram 1024 --cpus 2

# Đổi thư mục chứa máy ảo
VM_ROOT=/mnt/ssd/vms bash deploy/provision_vms.sh --workers 3

# Không khởi động, chỉ tạo
bash deploy/provision_vms.sh --workers 3
```

### Kiểm tra máy ảo đã lên chưa

Lần boot đầu cần **1–2 phút** để cloud-init hoàn tất. Sau đó lấy địa chỉ IP:

```bash
for v in finagent-server finagent-worker1 finagent-worker2 finagent-worker3; do
  printf '%-22s ' "$v"
  vmrun getGuestIPAddress "$HOME/vmware/finagent/$v/$v.vmx" 2>/dev/null || echo "(chưa sẵn sàng)"
done
```

Kết quả mong đợi (dải NAT của VMware):

```
finagent-server        172.16.xx.128
finagent-worker1       172.16.xx.129
finagent-worker2       172.16.xx.130
finagent-worker3       172.16.xx.131
```

Thử SSH:

```bash
ssh finagent@172.16.xx.128      # mật khẩu dự phòng: finagent
```

---

## 2. Vì sao dùng mạng NAT?

Cả máy chủ và máy con đều để `ethernet0.connectionType = "nat"`. Chế độ này
(VMware gọi là `vmnet8`) cho **cùng một dải `172.16.x.x`**, nên:

- Các máy ảo **thấy nhau** → máy con kết nối được Redis của máy chủ.
- Các máy ảo **vẫn ra được internet** → cào được giá và tin tức.
- **Không cần đụng tới card Wi-Fi của máy thật.** Chế độ bridged hay lỗi với Wi-Fi
  (driver không hỗ trợ promiscuous mode, hoặc router chặn), và khi đó máy ảo mất mạng
  hoàn toàn.

Kiểm tra nhanh từ trong máy chủ:

```bash
ip -brief addr show          # phải thấy IP 172.16.x.x
ping -c1 8.8.8.8             # phải thông (có internet)
```

---

## 3. Cài máy chủ

SSH vào máy chủ, sao chép mã nguồn sang rồi chạy script cài đặt.

**Cách thuận tiện — dùng khoá SSH để chuyển mã nguồn:**

```bash
# Trên MÁY THẬT (máy bạn đang ngồi)
SERVER_IP=172.16.xx.128
rsync -av --exclude '.venv' --exclude 'data' --exclude '.git' \
      ~/duanmang/finagent/ finagent@$SERVER_IP:~/finagent/

# Vào máy chủ
ssh finagent@$SERVER_IP
cd ~/finagent
sudo bash deploy/setup_server.sh
```

Script cài đặt sẽ:

- Cài Python, Redis, SQLite, công cụ build.
- Cấu hình Redis lắng nghe trên `0.0.0.0` để máy con kết nối được (giới hạn bằng tường lửa).
- Tạo tài khoản dịch vụ `finagent` (không đăng nhập được, chỉ chạy dịch vụ).
- Cài mã nguồn vào `/opt/finagent` và tạo môi trường Python.
- Cài hai dịch vụ systemd chạy nền.
- Mở đúng hai cổng: **22 (SSH)** và **6379 (Redis)**.

### Điền cấu hình

```bash
sudo nano /opt/finagent/.env
```

Bắt buộc điền:

| Biến | Lấy ở đâu |
|---|---|
| `DEEPSEEK_API_KEY` | https://platform.deepseek.com |
| `TELEGRAM_BOT_TOKEN` | Nhắn `@BotFather` trên Telegram → `/newbot` |
| `TELEGRAM_CHAT_ID` | Nhắn `@userinfobot` trên Telegram |

### Khởi động

```bash
sudo -u finagent /opt/finagent/.venv/bin/finagent init
sudo systemctl start finagent-server finagent-bot
sudo systemctl status finagent-server --no-pager

# Theo dõi log
sudo journalctl -u finagent-server -f
```

---

## 4. Cài các máy con

Với **từng máy con**, thay `172.16.xx.128` bằng IP thật của máy chủ:

```bash
# Trên MÁY THẬT — chuyển mã nguồn
WORKER_IP=172.16.xx.129
rsync -av --exclude '.venv' --exclude 'data' --exclude '.git' \
      ~/duanmang/finagent/ finagent@$WORKER_IP:~/finagent/

# Vào máy con
ssh finagent@$WORKER_IP
cd ~/finagent
sudo REDIS_HOST=172.16.xx.128 bash deploy/setup_worker.sh
```

Script sẽ tự kiểm tra kết nối tới Redis trước khi cài, rồi tạo và bật dịch vụ
`finagent-worker`. Máy con **không cần** khoá API LLM hay Telegram — nó chỉ thu thập dữ liệu.

Làm tương tự cho máy con 2 và 3.

---

## 5. Kiểm tra toàn hệ thống

Trên **máy chủ**:

```bash
cd /opt/finagent
sudo -u finagent .venv/bin/finagent status
```

Kết quả mong đợi:

```
🖥️  CỤM MÁY CON
   Slot đang dùng: 0/10
   Hàng đợi      : crawl
   • finagent-worker1   host=finagent-worker1   việc cuối=crawl_news
   • finagent-worker2   host=finagent-worker2   việc cuối=crawl_crypto
   • finagent-worker3   host=finagent-worker3   việc cuối=crawl_vn_stock
```

Thử một lượt thu thập và xem dữ liệu về:

```bash
sudo -u finagent .venv/bin/finagent collect
sudo -u finagent .venv/bin/finagent status
```

Số ở mục **Mẫu giá** và **Bài tin** phải tăng lên.

Quét thị trường thử (không gọi LLM để tiết kiệm token):

```bash
sudo -u finagent .venv/bin/finagent scan --no-llm
sudo -u finagent .venv/bin/finagent pending
```

Nếu đã cấu hình Telegram, bạn sẽ nhận được tin nhắn kèm hai nút **✅ DUYỆT** và **❌ TỪ CHỐI**.

---

## 6. Thêm hoặc bớt máy con

**Thêm máy con** — chỉ cần chạy `setup_worker.sh` trên máy mới. Máy chủ **không phải
sửa gì**: máy con tự đăng ký khi thực hiện việc đầu tiên, và máy chủ tự chia lại
danh sách nguồn tin cho vừa số máy đang online.

**Bớt máy con** — dừng dịch vụ trên máy đó:

```bash
sudo systemctl stop finagent-worker
sudo systemctl disable finagent-worker
```

Việc đang chạy dở sẽ được Celery giao lại cho máy khác (`task_acks_late=True`).

**Giới hạn tải** — dù có bao nhiêu máy con, tổng số việc chạy song song vẫn bị chặn
ở `FINAGENT_MAX_CONCURRENCY` (mặc định 10). Sửa trong `/opt/finagent/.env`:

```bash
FINAGENT_MAX_CONCURRENCY=10
```

---

## 7. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `vmrun start` → `Error: The operation was canceled` | Thiếu module `vmmon`/`vmnet` | `sudo vmware-modconfig --console --install-all` |
| Máy ảo vẫn không bật dù module đã nạp | VMX thiếu khai báo **PCI bridge**, hoặc dùng card `vmxnet3` | Xem mục 7.1 bên dưới |
| `vmrun getGuestIPAddress` báo "VMware Tools are not running" | Tools vừa được cài trong lần boot đầu, VMware chưa chuyển sang "running" | Khởi động lại máy ảo một lần. `make_seed_iso.py` đã tự động reboot sẵn; máy cũ thì dò DHCP ở mục 7.2 |
| VMX bị "Cannot read the virtual machine configuration file" | Trùng khoá trong VMX (VMware tự ghi thêm sau mỗi lần boot lỗi) | Kiểm tra `grep -oE "^[a-zA-Z0-9:._]+" <file>.vmx \| sort \| uniq -d` |
| `finagent status` báo chưa có máy con | Máy con chưa chạy, hoặc sai `REDIS_URL` | `sudo journalctl -u finagent-worker -n 50` trên máy con |
| Máy con không kết nối được Redis | Tường lửa máy chủ chặn 6379 | `sudo ufw allow 6379/tcp` trên máy chủ |
| Máy chủ không có internet | NAT lỗi | Kiểm tra `ping 8.8.8.8` trong máy ảo; thử `vmrun stop <vmx> hard` rồi start lại |
| Đề xuất không gửi được Telegram | Thiếu token/chat_id | Kiểm tra `journalctl -u finagent-server`, xem cảnh báo lúc khởi động |
| `finagent-bot` báo `failed` hoặc `activating` | Chưa điền token Telegram | Bình thường — bot thoát gọn và chờ. Điền token rồi `sudo systemctl start finagent-bot` |
| Máy ảo boot nhưng không vào được | cloud-init chưa xong | Đợi thêm 60 giây; xem console bằng `vmrun start <vmx>` (có giao diện) |
| Hết dung lượng đĩa | Nhiều máy ảo | Giảm `--workers`, hoặc `rm -rf ~/vmware/finagent/_base` sau khi tạo xong |

### 7.1. Máy ảo không bật được vì lỗi PCI

Đây là lỗi đã gặp thật khi dựng máy chủ đầu tiên, và nó **không** liên quan tới
module `vmmon`/`vmnet`. Trong `vmware.log` của máy ảo sẽ thấy:

```
Vmxnet3 PCI: failed to reserve slot for vmxnet3 PCIe device
Module 'DevicePowerOn' power on failed.
```

Hai nguyên nhân, phải sửa cả hai:

1. **Thiếu khai báo PCI bridge.** VMware Workstation cần biết các bridge để cấp
   phát slot PCIe. Thêm vào VMX:

   ```
   pciBridge0.present = "TRUE"
   pciBridge4.present = "TRUE"
   pciBridge4.virtualDev = "pcieRootPort"
   pciBridge4.functions = "8"
   pciBridge5.present = "TRUE"
   pciBridge5.virtualDev = "pcieRootPort"
   pciBridge5.functions = "8"
   pciBridge6.present = "TRUE"
   pciBridge6.virtualDev = "pcieRootPort"
   pciBridge6.functions = "8"
   pciBridge7.present = "TRUE"
   pciBridge7.virtualDev = "pcieRootPort"
   pciBridge7.functions = "8"
   ```

2. **Dùng card `e1000` thay `vmxnet3`.** `vmxnet3` cần slot PCIe mà bố cục mặc
   định không đủ chỗ; `e1000` chạy trên bus PCI truyền thống nên luôn hoạt động.

Mẫu VMX trong `provision_vms.sh` đã có sẵn cả hai, nên máy tạo mới không gặp lỗi này.

### 7.2. Lấy địa chỉ IP của máy ảo

**Cách luôn hoạt động — đọc bảng cấp phát DHCP của mạng NAT.** Đây là cách nên
dùng, vì nó không phụ thuộc VMware Tools:

```bash
# MAC của từng máy ảo
grep generatedAddress ~/vmware/finagent/*/*.vmx

# Địa chỉ đã cấp (khớp MAC ở trên)
grep -E "^lease|hardware ethernet" /etc/vmware/vmnet8/dhcpd/dhcpd.leases | tail -20

# Hoặc quét ARP toàn dải NAT
for i in $(seq 128 145); do (ping -c1 -W1 172.16.142.$i >/dev/null 2>&1 &); done
sleep 3; ip neigh | grep 172.16.142 | grep -v INCOMPLETE
```

**Cách tiện hơn — `vmrun getGuestIPAddress`:**

```bash
vmrun -T ws checkToolsState ~/vmware/finagent/finagent-worker1/finagent-worker1.vmx
vmrun getGuestIPAddress ~/vmware/finagent/finagent-worker1/finagent-worker1.vmx
```

Cách này cần `open-vm-tools` ở trạng thái **running**. Tools được cloud-init cài sẵn
(`make_seed_iso.py`), nhưng VMware chỉ chuyển sang `running` sau khi máy ảo khởi
động lại một lần — nên `make_seed_iso.py` đã cho máy tự reboot ở cuối cloud-init.

> **Lưu ý thực tế đã gặp:** trên cụm đã dựng, chỉ một số máy chuyển được sang
> `running`; các máy còn lại báo `installed` dù `vmtoolsd` trong máy ảo chạy tốt
> (`vmware-toolbox-cmd stat speed` vẫn trả kết quả). Đây là vấn đề theo dõi phía
> host của VMware Workstation, **không ảnh hưởng tới hoạt động của hệ thống** — máy
> con vẫn kết nối Redis và thu thập bình thường. Vì vậy hãy dùng cách đọc DHCP ở
> trên khi cần IP.

### Lệnh quản lý máy ảo hữu ích

```bash
VM=$HOME/vmware/finagent/finagent-server/finagent-server.vmx

vmrun list                        # máy ảo nào đang chạy
vmrun start "$VM" nogui           # bật (không giao diện)
vmrun start "$VM"                 # bật kèm giao diện (để xem console)
vmrun stop  "$VM" soft            # tắt nhẹ nhàng
vmrun stop  "$VM" hard            # tắt cưỡng bức khi treo
vmrun getGuestIPAddress "$VM"     # lấy IP
vmrun snapshot "$VM" ten-snapshot # tạo ảnh chụp để khôi phục
```

---

## 8. Mô hình tham chiếu

Cấu hình đã kiểm chứng, phù hợp một máy 16GB RAM:

| Máy | vCPU | RAM | Đĩa | Vai trò |
|---|---|---|---|---|
| `finagent-server` | 2 | 2048MB | 12GB | Redis, điều phối, LLM, Telegram, đặt lệnh |
| `finagent-worker1` | 2 | 1024MB | 12GB | Cào giá + tin tức |
| `finagent-worker2` | 2 | 1024MB | 12GB | Cào giá + tin tức |
| `finagent-worker3` | 2 | 1024MB | 12GB | Cào giá + tin tức |

Tổng RAM máy ảo: ~5GB. Có thể chạy 3 máy con luân phiên nếu máy thật ít RAM —
hệ thống vẫn hoạt động đúng vì việc thu thập được giao động theo máy nào đang online.

---

## 9. Kiểm chứng bằng QEMU khi VMware chưa chạy được

Nếu chưa chạy được `sudo vmware-modconfig` (mục 0.1), vẫn có thể kiểm chứng **cùng
một cloud image và cùng một đĩa seed cloud-init** bằng QEMU — thứ có sẵn trên
Ubuntu và dùng KVM nên tốc độ tương đương VMware.

```bash
cd finagent

# Tạo đĩa, boot máy chủ, chờ cloud-init xong (khoảng 1–3 phút)
bash deploy/test_vm_qemu.sh start

# Kiểm tra máy chủ đã dựng đúng chưa
bash deploy/test_vm_qemu.sh ssh
    hostname                 # phải là: finagent-server
    cloud-init status        # phải là: status: done
    dpkg -l | grep -c ii     # các gói đã cài

# Vào máy ảo rồi cài FinAgent y như trên VMware thật
    sudo bash deploy/setup_server.sh   # (sau khi đã chuyển mã nguồn vào)

# Dọn dẹp
bash deploy/test_vm_qemu.sh stop
bash deploy/test_vm_qemu.sh clean
```

Script tự chờ **cloud-init hoàn tất**, không chỉ chờ SSH thông — vì SSH thông chưa
có nghĩa là các gói `python3-venv`, `redis-server`… đã cài xong. Bỏ qua bước đó thì
lệnh cài đặt chạy ngay sau sẽ thất bại.

### Kết quả đã đạt được khi kiểm chứng

| Hạng mục | Kết quả |
|---|---|
| Hệ điều hành | Ubuntu 24.04.5 LTS |
| Hostname do cloud-init đặt | `finagent-server` ✓ |
| Tài khoản | `finagent` (có sudo, khoá SSH đã nạp) ✓ |
| Gói cloud-init cài | git, curl, rsync, sqlite3, redis-tools, python3-venv, build-essential ✓ |
| Đĩa tự giãn | 1,8GB dùng / 11GB khả dụng ✓ |
| Internet từ máy ảo | Binance 200, VNDirect 200 ✓ |
| Redis trên máy chủ | `active`, trả `PONG` ✓ |
| Tường lửa | chỉ mở 22 (SSH) và 6379 (Redis) ✓ |
| Phiên bản TradingAgents | 0.5.1 — khớp bản đã kiểm thử ✓ |
| Máy con kết nối | `worker-trong-may-chu` đăng ký thành công ✓ |
| Thu thập phân tán | **12 mẫu giá + 288 bài tin, 0 lỗi** ✓ |
| Phân loại chủ đề | vàng 21, vĩ mô 17, bất động sản 28, chứng khoán 78 ✓ |
| Ra quyết định | 7 đề xuất, 2 chờ người dùng duyệt ✓ |

Lệnh kiểm chứng nhanh toàn hệ thống bên trong máy chủ:

```bash
cd /opt/finagent
sudo -u finagent .venv/bin/finagent status      # cụm máy con + kho dữ liệu
sudo -u finagent .venv/bin/finagent collect     # một lượt thu thập phân tán
sudo -u finagent .venv/bin/finagent scan --no-llm   # quét thị trường bằng ML
```
