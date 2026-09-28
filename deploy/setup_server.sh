#!/usr/bin/env bash
# =============================================================================
#  FinAgent — cài đặt MÁY CHỦ trên Ubuntu Server 24.04 LTS
#
#  Script này biến một máy ảo Ubuntu Server sạch thành máy chủ FinAgent:
#  Redis, Python, mã nguồn, và các dịch vụ systemd chạy nền.
#
#  Cách dùng (trên máy chủ, quyền root):
#      sudo bash deploy/setup_server.sh
#
#  Biến môi trường tuỳ chọn:
#      FINAGENT_DIR=/opt/finagent   thư mục cài đặt
#      FINAGENT_USER=finagent       tài khoản chạy dịch vụ
#      SKIP_REDIS=1                 bỏ qua cài Redis (nếu đã có Redis ngoài)
# =============================================================================
set -euo pipefail

FINAGENT_DIR="${FINAGENT_DIR:-/opt/finagent}"
FINAGENT_USER="${FINAGENT_USER:-finagent}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Phải chạy bằng quyền root: sudo bash $0"

log "1/8 — Cập nhật hệ thống và cài gói cần thiết"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    build-essential git curl ca-certificates \
    redis-server sqlite3 ufw >/dev/null

log "2/8 — Cấu hình Redis (nghe trên mọi giao diện để máy con kết nối được)"
if [[ "${SKIP_REDIS:-0}" != "1" ]]; then
    # Máy con ở máy ảo khác phải kết nối được vào Redis của máy chủ, nên phải
    # lắng nghe trên 0.0.0.0. An toàn được bảo đảm bằng ufw (bước 8).
    sed -i 's/^bind 127.0.0.1 -::1/bind 0.0.0.0/' /etc/redis/redis.conf
    sed -i 's/^protected-mode yes/protected-mode no/' /etc/redis/redis.conf
    sed -i 's/^# maxmemory <bytes>/maxmemory 256mb/' /etc/redis/redis.conf
    sed -i 's/^# maxmemory-policy noeviction/maxmemory-policy allkeys-lru/' /etc/redis/redis.conf
    systemctl enable redis-server >/dev/null 2>&1
    systemctl restart redis-server
    sleep 2
    redis-cli ping | grep -q PONG || die "Redis không khởi động được"
    log "    Redis đã chạy và trả lời PONG"
else
    warn "Bỏ qua cài đặt Redis (SKIP_REDIS=1)"
fi

log "3/8 — Tạo tài khoản dịch vụ '$FINAGENT_USER'"
if ! id -u "$FINAGENT_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash "$FINAGENT_USER"
fi

log "4/8 — Sao chép mã nguồn vào $FINAGENT_DIR"
mkdir -p "$FINAGENT_DIR"
# Giữ lại .env và data của lần cài trước, không ghi đè cấu hình người dùng.
if [[ -f "$FINAGENT_DIR/.env" ]]; then
    cp "$FINAGENT_DIR/.env" /tmp/finagent.env.backup
fi
rsync -a --delete \
    --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '.git' \
    "$SOURCE_DIR/" "$FINAGENT_DIR/" 2>/dev/null \
  || cp -r "$SOURCE_DIR/." "$FINAGENT_DIR/"
if [[ -f /tmp/finagent.env.backup ]]; then
    cp /tmp/finagent.env.backup "$FINAGENT_DIR/.env"
    log "    Đã giữ lại file .env cũ"
fi

log "5/8 — Tạo môi trường Python và cài phụ thuộc"
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

log "6/8 — Chuẩn bị file cấu hình .env"
mkdir -p "$FINAGENT_DIR/data"
if [[ ! -f "$FINAGENT_DIR/.env" ]]; then
    cp "$FINAGENT_DIR/.env.example" "$FINAGENT_DIR/.env"
    warn "Đã tạo .env từ mẫu — CẦN ĐIỀN DEEPSEEK_API_KEY và TELEGRAM_* rồi chạy lại."
fi

log "7/8 — Cài dịch vụ systemd"
install -m 644 "$FINAGENT_DIR/deploy/finagent-server.service" /etc/systemd/system/
install -m 644 "$FINAGENT_DIR/deploy/finagent-bot.service"    /etc/systemd/system/
sed -i "s|__FINAGENT_DIR__|$FINAGENT_DIR|g; s|__FINAGENT_USER__|$FINAGENT_USER|g" \
    /etc/systemd/system/finagent-server.service /etc/systemd/system/finagent-bot.service

chown -R "$FINAGENT_USER:$FINAGENT_USER" "$FINAGENT_DIR"
systemctl daemon-reload
systemctl enable finagent-server finagent-bot >/dev/null 2>&1

log "8/8 — Cấu hình tường lửa"
ufw --force reset >/dev/null 2>&1
ufw default deny incoming  >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1
ufw allow 22/tcp   comment 'SSH'            >/dev/null 2>&1
ufw allow 6379/tcp comment 'Redis cho may con' >/dev/null 2>&1
ufw --force enable >/dev/null 2>&1
log "    Đã mở cổng 22 (SSH) và 6379 (Redis cho máy con)"

IP_ADDR="$(hostname -I | awk '{print $1}')"
cat <<EOF

╔══════════════════════════════════════════════════════════════════════════╗
║  CÀI ĐẶT MÁY CHỦ HOÀN TẤT                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

  Địa chỉ IP máy chủ : $IP_ADDR
  Thư mục cài đặt    : $FINAGENT_DIR
  Tài khoản dịch vụ  : $FINAGENT_USER

  BƯỚC TIẾP THEO — bắt buộc:

  1) Điền khoá API và Telegram vào file cấu hình:
         sudo nano $FINAGENT_DIR/.env
     Cần điền: DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

  2) Khởi tạo cơ sở dữ liệu:
         cd $FINAGENT_DIR && sudo -u $FINAGENT_USER .venv/bin/finagent init

  3) Bật dịch vụ:
         sudo systemctl start finagent-server finagent-bot
         sudo systemctl status finagent-server

  4) Trên MỖI MÁY CON, trỏ về máy chủ này:
         REDIS_URL=redis://$IP_ADDR:6379/0

  Lệnh hữu ích:
     sudo -u $FINAGENT_USER $FINAGENT_DIR/.venv/bin/finagent status
     sudo journalctl -u finagent-server -f

EOF
