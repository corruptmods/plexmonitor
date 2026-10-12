"""A small tool-using chat loop over Claude or any OpenAI-compatible API.

Two wire formats, one interface. The Anthropic Messages API and OpenAI's
Chat Completions (which Ollama, LM Studio, vLLM and most local servers also
speak) differ in how tool calls and their results travel, so each provider
keeps its own message list; callers only ever see:

    convo = provider.conversation(system)
    convo.add_user("text")
    reply = await run(provider, convo, tools)

Plain aiohttp rather than the vendor SDKs: two small JSON endpoints do not
justify two more dependencies in the image.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger("plexmonitor.ai")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-5-mini"}
REQUEST_TIMEOUT = 90.0
MAX_TOOL_ROUNDS = 6
MAX_TOOL_RESULT = 6000


class AIError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class Turn:
    text: str
    calls: List[ToolCall] = field(default_factory=list)
    raw: Any = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Awaitable[Any]]


class Conversation:
    def __init__(self, system: str) -> None:
        self.system = system
        self.messages: List[Dict[str, Any]] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant_text(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})


class Provider:
    kind = "base"

    def __init__(self, api_key: str, model: str = "",
                 base_url: str = "", max_tokens: int = 1024) -> None:
        self.api_key = api_key or ""
        self.model = model or DEFAULT_MODELS.get(self.kind, "")
        self.base_url = (base_url or "").rstrip("/")
        self.max_tokens = max(128, min(8192, int(max_tokens or 1024)))
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, url: str, headers: Dict[str, str],
                    body: Dict[str, Any]) -> Dict[str, Any]:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
        try:
            async with self._session.post(url, headers=headers,
                                          json=body) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    data = None
                if response.status >= 400:
                    detail = ""
                    if isinstance(data, dict):
                        err = data.get("error")
                        detail = (err.get("message") if isinstance(err, dict)
                                  else str(err or data.get("message") or ""))
                    raise AIError(detail or f"HTTP {response.status}",
                                  response.status)
                if not isinstance(data, dict):
                    raise AIError("the AI service returned something "
                                  "unreadable", response.status)
                return data
        except AIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise AIError(f"could not reach the AI service "
                          f"({exc or type(exc).__name__})") from exc

    def conversation(self, system: str) -> Conversation:
        return Conversation(system)

    async def complete(self, convo: Conversation,
                       tools: List[Tool]) -> Turn:  # pragma: no cover
        raise NotImplementedError

    def add_turn(self, convo: Conversation, turn: Turn) -> None:
        raise NotImplementedError  # pragma: no cover

    def add_results(self, convo: Conversation,
                    results: List[tuple]) -> None:
        raise NotImplementedError  # pragma: no cover


class AnthropicProvider(Provider):
    kind = "anthropic"

    async def complete(self, convo: Conversation, tools: List[Tool]) -> Turn:
        body: Dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens,
            "system": convo.system, "messages": convo.messages,
        }
        if tools:
            body["tools"] = [{"name": t.name, "description": t.description,
                              "input_schema": t.parameters} for t in tools]
        data = await self._post(
            self.base_url + "/v1/messages" if self.base_url
            else ANTHROPIC_URL,
            {"x-api-key": self.api_key,
             "anthropic-version": ANTHROPIC_VERSION,
             "content-type": "application/json"}, body)
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks
                       if b.get("type") == "text")
        calls = [ToolCall(str(b.get("id")), str(b.get("name")),
                          b.get("input") if isinstance(b.get("input"), dict)
                          else {})
                 for b in blocks if b.get("type") == "tool_use"]
        return Turn(text=text.strip(), calls=calls, raw=blocks)

    def add_turn(self, convo: Conversation, turn: Turn) -> None:
        convo.messages.append({"role": "assistant", "content": turn.raw})

    def add_results(self, convo: Conversation, results: List[tuple]) -> None:
        convo.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id,
             "content": output} for call, output in results]})


class OpenAIProvider(Provider):
    kind = "openai"

    def conversation(self, system: str) -> Conversation:
        convo = Conversation(system)
        convo.messages.append({"role": "system", "content": system})
        return convo

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Whether this model takes reasoning_effort="none". Learned from the
        # first refusal, so an older model costs one retry per restart.
        self._effort_none: Optional[bool] = None

    @staticmethod
    def _is_official(base: str) -> bool:
        return "api.openai.com" in base

    async def complete(self, convo: Conversation, tools: List[Tool]) -> Turn:
        base = self.base_url or OPENAI_URL
        official = self._is_official(base)
        body: Dict[str, Any] = {"model": self.model,
                                "messages": convo.messages}
        # OpenAI's newer models refuse max_tokens; most compatible servers
        # (Ollama, LM Studio) only know max_tokens.
        body["max_completion_tokens" if official
             else "max_tokens"] = self.max_tokens
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters}} for t in tools]
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        # Some OpenAI reasoning models refuse function
        # tools on /chat/completions unless reasoning_effort is "none". Older
        # models don't know the setting at all, so fall back once without it
        # and remember which kind this model is.
        if official and tools and self._effort_none is not False:
            try:
                data = await self._post(base + "/chat/completions", headers,
                                        {**body, "reasoning_effort": "none"})
                self._effort_none = True
            except AIError as exc:
                if self._effort_none is None and exc.status == 400 \
                        and "reasoning" in str(exc).lower():
                    self._effort_none = False
                    data = await self._post(base + "/chat/completions",
                                            headers, body)
                else:
                    raise
        else:
            data = await self._post(base + "/chat/completions", headers, body)
        choices = data.get("choices") or []
        if not choices:
            raise AIError("the AI service returned no answer")
        message = choices[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(str(call.get("id")),
                                  str(function.get("name")),
                                  args if isinstance(args, dict) else {}))
        return Turn(text=str(message.get("content") or "").strip(),
                    calls=calls, raw=message)

    def add_turn(self, convo: Conversation, turn: Turn) -> None:
        message = {"role": "assistant", "content": turn.raw.get("content")}
        if turn.raw.get("tool_calls"):
            message["tool_calls"] = turn.raw["tool_calls"]
        convo.messages.append(message)

    def add_results(self, convo: Conversation, results: List[tuple]) -> None:
        for call, output in results:
            convo.messages.append({"role": "tool", "tool_call_id": call.id,
                                   "content": output})


def make_provider(kind: str, api_key: str, model: str = "",
                  base_url: str = "", max_tokens: int = 1024
                  ) -> Optional[Provider]:
    kind = (kind or "").strip().lower()
    if kind in ("anthropic", "claude"):
        return AnthropicProvider(api_key, model, base_url, max_tokens) \
            if api_key else None
    if kind in ("openai", "openai-compatible", "ollama", "local"):
        # Local servers usually need no key; api.openai.com does.
        if not api_key and not base_url:
            return None
        return OpenAIProvider(api_key, model, base_url, max_tokens)
    return None


async def run(provider: Provider, convo: Conversation, tools: List[Tool],
              max_rounds: int = MAX_TOOL_ROUNDS) -> str:
    """Let the model call tools until it answers. Returns the answer text.

    Tool failures go back to the model as text rather than raising, so it
    can say "Seerr is down" instead of the whole reply failing.
    """
    by_name = {t.name: t for t in tools}
    for _ in range(max_rounds):
        turn = await provider.complete(convo, tools)
        if not turn.calls:
            return turn.text
        provider.add_turn(convo, turn)
        results = []
        for call in turn.calls:
            tool = by_name.get(call.name)
            if tool is None:
                output = f"error: no tool called {call.name}"
            else:
                try:
                    value = await tool.handler(**call.arguments)
                    output = value if isinstance(value, str) else json.dumps(
                        value, default=str, ensure_ascii=False)
                except TypeError as exc:
                    output = f"error: bad arguments ({exc})"
                except Exception as exc:
                    logger.warning("AI tool %s failed: %s", call.name, exc)
                    output = f"error: {exc}"
            results.append((call, output[:MAX_TOOL_RESULT]))
        provider.add_results(convo, results)
    # Out of rounds. Asking again without tools is not safe on every API
    # (Anthropic rejects tool_use history with no tools declared), so stop.
    return ("I went around in circles looking that up — could you ask "
            "a bit more specifically?")
