#!/usr/bin/env bash
# =============================================================================
#  FinAgent — tạo máy ảo VMware cho MÁY CHỦ và các MÁY CON
#
#  Dùng Ubuntu Server cloud image thay vì ISO cài đặt:
#    • không cần trình cài đặt tương tác, không cần ngồi bấm Enter
#    • chỉ tải ~600MB thay vì ~2,7GB
#    • mỗi máy ảo chỉ chiếm ~700MB đĩa thật (thin provision)
#
#  Cách dùng:
#      bash deploy/provision_vms.sh --workers 3
#      bash deploy/provision_vms.sh --workers 3 --ram 1536 --cpus 2
#      bash deploy/provision_vms.sh --workers 3 --start        # khởi động luôn
#
#  Sau khi script chạy xong, xem deploy/README.md để cài FinAgent vào từng máy.
# =============================================================================
set -euo pipefail

# --- Tham số ----------------------------------------------------------------
WORKERS=3
RAM_MB=1536
CPUS=2
DISK_GB=12
VM_ROOT="${VM_ROOT:-$HOME/vmware/finagent}"
BASE_DIR="$VM_ROOT/_base"
START_VMS=0
NODE_USER="finagent"
NODE_PASS="finagent"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519.pub}"

CLOUD_IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers) WORKERS="$2"; shift 2 ;;
        --ram)     RAM_MB="$2";  shift 2 ;;
        --cpus)    CPUS="$2";    shift 2 ;;
        --disk)    DISK_GB="$2"; shift 2 ;;
        --start)   START_VMS=1;  shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) die "Tham số không hợp lệ: $1" ;;
    esac
done

# --- Kiểm tra công cụ -------------------------------------------------------
log "0/6 — Kiểm tra công cụ và dung lượng đĩa"
for tool in qemu-img vmrun curl; do
    command -v "$tool" >/dev/null 2>&1 || die "Thiếu công cụ '$tool'."
done
[[ -x "$PYTHON_BIN" ]] || die "Không thấy Python tại $PYTHON_BIN
Hãy tạo môi trường trước: cd $PROJECT_DIR && python3 -m venv .venv && .venv/bin/pip install -e . pycdlib"
"$PYTHON_BIN" -c 'import pycdlib' 2>/dev/null || die "Thiếu pycdlib: $PYTHON_BIN -m pip install pycdlib"

# VMware chỉ bật được máy ảo khi hai module hạt nhân vmmon/vmnet đã được nạp.
# Sau mỗi lần nâng cấp hạt nhân, các module này mất và phải build lại — kiểm tra
# ngay từ đầu để báo lỗi rõ ràng thay vì để vmrun thất bại khó hiểu về sau.
if ! lsmod 2>/dev/null | grep -q '^vmmon'; then
    warn "Module hạt nhân 'vmmon' chưa được nạp — VMware chưa dùng được."
    warn "Đây là điều kiện tiên quyết của máy bạn, không phải lỗi của script."
    cat <<'VMWARE_FIX'

    CÁCH SỬA (cần quyền root, chạy một lần):

        sudo vmware-modconfig --console --install-all
        sudo systemctl restart vmware

    Kiểm tra lại:
        lsmod | grep -E 'vmmon|vmnet'

    Sau khi thấy cả vmmon và vmnet là đã sẵn sàng.
    Script vẫn tiếp tục tạo máy ảo (phần này không cần VMware),
    nhưng sẽ KHÔNG khởi động được cho tới khi sửa xong.

VMWARE_FIX
fi

TOTAL_VMS=$((WORKERS + 1))
# Đo thực tế: mỗi máy ảo chiếm ~1,9GB đĩa thật (đĩa ảo 12GB, thin provision),
# cộng ~700MB ảnh cloud gốc nếu chưa tải, nhân hệ số an toàn 1,5.
#
# Chỉ tính dung lượng cho những máy CHƯA tồn tại: máy đã tạo thì đĩa đã chiếm chỗ
# rồi, tính lại sẽ chặn oan — đúng tình huống gặp phải khi tạo thêm máy con trong
# khi máy chủ đã có sẵn.
NEW_VMS=0
for _vm in "finagent-server" $(for i in $(seq 1 "$WORKERS"); do echo "finagent-worker$i"; done); do
    [[ -f "$VM_ROOT/$_vm/$_vm.vmx" ]] || NEW_VMS=$((NEW_VMS + 1))
