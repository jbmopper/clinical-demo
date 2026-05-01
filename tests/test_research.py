"""Tests for public-web research snippets used by calibration UI."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from clinical_demo.research import (
    CriterionResearchRequest,
    build_gemini_research_prompt,
    build_research_query,
    fetch_criterion_research,
)
from clinical_demo.settings import Settings


def test_build_research_query_keeps_context_clinical_and_concise() -> None:
    request = CriterionResearchRequest(
        criterion_text="  eGFR   < 25 mL/min/1.73m2  ",
        criterion_kind="measurement",
        matcher_reason="ambiguous_criterion",
        matcher_rationale="Threshold for eGFR has no unit.",
    )

    assert build_research_query(request) == (
        "eGFR < 25 mL/min/1.73m2 ambiguous_criterion Threshold for eGFR has no unit. "
        "clinical convention guideline"
    )


def test_build_gemini_prompt_centers_matcher_verdict_question() -> None:
    prompt = build_gemini_research_prompt(
        CriterionResearchRequest(
            criterion_text="eGFR < 25",
            criterion_kind="measurement_threshold",
            matcher_verdict="indeterminate",
            matcher_reason="ambiguous_criterion",
            matcher_rationale=(
                "Threshold for 'eGFR' has no unit; cannot safely compare against patient lab values."
            ),
        ),
        [
            _source_response(
                title="Estimated Glomerular Filtration Rate",
                url="https://www.kidney.org/egfr",
                snippet="eGFR is reported in mL/min/1.73 m2.",
            )
        ],
    )

    assert "Matcher verdict:\nindeterminate" in prompt
    assert "Threshold for 'eGFR' has no unit" in prompt
    assert "No cited matcher evidence" in prompt
    assert "do not invent patient evidence" in prompt
    assert "eGFR is reported in mL/min/1.73 m2" in prompt


def test_fetch_criterion_research_parses_search_results_and_calls_gemini() -> None:
    html = """
    <html>
      <body>
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.kidney.org%2Fegfr">
          Estimated Glomerular Filtration Rate
        </a>
        <div class="result__snippet">
          eGFR below 30 can indicate severe chronic kidney disease.
        </div>
      </body>
    </html>
    """
    search_client = _StubSearchClient(httpx.Response(200, text=html))
    gemini_client = _StubGeminiClient(
        httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"blurb":"eGFR is conventionally reported as '
                                        "mL/min/1.73 m2, so the missing unit is usually "
                                        'inferable in clinical context.","suggested_label":'
                                        '"incorrect","expected_matcher_verdict":"indeterminate",'
                                        '"correct_answer":"The unit is inferable, but no cited '
                                        "patient evidence means the matcher should remain "
                                        'indeterminate for no-data reasons."}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )

    result = fetch_criterion_research(
        CriterionResearchRequest(
            criterion_text="eGFR < 25",
            criterion_kind="measurement",
            matcher_verdict="indeterminate",
            matcher_reason="ambiguous_criterion",
            matcher_rationale="Threshold for eGFR has no unit.",
        ),
        search_client=search_client,
        gemini_client=gemini_client,
        settings=Settings(google_api_key=SecretStr("google-test-key")),
    )

    assert search_client.params["q"] == (
        "eGFR < 25 ambiguous_criterion Threshold for eGFR has no unit. "
        "clinical convention guideline"
    )
    assert gemini_client.params["key"] == "google-test-key"
    assert "Threshold for eGFR has no unit" in gemini_client.prompt
    assert result.sources[0].title == "Estimated Glomerular Filtration Rate"
    assert result.sources[0].url == "https://www.kidney.org/egfr"
    assert "mL/min/1.73 m2" in result.blurb
    assert result.provider == "gemini"
    assert result.model == "gemini-3-flash-preview"
    assert "Matcher verdict:" in result.gemini_prompt
    assert result.gemini_error is None
    assert result.suggested_label == "incorrect"
    assert result.suggested_expected_matcher_verdict == "indeterminate"
    assert "no-data" in result.suggested_correct_answer


def test_fetch_criterion_research_falls_back_to_openai_when_gemini_is_rate_limited() -> None:
    html = """
    <html>
      <body>
        <a class="result__a" href="https://example.org/egfr">eGFR overview</a>
        <div class="result__snippet">eGFR is commonly reported in mL/min/1.73 m2.</div>
      </body>
    </html>
    """
    result = fetch_criterion_research(
        CriterionResearchRequest(
            criterion_text="eGFR < 25",
            matcher_verdict="indeterminate",
            matcher_reason="ambiguous_criterion",
            matcher_rationale="Threshold for eGFR has no unit.",
        ),
        search_client=_StubSearchClient(httpx.Response(200, text=html)),
        gemini_client=_StubGeminiClient(
            httpx.Response(429, headers={"retry-after": "30"}, json={"error": "quota"})
        ),
        openai_client=_StubOpenAIClient(
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"blurb":"OpenAI fallback: eGFR is usually reported in '
                                    'mL/min/1.73 m2.","suggested_label":"incorrect",'
                                    '"expected_matcher_verdict":"indeterminate",'
                                    '"correct_answer":"Infer the conventional unit, then evaluate '
                                    'against available patient evidence."}'
                                )
                            }
                        }
                    ]
                },
            )
        ),
        settings=Settings(
            google_api_key=SecretStr("google-test-key"),
            openai_api_key=SecretStr("openai-test-key"),
        ),
    )

    assert result.gemini_error == "Gemini research request returned HTTP 429; retry after 30s"
    assert result.provider == "openai"
    assert result.model == "gpt-5.4-mini"
    assert "OpenAI fallback" in result.blurb
    assert "mL/min/1.73 m2" in result.blurb
    assert result.suggested_label == "incorrect"
    assert result.suggested_expected_matcher_verdict == "indeterminate"


def test_fetch_criterion_research_uses_source_fallback_when_all_llms_fail() -> None:
    html = """
    <html>
      <body>
        <a class="result__a" href="https://example.org/egfr">eGFR overview</a>
        <div class="result__snippet">eGFR is commonly reported in mL/min/1.73 m2.</div>
      </body>
    </html>
    """

    result = fetch_criterion_research(
        CriterionResearchRequest(
            criterion_text="eGFR < 25",
            matcher_verdict="indeterminate",
            matcher_reason="ambiguous_criterion",
            matcher_rationale="Threshold for eGFR has no unit.",
        ),
        search_client=_StubSearchClient(httpx.Response(200, text=html)),
        gemini_client=_StubGeminiClient(
            httpx.Response(429, headers={"retry-after": "30"}, json={"error": "quota"})
        ),
        openai_client=_StubOpenAIClient(httpx.Response(429, json={"error": "quota"})),
        settings=Settings(
            google_api_key=SecretStr("google-test-key"),
            openai_api_key=SecretStr("openai-test-key"),
        ),
    )

    assert result.provider == "gemini"
    assert result.gemini_error == "Gemini research request returned HTTP 429; retry after 30s"
    assert "OpenAI: OpenAI research request returned HTTP 429" in result.blurb
    assert "Source snippets for matcher-verdict review" in result.blurb


def _source_response(*, title: str, url: str, snippet: str):
    from clinical_demo.research import ResearchSource

    return ResearchSource(title=title, url=url, snippet=snippet)


class _StubSearchClient:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.params: dict[str, str] = {}

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> httpx.Response:
        self.params = params
        return self._response


class _StubGeminiClient:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.params: dict[str, str] = {}
        self.prompt = ""

    def post(
        self,
        url: str,
        *,
        params: dict[str, str],
        json: dict,
    ) -> httpx.Response:
        self.params = params
        self.prompt = json["contents"][0]["parts"][0]["text"]
        return self._response


class _StubOpenAIClient:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.headers: dict[str, str] = {}
        self.prompt = ""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict,
    ) -> httpx.Response:
        self.headers = headers
        self.prompt = json["messages"][1]["content"]
        assert "max_completion_tokens" in json
        assert "max_tokens" not in json
        return self._response
