from collections.abc import Callable


class FakeLLM:
    """A scripted stand-in for tests: returns the queued answers in order and records every
    prompt it was given. An answer may be a callable that receives the prompt."""

    def __init__(self, *answers: str | Callable[[str], str], problem: str | None = None):
        self.model = "fake-llm"
        self._answers = list(answers)
        self._problem = problem
        self.prompts: list[str] = []
        self.schemas: list[dict | None] = []

    def generate(self, prompt: str, *, schema: dict | None = None) -> str:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if not self._answers:
            raise AssertionError("FakeLLM was asked for more answers than the test queued")
        answer = self._answers.pop(0)
        return answer(prompt) if callable(answer) else answer

    def check(self) -> str | None:
        return self._problem
