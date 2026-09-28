#!/usr/bin/env bash
# =============================================================================
#  FinAgent — kiểm chứng máy ảo bằng QEMU/KVM (không cần VMware)
#
#  Máy ảo VMware chỉ chạy được khi hai module hạt nhân vmmon/vmnet đã nạp. Khi
#  chúng chưa có (thường sau mỗi lần nâng cấp hạt nhân), script này cho phép vẫn
#  kiểm chứng được **cùng một cloud image và cùng một đĩa seed cloud-init** bằng
#  QEMU — vốn có sẵn trên Ubuntu và dùng KVM nên tốc độ tương đương.
#
#  Nhờ vậy khẳng định được: cloud-init đặt đúng hostname, tạo đúng tài khoản, nạp
#  đúng khoá SSH và cài đủ gói — tức là phần cấu hình máy chủ là đúng, độc lập với
#  việc VMware có chạy được hay không.
#
#  Cách dùng:
#      bash deploy/test_vm_qemu.sh start     # tạo đĩa, boot, chờ SSH sẵn sàng
#      bash deploy/test_vm_qemu.sh ssh       # vào máy ảo
#      bash deploy/test_vm_qemu.sh status    # kiểm tra máy ảo đang chạy
#      bash deploy/test_vm_qemu.sh stop      # tắt máy ảo
#      bash deploy/test_vm_qemu.sh clean     # xoá toàn bộ dữ liệu kiểm thử
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"

WORK_DIR="${WORK_DIR:-$PROJECT_DIR/.vmtest}"
BASE_IMG="${BASE_IMG:-$HOME/vmware/finagent/_base/noble-server-cloudimg-amd64.img}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSH_PORT="${SSH_PORT:-2222}"
RAM_MB="${RAM_MB:-2048}"
CPUS="${CPUS:-2}"
VM_NAME="finagent-server"
DISK_GB=12

PID_FILE="$WORK_DIR/qemu.pid"
LOG_FILE="$WORK_DIR/qemu.log"
DISK="$WORK_DIR/$VM_NAME.qcow2"
SEED="$WORK_DIR/seed.iso"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

