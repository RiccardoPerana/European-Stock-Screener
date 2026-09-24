"""
industry_worksheet.classify_via_gemini -- the network call is exercised
only through a monkeypatched urllib.request.urlopen, never a live
request. Response shape and error mapping are what matter here; the
parsing of whatever text comes back is ingest()'s job, already exercised
through the manual copy/paste path.
"""

import io
import json
import urllib.error
import urllib.request

import pytest

import industry_worksheet as iw


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _rows():
    return [{"lei": "L1", "ticker": "ABC", "name": "Example AB",
             "country": "SE", "revenue_m": 100.0}]


def test_classify_via_gemini_returns_the_reply_text(monkeypatch):
    def fake_urlopen(req, timeout=None):
        assert "key=secret" in req.full_url
        assert req.full_url.startswith(iw.GEMINI_URL)
        body = json.loads(req.data.decode("utf-8"))
        assert body["generationConfig"]["temperature"] == 0
        assert "ABC" in body["contents"][0]["parts"][0]["text"]
        return _FakeResponse({"candidates": [
            {"content": {"parts": [{"text": "ABC | Auto Parts\n"}]}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    text = iw.classify_via_gemini(_rows(), {"Auto Parts"}, "secret")
    assert text.strip() == "ABC | Auto Parts"


def test_classify_via_gemini_rate_limit_is_a_friendly_message(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {},
            io.BytesIO(b"quota exceeded"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(iw.GeminiError, match="rate limit"):
        iw.classify_via_gemini(_rows(), {"Auto Parts"}, "secret")


def test_classify_via_gemini_bad_key_names_the_status_code(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(b"API key not valid"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(iw.GeminiError, match="400"):
        iw.classify_via_gemini(_rows(), {"Auto Parts"}, "bad-key")


def test_classify_via_gemini_missing_candidates_reports_block_reason(
        monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"promptFeedback": {"blockReason": "SAFETY"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(iw.GeminiError, match="SAFETY"):
        iw.classify_via_gemini(_rows(), {"Auto Parts"}, "secret")


def test_classify_via_gemini_empty_text_is_an_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"candidates": [
            {"content": {"parts": [{"text": "   "}]}}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(iw.GeminiError, match="empty"):
        iw.classify_via_gemini(_rows(), {"Auto Parts"}, "secret")