done
BASE_MB=0
[[ -f "$BASE_DIR/noble-server-cloudimg-amd64.img" ]] || BASE_MB=700
NEEDED_MB=$(( (NEW_VMS * 1900 + BASE_MB) * 3 / 2 ))
AVAIL_MB=$(df -Pm "$VM_ROOT" 2>/dev/null | awk 'NR==2{print $4}' || df -Pm "$HOME" | awk 'NR==2{print $4}')
if [[ -n "$AVAIL_MB" && "$AVAIL_MB" -lt "$NEEDED_MB" ]]; then
    die "Không đủ dung lượng đĩa.
    Cần khoảng ${NEEDED_MB}MB cho ${NEW_VMS} máy ảo mới, chỉ còn ${AVAIL_MB}MB tại $(dirname "$VM_ROOT").
    Hãy giải phóng bớt dung lượng, giảm số máy con (--workers), hoặc giảm --disk."
fi
log "    Đĩa trống: ${AVAIL_MB}MB — cần khoảng ${NEEDED_MB}MB cho ${NEW_VMS} máy ảo mới. Đạt."

mkdir -p "$VM_ROOT" "$BASE_DIR"

# --- Tải cloud image --------------------------------------------------------
log "1/6 — Chuẩn bị Ubuntu Server cloud image"
BASE_IMG="$BASE_DIR/noble-server-cloudimg-amd64.img"
if [[ -f "$BASE_IMG" ]]; then
    log "    Đã có sẵn: $BASE_IMG ($(du -h "$BASE_IMG" | cut -f1))"
else
    log "    Đang tải (~600MB, có thể mất vài phút)…"
    curl -L --progress-bar --retry 3 --continue-at - -o "$BASE_IMG.part" "$CLOUD_IMG_URL"
    mv "$BASE_IMG.part" "$BASE_IMG"
    log "    Đã tải xong: $(du -h "$BASE_IMG" | cut -f1)"
fi

# --- Sinh khoá SSH để truy cập máy ảo --------------------------------------
log "2/6 — Chuẩn bị khoá SSH"
if [[ ! -f "$SSH_KEY" ]]; then
    KEY_BASE="${SSH_KEY%.pub}"
    if [[ ! -f "$KEY_BASE" ]]; then
        warn "Chưa có khoá SSH — đang tạo mới tại $KEY_BASE"
        ssh-keygen -t ed25519 -N "" -C "finagent@$(hostname)" -f "$KEY_BASE" >/dev/null
    fi
    SSH_KEY="$KEY_BASE.pub"
fi
log "    Dùng khoá công khai: $SSH_KEY"

# --- Hàm tạo một máy ảo -----------------------------------------------------
create_vm() {
    local name="$1" role="$2"
    local vm_dir="$VM_ROOT/$name"
    local vmx="$vm_dir/$name.vmx"
    local seed="$vm_dir/seed.iso"

    log "    • $name ($role)"

    if [[ -f "$vmx" ]]; then
        warn "      Đã tồn tại, bỏ qua: $vmx"
        return 0
    fi
    mkdir -p "$vm_dir"

    # 1) Đĩa ảo thin-provision, dựa trên ảnh gốc rồi nới rộng.
    qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMG" "$vm_dir/tmp.qcow2" >/dev/null
    qemu-img resize -q "$vm_dir/tmp.qcow2" "${DISK_GB}G" >/dev/null
    qemu-img convert -q -f qcow2 -O vmdk \
        -o subformat=monolithicSparse,adapter_type=lsilogic \
        "$vm_dir/tmp.qcow2" "$vm_dir/$name.vmdk"
    rm -f "$vm_dir/tmp.qcow2"

    # 2) Đĩa seed cloud-init riêng: hostname, tài khoản, khoá SSH.
    "$PYTHON_BIN" "$SCRIPT_DIR/make_seed_iso.py" \
        --out "$seed" --hostname "$name" --role "$role" \
        --user "$NODE_USER" --password "$NODE_PASS" --ssh-key "$SSH_KEY" >/dev/null

    # 3) File cấu hình máy ảo.
    #
    # Hai điểm BẮT BUỘC, nếu thiếu thì VMware báo "failed to reserve slot" và máy
    # ảo không bật lên được (đã gặp thực tế khi dựng máy chủ đầu tiên):
    #   • phải khai báo các PCI bridge để VMware cấp phát slot PCIe;
    #   • dùng card mạng e1000 thay vì vmxnet3 — vmxnet3 cần slot PCIe mà bố cục
    #     mặc định của Workstation không đủ chỗ.
    cat > "$vmx" <<EOF
#!/usr/bin/vmware
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "21"

displayName = "FinAgent - $name ($role)"
guestOS = "ubuntu-64"
annotation = "FinAgent $role - sinh boi provision_vms.sh"

numvcpus = "$CPUS"
cpuid.coresPerSocket = "$CPUS"
memSize = "$RAM_MB"

# PCI bridges — bắt buộc, xem ghi chú phía trên.
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
pciBridge0.pciSlotNumber = "17"
pciBridge4.pciSlotNumber = "21"
pciBridge5.pciSlotNumber = "22"
pciBridge6.pciSlotNumber = "23"
pciBridge7.pciSlotNumber = "24"

scsi0.present = "TRUE"
scsi0.virtualDev = "lsilogic"
scsi0.pciSlotNumber = "16"

scsi0:0.present = "TRUE"
scsi0:0.fileName = "$name.vmdk"
scsi0:0.deviceType = "disk"

sata0.present = "TRUE"
sata0.pciSlotNumber = "36"

# Đĩa seed cloud-init (đóng vai trò "cấu hình khởi động")
sata0:0.present = "TRUE"
sata0:0.deviceType = "cdrom-image"
sata0:0.fileName = "$seed"
sata0:0.startConnected = "TRUE"

# Mạng NAT: các máy ảo cùng dải 172.16.x.x nên thấy nhau, vẫn ra được internet
ethernet0.present = "TRUE"
ethernet0.connectionType = "nat"
ethernet0.virtualDev = "e1000"
ethernet0.pciSlotNumber = "33"
ethernet0.wakeOnPcktRcv = "FALSE"
ethernet0.addressType = "generated"

svga.present = "TRUE"
svga.vramSize = "16777216"
mks.enable3d = "FALSE"
usb.present = "TRUE"
usb.pciSlotNumber = "32"
ehci.pciSlotNumber = "35"
sound.present = "FALSE"
sound.pciSlotNumber = "34"

firmware = "efi"
bios.bootOrder = "hdd,cdrom"
tools.syncTime = "TRUE"
tools.upgrade.policy = "manual"
hpet0.present = "TRUE"
vmci0.present = "TRUE"
powerType.powerOff = "soft"
powerType.powerOn = "soft"
powerType.suspend = "soft"
powerType.reset = "soft"
EOF
}

