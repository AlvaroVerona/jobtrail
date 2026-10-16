from fastapi.testclient import TestClient

from jobtrail import __version__
from jobtrail.cli import build_parser
from jobtrail.main import create_app


def test_health_endpoint():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_cli_exposes_planned_commands():
    parser = build_parser()
    for command in ("serve", "demo"):
        assert parser.parse_args([command]).command == command
    assert parser.parse_args(["import-csv", "x.csv"]).path == "x.csv"
