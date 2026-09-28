#!/usr/bin/env python3
"""Tạo đĩa seed NoCloud (ISO) cho cloud-init mà không cần quyền root.

Ubuntu Server cloud image không có trình cài đặt: nó đọc cấu hình từ một đĩa
seed gắn kèm khi khởi động. Module này dựng đĩa seed đó bằng ``pycdlib`` thuần
Python, nên không phải cài ``cloud-localds``/``genisoimage`` (vốn cần sudo).

Cách dùng::

    python3 make_seed_iso.py --out seed.iso --hostname may-chu \\
        --user finagent --password finagent --ssh-key ~/.ssh/id_ed25519.pub
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

try:
    import pycdlib
except ImportError:  # pragma: no cover
    sys.exit("Thiếu pycdlib. Cài bằng: pip install pycdlib")

# Cấu hình cloud-init cho máy chủ FinAgent.
USER_DATA_TEMPLATE = """#cloud-config
# Sinh tự động bởi make_seed_iso.py — không sửa tay.
hostname: {hostname}
manage_etc_hosts: true
preserve_hostname: false

users:
  - name: {user}
    gecos: FinAgent Service Account
    groups: [adm, sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: false
    passwd: {password_hash}
    ssh_authorized_keys:
{ssh_keys}

ssh_pwauth: true
disable_root: true

package_update: true
packages:
  - python3
  - python3-pip
  - python3-venv
  - python3-dev
  - build-essential
  - git
  - curl
  - rsync
  - sqlite3
  - redis-tools
  # open-vm-tools là bắt buộc để `vmrun getGuestIPAddress` hoạt động. Thiếu nó thì
  # không lấy được địa chỉ IP của máy ảo từ bên ngoài, phải dò tay qua DHCP lease.
  - open-vm-tools

write_files:
  - path: /etc/motd
    content: |
      ================================================
        FinAgent — {role}
        Hostname: {hostname}
      ================================================

runcmd:
  # Ghi dấu máy đã dựng xong. Tách thành hai lệnh riêng để tránh lồng dấu nháy —
  # nếu bọc `$(date)` trong nháy đơn thì shell sẽ không thay giá trị, và log sẽ
  # ghi nguyên chuỗi "$(date)" thay vì thời gian thật.
  - [ sh, -c, "hostname > /var/log/finagent-boot.log" ]
  - [ sh, -c, "date >> /var/log/finagent-boot.log" ]
  - [ sh, -c, "echo '{role}' >> /var/log/finagent-boot.log" ]
  - [ sh, -c, "apt-get clean" ]

# Khởi động lại một lần sau khi cài xong.
#
# `open-vm-tools` được cài ngay trong lần boot đầu, nhưng VMware chỉ chuyển trạng
# thái tools sang "running" sau khi máy ảo khởi động lại. Không khởi động lại thì
# `vmrun getGuestIPAddress` báo "VMware Tools are not running" và phải dò IP thủ
# công qua bảng cấp phát DHCP.
power_state:
  mode: reboot
  message: "FinAgent {role}: khởi động lại để VMware Tools hoạt động"
  timeout: 30
  condition: true

final_message: "FinAgent {role} {hostname} đã sẵn sàng sau $UPTIME giây."
"""

META_DATA_TEMPLATE = """instance-id: {instance_id}
local-hostname: {hostname}
"""

NETWORK_CONFIG_TEMPLATE = """version: 2
ethernets:
  main:
    match:
      name: "e*"
    dhcp4: true
    dhcp-identifier: mac
"""


def hash_password(password: str) -> str:
    """Băm mật khẩu theo SHA-512 crypt để cloud-init dùng được."""
    import crypt

    # `crypt` bị đánh dấu lỗi thời ở Python 3.13 nhưng vẫn là cách chuẩn ở 3.12.
    for method in ("sha512", "sha256"):
        try:
            return crypt.crypt(password, crypt.mksalt(getattr(crypt, f"METHOD_{method.upper()}")))
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("Không băm được mật khẩu.")


def build_iso(out_path: Path, files: dict[str, str]) -> None:
    """Đóng gói các file văn bản thành ISO cho cloud-init.

    Chuẩn ISO9660 chỉ cho phép ký tự ``A-Z 0-9 _`` trong tên file, nên tên thật
    (``user-data``, ``meta-data``…) chứa dấu gạch ngang không đặt trực tiếp được.
    Cách làm giống ``cloud-localds``: đặt tên ISO hợp lệ (chữ hoa, gạch dưới) rồi
    mang tên thật qua lớp **Rock Ridge** và **Joliet** — hai lớp này được Linux
    tự động ưu tiên khi gắn đĩa, nên cloud-init vẫn đọc đúng tên.
    """
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")

    for name, content in files.items():
        data = content.encode("utf-8")
        # Tên ISO hợp lệ: thay gạch ngang bằng gạch dưới, viết hoa.
        iso_name = name.upper().replace("-", "_")
        iso.add_fp(
            io.BytesIO(data), len(data),
            iso_path=f"/{iso_name}.;1",
            rr_name=name,
            joliet_path=f"/{name}",
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    iso.write(str(out_path))
    iso.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Tạo đĩa seed cloud-init cho máy ảo FinAgent.")
    parser.add_argument("--out", required=True, help="Đường dẫn file ISO đầu ra")
    parser.add_argument("--hostname", required=True, help="Tên máy ảo")
    parser.add_argument("--user", default="finagent", help="Tài khoản tạo trong máy ảo")
    parser.add_argument("--password", default="finagent", help="Mật khẩu đăng nhập dự phòng")
    parser.add_argument("--role", default="máy chủ", help="Vai trò, chỉ dùng cho thông báo")
    parser.add_argument("--ssh-key", action="append", default=[], help="File khoá công khai SSH (lặp lại được)")
    args = parser.parse_args()

    keys: list[str] = []
    for key_path in args.ssh_key:
        path = Path(key_path).expanduser()
        if not path.exists():
            print(f"⚠️  Bỏ qua khoá SSH không tồn tại: {path}", file=sys.stderr)
            continue
        keys.append(f"      - {path.read_text().strip()}")
    if not keys:
        keys.append("      - ''")   # cloud-init yêu cầu phần tử không rỗng

    user_data = USER_DATA_TEMPLATE.format(
        hostname=args.hostname,
        user=args.user,
        password_hash=hash_password(args.password),
        ssh_keys="\n".join(keys),
        role=args.role,
    )
    meta_data = META_DATA_TEMPLATE.format(
        instance_id=f"finagent-{args.hostname}", hostname=args.hostname
    )

    build_iso(Path(args.out), {
        "user-data": user_data,
        "meta-data": meta_data,
        "network-config": NETWORK_CONFIG_TEMPLATE,
    })
    print(f"✅ Đã tạo đĩa seed: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
