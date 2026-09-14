"""LLM provider registry — endpoint, auth, and thinking-mode quirks per backend.

Both backends speak the OpenAI chat-completions shape, but they disagree on
everything around it:

``local``    mlx-serve on the LAN. Exposes ``/v1/models`` (so the context window
             is discovered at runtime), takes the non-standard
             ``enable_thinking`` request flag, and streams thinking back as
             ``reasoning_content``.
``deepseek`` api.deepseek.com (key from ``DEEPSEEK_API``). Hosted and
             OpenAI-format; thinking mode is ON by
             default and driven by ``thinking`` / ``reasoning_effort``. Two
             behaviours matter for us: (1) when a request carries ``tools``,
             DeepSeek REQUIRES the previous turns' ``reasoning_content`` to be
             echoed back or it returns HTTP 400 — hence ``reasoning_replay``;
             (2) streamed responses only report token usage when
             ``stream_options.include_usage`` is set — hence ``stream_usage``.

Add a provider by appending one :class:`Provider` to :data:`PROVIDERS`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

#: Default model for the local mlx-serve backend (a hash id, per that server).
LOCAL_DEFAULT_MODEL = "3833d0220ac862d6de38448c0cd414bd2ca29d00"


@dataclass(frozen=True)
class Provider:
    """One LLM backend: where it lives, how to auth, and how to ask for thinking."""

    name: str
    label: str
    base_url: str
    api_key_env: str
    default_model: str
    context_length: int
    model_env: str
    base_url_env: str | None = None
    thinking_style: str = "none"       # none | enable_thinking | reasoning_effort
    models_endpoint: bool = False      # GET /models to discover context_length
    reasoning_replay: bool = False     # echo reasoning_content back when tools sent
    stream_usage: bool = True          # ask for usage on the final SSE chunk
    default_reasoning_effort: str = "high"
    api_key_env_fallbacks: tuple[str, ...] = ()  # alternate key var names

    # ---- endpoints -----------------------------------------------------
    @property
    def url_base(self) -> str:
        override = os.getenv(self.base_url_env, "").strip() if self.base_url_env else ""
        return (override or self.base_url).rstrip("/")

    @property
    def chat_url(self) -> str:
        return f"{self.url_base}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.url_base}/models"

    # ---- auth ----------------------------------------------------------
    def api_key(self, required: bool = True) -> str:
        """First non-empty key from the provider's env vars (the primary name,
        then any aliases). Raises KeyError naming the primary when required and
        nothing is set."""
        for env in (self.api_key_env, *self.api_key_env_fallbacks):
            key = os.getenv(env, "").strip()
            if key:
                return key
        if required:
            raise KeyError(self.api_key_env)
        return ""

    def headers(self, required: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        key = self.api_key(required=required)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # ---- model + context -----------------------------------------------
    def resolve_model(self, model: str | None = None) -> str:
        """Explicit name > provider env var > provider default."""
        if model:
            return model
        return os.getenv(self.model_env, "").strip() or self.default_model

    def _fetch_models(self, timeout: float = 5.0) -> list[dict]:
        """Best-effort ``GET /models``; empty when unreachable or unauthorised."""
        try:
            r = requests.get(self.models_url, headers=self.headers(required=False),
                             timeout=timeout)
            if r.status_code == 200:
                return r.json().get("data") or []
        except Exception:
            pass
        return []

    def list_models(self, timeout: float = 5.0) -> list[str]:
        """Model ids the provider advertises, for /model suggestions."""
        return [m["id"] for m in self._fetch_models(timeout) if m.get("id")]

    def resolve_context_length(self, timeout: float = 5.0) -> int:
        """Ask the server for its context window, falling back to the static
        value. Local servers are queried; hosted providers aren't (and a dead
        LAN server must not stop us from running against DeepSeek)."""
        if not self.models_endpoint:
            return self.context_length
        entries = self._fetch_models(timeout)
        if entries:
            length = entries[0].get("context_length")
            if length:
                return int(length)
        return self.context_length

    # ---- request quirks -------------------------------------------------
    def thinking_params(self) -> dict:
        """Extra request fields that turn on reasoning, per backend dialect."""
        if self.thinking_style == "enable_thinking":
            value = os.getenv("MLX_ENABLE_THINKING", "1").strip().lower()
            return {} if value in ("0", "false", "no", "off") else {"enable_thinking": True}
        if self.thinking_style == "reasoning_effort":
            effort = (os.getenv("DEEPSEEK_REASONING_EFFORT", "").strip().lower()
                      or self.default_reasoning_effort)
            if effort in ("none", "off", "0", "false"):
                return {"reasoning_effort": "none"}
            return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
        return {}

    def thinking_label(self, provider=None) -> str:
        """The active thinking level, mirroring presentation.thinking_label,
        with ``reasoning_effort: none`` normalised to ``off``."""
        try:
            params = self.thinking_params()
        except Exception:
            return ""
        if "reasoning_effort" in params:
            effort = str(params["reasoning_effort"])
            return "off" if effort in ("none", "off") else effort
        return "on" if params.get("enable_thinking") else "off"

    def stream_usage_param(self) -> dict:
        return {"stream_options": {"include_usage": True}} if self.stream_usage else {}

    # ---- thinking control ----------------------------------------------
    def thinking_levels(self) -> list[str]:
        """Levels this backend accepts, for /thinking suggestions."""
        if self.thinking_style == "enable_thinking":
            return ["on", "off"]
        if self.thinking_style == "reasoning_effort":
            return ["off", "low", "medium", "high"]
        return []

    def set_thinking(self, level: str) -> str:
        """Set the thinking level for subsequent requests.

        ``Provider`` is frozen (shared, registered globally), so the setting
        lives in an env var — the same channel ``thinking_params`` already
        reads, which also makes it visible to a restarted process.

        Returns the new level as reported by :meth:`thinking_label`; raises
        ValueError on a level this dialect does not accept.
        """
        level = (level or "").strip().lower()
        if self.thinking_style == "enable_thinking":
            if level not in ("on", "off"):
                raise ValueError("level must be on|off for this provider")
            os.environ["MLX_ENABLE_THINKING"] = "1" if level == "on" else "0"
        elif self.thinking_style == "reasoning_effort":
            if level not in ("off", "none", "low", "medium", "high"):
                raise ValueError("level must be off|low|medium|high for this provider")
            if level == "none":
                level = "off"
            os.environ["DEEPSEEK_REASONING_EFFORT"] = level
        else:
            raise ValueError("this provider has no thinking control")
        return self.thinking_label(self)


PROVIDERS: dict[str, Provider] = {
    "local": Provider(
        name="local",
        label="mlx-serve (local)",
        base_url="http://192.168.1.92:8080/v1",
        base_url_env="LLM_BASE_URL",
        api_key_env="API_KEY",
        default_model=LOCAL_DEFAULT_MODEL,
        model_env="LLM_MODEL",
        context_length=32768,
        thinking_style="enable_thinking",
        models_endpoint=True,
        reasoning_replay=False,
        stream_usage=False,
    ),
    "deepseek": Provider(
        name="deepseek",
        label="DeepSeek API",
        base_url="https://api.deepseek.com",
        base_url_env="DEEPSEEK_BASE_URL",
        api_key_env="DEEPSEEK_API",
        api_key_env_fallbacks=("DEEPSEEK_API_KEY",),
        default_model="deepseek-flash",
        model_env="DEEPSEEK_MODEL",
        context_length=1_000_000,
        thinking_style="reasoning_effort",
        models_endpoint=False,
        reasoning_replay=True,
        stream_usage=True,
    ),
}

PROVIDER_NAMES = tuple(PROVIDERS)


def get_provider(name: str | None = None) -> Provider:
    """Resolve a provider by explicit name, then ``$PLEIADES_PROVIDER``, then 'local'."""
    key = (name or os.getenv("PLEIADES_PROVIDER", "") or "local").strip().lower()
    if key not in PROVIDERS:
        raise ValueError(
            f"unknown provider {key!r} — known providers: {', '.join(PROVIDER_NAMES)}")
    return PROVIDERS[key]
