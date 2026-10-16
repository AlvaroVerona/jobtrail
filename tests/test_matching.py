import pytest
from sqlalchemy import select

from helpers import (
    CV,
    HONEST,
    POSTING,
    REQUIREMENTS,
    fit_json,
    parsed_posting,
    posting_json,
)
from jobtrail.database.models import CVVersion, FitAnalysis, Posting
from jobtrail.domain.status import Status
from jobtrail.ingestion.cv import CVError
from jobtrail.llm.base import LLMError
from jobtrail.llm.fake import FakeLLM
from jobtrail.schemas.matching import ParsedPosting
from jobtrail.services.fit_scorer import MAX_CV_CHARS, FitScorer
from jobtrail.services.matching import CVService, MatchingService, NotFound
from jobtrail.services.posting_parser import MAX_POSTING_CHARS, PostingParser
from jobtrail.services.tracker import ApplicationNotFound

# -- posting parser ---------------------------------------------------------------------------


def test_parser_extracts_and_verifies_a_posting():
    llm = FakeLLM(posting_json())
    parsed = PostingParser(llm).parse(POSTING)
    assert (parsed.title, parsed.company, parsed.location) == (
        "Data Scientist (mid-level)",
        "Northwind Labs",
        "Madrid",
    )
    assert parsed.salary_text == "45.000-55.000 EUR"
    assert [r.id for r in parsed.requirements] == [1, 2, 3, 4, 5, 6]
    assert (parsed.dropped, parsed.truncated) == (0, False)
    assert llm.schemas[0] is not None  # output is constrained by a JSON schema
    assert "Northwind Labs" in llm.prompts[0]  # the posting reached the model


def test_parser_drops_requirements_whose_quote_is_not_in_the_posting():
    invented = ("Rust", "must", "Production experience with Rust")
    parsed = PostingParser(FakeLLM(posting_json([*REQUIREMENTS, invented]))).parse(POSTING)
    assert parsed.dropped == 1
    assert "Rust" not in [r.text for r in parsed.requirements]
    assert len(parsed.requirements) == 6


def test_parser_stores_quotes_without_bullet_markers():
    bulleted = [
        ("Python", "must", "- Strong Python and SQL"),
        ("Spark", "nice", "\u2022 Knowledge of Spark"),
    ]
    parsed = PostingParser(FakeLLM(posting_json(bulleted))).parse(POSTING)
    assert [r.quote for r in parsed.requirements] == ["Strong Python and SQL", "Knowledge of Spark"]


def test_parser_clears_fields_the_posting_does_not_contain():
    answer = posting_json(company="Globex Corp", location="Barcelona", salary_text="80.000 EUR")
    parsed = PostingParser(FakeLLM(answer)).parse(POSTING)
    assert (parsed.company, parsed.location, parsed.salary_text) == (None, None, None)
    assert parsed.title == "Data Scientist (mid-level)"


def test_parser_retries_with_feedback_when_nothing_verifies():
    bad = posting_json([("x", "must", "A sentence the posting never contains")])
    llm = FakeLLM(bad, posting_json())
    parsed = PostingParser(llm).parse(POSTING)
    assert len(parsed.requirements) == 6
    assert "REJECTED" in llm.prompts[1]
    assert "A sentence the posting never contains" in llm.prompts[1]


def test_parser_gives_up_after_the_retries():
    bad = posting_json([("x", "must", "Not in the posting")])
    with pytest.raises(LLMError, match="posting parser failed after 3 attempts"):
        PostingParser(FakeLLM(bad, bad, bad)).parse(POSTING)


def test_parser_retries_on_a_schema_violation():
    llm = FakeLLM(posting_json(seniority="wizard"), posting_json())
    assert PostingParser(llm).parse(POSTING).seniority == "mid"


def test_parser_tolerates_fences_null_strings_and_python_literals():
    answer = "Sure!\n```json\n" + posting_json(company="null", salary_text=None) + "\n```"
    parsed = PostingParser(FakeLLM(answer)).parse(POSTING)
    assert parsed.company is None and parsed.salary_text is None
    python_style = posting_json().replace('"title"', '"remote_note": None, "title"')
    assert PostingParser(FakeLLM(python_style)).parse(POSTING).company == "Northwind Labs"


def test_parser_merges_duplicate_requirements():
    twice = [REQUIREMENTS[1], ("python and sql", "must", "Strong Python and SQL")]
    parsed = PostingParser(FakeLLM(posting_json(twice))).parse(POSTING)
    assert len(parsed.requirements) == 1


