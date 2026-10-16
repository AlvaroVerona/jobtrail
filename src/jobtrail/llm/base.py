from typing import Protocol


class LLMUnavailable(RuntimeError):
    """The model server cannot be reached, or the model is not installed."""


class LLMError(RuntimeError):
    """The model answered, but not with anything usable after retries."""


class LLMClient(Protocol):
    model: str

    def generate(self, prompt: str, *, schema: dict | None = None) -> str:
        """Return the model's text. With `schema`, the server constrains the output to it."""
        ...

    def check(self) -> str | None:
        """None when ready to use; otherwise a short explanation of what is wrong."""
        ...
