import argparse
import os
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from jobtrail import __version__
from jobtrail.config import get_settings
from jobtrail.database.session import init_db, make_engine
from jobtrail.demo import DEMO_DB_FILE, load_demo
from jobtrail.ingestion.csv_import import ImportReport, import_csv
from jobtrail.reporting import format_report
from jobtrail.services.analytics import AnalyticsService
from jobtrail.watchdog import parent_pid_from_env, watch_parent


def _serve(_: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if (parent := parent_pid_from_env()) is not None:
        # Started by the macOS app: do not outlive it.
        watch_parent(parent, lambda: os._exit(0))
    uvicorn.run("jobtrail.main:create_app", factory=True, host=settings.host, port=settings.port)
    return 0


def _print_report(report: ImportReport, *, dry_run: bool = False) -> None:
    verb = "Would create" if dry_run else "Created"
    print(
        f"{verb} {report.created}, skipped {report.skipped_duplicates} duplicates, "
        f"{len(report.errors)} errors ({report.total_rows} rows)."
    )
    for line, message in report.errors[:10]:
        print(f"  line {line}: {message}", file=sys.stderr)
    if len(report.errors) > 10:
        print(f"  ... and {len(report.errors) - 10} more", file=sys.stderr)


def _import_csv(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 2
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    init_db(engine)
    try:
        # utf-8-sig: Notion and Excel exports start with a byte-order mark.
        with Session(engine) as session, path.open(newline="", encoding="utf-8-sig") as source:
            report = import_csv(session, source, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"Cannot import: {exc}", file=sys.stderr)
        return 2
    _print_report(report, dry_run=args.dry_run)
    return 1 if report.errors else 0


def _demo(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    demo_path = settings.data_dir / DEMO_DB_FILE
    if args.reset:
        demo_path.unlink(missing_ok=True)
    engine = make_engine(f"sqlite:///{demo_path}")
    init_db(engine)
    with Session(engine) as session:
        report = load_demo(session)
    _print_report(report)
    print(f"Demo data is in {demo_path}, separate from your real database.")
    print(f"Browse it with:  JOBTRAIL_DB_FILE={DEMO_DB_FILE} uv run jobtrail serve")
    return 1 if report.errors else 0


def _stats(args: argparse.Namespace) -> int:
    settings = get_settings()
    db_file = DEMO_DB_FILE if args.demo else settings.db_file
    path = settings.data_dir / db_file
    if not path.is_file():
        hint = "Run `jobtrail demo` first." if args.demo else "Run `jobtrail import-csv` first."
        print(f"No database at {path}. {hint}", file=sys.stderr)
        return 2
    engine = make_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        service = AnalyticsService(
            session,
            min_sample=settings.min_sample,
            stale_after_days=settings.stale_after_days,
        )
        print(
            format_report(
                service.report(),
                min_sample=settings.min_sample,
                stale_after_days=settings.stale_after_days,
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobtrail", description="Local-first job search tracker.")
    parser.add_argument("--version", action="version", version=f"jobtrail {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="Run the web app on localhost.").set_defaults(func=_serve)

    demo = sub.add_parser("demo", help="Load synthetic demo data into a separate demo database.")
    demo.add_argument("--reset", action="store_true", help="Delete the demo database first.")
    demo.set_defaults(func=_demo)

    imp = sub.add_parser("import-csv", help="Import applications from a CSV file.")
    imp.add_argument("path")
    imp.add_argument(
        "--dry-run", action="store_true", help="Report what would happen, write nothing."
    )
    imp.set_defaults(func=_import_csv)

    stats = sub.add_parser("stats", help="Print funnel analytics for your applications.")
    stats.add_argument("--demo", action="store_true", help="Read the demo database instead.")
    stats.set_defaults(func=_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
