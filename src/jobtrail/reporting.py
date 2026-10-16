"""Plain-text rendering of the analytics report (used by `jobtrail stats`)."""

from jobtrail.services.analytics import AnalyticsReport, FunnelRow, Rate

INCONCLUSIVE = "too few to compare"


def _pct(rate: Rate) -> str:
    if rate.value is None:
        return "-"
    return f"{rate.value:.0%} ({rate.low:.0%}-{rate.high:.0%})"


def _days(row: FunnelRow) -> str:
    if row.median_days_to_response is None:
        return "-"
    return f"{row.median_days_to_response:.0f}d (n={row.response_time_n})"


def _table(title: str, rows: list[FunnelRow], min_sample: int) -> list[str]:
    header = ("", "Applied", "Responded", "Advanced", "Interview", "Offer", "Median reply", "")
    body = [
        (
            row.label,
            str(row.applied),
            f"{row.responded}  {_pct(row.response_rate)}",
            f"{row.advanced}  {_pct(row.advance_rate)}",
            str(row.interviewing),
            str(row.offers),
            _days(row),
            "" if row.conclusive else f"n<{min_sample}: {INCONCLUSIVE}",
        )
        for row in rows
    ]
    widths = [max(len(r[i]) for r in [header, *body]) for i in range(len(header))]
    lines = [title]
    for r in [header, *body]:
        lines.append(
            "  " + "  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip()
        )
    return lines


def format_report(report: AnalyticsReport, *, min_sample: int, stale_after_days: int) -> str:
    out: list[str] = []
    out += _table("Overall (percentages show the 95% interval)", [report.overall], min_sample)
    out += ["", *_table("By channel", report.by_channel, min_sample)]
    out += ["", *_table("By track", report.by_track, min_sample)]

    out += ["", f"Waiting more than {stale_after_days} days: {len(report.stale)}"]
    for item in report.stale[:10]:
        app = item.application
        out.append(f"  {item.days_waiting:>4}d  {app.company.name} - {app.role or '(no role)'}")
    if len(report.stale) > 10:
        out.append(f"  ... and {len(report.stale) - 10} more")

    out += ["", "Applications sent per week"]
    peak = max((count for _, count in report.weekly_applications), default=0)
    for monday, count in report.weekly_applications:
        bar = "#" * (round(20 * count / peak) if peak else 0)
        out.append(f"  {monday:%Y-%m-%d}  {count:>3}  {bar}")
    return "\n".join(out)