cmd_start() {
    command -v qemu-system-x86_64 >/dev/null || die "Thiếu qemu-system-x86_64."
    [[ -f "$BASE_IMG" ]] || die "Không thấy cloud image tại $BASE_IMG
Hãy tạo máy ảo trước bằng: bash deploy/provision_vms.sh --workers 0
hoặc tải thủ công: curl -LO https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"

    # KVM nhanh hơn hẳn TCG; thiếu KVM vẫn chạy được nhưng rất chậm.
    local accel="kvm"
    if [[ ! -r /dev/kvm ]]; then
        warn "Không truy cập được /dev/kvm — sẽ chạy bằng TCG (rất chậm)."
        accel="tcg"
    fi

    if running; then
        warn "Máy ảo đang chạy sẵn (pid $(cat "$PID_FILE")). Dùng: $0 stop"
        return 0
    fi

    mkdir -p "$WORK_DIR"

    # Đĩa ghi được, dựa trên ảnh gốc để không phải tải lại.
    if [[ ! -f "$DISK" ]]; then
        log "Tạo đĩa ảo ${DISK_GB}GB từ ảnh gốc (thin provision)"
        qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMG" "$DISK"
        qemu-img resize -q "$DISK" "${DISK_GB}G"
    fi

    # Đĩa seed cloud-init — dùng lại đúng công cụ mà bản VMware dùng.
    if [[ ! -f "$SEED" ]]; then
        log "Sinh đĩa seed cloud-init"
        "$PYTHON_BIN" "$SCRIPT_DIR/make_seed_iso.py" \
            --out "$SEED" --hostname "$VM_NAME" --role "máy chủ" \
            --user finagent --password finagent --ssh-key "$SSH_KEY.pub" >/dev/null
    fi

    log "Khởi động máy chủ ảo (${CPUS} vCPU, ${RAM_MB}MB RAM, tăng tốc: $accel)"
    qemu-system-x86_64 \
        -name "$VM_NAME" \
        -accel "$accel" \
        -machine q35 \
        -cpu host -smp "$CPUS" -m "$RAM_MB" \
        -drive file="$DISK",if=virtio,format=qcow2,cache=writeback \
        -drive file="$SEED",if=virtio,format=raw,readonly=on \
        -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -display none -serial file:"$LOG_FILE" \
        -pidfile "$PID_FILE" \
        -daemonize

    log "Đang chờ máy ảo khởi động và cloud-init hoàn tất…"
    local waited=0
    until ssh -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
              -o ConnectTimeout=3 -o BatchMode=yes \
              -i "$SSH_KEY" -p "$SSH_PORT" finagent@127.0.0.1 true 2>/dev/null; do
        sleep 5
        waited=$((waited + 5))
        if ! running; then
            die "Máy ảo đã thoát. Xem log: $LOG_FILE"
        fi
        if (( waited >= 420 )); then
            die "Hết thời gian chờ SSH (${waited}s). Xem log: $LOG_FILE"
        fi
        if (( waited % 30 == 0 )); then
            echo "    … đã chờ ${waited}s"
        fi
    done

    echo "    SSH đã thông sau ${waited}s"

    # SSH thông **chưa** có nghĩa là cloud-init đã cài xong gói. Bỏ qua bước này
    # thì các lệnh cài đặt chạy ngay sau sẽ thất bại vì thiếu python3-venv,
    # redis-server… — đúng lỗi gặp phải khi thử lần đầu.
    log "Đang chờ cloud-init cài xong các gói cần thiết…"
    local ci_waited=0 ci_status=""
    while true; do
        ci_status=$(ssh -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                    -o ConnectTimeout=5 -o BatchMode=yes \
                    -i "$SSH_KEY" -p "$SSH_PORT" finagent@127.0.0.1 \
                    'cloud-init status 2>/dev/null || echo "status: unknown"' 2>/dev/null \
                    || echo "status: unknown")
        case "$ci_status" in
            *"status: done"*)    echo "    cloud-init đã hoàn tất sau ${ci_waited}s"; break ;;
            *"status: error"*)   warn "cloud-init báo lỗi — kiểm tra: $0 ssh, rồi 'sudo cloud-init status --long'"; break ;;
        esac
        sleep 5
        ci_waited=$((ci_waited + 5))
        if (( ci_waited >= 600 )); then
            warn "cloud-init chạy quá ${ci_waited}s — vẫn tiếp tục, nhưng nên kiểm tra lại."
            break
        fi
        if (( ci_waited % 30 == 0 )); then
            echo "    … cloud-init đang chạy (${ci_waited}s)"
        fi
    done

    log "Máy chủ đã sẵn sàng (SSH ${waited}s, cloud-init ${ci_waited}s)"
    printf '\n    SSH : ssh -i %s -p %s finagent@127.0.0.1\n' "$SSH_KEY" "$SSH_PORT"
    printf '    Mật khẩu dự phòng qua console: finagent\n\n'
}

cmd_ssh() {
    running || die "Máy ảo chưa chạy. Dùng: $0 start"
    exec ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -i "$SSH_KEY" -p "$SSH_PORT" finagent@127.0.0.1
}

cmd_status() {
    if running; then
        echo "Máy ảo ĐANG CHẠY (pid $(cat "$PID_FILE"), SSH cổng $SSH_PORT)"
        echo "Nhật ký console: $LOG_FILE"
    else
        echo "Máy ảo KHÔNG chạy."
    fi
    [[ -f "$DISK" ]] && echo "Đĩa: $(du -h "$DISK" | cut -f1) — $DISK"
}

cmd_stop() {
    if running; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        sleep 2
        rm -f "$PID_FILE"
        log "Đã tắt máy ảo."
    else
        warn "Máy ảo không chạy."
    fi
}

cmd_clean() {
    cmd_stop || true
    rm -rf "$WORK_DIR"
    log "Đã xoá $WORK_DIR"
}

case "${1:-start}" in
    start)  cmd_start ;;
    ssh)    cmd_ssh ;;
    status) cmd_status ;;
    stop)   cmd_stop ;;
    clean)  cmd_clean ;;
    *) die "Lệnh không hợp lệ: $1 (dùng: start|ssh|status|stop|clean)" ;;
esac
