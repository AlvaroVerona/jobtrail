"""Run the posting parser and fit scorer on the synthetic fixtures with real local models.

    uv run python scripts/compare_models.py llama3.1 qwen3:8b

Not part of the test suite (it needs Ollama and takes minutes). It exists to choose the default
model with evidence. Ground truth for the fixtures: Python+SQL and Power BI are in the CV;
Java (the CV only has JavaScript), Kubernetes and Spark are not; the 3+ years requirement is
borderline (the CV shows about two years).
"""

import sys
import time
from pathlib import Path

from jobtrail.config import get_settings
from jobtrail.llm.base import LLMError, LLMUnavailable
from jobtrail.llm.ollama_client import OllamaClient
from jobtrail.services.fit_scorer import FitScorer
from jobtrail.services.posting_parser import PostingParser

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
# requirement keyword -> statuses that are acceptable once verified against the CV
EXPECTED = {
    "python": {"met"},
    "power bi": {"met"},
    "java": {"missing"},
    "kubernetes": {"missing"},
    "spark": {"missing"},
    "years": {"missing", "partial"},
}


def run(model: str, runs: int) -> None:
    settings = get_settings()
    llm = OllamaClient(
        model, settings.ollama_host, settings.ollama_num_ctx, settings.ollama_timeout
    )
    print(f"\n=== {model}")
    if problem := llm.check():
        print("  unavailable:", problem)
        return
    posting_text = (FIXTURES / "posting.txt").read_text()
    cv_text = (FIXTURES / "cv.md").read_text()

    for n in range(1, runs + 1):
        try:
            t0 = time.time()
            parsed = PostingParser(llm).parse(posting_text)
            t_parse = time.time() - t0
            t0 = time.time()
            fit = FitScorer(llm).score(cv_text, parsed)
            t_fit = time.time() - t0
        except (LLMError, LLMUnavailable) as exc:
            print(f"  run {n}: FAILED {str(exc)[:300]}")
            continue

        correct = wrong = 0
        wrong_items = []
        for item in fit.items:
            for keyword, acceptable in EXPECTED.items():
                if keyword in item.text.lower():
                    if item.status in acceptable:
                        correct += 1
                    else:
                        wrong += 1
                        wrong_items.append(f"{item.text!r} -> {item.status}")
        false_positives = [
            i.text
            for i in fit.items
            if i.status != "missing"
            and any(k in i.text.lower() for k in ("java", "kubernetes", "spark"))
        ]
        print(
            f"  run {n}: parse {t_parse:.0f}s ({len(parsed.requirements)} reqs, "
            f"{parsed.dropped} dropped) | fit {t_fit:.0f}s score={fit.score} "
            f"downgraded={fit.downgraded} | correct {correct}/{correct + wrong}"
            f" | FALSE POSITIVES after verification: {false_positives or 'none'}"
        )
        for line in wrong_items:
            print("      wrong:", line)


if __name__ == "__main__":
    models = sys.argv[1:] or ["llama3.1"]
    for name in models:
        run(name, runs=2)
