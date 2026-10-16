"""Synthetic demo data, so the project can be tried without any personal information."""

from importlib.resources import files
from io import StringIO

from sqlalchemy.orm import Session

from jobtrail.ingestion.csv_import import ImportReport, import_csv

DEMO_DB_FILE = "demo.db"


def load_demo(session: Session) -> ImportReport:
    text = (files("jobtrail") / "demo_data" / "demo_applications.csv").read_text(encoding="utf-8")
    return import_csv(session, StringIO(text))
