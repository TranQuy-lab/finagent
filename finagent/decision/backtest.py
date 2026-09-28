"""Backtest: chạy chính pipeline phân tích trên lưới mã × ngày để đo chất lượng.

Một lần phân tích chỉ cho một quyết định — không thể biết hệ thống ra quyết định
tốt hay kém. Module này chạy cùng pipeline qua nhiều mã và nhiều ngày trong quá
khứ, rồi chấm điểm những quyết định đã đủ thời gian nắm giữ.

Điểm chấm là **alpha** — lợi nhuận vượt chỉ số chuẩn — chứ không phải lợi nhuận
thô. Cần vậy vì trong một thị trường tăng, mọi quyết định mua đều lãi; chỉ số mới
cho biết hệ thống có chọn đúng hơn mức trung bình hay không.

Lưu ý về chi phí: mỗi ô trong lưới là một lượt chạy đầy đủ 12 tác nhân, tốn vài
phút và một lượng hạn mức API đáng kể. Nên bắt đầu bằng lưới nhỏ.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from finagent.config import settings
from finagent.decision import engine, vendor
from finagent.storage import get_conn

logger = logging.getLogger(__name__)


@dataclass
class BacktestReport:
    """Kết quả một lượt backtest, dạng dễ in và dễ lưu."""

    run_id: str
    symbols: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    cells_run: int = 0
    cells_skipped: int = 0
    failures: list[tuple] = field(default_factory=list)
    settlement_failures: list[tuple] = field(default_factory=list)
    log_path: str = ""
    #: Số ô đã chấm được điểm / còn chờ đủ thời gian nắm giữ / không chấm được.
    resolved: int = 0
    pending: int = 0
    unscored: int = 0
    holding: str = ""
    #: Điểm theo từng mức tín hiệu: số mẫu, alpha trung bình, tỷ lệ đoán đúng hướng.
    scores: list[dict] = field(default_factory=list)
    #: Mã mà chỉ số chuẩn chính là nó — alpha không tính được, chỉ có lợi nhuận thô.
    benchmark_skipped: set[str] = field(default_factory=set)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        """Báo cáo dạng văn bản cho người đọc."""
        if self.error:
            return f"❌ Backtest lỗi: {self.error}"

        lines = [
            f"📊 KẾT QUẢ BACKTEST — {self.run_id}",
            "",
            f"• Mã        : {', '.join(self.symbols)}",
            f"• Khoảng    : {self.dates[0]} → {self.dates[-1]} ({len(self.dates)} ngày)",
            f"• Ô đã chạy : {self.cells_run} (bỏ qua {self.cells_skipped} ô đã có)",
            f"• Chấm điểm : {self.resolved} đã xong, {self.pending} còn chờ",
        ]
        if self.holding:
            lines.append(f"• Thời gian nắm giữ: {self.holding}")

        if self.scores:
            lines += ["", "*Alpha theo mức tín hiệu* (so với chỉ số chuẩn):", ""]
            for score in self.scores:
                alpha = score.get("mean_alpha")
                hit = score.get("hit_rate")
                alpha_text = f"{alpha:+.2%}" if isinstance(alpha, (int, float)) else "—"
                # Hold không dự đoán hướng nên không có tỷ lệ đúng — nói thẳng ra
                # thay vì bịa một con số.
                hit_text = f"{hit:.0%}" if isinstance(hit, (int, float)) else "không dự đoán hướng"
                lines.append(
                    f"  {score['rating']:<10} n={score['count']:<5} "
                    f"{hit_text:<22} alpha {alpha_text}"
                )
        else:
            lines += ["", "_Chưa có ô nào chấm được điểm._"]

        if self.benchmark_skipped:
            lines += [
                "",
                f"ℹ️  Không tính alpha được cho: {', '.join(sorted(self.benchmark_skipped))} "
                "(chỉ số chuẩn chính là mã đó). Các dòng trên dùng lợi nhuận thô.",
            ]
        if self.pending:
            lines += ["", f"ℹ️  {self.pending} ô còn chờ đủ thời gian nắm giữ — chạy lại sau để chấm."]
        if self.failures:
            lines += ["", f"⚠️  {len(self.failures)} ô lỗi, ví dụ: {self.failures[0]}"]

        return "\n".join(lines)


def build_date_grid(start: str, end: str, every_n_days: int = 7) -> list[str]:
    """Danh sách ngày phân tích trong khoảng, cách nhau ``every_n_days``."""
    from tradingagents.backtest import iter_grid

    return iter_grid(start, end, every_n_days=every_n_days)


def _summarize_scores(summary) -> list[dict]:
    """Chuyển bảng điểm của TradingAgents thành dict gọn để in và lưu.

    ``by_rating`` là dict ``{mức tín hiệu: RatingScore}`` chứ không phải danh sách.
    """
    by_rating = getattr(summary, "by_rating", None) or {}
    return [
        {
            "rating": str(rating),
            "count": int(getattr(score, "count", 0)),
            "mean_alpha": getattr(score, "mean_alpha", None),
            "hit_rate": getattr(score, "hit_rate", None),
        }
        for rating, score in by_rating.items()
    ]


def run(
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    every_n_days: int = 7,
    run_id: str | None = None,
    run_llm: bool = True,
) -> BacktestReport:
    """Chạy backtest trên lưới mã × ngày và trả về báo cáo.

    ``run_llm=False`` sẽ chạy bằng mô hình ML nhỏ thay vì LLM — nhanh và miễn phí,
    nhưng không đánh giá được phần suy luận đa tác nhân.
    """
    targets = vendor.tradable_symbols(symbols or [*settings.crypto_symbols, *settings.vn_symbols])
    if not targets:
        return BacktestReport(run_id="", error="Không có mã nào để chạy backtest.")

    if not start or not end:
        return BacktestReport(run_id="", error="Cần có ngày bắt đầu và ngày kết thúc.")

    dates = build_date_grid(start, end, every_n_days)
    if not dates:
        return BacktestReport(run_id="", error=f"Không có ngày nào trong khoảng {start} → {end}.")

    logger.info(
        "Backtest: %d mã × %d ngày = %d ô. Mỗi ô tốn vài phút nếu dùng LLM.",
        len(targets), len(dates), len(targets) * len(dates),
    )

    if not run_llm:
        return _run_ml_only(targets, dates, run_id)

    try:
        from tradingagents.backtest import run_backtest, summarize
    except ImportError as exc:  # pragma: no cover
        return BacktestReport(run_id="", error=f"Không import được TradingAgents: {exc}")

    vendor.register_vendor()
    # Chỉ số chuẩn khác nhau theo nhóm tài sản, nên tách lưới theo nhóm để mỗi
    # lượt chạy dùng đúng chỉ số của mình.
    reports: list[BacktestReport] = []

    for asset_class, group in _group_by_asset_class(targets).items():
        config = engine.build_graph_config(asset_class)
        try:
            result = run_backtest(
                group, dates, config,
                asset_type="crypto" if asset_class == "crypto" else "stock",
                run_id=f"{run_id}_{asset_class}" if run_id else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backtest nhóm %s thất bại", asset_class)
            reports.append(BacktestReport(run_id=run_id or "", symbols=group, dates=dates,
                                          error=str(exc)))
            continue

        report = BacktestReport(
            run_id=result.run_id,
            symbols=group,
            dates=dates,
            cells_run=result.cells_run,
            cells_skipped=result.skipped,
            failures=list(getattr(result, "failures", []) or []),
            settlement_failures=list(getattr(result, "settlement_failures", []) or []),
            log_path=str(result.log_path),
        )
        try:
            summary = summarize(result)
            report.scores = _summarize_scores(summary)
            report.resolved = int(getattr(summary, "resolved", 0))
            report.pending = int(getattr(summary, "pending", 0))
            report.unscored = int(getattr(summary, "unscored", 0))
            report.holding = str(getattr(summary, "holding", "") or "")
        except Exception as exc:  # noqa: BLE001 - thiếu bảng điểm không làm hỏng lượt chạy
            logger.warning("Không tổng hợp được điểm backtest: %s", exc)
        reports.append(report)

    return _merge_reports(reports, targets, dates, run_id or "")


def _group_by_asset_class(symbols: list[str]) -> dict[str, list[str]]:
    """Nhóm mã theo nhóm tài sản, vì mỗi nhóm dùng chỉ số chuẩn khác nhau."""
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(vendor.detect_asset_class(symbol), []).append(symbol)
    return groups


def _merge_reports(reports: list[BacktestReport], symbols: list[str],
                   dates: list[str], run_id: str) -> BacktestReport:
    """Gộp báo cáo của nhiều nhóm tài sản thành một."""
    merged = BacktestReport(run_id=run_id, symbols=symbols, dates=dates)
    for report in reports:
        merged.cells_run += report.cells_run
        merged.cells_skipped += report.cells_skipped
        merged.failures.extend(report.failures)
        merged.settlement_failures.extend(report.settlement_failures)
        merged.scores.extend(report.scores)
        merged.resolved += report.resolved
        merged.pending += report.pending
        merged.unscored += report.unscored
        if not merged.holding:
            merged.holding = report.holding
        if report.error:
            merged.error = (merged.error + "; " if merged.error else "") + report.error
    if reports:
        merged.log_path = reports[0].log_path
    return merged


def _run_ml_only(symbols: list[str], dates: list[str], run_id: str | None) -> BacktestReport:
    """Backtest bằng mô hình ML nhỏ — nhanh, miễn phí, nhưng chỉ đo phần ML.

    Vẫn tính **alpha so với chỉ số chuẩn** chứ không dùng lợi nhuận thô: trong một
    thị trường tăng, mọi tín hiệu mua đều lãi, nên lợi nhuận thô không nói lên
    điều gì về chất lượng mô hình.

    Dùng để thử nhanh trước khi chạy lưới đầy đủ bằng LLM.
    """
    from finagent.decision import ml_model

    report = BacktestReport(run_id=run_id or "ml-only", symbols=symbols, dates=dates)
    outcomes: dict[str, list[float]] = {}

    # Chỉ số chuẩn lấy một lần cho mỗi nhóm tài sản.
    benchmarks: dict[str, dict[str, float]] = {}
    for asset_class in {vendor.detect_asset_class(s) for s in symbols}:
        benchmark = vendor.benchmark_for(asset_class)
        try:
            series = vendor.daily_history(benchmark, days=500)
            benchmarks[asset_class] = {row["date"]: row["price"] for row in series}
        except Exception as exc:  # noqa: BLE001 - thiếu chỉ số thì chấm bằng lợi nhuận thô
            logger.warning("Không lấy được chỉ số chuẩn %s: %s", benchmark, exc)
            benchmarks[asset_class] = {}

    for symbol in symbols:
        asset_class = vendor.detect_asset_class(symbol)
        try:
            history = vendor.daily_history(symbol, days=500)
        except Exception as exc:  # noqa: BLE001
            report.failures.append((symbol, "-", str(exc)))
            continue

        if len(history) < ml_model.MIN_SAMPLES + len(dates):
            report.failures.append((symbol, "-", f"chỉ có {len(history)} phiên, không đủ để backtest"))
            continue

        closes = [row["price"] for row in history]
        index_of = {row["date"]: i for i, row in enumerate(history)}
        benchmark = benchmarks.get(asset_class) or {}

        for date in dates:
            position = index_of.get(date)
            if position is None or position + 1 >= len(closes):
                continue

            # Huấn luyện trên dữ liệu **trước** ngày đó rồi mới dự đoán — không
            # nhìn về tương lai, nếu không kết quả sẽ đẹp một cách giả tạo.
            past = [{"price": price, "volume": 1.0} for price in closes[: position + 1]]
            prediction = ml_model.predict_from_history(past)
            if not prediction.get("available"):
                continue

            probability = float(prediction["probability"])
            asset_return = (closes[position + 1] - closes[position]) / closes[position]

            benchmark_return = 0.0
            # Khi mã đang xét **chính là** chỉ số chuẩn (BTC so với BTC), alpha sẽ
            # luôn bằng 0 một cách vô nghĩa. Trường hợp đó giữ nguyên lợi nhuận thô
            # và ghi lại để báo cáo nói rõ, thay vì báo "alpha 0%" gây hiểu nhầm.
            if benchmark and symbol.upper() != vendor.benchmark_for(asset_class).upper():
                start_price = benchmark.get(date)
                end_price = benchmark.get(history[position + 1]["date"])
                if start_price and end_price:
                    benchmark_return = (end_price - start_price) / start_price
            else:
                report.benchmark_skipped.add(symbol.upper())

            signal = "Buy" if probability >= 0.55 else "Sell" if probability <= 0.45 else "Hold"
            # Lợi nhuận theo hướng đã chọn, trừ đi lợi nhuận của chỉ số chuẩn.
            if signal == "Buy":
                raw = asset_return
            elif signal == "Sell":
                raw = -asset_return
            else:
                raw = 0.0

            outcomes.setdefault(signal, []).append(raw - benchmark_return)
            report.cells_run += 1

    report.resolved = report.cells_run
    report.scores = [
        {
            "rating": signal,
            # Hold không dự đoán hướng, nên tỷ lệ "đúng hướng" vô nghĩa với nó —
            # để trống thay vì bịa ra một con số.
            "hit_rate": None if signal == "Hold"
                        else (round(sum(1 for r in results if r > 0) / len(results), 4) if results else None),
            "count": len(results),
            "mean_alpha": round(sum(results) / len(results), 6) if results else None,
        }
        for signal, results in sorted(outcomes.items())
    ]
    return report
