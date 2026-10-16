import ollama

from jobtrail.llm.base import LLMUnavailable


class OllamaClient:
    """Local model through Ollama. Nothing leaves the machine."""

    def __init__(
        self,
        model: str = "llama3.1",
        host: str = "http://localhost:11434",
        num_ctx: int = 8192,
        timeout: float = 300,
    ):
        self.model = model
        self.host = host
        self.num_ctx = num_ctx
        self._client = ollama.Client(host=host, timeout=timeout)
        self._probe = ollama.Client(host=host, timeout=3)  # fast failure for status checks

    def generate(self, prompt: str, *, schema: dict | None = None) -> str:
        try:
            response = self._client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                format=schema,
                think=False,
                options={"num_ctx": self.num_ctx, "temperature": 0},
            )
        except ollama.ResponseError as exc:
            if exc.status_code == 404:
                raise LLMUnavailable(
                    f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"
                ) from exc
            raise LLMUnavailable(f"Ollama returned an error: {exc}") from exc
        except (ConnectionError, OSError) as exc:
            raise LLMUnavailable(self._unreachable()) from exc
        except Exception as exc:  # httpx connection/timeout errors
            if exc.__class__.__module__.startswith("httpx"):
                raise LLMUnavailable(self._unreachable()) from exc
            raise
        return response["message"]["content"]

    def check(self) -> str | None:
        try:
            installed = [m.model for m in self._probe.list().models]
        except Exception:  # noqa: BLE001 - any failure here means "not usable right now"
            return self._unreachable()
        names = {name.split(":")[0] if ":" not in self.model else name for name in installed}
        if self.model not in installed and self.model not in names:
            return f"Model '{self.model}' is not installed. Run: ollama pull {self.model}"
        return None

    def _unreachable(self) -> str:
        return f"Cannot reach Ollama at {self.host}. Is it running? Start it with: ollama serve"
