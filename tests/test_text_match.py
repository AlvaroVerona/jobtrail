import pytest

from jobtrail.domain.scoring import fit_score
from jobtrail.domain.text_match import normalize, quote_in_text

CV = """Built dashboards in Power BI used by 40 finance users
- Automated monthly reporting with Python and SQL
Skills: Python, SQL, JavaScript, C++, R"""


@pytest.mark.parametrize(
    "quote",
    [
        "Automated monthly reporting with Python and SQL",
        "automated MONTHLY reporting with python and sql",  # case
        "Automated monthly\n  reporting with   Python and SQL",  # whitespace and line breaks
        "- Automated monthly reporting with Python and SQL.",  # trailing punctuation, bullet
        "“Automated monthly reporting”",  # curly quotes
        "...Automated monthly reporting with Python and SQL...",  # ellipsis at the edges
        "Power BI",
        "C++",
        "Skills: Python, SQL, JavaScript, C++, R",  # the way to cite a one-letter language
    ],
)
def test_real_quotes_are_found(quote):
    assert quote_in_text(quote, CV)


@pytest.mark.parametrize(
    "quote",
    [
        "Built a data lake on Kubernetes",  # invented
        "Automated reporting with Python",  # paraphrase: words dropped from the middle
        "Java",  # only JavaScript is in the CV
        "Pow",  # fragment of a word
        "Scrip",
        "",
        None,
        "a",  # too short to prove anything
        "R",  # same rule: a single character proves nothing, so cite the whole skills line
        "...",
    ],
)
def test_invented_or_trivial_quotes_are_rejected(quote):
    assert not quote_in_text(quote, CV)


def test_quote_is_not_found_in_an_empty_text():
    assert not quote_in_text("Python", "")


def test_normalize_handles_ligatures_and_bullets():
    assert normalize("• eﬃcient  work") == "efficient work"


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([("must", "met"), ("must", "met")], 100),
        ([("must", "missing"), ("nice", "missing")], 0),
        ([("must", "met"), ("must", "missing")], 50),
        ([("must", "partial")], 50),
        ([("must", "met"), ("nice", "missing")], 67),  # 2 of 3 weight
        ([("must", "missing"), ("nice", "met")], 33),  # a nice-to-have cannot rescue a must-have
        ([], None),
    ],
)
def test_fit_score_formula(verdicts, expected):
    assert fit_score(verdicts) == expected
