# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Documentation fetch limits and on-page row check. Offline."""
import os
import re
import sys

# Same test region as test_pipeline.py: modules read it at import time.
os.environ.setdefault("AWS_REGION", "eu-central-1")
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, strategies as st

import data_extractor as de
from page_check import keep_items_on_page

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from audit_service_configs import identifier_hit_ratio  # noqa: E402

PAGE = (b"<html><body><div class='table-contents'><table>"
        b"<tr><th>Name</th><th>Identifier</th><th>Deprecation date</th></tr>"
        b"<tr><td>Python 3.8</td><td>python3.8</td><td>Oct 14, 2024</td></tr>"
        b"<tr><td>Node.js 16</td><td>nodejs16.x</td><td>Jun 12, 2024</td></tr>"
        b"</table></div></body></html>")


class FakeResponse:
    def __init__(self, status=200, body=b"", location=None, chunk=64 * 1024):
        self.status_code, self._body, self._chunk = status, body, chunk
        self.headers = {"Location": location} if location else {}
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i:i + self._chunk]

    def close(self):
        self.closed = True


@pytest.fixture
def extractor():
    with patch.object(de.boto3, "client"):
        ex = de.DataExtractor()
    ex.session = MagicMock()
    return ex


# ---------------------------------------------------------------------------
# Documentation fetch limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html#runtimes-deprecated",
    "https://DOCS.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html",
    "https://docs.aws.amazon.com:443/x.html",
])
def test_documentation_urls_are_allowed(url):
    assert de.check_documentation_url(url) == url


@pytest.mark.parametrize("url", [
    "http://docs.aws.amazon.com/lambda/", "https://example.com/", "https://docs.aws.amazon.com.example.com/",
    "https://evil.example/docs.aws.amazon.com/", "https://user:pw@docs.aws.amazon.com/",
    "https://docs.aws.amazon.com:8443/", "file:///etc/passwd", "ftp://docs.aws.amazon.com/", "https://10.0.0.1/",
    "https://[::1]/", "not a url", "",
])
def test_other_urls_are_refused(url):
    with pytest.raises(de.DocumentationFetchError):
        de.check_documentation_url(url)


def test_every_configured_url_is_allowed():
    import json
    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "service_configs.json")
    for name, cfg in json.load(open(path))["services"].items():
        for url in cfg["documentation_urls"]:
            assert de.check_documentation_url(url) == url, name


def test_fetch_parses_tables_and_returns_their_text(extractor):
    extractor.session.get.return_value = FakeResponse(body=PAGE)
    out = extractor._fetch_html_tables("https://docs.aws.amazon.com/lambda/x.html")
    assert out["status"] == "success" and out["tables_found"] == 1
    assert "python3.8" in out["table_text"] and "Deprecation date" in out["table_text"]
    kwargs = extractor.session.get.call_args.kwargs
    assert kwargs == {"timeout": 30, "allow_redirects": False, "stream": True}


def test_redirects_within_the_documentation_are_followed(extractor):
    extractor.session.get.side_effect = [
        FakeResponse(301, location="/lambda/latest/dg/new.html"),
        FakeResponse(302, location="https://docs.aws.amazon.com/lambda/final.html"),
        FakeResponse(body=PAGE),
    ]
    out = extractor._fetch_html_tables("https://docs.aws.amazon.com/lambda/old.html")
    assert out["status"] == "success"
    assert [c.args[0] for c in extractor.session.get.call_args_list] == [
        "https://docs.aws.amazon.com/lambda/old.html",
        "https://docs.aws.amazon.com/lambda/latest/dg/new.html",
        "https://docs.aws.amazon.com/lambda/final.html",
    ]


@pytest.mark.parametrize("location", ["https://example.com/", "http://docs.aws.amazon.com/x", "//evil.example/x"])
def test_redirects_away_from_the_documentation_are_refused(extractor, location):
    first = FakeResponse(302, location=location)
    extractor.session.get.side_effect = [first]
    out = extractor._fetch_html_tables("https://docs.aws.amazon.com/lambda/x.html")
    assert out["status"] == "error" and "Refused URL" in out["error"]
    assert extractor.session.get.call_count == 1 and first.closed


def test_redirect_loops_stop(extractor):
    extractor.session.get.side_effect = lambda url, **kw: FakeResponse(302, location="/loop.html")
    out = extractor._fetch_html_tables("https://docs.aws.amazon.com/start.html")
    assert out["status"] == "error" and "redirects" in out["error"]
    assert extractor.session.get.call_count == de.MAX_REDIRECTS + 1


def test_oversized_pages_are_refused(extractor):
    extractor.session.get.return_value = FakeResponse(body=b"x" * (de.MAX_PAGE_BYTES + 1))
    out = extractor._fetch_html_tables("https://docs.aws.amazon.com/big.html")
    assert out["status"] == "error" and "larger than" in out["error"]


def test_refused_urls_are_never_requested(extractor):
    out = extractor._fetch_html_tables("https://attacker.example/page")
    assert out["status"] == "error"
    extractor.session.get.assert_not_called()


# ---------------------------------------------------------------------------
# The model request is unchanged
# ---------------------------------------------------------------------------

