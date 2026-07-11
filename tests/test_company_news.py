from __future__ import annotations

import json
from pathlib import Path

import pytest

from outreach.company_news import (
    CompanyNewsEntry,
    CompanyNewsSource,
    capture_company_news,
    company_news_signal_id,
    company_signals_from_news_entries,
    extract_company_from_headline,
    load_company_news_signals,
    parse_company_news_feed,
    structured_company_signals_from_path,
)


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Startup News</title>
    <item>
      <title>Orbit AI raises $25M to automate finance workflows</title>
      <link>https://news.example/orbit-ai-round</link>
      <description><![CDATA[The San Francisco startup is hiring product operators.]]></description>
      <pubDate>Fri, 10 Jul 2026 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Why seed rounds are getting harder</title>
      <link>https://news.example/seed-analysis</link>
    </item>
  </channel>
</rss>
"""


ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Company launches</title>
  <entry>
    <title>Relay launches an AI operations platform</title>
    <link rel="alternate" href="https://news.example/relay-launch" />
    <summary>Relay is based in Los Angeles.</summary>
    <updated>2026-07-10T11:00:00Z</updated>
  </entry>
</feed>
"""


SOURCE = CompanyNewsSource(
    source_id="fixture_news",
    label="Fixture News",
    feed_url="https://news.example/feed",
    source_type="funding_news",
)


def test_rss_and_atom_adapters_preserve_entry_provenance() -> None:
    rss_entries = parse_company_news_feed(RSS)
    atom_entries = parse_company_news_feed(ATOM)

    assert [item.title for item in rss_entries] == [
        "Orbit AI raises $25M to automate finance workflows",
        "Why seed rounds are getting harder",
    ]
    assert rss_entries[0].summary == "The San Francisco startup is hiring product operators."
    assert atom_entries == [
        CompanyNewsEntry(
            title="Relay launches an AI operations platform",
            url="https://news.example/relay-launch",
            summary="Relay is based in Los Angeles.",
            published_at="2026-07-10T11:00:00Z",
        )
    ]


def test_news_entries_only_emit_explicit_company_headlines_and_stay_review_gated() -> None:
    signals = company_signals_from_news_entries(
        parse_company_news_feed(RSS),
        source=SOURCE,
        run_id="nightly-2026-07-10",
        observed_at="2026-07-10T12:00:00+00:00",
    )

    assert [item.company_name for item in signals] == ["Orbit AI"]
    signal = signals[0]
    assert signal.provenance[0].source_type == "funding_news"
    assert signal.provenance[0].source_url == "https://news.example/orbit-ai-round"
    assert signal.provenance[0].signal_type == "funding"
    assert signal.rubric.geography_remote.score == 3
    assert signal.rubric.role_surface.score == 2


def test_headline_extraction_is_conservative() -> None:
    assert extract_company_from_headline("Ollama raises $65M for its AI developer tool") == "Ollama"
    assert extract_company_from_headline("AI chip maker SambaNova raises $1B") == "SambaNova"
    assert extract_company_from_headline("Hot French startup ZML releases an inference tool") == "ZML"
    assert extract_company_from_headline("Savi’s app aims to stop AI scams") == "Savi"
    assert extract_company_from_headline("Why raising your first fund is harder") == ""
    assert extract_company_from_headline("These AI startups are growing quickly") == ""


def test_capture_upserts_a_durable_ledger_and_reports_source_failures(tmp_path: Path) -> None:
    ledger = tmp_path / "company_news_signals.json"
    feeds = {
        "https://news.example/feed": RSS,
        "https://broken.example/feed": RuntimeError("source unavailable"),
    }

    def fetch(url: str) -> str:
        value = feeds[url]
        if isinstance(value, Exception):
            raise value
        return value

    # Override the registry-facing lookup by using the fixture source through the
    # default URL is exercised separately; here a one-source capture checks ledger semantics.
    import outreach.company_news as module

    original = module.DEFAULT_COMPANY_NEWS_SOURCES
    module.DEFAULT_COMPANY_NEWS_SOURCES = (
        SOURCE,
        CompanyNewsSource(
            source_id="broken",
            label="Broken feed",
            feed_url="https://broken.example/feed",
        ),
    )
    try:
        first = capture_company_news(
            run_id="run-1",
            ledger_path=ledger,
            fetch_text=fetch,
            known_companies=(),
            observed_at="2026-07-10T12:00:00+00:00",
        )
        second = capture_company_news(
            run_id="run-2",
            ledger_path=ledger,
            fetch_text=fetch,
            known_companies=(),
            observed_at="2026-07-11T12:00:00+00:00",
        )
    finally:
        module.DEFAULT_COMPANY_NEWS_SOURCES = original

    assert first.status == "partial"
    assert len(first.captured_signal_ids) == 1
    assert first.added_signal_ids == first.captured_signal_ids
    assert second.added_signal_ids == []
    assert [item.company_name for item in load_company_news_signals(ledger)] == ["Orbit AI"]
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["signals"][0]["seen_run_ids"] == ["run-1", "run-2"]
    assert payload["signals"][0]["signal_id"] == company_news_signal_id(second.signals[0])


def test_structured_csv_adapter_uses_same_canonical_signal_contract(tmp_path: Path) -> None:
    source = tmp_path / "accelerator_export.csv"
    source.write_text(
        "company_name,website,headline,source_name,source_type,source_url,"
        "domain_fit_score,domain_fit_evidence\n"
        "Vector Labs,https://vector.example,Vector Labs is hiring product strategy,"
        "LA Accelerator,accelerator_directory,https://accelerator.example/vector,3,AI platform fit\n",
        encoding="utf-8",
    )

    signals = structured_company_signals_from_path(source, run_id="manual-import-1")

    assert len(signals) == 1
    assert signals[0].company_name == "Vector Labs"
    assert signals[0].website == "https://vector.example"
    assert signals[0].rubric.domain_fit.score == 3
    assert signals[0].rubric.domain_fit.evidence == "AI platform fit"
    assert signals[0].provenance[0].source_type == "accelerator_directory"


def test_capture_fails_closed_without_replacing_a_corrupt_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "company_news_signals.json"
    corrupt_payload = "{not valid json\n"
    ledger.write_text(corrupt_payload, encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid JSON in company-news ledger"):
        capture_company_news(
            run_id="run-corrupt-ledger",
            ledger_path=ledger,
            fetch_text=lambda _url: RSS,
            source_ids=("techcrunch_startups",),
            observed_at="2026-07-11T12:00:00+00:00",
        )

    assert ledger.read_text(encoding="utf-8") == corrupt_payload
    assert list(tmp_path.glob(".company_news_signals.json.*.tmp")) == []