def test_parser_truncates_long_postings_and_only_trusts_the_visible_part():
    tail = "Requires fluent Esperanto and underwater welding"
    long_text = POSTING + "x " * MAX_POSTING_CHARS + tail
    answer = posting_json([REQUIREMENTS[1], ("Esperanto", "must", tail)])
    parsed = PostingParser(FakeLLM(answer)).parse(long_text)
    assert parsed.truncated
    assert [r.text for r in parsed.requirements] == [
        "Python and SQL"
    ]  # the cut-off quote is dropped


def test_parser_rejects_an_empty_posting():
    with pytest.raises(ValueError, match="empty"):
        PostingParser(FakeLLM()).parse("   ")


# -- fit scorer -------------------------------------------------------------------------------


def test_fit_with_verified_evidence_scores_by_formula():
    result = FitScorer(FakeLLM(HONEST)).score(CV, parsed_posting())
    assert result.downgraded == 0
    # must: met, met, missing, missing (2+2 of 8); nice: met, missing (1 of 2) -> 5 of 10
    assert result.score == 50
    assert [i.status for i in result.items] == [
        "met",
        "met",
        "missing",
        "missing",
        "met",
        "missing",
    ]
    assert all(i.verified for i in result.items)
    assert result.items[1].quote == "Automated monthly reporting with Python and SQL"


def test_a_claim_with_an_invented_quote_is_downgraded_to_missing():
    answer = fit_json(
        (1, "met", "Led a team of 12 data scientists for five years"),  # not in the CV
        (2, "met", "Automated monthly reporting with Python and SQL"),
        (3, "missing", None),
        (4, "met", "Deployed models on Kubernetes clusters"),  # not in the CV
        (5, "missing", None),
        (6, "missing", None),
    )
    result = FitScorer(FakeLLM(answer), max_retries=0).score(CV, parsed_posting())
    assert result.downgraded == 2
    invented = result.items[0]
    assert (invented.status, invented.claimed_status, invented.verified) == (
        "missing",
        "met",
        False,
    )
    assert invented.quote is None  # the fake evidence is not kept
    # Only requirement 2 counts: 2 of (2+2+2+2+1+1) -> 20
    assert result.score == 20


def test_java_is_not_satisfied_by_javascript():
    answer = fit_json(
        (1, "missing", None),
        (2, "missing", None),
        (3, "met", "Java"),  # the CV only says JavaScript
        (4, "missing", None),
        (5, "missing", None),
        (6, "missing", None),
    )
    result = FitScorer(FakeLLM(answer), max_retries=0).score(CV, parsed_posting())
    assert result.items[2].status == "missing" and not result.items[2].verified
    assert result.score == 0


def test_a_met_claim_without_any_quote_is_downgraded():
    answer = fit_json(*[(i, "met", None) for i in range(1, 7)])
    result = FitScorer(FakeLLM(answer), max_retries=0).score(CV, parsed_posting())
    assert result.score == 0 and result.downgraded == 6


def test_partial_evidence_counts_half():
    answer = fit_json(
        (1, "missing", None),
        (2, "partial", "Python, SQL, pandas, scikit-learn"),
        *[(i, "missing", None) for i in range(3, 7)],
    )
    result = FitScorer(FakeLLM(answer), max_retries=0).score(CV, parsed_posting())
    assert result.items[1].status == "partial" and result.items[1].verified
    assert result.score == 10  # 0.5 * 2 of 10


def test_fit_retries_with_the_bad_quote_in_the_feedback_then_accepts_the_fix():
    bad = fit_json(
        (1, "met", "Ran a 50-person data team"), *[(i, "missing", None) for i in range(2, 7)]
    )
    llm = FakeLLM(bad, HONEST)
    result = FitScorer(llm).score(CV, parsed_posting())
    assert len(llm.prompts) == 2
    assert "Some quotes are not in the CV" in llm.prompts[1]
    assert "1: 'met'" in llm.prompts[1]
    assert result.downgraded == 0 and result.score == 50


def test_on_the_last_attempt_unverified_claims_are_downgraded_not_fatal():
    bad = fit_json(
        (1, "met", "Ran a 50-person data team"), *[(i, "missing", None) for i in range(2, 7)]
    )
    result = FitScorer(FakeLLM(bad, bad), max_retries=1).score(CV, parsed_posting())
    assert result.downgraded == 1 and result.score == 0


def test_requirements_the_model_skipped_are_marked_missing():
    answer = fit_json((2, "met", "Automated monthly reporting with Python and SQL"))
    result = FitScorer(FakeLLM(answer), max_retries=0).score(CV, parsed_posting())
    skipped = result.items[0]
    assert (skipped.status, skipped.claimed_status) == ("missing", None)
    assert "did not assess" in skipped.reason


