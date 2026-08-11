from __future__ import annotations

import backfill_ollama
from botocore.exceptions import ClientError


def _filings(accessions, forms, reports, filed):
    return {
        "accessionNumber": accessions,
        "form": forms,
        "reportDate": reports,
        "filingDate": filed,
    }


def test_historical_submissions_are_range_bounded_and_deduplicated(monkeypatch):
    recent = _filings(
        ["0000000001-21-000001", "0000000001-22-000001"],
        ["10-K", "10-K"],
        ["2020-12-31", "2021-12-31"],
        ["2021-02-01", "2022-02-01"],
    )
    history = _filings(
        ["0000000001-10-000001", "0000000001-21-000001"],
        ["10-K", "10-K"],
        ["2009-12-31", "2020-12-31"],
        ["2010-02-01", "2021-02-01"],
    )
    payloads = {
        "https://data.sec.gov/submissions/CIK0000000001.json": {
            "filings": {
                "recent": recent,
                "files": [{
                    "name": "CIK0000000001-submissions-001.json",
                    "filingFrom": "2010-01-01",
                    "filingTo": "2021-02-01",
                }],
            }
        },
        "https://data.sec.gov/submissions/CIK0000000001-submissions-001.json": history,
    }
    monkeypatch.setattr(backfill_ollama, "_get_json", payloads.__getitem__)

    result = backfill_ollama._latest_filings(
        "1", ("10-K",), start_date="2010-01-01", before_date="2021-08-01"
    )

    assert [row["accession_number"] for row in result] == [
        "0000000001-21-000001",
        "0000000001-10-000001",
    ]


def test_irrelevant_historical_submission_files_are_not_fetched(monkeypatch):
    recent_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    payload = {
        "filings": {
            "recent": _filings([], [], [], []),
            "files": [{
                "name": "old.json",
                "filingFrom": "2000-01-01",
                "filingTo": "2009-12-31",
            }],
        }
    }
    calls = []

    def fake_get_json(url):
        calls.append(url)
        return payload

    monkeypatch.setattr(backfill_ollama, "_get_json", fake_get_json)
    assert backfill_ollama._latest_filings(
        "1", ("10-K",), start_date="2010-01-01", before_date="2021-08-01"
    ) == []
    assert calls == [recent_url]


def test_pit_archive_exists_distinguishes_missing_object(monkeypatch):
    class FakeS3:
        def head_object(self, **kwargs):
            if kwargs["Key"].endswith("missing.xml"):
                raise ClientError(
                    {"Error": {"Code": "404", "Message": "missing"}},
                    "HeadObject",
                )
            return {}

    monkeypatch.setattr(backfill_ollama, "s3", FakeS3())

    assert backfill_ollama._pit_archive_exists("annual", "0001", "present")
    assert not backfill_ollama._pit_archive_exists("annual", "0001", "missing")
