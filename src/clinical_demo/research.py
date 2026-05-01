"""Lightweight source-backed research snippets for reviewer calibration.

This is a dev-assist feature, not clinical decision support. The reviewer UI
uses it to pull the kind of quick public-web context a human would otherwise
Google while hand-labeling Layer-3 judge targets.
"""

from __future__ import annotations

import json
from html import unescape
from html.parser import HTMLParser
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, Field

from clinical_demo.settings import Settings, get_settings

ReviewerLabel = Literal["correct", "incorrect", "unjudgeable"]
MatcherVerdict = Literal["pass", "fail", "indeterminate"]


class CriterionResearchRequest(BaseModel):
    """Request public-web context for one criterion text."""

    criterion_text: str = Field(min_length=1, max_length=500)
    criterion_kind: str | None = None
    matcher_verdict: str | None = None
    matcher_reason: str | None = None
    matcher_rationale: str | None = Field(default=None, max_length=1000)
    matcher_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


class ResearchSource(BaseModel):
    title: str
    url: str
    snippet: str


class CriterionResearchBlurb(BaseModel):
    query: str
    provider: str
    model: str
    gemini_prompt: str
    blurb: str
    sources: list[ResearchSource]
    gemini_error: str | None = None
    suggested_label: ReviewerLabel | None = None
    suggested_expected_matcher_verdict: MatcherVerdict | None = None
    suggested_correct_answer: str = ""


class _ResearchLLMOutput(BaseModel):
    blurb: str
    suggested_label: ReviewerLabel | None = None
    expected_matcher_verdict: MatcherVerdict | None = None
    correct_answer: str = ""


class ResearchFetchError(RuntimeError):
    """Raised when the public-web context lookup cannot produce a blurb."""


