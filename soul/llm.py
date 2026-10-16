"""The only place that talks to a model. Everything else takes a client with one method:

    client.complete(system: str, user: str, model: str, max_tokens: int = 1024) -> str

One stateless, tool-less call per question: memory is injected into the system prompt on every call
instead of living in a long-running session (a session costs more and drifts). `AnthropicClient`
is the real one; tests pass a fake with the same method.
"""
import os


class AnthropicClient:
    """Anthropic Messages API through the official `anthropic` package (pip install anthropic).
    Reads ANTHROPIC_API_KEY from the environment; nothing is stored."""

    def __init__(self, api_key=None, timeout_s=60.0):
        import anthropic                       # imported here so the rest of the package needs no SDK
        self._c = anthropic.Anthropic(api_key=api_key or os.environ.get('ANTHROPIC_API_KEY'), timeout=timeout_s)

    def complete(self, system, user, model, max_tokens=1024):
        r = self._c.messages.create(model=model, max_tokens=max_tokens, system=system,
                                    messages=[{'role': 'user', 'content': user}])
        return ''.join(b.text for b in r.content if getattr(b, 'type', '') == 'text')
