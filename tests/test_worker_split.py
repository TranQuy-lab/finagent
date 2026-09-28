"""Test ranh giới giữa máy con (client) và máy chủ.

Máy con cố ý chỉ cài một phần nhỏ: cào dữ liệu rồi gửi về máy chủ. Nếu ai đó lỡ
thêm một dòng ``import`` kéo theo phần ra quyết định hay Telegram vào mã máy con,
gói client sẽ phình ra và máy con cài đặt thất bại — mà lỗi đó chỉ lộ ra khi triển
khai lên máy mới, không lộ khi chạy test ở máy chủ.

Nhóm test này chặn đúng tình huống đó: soi cây import của phần máy con và bắt buộc
nó không được chạm tới phần chỉ máy chủ dùng.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Mã nguồn máy con thực sự cần. Danh sách này phải khớp với
#: ``WORKER_MODULES`` trong deploy/make_worker_bundle.sh.
WORKER_MODULES = [
    "finagent/__init__.py",
    "finagent/config.py",
    "finagent/celery_app.py",
    "finagent/tasks.py",
    "finagent/collectors/__init__.py",
    "finagent/collectors/base.py",
    "finagent/collectors/crypto.py",
    "finagent/collectors/gold.py",
    "finagent/collectors/news.py",
    "finagent/collectors/vnstock.py",
]

#: Thư viện CHỈ máy chủ dùng. Máy con không được chạm tới.
SERVER_ONLY_LIBRARIES = {
    "telegram", "pandas", "apscheduler",
    "langchain", "langchain_core", "langchain_google_genai", "langgraph",
    "numpy", "tradingagents", "pytest",
}

#: Module nội bộ CHỈ máy chủ dùng.
SERVER_ONLY_MODULES = {
    "finagent.decision", "finagent.broker", "finagent.scheduler",
    "finagent.storage", "finagent.telegram_bot", "finagent.cli",
}


def _imported_names(path: Path) -> set[str]:
    """Tập hợp tên module được import trong một tệp."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module)
    return names


def _top_level(name: str) -> str:
    return name.split(".")[0]


class TestWorkerModuleBoundaries:
    def test_cac_tep_may_con_deu_ton_tai(self):
        for relative in WORKER_MODULES:
            assert (PROJECT_ROOT / relative).is_file(), f"thiếu {relative}"

    @pytest.mark.parametrize("relative", WORKER_MODULES)
    def test_may_con_khong_import_thu_vien_chi_may_chu(self, relative):
        imported = _imported_names(PROJECT_ROOT / relative)
        vi_pham = {name for name in imported if _top_level(name) in SERVER_ONLY_LIBRARIES}

        assert not vi_pham, (
            f"{relative} import thư viện chỉ máy chủ dùng: {sorted(vi_pham)}. "
            "Máy con sẽ phải cài thêm gói nặng — hãy chuyển phần đó sang máy chủ."
        )

    @pytest.mark.parametrize("relative", WORKER_MODULES)
    def test_may_con_khong_import_module_chi_may_chu(self, relative):
        imported = _imported_names(PROJECT_ROOT / relative)

        vi_pham = {
            name for name in imported
            if any(name == module or name.startswith(module + ".") for module in SERVER_ONLY_MODULES)
        }

        assert not vi_pham, (
            f"{relative} import module chỉ máy chủ dùng: {sorted(vi_pham)}. "
            "Gói máy con không chứa những tệp đó nên sẽ lỗi khi cài lên máy mới."
        )