CONFIG = {
    "name": "AWS Lambda", "service_name": "lambda", "documentation_urls": ["https://docs.aws.amazon.com/lambda/x.html"],
    "extraction_focus": "Extract every row.", "schema_key": "runtimes",
    "item_properties": {"name": "Runtime name", "identifier": "Runtime identifier", "deprecation_date": "Date"},
    "required_fields": ["name", "identifier"],
}


def test_the_bedrock_request_is_the_one_the_extraction_was_tuned_with(extractor):
    """No system prompt: one tested in the Lambda changed which rows the model returned."""
    extractor.bedrock.converse.return_value = {"output": {"message": {"content": [{"text": '{"items": []}'}]}}}
    tables = [{"type": "table-contents-div", "data": {"headers": ["Name"], "rows": [{"Name": "x"}], "raw_rows": [["x"]]}}]
    extractor._llm_extract_deprecation_data(tables, CONFIG)
    kwargs = extractor.bedrock.converse.call_args.kwargs
    assert set(kwargs) == {"modelId", "messages", "inferenceConfig"}
    user = kwargs["messages"][0]["content"][0]["text"]
    assert user.startswith("Extract AWS Lambda information from this table data.\n\nExtract every row.\n\nTABLE DATA:")
    assert kwargs["inferenceConfig"] == {"maxTokens": 16000, "temperature": 0.1, "topP": 0.9}


# ---------------------------------------------------------------------------
# On-page row check
# ---------------------------------------------------------------------------

def test_rows_not_in_the_tables_are_dropped(extractor):
    extractor.session.get.return_value = FakeResponse(body=PAGE)
    model_rows = ('{"items": ['
                  '{"name": "Python 3.8", "identifier": "python3.8", "deprecation_date": "2024-10-14"},'
                  '{"name": "Node.js 16", "identifier": "nodejs16.x", "deprecation_date": "2024-06-12"},'
                  '{"name": "Ruby 1.9", "identifier": "ruby1.9", "deprecation_date": "2029-06-30"},'
                  '"not an object"]}')
    extractor.bedrock.converse.return_value = {"output": {"message": {"content": [{"text": model_rows}]}}}
    with patch.object(de, "get_service_config", return_value=dict(CONFIG)):
        out = extractor.extract_service_data("lambda")
    assert [i["identifier"] for i in out["items"]] == ["python3.8", "nodejs16.x"]
    assert out["processing_results"][0]["items_not_on_page"] == 2
    assert all(i["source_url"] == CONFIG["documentation_urls"][0] for i in out["items"])


@pytest.mark.parametrize("item,kept", [
    ({"identifier": "python3.8"}, True),
    ({"name": "Python 3.8 (deprecated)"}, True),           # suffix dropped
    ({"identifier": "lambda-nodejs16.x"}, True),           # leading word- prefix tried without it
    ({"identifier": "x", "version": "Node.js 16"}, True),  # any identifying field
    ({"identifier": "ruby1.9", "name": "Ruby 1.9"}, False),
    ({"identifier": "py"}, False),                         # short values must be a whole word
    ({"engine": "Aurora MySQL", "version": "3"}, True),     # major version, a whole word of the text
    ({"engine": "Aurora MySQL", "version": "8"}, False),    # not on the page
    ({"deprecation_date": "2024-10-14"}, False),           # nothing identifying
])
def test_matching_rules(item, kept):
    text = "Name Identifier Python 3.8 python3.8 Node.js 16 nodejs16.x Aurora MySQL version 3"
    assert (len(keep_items_on_page([item], text)[0]) == 1) is kept


values = st.text(alphabet="abcxyz019.-_ ()[]", min_size=0, max_size=12)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.fixed_dictionaries({}, optional={"identifier": values, "name": values, "version": values}),
                max_size=6),
       st.text(alphabet="abcxyz019.-_ ()", max_size=60))
def test_same_verdict_as_the_offline_audit(items, page_text):
    """The pipeline keeps exactly the rows the audit script counts as found on the page."""
    kept, _ = keep_items_on_page(items, page_text)
    cands = [[str(i[f]) for f in ("identifier", "name", "version") if i.get(f)] for i in items]
    hits, _ = identifier_hit_ratio(cands, page_text)
    assert len(kept) == hits


@settings(max_examples=300, deadline=None)
@given(st.lists(st.fixed_dictionaries({"identifier": values}), min_size=1, max_size=4), st.text(max_size=80))
def test_every_row_whose_identifier_is_in_the_table_is_kept(items, filler):
    """No false drop: a row whose identifier is copied from the table is kept, if the identifier
    is 3+ characters or one short word (a two-character value split by a space is too weak to count)."""
    page_text = filler + " " + " ".join(i["identifier"] for i in items)
    kept, dropped = keep_items_on_page(items, page_text)

    def evidence(value):
        core = re.split(r"\s*[\(\[]", value.lower())[0]
        norm = re.sub(r"[^a-z0-9.]", "", core).strip(".")
        return len(norm) >= 3 or (norm != "" and re.fullmatch(r"[a-z0-9.]+", core.strip()) is not None)
    assert [i for i in dropped if evidence(i["identifier"])] == []
