from datetime import UTC, date, datetime
from io import StringIO

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobtrail.cli import main
from jobtrail.database.models import Application
from jobtrail.database.session import init_db, make_engine
from jobtrail.demo import load_demo
from jobtrail.domain.status import Status
from jobtrail.ingestion.csv_import import (
    RowError,
    import_csv,
    parse_date,
    parse_salary,
    parse_status,
)

HEADER = "Company,Role,Status,Applied date,Canal,Track,Expected salary,Fecha respuesta,Notes\n"


def run(session, body: str, header: str = HEADER, **kwargs):
    return import_csv(session, StringIO(header + body), **kwargs)


def apps(session):
    return list(session.scalars(select(Application).order_by(Application.id)))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-05-19", date(2026, 5, 19)),
        ("19/05/2026", date(2026, 5, 19)),  # day first
        ("May 19, 2026", date(2026, 5, 19)),  # Notion export style
        ("19-05-2026", date(2026, 5, 19)),
    ],
)
def test_parse_date_formats(text, expected):
    parsed = parse_date(text)
    assert parsed == datetime(expected.year, expected.month, expected.day, tzinfo=UTC)


def test_parse_date_rejects_garbage():
    with pytest.raises(RowError, match="date"):
        parse_date("last tuesday")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Applied", Status.APPLIED),
        ("To apply", Status.TO_APPLY),
        ("to_apply", Status.TO_APPLY),
        ("Sin respuesta", Status.NO_RESPONSE),
        ("Follow-up enviado", Status.FOLLOW_UP_SENT),
        ("  REJECTED ", Status.REJECTED),
    ],
)
def test_parse_status_aliases(text, expected):
    assert parse_status(text) is expected


def test_parse_status_rejects_unknown():
    with pytest.raises(RowError, match="Unknown status"):
        parse_status("ghosted")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("45", 45), ("€45", 45), ("45.5", 46), ("45.000", 45000), ("45,000", 45000), ("42,5", 42)],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_basic_import_creates_applications_with_history(session):
    report = run(
        session,
        "Acme,Data Scientist,Applied,2026-05-19,Company website,Data,45,,hello\n"
        "Globex,ML Engineer,Rejected,2026-05-20,Easy Apply,Data,,2026-05-27,\n",
    )
    assert (report.created, report.skipped_duplicates, report.errors) == (2, 0, [])

    acme, globex = apps(session)
    assert acme.status is Status.APPLIED and acme.salary_expected == 45 and acme.notes == "hello"
    assert acme.applied_at == datetime(2026, 5, 19, tzinfo=UTC)
    assert acme.channel.name == "Company website" and acme.track.name == "Data"

    # Rejected with a response date gets a two-step history on the real dates.
    assert [(e.to_status, e.at.day) for e in globex.events] == [
        (Status.APPLIED, 20),
        (Status.REJECTED, 27),
    ]


def test_final_status_without_response_date_is_a_single_event(session):
    run(session, "Acme,DS,Rejected,2026-05-19,,,,,\n")
    (app,) = apps(session)
    assert [e.to_status for e in app.events] == [Status.REJECTED]
    assert app.applied_at == datetime(2026, 5, 19, tzinfo=UTC)


def test_illegal_two_step_falls_back_to_single_event(session):
    # applied -> offer is not a legal move, so the history is not invented.
    run(session, "Acme,DS,Offer,2026-05-19,,,,2026-06-01,\n")
    (app,) = apps(session)
    assert app.status is Status.OFFER
    assert len(app.events) == 1


def test_reimporting_the_same_file_is_idempotent(session):
    body = "Acme,DS,Applied,2026-05-19,,,,,\nGlobex,ML,Applied,2026-05-20,,,,,\n"
    first = run(session, body)
    second = run(session, body)
    assert (first.created, second.created, second.skipped_duplicates) == (2, 0, 2)
    assert len(apps(session)) == 2