class TestWorkerBundleDefinition:
    """Danh sách tệp trong gói phải khớp với danh sách kiểm tra ở trên."""

    def test_script_dong_goi_ton_tai(self):
        assert (PROJECT_ROOT / "deploy/make_worker_bundle.sh").is_file()

    def test_danh_sach_goi_khop_voi_danh_sach_kiem_tra(self):
        script = (PROJECT_ROOT / "deploy/make_worker_bundle.sh").read_text()
        # Lấy các dòng khai báo trong mảng WORKER_MODULES của script.
        start = script.index("WORKER_MODULES=(")
        end = script.index(")", start)
        declared = {
            line.strip().strip('"')
            for line in script[start:end].splitlines()
            if line.strip().startswith('"')
        }
        # Script dùng thư mục cho collectors, còn test liệt kê từng tệp.
        expected = set()
        for item in declared:
            path = PROJECT_ROOT / item
            if path.is_dir():
                expected.update(
                    str(f.relative_to(PROJECT_ROOT))
                    for f in path.glob("*.py")
                )
            else:
                expected.add(item)

        assert expected == set(WORKER_MODULES), (
            "Danh sách tệp trong make_worker_bundle.sh lệch với test. "
            f"Chỉ có trong script: {sorted(expected - set(WORKER_MODULES))}. "
            f"Chỉ có trong test: {sorted(set(WORKER_MODULES) - expected)}."
        )

    def test_script_khong_chep_thu_muc_nang(self):
        """Script đóng gói không được chép vendor/ hay các thư mục chỉ máy chủ dùng.

        Kiểm tra trên danh sách tệp thật sự được chép, không phải trên chú thích.
        """
        script = (PROJECT_ROOT / "deploy/make_worker_bundle.sh").read_text()
        start = script.index("WORKER_MODULES=(")
        end = script.index(")", start)
        declared = [
            line.strip().strip('"')
            for line in script[start:end].splitlines()
            if line.strip().startswith('"')
        ]

        for item in declared:
            assert not item.startswith("vendor/"), f"gói máy con không được chứa {item}"
            assert item not in ("finagent/decision", "finagent/broker"), (
                f"gói máy con không được chứa {item}"
            )
            assert "telegram" not in item


class TestWorkerSetupScript:
    def test_khong_cai_tradingagents(self):
        script = (PROJECT_ROOT / "deploy/setup_worker.sh").read_text()
        # Chỉ xét các dòng thực thi, bỏ qua dòng chú thích.
        active = [
            line for line in script.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        for line in active:
            assert "pip install" not in line or "vendor/TradingAgents" not in line, (
                "setup_worker.sh cài TradingAgents — máy con không cần và gói rất nặng."
            )

    def test_chi_cai_nhom_worker(self):
        script = (PROJECT_ROOT / "deploy/setup_worker.sh").read_text()

        assert '.[worker]' in script, "setup_worker.sh phải cài nhóm [worker]"

    def test_ho_tro_che_do_goi_dong_san(self):
        script = (PROJECT_ROOT / "deploy/setup_worker.sh").read_text()

        assert "FINAGENT_PREBUILT" in script


class TestServerSetupScript:
    def test_may_chu_cai_nhom_server(self):
        script = (PROJECT_ROOT / "deploy/setup_server.sh").read_text()

        assert '.[server]' in script

    def test_may_chu_cai_tradingagents(self):
        script = (PROJECT_ROOT / "deploy/setup_server.sh").read_text()

        assert "vendor/TradingAgents" in script


class TestPyprojectSplit:
    def test_nhom_worker_du_goi_can_thiet(self):
        import tomllib

        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        worker = " ".join(data["project"]["optional-dependencies"]["worker"]).lower()

        for package in ("celery", "redis", "requests", "feedparser", "beautifulsoup4"):
            assert package in worker, f"nhóm [worker] thiếu {package}"

    def test_nhom_worker_khong_chua_goi_nang(self):
        import tomllib

        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        worker = " ".join(data["project"]["optional-dependencies"]["worker"]).lower()

        for package in ("telegram", "pandas", "apscheduler"):
            assert package not in worker, f"nhóm [worker] không được chứa {package}"

    def test_nhom_server_gom_ca_worker(self):
        import tomllib

        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        server = " ".join(data["project"]["optional-dependencies"]["server"]).lower()

        assert "finagent[worker]" in server, "nhóm [server] phải bao gồm [worker]"
        for package in ("telegram", "pandas", "apscheduler"):
            assert package in server

    def test_goi_co_ban_chi_de_doc_env(self):
        """Phần bắt buộc phải nhẹ, nếu không máy con vẫn cõng theo gói nặng."""
        import tomllib

        data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        base = " ".join(data["project"]["dependencies"]).lower()

        assert len(data["project"]["dependencies"]) == 1
        assert "python-dotenv" in base
