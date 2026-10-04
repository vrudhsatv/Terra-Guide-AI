"""Stage 2: organizational-policy review with an LLM (Anthropic, OpenAI or Gemini)."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import requests

from config import Config
from llm.prompts import REVIEW_SCHEMA, SYSTEM_PROMPT, build_review_prompt
from llm.rules import RuleSet
from llm.validator import FindingValidator
from models import Finding, LLMReview
from processor.mapper import DiffMap

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse the model's JSON answer, tolerating code fences or stray prose around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"model response is not JSON: {text[:300]!r}") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"model response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("model response JSON is not an object")
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        raise LLMError("'findings' in model response is not a list")
    return {"summary": str(data.get("summary") or "").strip(), "findings": findings}


# --------------------------------------------------------------------------- providers


class Provider(ABC):
    name: str
    native_schema: bool  # True when the provider enforces REVIEW_SCHEMA itself

    def __init__(self, config: Config):
        self.config = config
        self.model = config.llm_model

    @abstractmethod
    def complete(self, system: str, prompt: str) -> str:
        """Return the raw text of the model's answer."""


class AnthropicProvider(Provider):
    name = "anthropic"
    native_schema = True

    # Models that accept the server-side refusal fallback (`fallbacks: "default"`).
    _FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")

    def __init__(self, config: Config):
        super().__init__(config)
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
            raise LLMError("the 'anthropic' package is not installed") from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=config.llm_api_key, timeout=config.llm_timeout_s, max_retries=3)

    def complete(self, system: str, prompt: str) -> str:
        anthropic = self._anthropic
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}}
        if "haiku" not in self.model:
            output_config["effort"] = self.config.llm_effort
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.config.llm_max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        use_fallback = self.model in self._FALLBACK_MODELS
        try:
            # Streaming keeps long reviews (large max_tokens) clear of HTTP timeouts.
            if use_fallback:
                with self.client.beta.messages.stream(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                ) as stream:
                    message = stream.get_final_message()
            else:
                with self.client.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Anthropic model '{self.model}' not found: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Anthropic rate limit exceeded after retries: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMError(f"Anthropic declined the request ({getattr(details, 'category', None) or 'refusal'})")
        if message.stop_reason == "max_tokens":
            raise LLMError("Anthropic response hit LLM_MAX_TOKENS before finishing; raise LLM_MAX_TOKENS")
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text:
            raise LLMError("Anthropic response contained no text")
        return text


class OpenAIProvider(Provider):
    name = "openai"
    native_schema = True

    def __init__(self, config: Config):
        super().__init__(config)
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise LLMError("the 'openai' package is not installed") from exc
        self._openai = openai
        self.client = openai.OpenAI(api_key=config.llm_api_key, timeout=config.llm_timeout_s, max_retries=3)

    def complete(self, system: str, prompt: str) -> str:
        openai = self._openai
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_completion_tokens=self.config.llm_max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "terraform_policy_review", "schema": REVIEW_SCHEMA, "strict": True},
                },
            )
        except openai.RateLimitError as exc:
            raise LLMError(f"OpenAI rate limit exceeded after retries: {exc}") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"OpenAI API error {exc.status_code}: {exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"Could not reach the OpenAI API: {exc}") from exc
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LLMError(f"OpenAI declined the request: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise LLMError("OpenAI response hit LLM_MAX_TOKENS before finishing; raise LLM_MAX_TOKENS")
        return choice.message.content or ""


class GeminiProvider(Provider):
    name = "gemini"
    native_schema = False  # JSON mode only; the schema is embedded in the prompt

    _URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def complete(self, system: str, prompt: str) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": self.config.llm_max_tokens,
            },
        }
        last_error = ""
        for attempt in range(3):
            try:
                resp = requests.post(
                    self._URL.format(model=self.model),
                    headers={"x-goog-api-key": self.config.llm_api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.config.llm_timeout_s,
                )
            except requests.RequestException as exc:
                last_error = f"Could not reach the Gemini API: {exc}"
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"Gemini API error {resp.status_code}: {resp.text[:300]}"
                log.warning("%s (attempt %d/3)", last_error, attempt + 1)
                continue
            if resp.status_code != 200:
                raise LLMError(f"Gemini API error {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates returned")
                raise LLMError(f"Gemini returned no answer: {reason}")
            candidate = candidates[0]
            if candidate.get("finishReason") == "MAX_TOKENS":
                raise LLMError("Gemini response hit LLM_MAX_TOKENS before finishing; raise LLM_MAX_TOKENS")
            parts = (candidate.get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        raise LLMError(last_error or "Gemini request failed")


PROVIDERS: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


# --------------------------------------------------------------------------- engine


class PolicyReviewEngine:
    def __init__(self, config: Config, rules: RuleSet, provider: Provider | None = None):
        self.config = config
        self.rules = rules
        self._provider = provider

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            if not self.config.llm_api_key and self.config.llm_provider in PROVIDERS:
                raise LLMError(f"no API key configured for provider '{self.config.llm_provider}'")
            self._provider = PROVIDERS[self.config.llm_provider](self.config)
        return self._provider

    def review(self, diff_map: DiffMap, deterministic: list[Finding]) -> LLMReview:
        review = LLMReview(provider=self.config.llm_provider, model=self.config.llm_model)
        if not diff_map.files:
            review.skipped_reason = "no Terraform changes in this PR"
            return review

        validator = FindingValidator(diff_map, self.rules, self.config.workspace, self.config.llm_line_tolerance)
        chunks = diff_map.chunk_hunks(self.config.llm_max_diff_chars)
        summaries: list[str] = []
        errors: list[str] = []
        for index, chunk in enumerate(chunks):
            files_in_chunk = {path for path, _ in chunk}
            related = [f for f in deterministic if not f.file or f.file in files_in_chunk]
            prompt = build_review_prompt(
                self.rules.text,
                diff_map.render_hunks(chunk),
                related,
                chunk_index=index,
                chunk_count=len(chunks),
                include_schema=not self.provider.native_schema,
            )
            log.info(
                "LLM review chunk %d/%d (%d files, %d chars) via %s:%s",
                index + 1, len(chunks), len(files_in_chunk), len(prompt), self.provider.name, self.provider.model,
            )
            try:
                raw = self.provider.complete(SYSTEM_PROMPT, prompt)
                parsed = parse_json_response(raw)
            except LLMError as exc:
                log.error("LLM chunk %d failed: %s", index + 1, exc)
                errors.append(str(exc))
                continue
            if parsed["summary"]:
                summaries.append(parsed["summary"])
            accepted, rejected = validator.validate_all(parsed["findings"])
            review.accepted.extend(accepted)
            review.rejected.extend(rejected)

        review.summary = "\n\n".join(summaries)
        review.error = "; ".join(dict.fromkeys(errors))
        for item in review.rejected:
            log.info("Rejected LLM finding (%s): %s", item["reason"], json.dumps(item["finding"])[:300])
        log.info("LLM stage: %d accepted, %d rejected", len(review.accepted), len(review.rejected))
        return review