class _SearchClient(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> httpx.Response: ...


class _GeminiClient(Protocol):
    def post(
        self,
        url: str,
        *,
        params: dict[str, str],
        json: dict[str, Any],
    ) -> httpx.Response: ...


class _OpenAIClient(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> httpx.Response: ...


DUCKDUCKGO_HTML_URL = "https://duckduckgo.com/html/"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_SYSTEM_INSTRUCTION = """\
You are a realistic human clinician-reviewer calibrating an eligibility matcher.
You may use ordinary clinical conventions and common medical background
knowledge, not just literal source snippets. You are allowed to make practical
human inferences, but stay honest about uncertainty and do not claim a patient
passes or fails unless the cited matcher evidence supports that patient fact.

Return strict JSON only:
{
  "blurb": "2-4 sentences explaining the review judgment",
  "suggested_label": "correct | incorrect | unjudgeable | null",
  "expected_matcher_verdict": "pass | fail | indeterminate | null",
  "correct_answer": "short correction the reviewer could save"
}

Label semantics:
- suggested_label grades whether the matcher's actual verdict/rationale is acceptable.
- expected_matcher_verdict is what the matcher should have returned, considering both
  clinical convention and cited patient evidence.
- If clinical notation is conventional but patient evidence is absent, say so; the
  expected matcher verdict may still be indeterminate for no-data reasons.
"""


def fetch_criterion_research(
    request: CriterionResearchRequest,
    *,
    search_client: _SearchClient | None = None,
    gemini_client: _GeminiClient | None = None,
    openai_client: _OpenAIClient | None = None,
    settings: Settings | None = None,
    max_sources: int = 3,
) -> CriterionResearchBlurb:
    """Fetch a Gemini-written, source-backed blurb for a matcher verdict."""

    query = build_research_query(request)
    close_search_client = False
    if search_client is None:
        search_client = httpx.Client(timeout=8.0, follow_redirects=True)
        close_search_client = True

    try:
        response = search_client.get(
            DUCKDUCKGO_HTML_URL,
            params={"q": query},
            headers={
                "user-agent": (
                    "clinical-demo research helper "
                    "(local calibration workflow; contact: demo developer)"
                )
            },
        )
    except httpx.HTTPError as exc:
        raise ResearchFetchError(f"research lookup failed: {exc}") from exc
    finally:
        if close_search_client:
            assert isinstance(search_client, httpx.Client)
            search_client.close()

    if response.status_code >= 400:
        raise ResearchFetchError(f"research lookup returned HTTP {response.status_code}")

    sources = _parse_duckduckgo_html(response.text)[:max_sources]
    if not sources:
        raise ResearchFetchError("research lookup returned no usable sources")

    settings = settings or get_settings()
    prompt = build_gemini_research_prompt(request, sources)
    close_gemini_client = False
    close_openai_client = False
    if gemini_client is None:
        gemini_client = httpx.Client(timeout=20.0)
        close_gemini_client = True
    gemini_error = None
    provider = "gemini"
    model = settings.research_model
    try:
        llm_output = _generate_gemini_output(prompt, client=gemini_client, settings=settings)
    except ResearchFetchError as exc:
        gemini_error = str(exc)
        if openai_client is None:
            openai_client = httpx.Client(timeout=20.0)
            close_openai_client = True
        try:
            llm_output = _generate_openai_output(prompt, client=openai_client, settings=settings)
            provider = "openai"
            model = settings.research_openai_model
        except ResearchFetchError as openai_exc:
            llm_output = _fallback_output(
                request,
                sources,
                gemini_error=gemini_error,
                openai_error=str(openai_exc),
            )
    finally:
        if close_gemini_client:
            assert isinstance(gemini_client, httpx.Client)
            gemini_client.close()
        if close_openai_client:
            assert isinstance(openai_client, httpx.Client)
            openai_client.close()

    return CriterionResearchBlurb(
        query=query,
        provider=provider,
        model=model,
        gemini_prompt=prompt,
        blurb=llm_output.blurb,
        sources=sources,
        gemini_error=gemini_error,
        suggested_label=llm_output.suggested_label,
        suggested_expected_matcher_verdict=llm_output.expected_matcher_verdict,
        suggested_correct_answer=llm_output.correct_answer,
    )


def build_research_query(request: CriterionResearchRequest) -> str:
    """Build a privacy-preserving clinical-context search query."""

    text = " ".join(request.criterion_text.split())
    if len(text) > 240:
        text = text[:240].rsplit(" ", 1)[0]
    rationale = " ".join((request.matcher_rationale or "").split())
    if len(rationale) > 180:
        rationale = rationale[:180].rsplit(" ", 1)[0]
    reason = request.matcher_reason or request.criterion_kind or ""
    matcher_context = " ".join(part for part in [reason, rationale] if part)
    return f"{text} {matcher_context} clinical convention guideline".strip()


def build_gemini_research_prompt(
    request: CriterionResearchRequest,
    sources: list[ResearchSource],
) -> str:
    """Build the auditable Gemini request for the calibration UI."""

    source_lines = "\n".join(
        f"- {source.title}\n  URL: {source.url}\n  Snippet: {source.snippet}" for source in sources
    )
    evidence_json = (
        json.dumps(request.matcher_evidence, indent=2, sort_keys=True)
        if request.matcher_evidence
        else "No cited matcher evidence."
    )
    return f"""\
The reviewer is deciding whether this matcher verdict should be labeled
`correct`, `incorrect`, or `unjudgeable`.

Criterion text:
{request.criterion_text}

Criterion kind:
{request.criterion_kind or "unknown"}

Matcher verdict:
{request.matcher_verdict or "unknown"}

Matcher reason:
{request.matcher_reason or "unknown"}

Matcher rationale:
{request.matcher_rationale or "none provided"}

Matcher evidence:
{evidence_json}

Reviewer question:
Is the matcher verdict clinically justified? Focus on the specific uncertainty
named by the matcher rationale. If the issue is a missing unit, threshold,
standard interpretation, or conventional clinical notation, use normal clinical
practice the way a human reviewer would. But if pass/fail depends on patient
facts and the matcher cited no evidence, do not invent patient evidence.

Public source snippets:
{source_lines}
"""


def _generate_gemini_output(
    prompt: str,
    *,
    client: _GeminiClient,
    settings: Settings,
) -> _ResearchLLMOutput:
    if settings.google_api_key is None:
        raise ResearchFetchError("GOOGLE_API_KEY is not set; cannot call Gemini research helper")

    try:
        response = client.post(
            GEMINI_API_URL.format(model=settings.research_model),
            params={"key": settings.google_api_key.get_secret_value()},
            json={
                "systemInstruction": {"parts": [{"text": GEMINI_SYSTEM_INSTRUCTION}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.0,
                    "maxOutputTokens": settings.research_max_output_tokens,
                    "responseMimeType": "application/json",
                },
            },
        )
    except httpx.HTTPError as exc:
        raise ResearchFetchError(f"Gemini research request failed: {exc}") from exc

    if response.status_code >= 400:
        retry_after = response.headers.get("retry-after")
        retry = f"; retry after {retry_after}s" if retry_after else ""
        raise ResearchFetchError(
            f"Gemini research request returned HTTP {response.status_code}{retry}"
        )

    payload = response.json()
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ResearchFetchError("Gemini research response did not include text") from exc
    raw = " ".join(str(part.get("text", "")).strip() for part in parts if part.get("text"))
    if not raw:
        raise ResearchFetchError("Gemini research response was empty")
    return _parse_llm_output(raw, provider="Gemini")


def _generate_openai_output(
    prompt: str,
    *,
    client: _OpenAIClient,
    settings: Settings,
) -> _ResearchLLMOutput:
    if settings.openai_api_key is None:
        raise ResearchFetchError("OPENAI_API_KEY is not set; cannot call OpenAI research helper")

    try:
        response = client.post(
            OPENAI_CHAT_COMPLETIONS_URL,
            headers={
                "authorization": f"Bearer {settings.openai_api_key.get_secret_value()}",
                "content-type": "application/json",
            },
            json={
                "model": settings.research_openai_model,
                "messages": [
                    {"role": "system", "content": GEMINI_SYSTEM_INSTRUCTION},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
                "max_completion_tokens": settings.research_max_output_tokens,
                "response_format": {"type": "json_object"},
            },
        )
    except httpx.HTTPError as exc:
        raise ResearchFetchError(f"OpenAI research request failed: {exc}") from exc

    if response.status_code >= 400:
        raise ResearchFetchError(f"OpenAI research request returned HTTP {response.status_code}")

    payload = response.json()
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ResearchFetchError("OpenAI research response did not include text") from exc
    if not isinstance(text, str) or not text.strip():
        raise ResearchFetchError("OpenAI research response was empty")
    return _parse_llm_output(text, provider="OpenAI")


def _fallback_output(
    request: CriterionResearchRequest,
    sources: list[ResearchSource],
    *,
    gemini_error: str,
    openai_error: str | None = None,
) -> _ResearchLLMOutput:
    snippets = " ".join(source.snippet for source in sources if source.snippet)
    if not snippets:
        snippets = " ".join(source.title for source in sources)
    blurb = (
        "LLM research helpers could not answer this request "
        f"(Gemini: {gemini_error}"
        f"{'; OpenAI: ' + openai_error if openai_error else ''}). "
        f"Source snippets for matcher-verdict review: {snippets} "
        "Use the linked sources and the shown Gemini request to decide whether the matcher "
        f"rationale for `{request.criterion_text}` is justified."
    )
    return _ResearchLLMOutput(blurb=blurb)


def _parse_llm_output(raw: str, *, provider: str) -> _ResearchLLMOutput:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResearchFetchError(f"{provider} research response was not JSON") from exc
    return _ResearchLLMOutput.model_validate(payload)


def _parse_duckduckgo_html(html: str) -> list[ResearchSource]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.sources


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[ResearchSource] = []
        self._pending_title = ""
        self._pending_url = ""
        self._pending_snippet = ""
        self._capturing: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value or "" for name, value in attrs}
        classes = set(attr.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._capturing = "title"
            self._buffer = []
            self._pending_url = _normalize_result_url(attr.get("href", ""))
        elif "result__snippet" in classes:
            self._capturing = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturing is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capturing == "title" and tag == "a":
            self._pending_title = _clean_text("".join(self._buffer))
            self._capturing = None
            self._buffer = []
        elif self._capturing == "snippet":
            self._pending_snippet = _clean_text("".join(self._buffer))
            self._capturing = None
            self._buffer = []
            self._finalize_pending()

    def _finalize_pending(self) -> None:
        if not self._pending_title or not self._pending_url:
            return
        self.sources.append(
            ResearchSource(
                title=self._pending_title,
                url=self._pending_url,
                snippet=self._pending_snippet,
            )
        )
        self._pending_title = ""
        self._pending_url = ""
        self._pending_snippet = ""


def _clean_text(value: str) -> str:
    return " ".join(unescape(value).split())


def _normalize_result_url(value: str) -> str:
    if value.startswith("//"):
        value = f"https:{value}"
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    if query.get("uddg"):
        return unquote(query["uddg"][0])
    return value


__all__ = [
    "CriterionResearchBlurb",
    "CriterionResearchRequest",
    "ResearchFetchError",
    "ResearchSource",
    "build_gemini_research_prompt",
    "build_research_query",
    "fetch_criterion_research",
]
