"""Synthetic demo data, so the project can be tried without any personal information."""

import csv
from datetime import date, timedelta
from importlib.resources import files
from io import StringIO

from sqlalchemy.orm import Session

from jobtrail.ingestion.csv_import import ImportReport, import_csv

DEMO_DB_FILE = "demo.db"


def _shift_to_recent(text: str, today: date) -> str:
    """Move every date so the latest application was sent three days ago.

    The demo is a snapshot with fixed dates; left alone it would go stale, leaving the weekly
    chart empty and nothing overdue on the follow-ups page. The gaps between dates are kept,
    except that no reply is dated in the future: a reply that would land after yesterday is
    pulled back to yesterday."""
    reader = csv.DictReader(StringIO(text))
    rows = list(reader)
    applied = [date.fromisoformat(r["Applied date"]) for r in rows if r["Applied date"]]
    if not applied:
        return text
    delta = (today - timedelta(days=3)) - max(applied)
    yesterday = today - timedelta(days=1)
    for row in rows:
        if row["Applied date"]:
            row["Applied date"] = (date.fromisoformat(row["Applied date"]) + delta).isoformat()
        if row["Response date"]:
            shifted = date.fromisoformat(row["Response date"]) + delta
            row["Response date"] = min(shifted, yesterday).isoformat()
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def load_demo(session: Session, today: date | None = None) -> ImportReport:
    text = (files("jobtrail") / "demo_data" / "demo_applications.csv").read_text(encoding="utf-8")
    return import_csv(session, StringIO(_shift_to_recent(text, today or date.today())))