# --- Tạo máy chủ và các máy con ---------------------------------------------
log "3/6 — Tạo máy chủ"
create_vm "finagent-server" "máy chủ"

log "4/6 — Tạo $WORKERS máy con"
for i in $(seq 1 "$WORKERS"); do
    create_vm "finagent-worker$i" "máy con $i"
done

# --- Tổng kết ---------------------------------------------------------------
log "5/6 — Kiểm tra kết quả"
printf '    %-24s %-10s %s\n' "MÁY ẢO" "ĐĨA" "ĐƯỜNG DẪN"
for vm in finagent-server $(for i in $(seq 1 "$WORKERS"); do echo "finagent-worker$i"; done); do
    vmx="$VM_ROOT/$vm/$vm.vmx"
    if [[ -f "$vmx" ]]; then
        printf '    %-24s %-10s %s\n' "$vm" "$(du -sh "$VM_ROOT/$vm" | cut -f1)" "$vmx"
    fi
done
log "    Tổng dung lượng đã dùng: $(du -sh "$VM_ROOT" | cut -f1)"

# --- Khởi động (tuỳ chọn) ---------------------------------------------------
if [[ "$START_VMS" == "1" ]]; then
    log "6/6 — Khởi động các máy ảo"
    for vm in finagent-server $(for i in $(seq 1 "$WORKERS"); do echo "finagent-worker$i"; done); do
        vmx="$VM_ROOT/$vm/$vm.vmx"
        [[ -f "$vmx" ]] || continue
        if vmrun start "$vmx" nogui >/dev/null 2>&1; then
            echo "    ▶ $vm đang khởi động…"
        else
            warn "    Không khởi động được $vm"
        fi
    done
    log "    Đợi cloud-init hoàn tất (khoảng 60–120 giây cho lần boot đầu)…"
else
    log "6/6 — Bỏ qua khởi động (thêm --start để bật máy luôn)"
fi

cat <<EOF

╔══════════════════════════════════════════════════════════════════════════╗
║  ĐÃ TẠO XONG MÁY ẢO                                                      ║
╚══════════════════════════════════════════════════════════════════════════╝

  Thư mục gốc : $VM_ROOT
  Tài khoản   : $NODE_USER / $NODE_PASS   (đăng nhập console dự phòng)
  Khoá SSH    : $SSH_KEY

  BẬT MÁY (nếu chưa dùng --start):
     vmrun start "$VM_ROOT/finagent-server/finagent-server.vmx" nogui
     vmrun start "$VM_ROOT/finagent-worker1/finagent-worker1.vmx" nogui

  LẤY ĐỊA CHỈ IP (sau khi máy đã boot ~2 phút):
     for v in finagent-server finagent-worker{1..$WORKERS}; do
       echo -n "\$v: "; vmrun getGuestIPAddress "$VM_ROOT/\$v/\$v.vmx" 2>/dev/null || echo "(chưa sẵn sàng)"
     done

  CÀI FINAGENT — xem hướng dẫn đầy đủ trong deploy/README.md

EOF