def test_unknown_and_duplicate_ids_are_ignored():
    answer = fit_json(
        (99, "met", "Python"),
        (2, "met", "Automated monthly reporting with Python and SQL"),
        (2, "missing", None),  # the first answer for an id wins
    )
    result = FitScorer(FakeLLM(answer), max_retries=0).score(CV, parsed_posting())
    assert result.items[1].status == "met"
    assert len(result.items) == 6


def test_a_quote_beyond_the_visible_part_of_a_long_cv_is_not_trusted():
    hidden = "Chief Data Officer at a unicorn"
    long_cv = CV + "x " * MAX_CV_CHARS + hidden
    answer = fit_json((1, "met", hidden), *[(i, "missing", None) for i in range(2, 7)])
    result = FitScorer(FakeLLM(answer), max_retries=0).score(long_cv, parsed_posting())
    assert result.cv_truncated and result.downgraded == 1


def test_fit_needs_requirements():
    with pytest.raises(ValueError, match="no requirements"):
        FitScorer(FakeLLM()).score(CV, ParsedPosting())


def test_fit_prompt_contains_the_cv_and_every_requirement():
    llm = FakeLLM(HONEST)
    FitScorer(llm).score(CV, parsed_posting())
    prompt = llm.prompts[0]
    assert "Built dashboards in Power BI" in prompt
    assert "3. [must] Java" in prompt and "6. [nice] Spark" in prompt


# -- persistence ------------------------------------------------------------------------------


def test_cv_versions_are_deduplicated_by_content(session):
    service = CVService(session)
    first, created = service.add_text("v1", CV)
    again, created_again = service.add_text("copy", "  " + CV.upper() + "\n")  # same words
    assert (created, created_again) == (True, False)
    assert again.id == first.id
    assert service.add_text("v2", CV + "\n- New skill: Rust")[1] is True


def test_cv_file_upload_uses_the_filename_as_default_label(session):
    version, _ = CVService(session).add_file("", "my_cv.md", CV.encode())
    assert version.label == "my_cv" and "Power BI" in version.text


def test_empty_or_unreadable_cv_is_refused(session):
    with pytest.raises(CVError):
        CVService(session).add_text("x", "  ")
    with pytest.raises(CVError):
        CVService(session).add_file("x", "cv.pdf", b"nope")


def test_add_posting_and_analyze_end_to_end(session):
    llm = FakeLLM(posting_json(), HONEST)
    service = MatchingService(session, llm)
    cv, _ = CVService(session).add_text("v1", CV)

    posting = service.add_posting(POSTING, url="https://northwind.example/jobs/1")
    analysis = service.analyze(posting, cv)
    session.commit()

    assert posting.parsed["company"] == "Northwind Labs" and posting.model == "fake-llm"
    assert analysis.score == 50 and analysis.detail["downgraded"] == 0
    assert posting.fits == [analysis]
    assert service.list_postings() == [posting]


def test_posting_urls_must_be_web_urls(session):
    service = MatchingService(session, FakeLLM())
    for url in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x.example/a", "northwind"):
        with pytest.raises(ValueError, match="http"):
            service.add_posting(POSTING, url=url)
    assert session.scalars(select(Posting)).all() == []


def test_posting_can_be_linked_to_an_application(session, tracker):
    application = tracker.create_application("Northwind Labs", status=Status.APPLIED)
    posting = MatchingService(session, FakeLLM(posting_json())).add_posting(
        POSTING, application_id=application.id
    )
    assert application.postings == [posting]

    with pytest.raises(ApplicationNotFound):
        MatchingService(session, FakeLLM(posting_json())).add_posting(POSTING, application_id=999)


def test_deleting_an_application_keeps_its_posting(session, tracker):
    application = tracker.create_application("Northwind Labs")
    posting = MatchingService(session, FakeLLM(posting_json())).add_posting(
        POSTING, application_id=application.id
    )
    tracker.delete_application(application)
    session.commit()
    assert session.get(Posting, posting.id).application_id is None


def test_deleting_a_cv_removes_only_its_analyses(session):
    llm = FakeLLM(posting_json(), HONEST, HONEST)
    service = MatchingService(session, llm)
    cvs = CVService(session)
    one, _ = cvs.add_text("one", CV)
    two, _ = cvs.add_text("two", CV + "\n- Extra line about Rust")
    posting = service.add_posting(POSTING)
    service.analyze(posting, one)
    keep = service.analyze(posting, two)
    cvs.delete(one)
    session.commit()
    assert session.scalars(select(FitAnalysis)).all() == [keep]
    assert session.scalars(select(CVVersion)).all() == [two]


def test_not_found_errors(session):
    with pytest.raises(NotFound):
        CVService(session).get(1)
    with pytest.raises(NotFound):
        MatchingService(session, FakeLLM()).get_posting(1)
