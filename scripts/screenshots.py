"""Regenerate the README screenshots in docs/images/.

    uv run python scripts/screenshots.py

Starts the app against a throwaway data directory, loads the synthetic demo data, runs the real
local model on the sample CV and posting (so Ollama must be running with the default model), and
captures light-theme screenshots with headless Chrome. Nothing here touches your own database.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "src" / "jobtrail" / "demo_data"
CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("Chrome not found. Set the CHROME environment variable to its path.")


def wait_for(client: httpx.Client, attempts: int = 30) -> None:
    for _ in range(attempts):
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)
    sys.exit("The server did not start.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images")
    args = parser.parse_args()
    chrome = find_chrome()
    base = f"http://127.0.0.1:{args.port}"
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "JOBTRAIL_DATA_DIR": str(Path(tmp) / "data"),
            "JOBTRAIL_DB_FILE": "demo.db",
            "JOBTRAIL_PORT": str(args.port),
        }
        run = [sys.executable, "-m", "jobtrail.cli"]
        subprocess.run([*run, "demo"], env=env, check=True, stdout=subprocess.DEVNULL)
        server = subprocess.Popen(
            [*run, "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            with httpx.Client(base_url=base, timeout=600, follow_redirects=True) as client:
                wait_for(client)
                pages = prepare(client)
                for name, html, width, height in pages:
                    capture(
                        chrome,
                        base,
                        html,
                        Path(tmp) / f"{name}.html",
                        args.out / f"{name}.png",
                        width,
                        height,
                    )
        finally:
            server.terminate()
            server.wait(timeout=10)
    print("Wrote", ", ".join(sorted(p.name for p in args.out.glob("*.png"))))


def prepare(client: httpx.Client) -> list[tuple[str, str, int, int]]:
    """Create the data the screenshots need and return (name, html, width, height)."""
    cv = SAMPLES / "sample_cv.md"
    client.post(
        "/cv", data={"label": "Data science CV"}, files={"file": (cv.name, cv.read_bytes())}
    ).raise_for_status()

    applied = (date.today() - timedelta(days=20)).isoformat()
    created = client.post(
        "/applications",
        data={
            "company": "Northwind Labs",
            "role": "Data Scientist",
            "status": "applied",
            "applied_date": applied,
            "channel": "Company website",
            "track": "Data Science/AI",
        },
    )
    created.raise_for_status()
    application_path = created.url.path  # the redirect lands on /applications/<id>

    posting_text = (SAMPLES / "sample_posting.txt").read_text()
    added = client.post(
        "/postings",
        data={"raw_text": posting_text, "application_id": application_path.rsplit("/", 1)[1]},
    )
    added.raise_for_status()
    posting_path = added.url.path  # /postings/<id>

    client.post(f"{posting_path}/fit", data={"cv_version_id": "1"}).raise_for_status()
    fit_id = re.search(r'id="fit-(\d+)"', client.get(posting_path).text).group(1)
    client.post(f"{posting_path}/fit/{fit_id}/advice").raise_for_status()

    draft = client.post(
        f"{application_path}/draft",
        data={"kind": "follow_up", "language": "en", "recipient": "Sam", "cv_version_id": "1"},
    )
    draft.raise_for_status()
    return [
        ("board", client.get("/board").text, 1600, 900),
        ("fit", client.get(posting_path).text, 1200, 1680),
        ("analytics", client.get("/analytics").text, 1200, 1130),
        ("draft", draft.text, 1200, 1500),
    ]


def capture(
    chrome: str, base: str, html: str, tmp_file: Path, target: Path, width: int, height: int
) -> None:
    """Save the page with the light theme forced and absolute asset URLs, then screenshot it."""
    html = html.replace('<html lang="en">', '<html lang="en" data-theme="light">', 1)
    html = html.replace("<head>", f'<head><base href="{base}/">', 1)
    tmp_file.write_text(html, encoding="utf-8")
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={width},{height}",
            f"--screenshot={target}",
            f"file://{tmp_file}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    main()
