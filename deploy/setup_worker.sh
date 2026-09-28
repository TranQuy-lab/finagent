#!/usr/bin/env bash
# =============================================================================
#  FinAgent — cài đặt MÁY CON trên Ubuntu Server 24.04 LTS
#
#  Biến một máy ảo Ubuntu Server sạch thành máy con: chỉ cài Python và chạy
#  tiến trình lắng nghe hàng đợi Redis của máy chủ để thu thập dữ liệu.
#
#  Cách dùng (trên máy con, quyền root):
#      sudo REDIS_HOST=192.168.1.100 bash deploy/setup_worker.sh
#
#  Biến môi trường:
#      REDIS_HOST=...        IP máy chủ (BẮT BUỘC, trừ khi đã có .env)
#      REDIS_URL=...         ghi đè hoàn toàn, ưu tiên hơn REDIS_HOST
#      WORKER_NAME=...       tên máy con (mặc định: hostname)
#      FINAGENT_DIR=...      thư mục cài đặt
#      FINAGENT_USER=...     tài khoản chạy dịch vụ
# =============================================================================
set -euo pipefail

FINAGENT_DIR="${FINAGENT_DIR:-/opt/finagent}"
FINAGENT_USER="${FINAGENT_USER:-finagent}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_NAME="${WORKER_NAME:-$(hostname)}"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Phải chạy bằng quyền root: sudo bash $0"

log "1/6 — Cài gói cần thiết"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv python3-dev build-essential git curl rsync >/dev/null

log "2/6 — Xác định địa chỉ máy chủ"
# Ưu tiên REDIS_URL nếu được cấp thẳng; nếu không thì ghép từ REDIS_HOST.
if [[ -n "${REDIS_URL:-}" ]]; then
    RESOLVED_REDIS_URL="$REDIS_URL"
elif [[ -n "${REDIS_HOST:-}" ]]; then
    RESOLVED_REDIS_URL="redis://${REDIS_HOST}:6379/0"
elif [[ -f "$FINAGENT_DIR/.env" ]] && grep -q '^REDIS_URL=redis://' "$FINAGENT_DIR/.env"; then
    RESOLVED_REDIS_URL="$(grep '^REDIS_URL=' "$FINAGENT_DIR/.env" | head -1 | cut -d= -f2-)"
else
    die "Chưa biết địa chỉ máy chủ. Hãy chạy:\n    sudo REDIS_HOST=<IP_MAY_CHU> bash $0"
fi
log "    Sẽ kết nối tới: $RESOLVED_REDIS_URL"

log "3/6 — Kiểm tra kết nối tới máy chủ"
REDIS_HOST_ONLY="$(echo "$RESOLVED_REDIS_URL" | sed -E 's|redis://([^:/]+).*|\1|')"
if command -v redis-cli >/dev/null 2>&1; then
    if redis-cli -h "$REDIS_HOST_ONLY" -p 6379 ping 2>/dev/null | grep -q PONG; then
        log "    Kết nối Redis thành công (PONG)"
    else
        warn "Chưa ping được Redis tại $REDIS_HOST_ONLY:6379 — vẫn tiếp tục cài đặt."
        warn "Kiểm tra: máy chủ đã chạy Redis chưa, tường lửa đã mở cổng 6379 chưa."
    fi
else
    warn "Không có redis-cli để kiểm tra kết nối — sẽ kiểm tra sau bằng 'finagent ping'."
fi

log "4/6 — Tạo tài khoản '$FINAGENT_USER' và sao chép mã nguồn"
id -u "$FINAGENT_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$FINAGENT_USER"
mkdir -p "$FINAGENT_DIR"
rsync -a --delete \
    --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '.git' \
    "$SOURCE_DIR/" "$FINAGENT_DIR/" 2>/dev/null \
  || cp -r "$SOURCE_DIR/." "$FINAGENT_DIR/"

log "5/6 — Cài môi trường Python"
cd "$FINAGENT_DIR"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip

# Cài bản TradingAgents đã kiểm thử nằm trong dự án TRƯỚC.
# Không được để pip tự lấy "tradingagents" từ PyPI: gói cùng tên ở đó là phiên
# bản khác, sẽ làm hỏng các bản vá dữ liệu Việt Nam trong finagent/decision/vendor.py.
if [[ -d vendor/TradingAgents ]]; then
    ./.venv/bin/pip install --quiet -e vendor/TradingAgents
    log "    Đã cài TradingAgents (bản trong dự án)"
else
    warn "Không thấy vendor/TradingAgents — hệ thống có thể không chạy đúng."
fi

./.venv/bin/pip install --quiet -e .
log "    Đã cài xong gói finagent"

log "6/6 — Ghi cấu hình và cài dịch vụ"
mkdir -p "$FINAGENT_DIR/data"
cat > "$FINAGENT_DIR/.env" <<EOF
# Cấu hình máy con — sinh tự động bởi setup_worker.sh
REDIS_URL=$RESOLVED_REDIS_URL
FINAGENT_WORKER_NAME=$WORKER_NAME
FINAGENT_CRAWL_QUEUE=${FINAGENT_CRAWL_QUEUE:-crawl}
FINAGENT_WORKER_CONCURRENCY=${FINAGENT_WORKER_CONCURRENCY:-4}
# Máy con chỉ thu thập dữ liệu, không giao dịch và không cần khoá LLM/Telegram.
FINAGENT_LOG_LEVEL=${FINAGENT_LOG_LEVEL:-INFO}
EOF

install -m 644 "$FINAGENT_DIR/deploy/finagent-worker.service" /etc/systemd/system/
sed -i "s|__FINAGENT_DIR__|$FINAGENT_DIR|g; s|__FINAGENT_USER__|$FINAGENT_USER|g; s|__WORKER_NAME__|$WORKER_NAME|g" \
    /etc/systemd/system/finagent-worker.service

chown -R "$FINAGENT_USER:$FINAGENT_USER" "$FINAGENT_DIR"
systemctl daemon-reload
systemctl enable finagent-worker >/dev/null 2>&1
systemctl restart finagent-worker
sleep 4

if systemctl is-active --quiet finagent-worker; then
    log "Dịch vụ finagent-worker đang chạy"
else
    warn "Dịch vụ chưa chạy được. Xem log: journalctl -u finagent-worker -n 50"
fi

cat <<EOF

╔══════════════════════════════════════════════════════════════════════════╗
║  CÀI ĐẶT MÁY CON HOÀN TẤT                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

  Tên máy con : $WORKER_NAME
  Máy chủ     : $RESOLVED_REDIS_URL
  Thư mục     : $FINAGENT_DIR

  Kiểm tra:
     sudo journalctl -u finagent-worker -f
     # Trên MÁY CHỦ chạy: finagent status   → phải thấy '$WORKER_NAME'

EOF
