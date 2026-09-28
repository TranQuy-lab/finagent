#!/usr/bin/env bash
# Đóng gói phần MÁY CON (client) thành một gói nhỏ để cài lên nhiều máy khác.
#
# Máy con chỉ cào dữ liệu rồi gửi về máy chủ, nên không cần: khung đa tác nhân
# TradingAgents (5,2 MB cùng langchain), phần ra quyết định, broker, bot Telegram,
# hay pandas. Gói tạo ra ở đây chỉ chứa đúng những gì máy con dùng.
#
# Cách dùng:
#     bash deploy/make_worker_bundle.sh
#
# Kết quả: dist/finagent-worker-<phiên bản>.tar.gz
#
# Cài trên máy mới:
#     tar xzf finagent-worker-*.tar.gz && cd finagent-worker-* \
#         && sudo REDIS_HOST=<ip máy chủ> bash install.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VERSION="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
STAMP="$(date +%Y%m%d)"
BUNDLE_NAME="finagent-worker-${VERSION}"
DIST_DIR="$PROJECT_ROOT/dist"
STAGE="$DIST_DIR/$BUNDLE_NAME"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

# Những gì máy con thực sự cần, suy ra từ cây import của tasks.py và celery_app.py.
#
# Liệt kê TỪNG TỆP chứ không chép cả thư mục collectors/. Đã từng chép cả thư mục
# và suýt mang theo microstructure.py — tệp đó chỉ máy chủ dùng (vendor.py gọi để
# lấy sổ lệnh và khối ngoại lúc phân tích). Danh sách tường minh khiến ranh giới
# rõ ràng và tệp mới thêm vào sẽ không tự động lọt vào gói.
WORKER_MODULES=(
    "finagent/__init__.py"
    "finagent/config.py"
    "finagent/celery_app.py"
    "finagent/tasks.py"
    "finagent/collectors/__init__.py"
    "finagent/collectors/base.py"
    "finagent/collectors/crypto.py"
    "finagent/collectors/gold.py"
    "finagent/collectors/news.py"
    "finagent/collectors/vnstock.py"
)

say "Dọn gói cũ"
rm -rf "$STAGE"
mkdir -p "$STAGE"

say "Sao chép mã nguồn máy con"
for item in "${WORKER_MODULES[@]}"; do
    if [[ ! -e "$item" ]]; then
        echo "  LỖI: thiếu $item — máy con sẽ không chạy được." >&2
        exit 1
    fi
    mkdir -p "$STAGE/$(dirname "$item")"
    cp -r "$item" "$STAGE/$item"
    echo "    $item"
done

say "Dọn rác Python"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

say "Sao chép tệp cài đặt và cấu hình mẫu"
mkdir -p "$STAGE/deploy"
cp deploy/setup_worker.sh "$STAGE/deploy/"
cp deploy/finagent-worker.service "$STAGE/deploy/"
cp .env.example "$STAGE/.env.example"
cp pyproject.toml "$STAGE/pyproject.toml"
cp README.md "$STAGE/README.md" 2>/dev/null || true

# Script cài đặt gọi từ gốc gói cho tiện, không phải nhớ đường dẫn dài.
cat > "$STAGE/install.sh" <<'INSTALL'
#!/usr/bin/env bash
# Cài máy con FinAgent. Chạy từ thư mục gốc của gói:
#     sudo REDIS_HOST=<ip máy chủ> bash install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Gói này đã chỉ chứa phần máy con, nên cài thẳng vào /opt/finagent được luôn —
# không cần rsync như khi cài từ kho mã nguồn đầy đủ.
export FINAGENT_PREBUILT=1
exec bash "$HERE/deploy/setup_worker.sh" "$@"
INSTALL
chmod +x "$STAGE/install.sh" "$STAGE/deploy/setup_worker.sh"

say "Nén"
mkdir -p "$DIST_DIR"
ARCHIVE="$DIST_DIR/${BUNDLE_NAME}-${STAMP}.tar.gz"
tar -czf "$ARCHIVE" -C "$DIST_DIR" "$BUNDLE_NAME"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
FILES="$(tar -tzf "$ARCHIVE" | grep -c . || true)"

# So sánh với toàn bộ kho, để thấy rõ phần tiết kiệm được.
REPO_SIZE="$(du -sh --exclude=.venv --exclude=.git --exclude=dist . 2>/dev/null | cut -f1)"

cat <<SUMMARY

$(say "Xong")

  Gói máy con : $ARCHIVE
  Kích thước  : $SIZE ($FILES tệp)
  Toàn bộ kho : $REPO_SIZE

Cài trên một máy mới:

    scp $ARCHIVE user@<ip-máy-mới>:/tmp/
    ssh user@<ip-máy-mới>
    tar xzf /tmp/$(basename "$ARCHIVE")
    cd $BUNDLE_NAME
    sudo REDIS_HOST=<ip-máy-chủ> bash install.sh

Gói này KHÔNG chứa khung đa tác nhân TradingAgents, phần ra quyết định, broker hay
bot Telegram — máy con không dùng tới. Nhờ vậy cài đặt nhanh và nhẹ hơn nhiều.
SUMMARY
