"""Shared builders for the matching tests: fixture texts and scripted model answers."""

import json
from pathlib import Path

from jobtrail.schemas.matching import ParsedPosting, Requirement

FIXTURES = Path(__file__).parent / "fixtures"
CV = (FIXTURES / "cv.md").read_text()
POSTING = (FIXTURES / "posting.txt").read_text()

REQUIREMENTS = [
    ("3+ years in data science", "must", "3+ years of experience in data science or analytics"),
    ("Python and SQL", "must", "Strong Python and SQL"),
    ("Java", "must", "Experience with Java for our data pipelines"),
    ("Kubernetes", "must", "Experience deploying models with Kubernetes"),
    ("Power BI", "nice", "Experience with Power BI dashboards"),
    ("Spark", "nice", "Knowledge of Spark"),
]


def posting_json(requirements=REQUIREMENTS, **fields) -> str:
    body = {
        "title": "Data Scientist (mid-level)",
        "company": "Northwind Labs",
        "seniority": "mid",
        "location": "Madrid",
        "remote": "hybrid",
        "salary_text": "45.000-55.000 EUR",
        "requirements": [{"text": t, "kind": k, "quote": q} for t, k, q in requirements],
    }
    body.update(fields)
    return json.dumps(body)


def fit_json(*assessments) -> str:
    return json.dumps(
        {
            "assessments": [
                {"id": i, "status": s, "cv_quote": q, "reason": "because"}
                for i, s, q in assessments
            ]
        }
    )


def parsed_posting() -> ParsedPosting:
    return ParsedPosting(
        requirements=[
            Requirement(id=i, text=t, kind=k, quote=q)
            for i, (t, k, q) in enumerate(REQUIREMENTS, 1)
        ]
    )


HONEST = fit_json(
    (1, "met", "Data Analyst, Fictional Bank (2023-2025)"),
    (2, "met", "Automated monthly reporting with Python and SQL"),
    (3, "missing", None),
    (4, "missing", None),
    (5, "met", "Built dashboards in Power BI used by 40 finance users"),
    (6, "missing", None),
)