def test_same_role_on_a_different_date_is_a_new_application(session):
    report = run(
        session,
        "Acme,DS,Applied,2026-05-19,,,,,\nacme,ds,Applied,2026-10-14,,,,,\n",
    )
    assert report.created == 2


def test_bad_rows_are_reported_and_do_not_stop_the_import(session):
    report = run(
        session,
        "Acme,DS,Applied,2026-05-19,,,,,\n"
        ",DS,Applied,2026-05-19,,,,,\n"  # no company
        "Globex,DS,ghosted,2026-05-19,,,,,\n"  # unknown status
        "Initech,DS,Applied,,,,,,\n"  # submitted but no date
        "Umbrella,DS,Applied,2026-05-21,,,,,\n",
    )
    assert report.created == 2
    assert [line for line, _ in report.errors] == [3, 4, 5]
    assert "Missing company" in report.errors[0][1]
    assert "required" in report.errors[2][1]


def test_wishlist_row_needs_no_date(session):
    run(session, "Acme,DS,Wishlist,,,,,,\n")
    (app,) = apps(session)
    assert app.status is Status.WISHLIST and app.applied_at is None


def test_spanish_notion_columns_and_channel_names(session):
    header = "Company,Role,Status,Applied date,Canal,Track,Fecha respuesta\n"
    run(
        session,
        "Acme,DS,Sin respuesta,19/05/2026,Web de empresa,Data,\n"
        "Globex,ML,Applied,20/05/2026,Referido/contacto,Data,\n",
        header=header,
    )
    acme, globex = apps(session)
    assert acme.status is Status.NO_RESPONSE
    assert acme.channel.name == "Company website"
    assert globex.channel.name == "Referral"


def test_headers_are_matched_case_insensitively():
    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as session:
        report = import_csv(session, StringIO("COMPANY,ROLE,STATUS\nAcme,DS,wishlist\n"))
        assert report.created == 1


def test_missing_company_column_is_a_hard_error(session):
    with pytest.raises(ValueError, match="company column"):
        import_csv(session, StringIO("Role,Status\nDS,Applied\n"))


def test_dry_run_writes_nothing(session):
    report = run(session, "Acme,DS,Applied,2026-05-19,,,,,\n", dry_run=True)
    assert report.created == 1
    assert apps(session) == []


def test_demo_data_loads_cleanly_and_is_synthetic(session):
    report = load_demo(session)
    assert report.errors == []
    assert report.created >= 50
    assert {a.channel.name for a in apps(session) if a.channel} >= {"Easy Apply", "Referral"}


# -- CLI -------------------------------------------------------------------------------------


def test_cli_import_csv_end_to_end(isolated_data, capsys):
    csv_file = isolated_data / "apps.csv"
    csv_file.write_text(
        "﻿Company,Role,Status,Applied date\nAcme,DS,Applied,2026-05-19\n", encoding="utf-8"
    )
    assert main(["import-csv", str(csv_file)]) == 0
    assert "Created 1" in capsys.readouterr().out
    assert main(["import-csv", str(csv_file)]) == 0
    assert "skipped 1" in capsys.readouterr().out


def test_cli_import_csv_exit_codes(isolated_data, capsys):
    assert main(["import-csv", str(isolated_data / "missing.csv")]) == 2

    bad = isolated_data / "bad.csv"
    bad.write_text("Company,Status\nAcme,ghosted\n", encoding="utf-8")
    assert main(["import-csv", str(bad)]) == 1
    assert "Unknown status" in capsys.readouterr().err

    no_company = isolated_data / "nocol.csv"
    no_company.write_text("Role\nDS\n", encoding="utf-8")
    assert main(["import-csv", str(no_company)]) == 2


def test_cli_demo_uses_a_separate_database(isolated_data, capsys):
    assert main(["demo"]) == 0
    assert (isolated_data / "demo.db").exists()
    assert not (isolated_data / "jobtrail.db").exists()
    assert "JOBTRAIL_DB_FILE=demo.db" in capsys.readouterr().out
    assert main(["demo", "--reset"]) == 0
