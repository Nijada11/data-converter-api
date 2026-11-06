import json
import logging

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.converters import Format
from src.logging_config import JsonFormatter
from src.main import _download_name, create_app
from tests.conftest import AUTH


def upload(name="data.csv", content=b"a,b\n1,2\n"):
    return {"file": (name, content)}


# --- /health and docs ----------------------------------------------------------------


def test_health_needs_no_key(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_and_schema_are_public(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "X-API-Key" in json.dumps(schema["components"]["securitySchemes"])


def test_health_is_not_rate_limited(make_client):
    client = make_client(rate_limit="1/minute")
    assert {client.get("/health").status_code for _ in range(5)} == {200}


# --- /convert ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "content", "media_type", "expected"),
    [
        ("csv", "json", b"a\n1\n", "application/json", '"a": "1"'),
        ("csv", "xml", b"a\n1\n", "application/xml", "<a>1</a>"),
        ("json", "csv", b'[{"a": 1}]', "text/csv", "a\n1\n"),
        ("json", "xml", b'[{"a": 1}]', "application/xml", "<a>1</a>"),
        ("xml", "csv", b"<r><p><a>1</a></p><p><a>2</a></p></r>", "text/csv", "a\n1\n2\n"),
        ("xml", "json", b"<r><a>1</a><b>2</b></r>", "application/json", '"a": "1"'),
    ],
)
def test_convert_each_pair(client, source, target, content, media_type, expected):
    response = client.post(
        f"/convert?from={source}&to={target}",
        headers=AUTH,
        files=upload(f"data.{source}", content),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)
    assert expected in response.text
    assert response.headers["content-disposition"] == f'attachment; filename="data.{target}"'


def test_latin1_upload_comes_back_as_utf8(client):
    response = client.post(
        "/convert?from=csv&to=json",
        headers=AUTH,
        files=upload(content="n\nMüller\n".encode("cp1252")),
    )
    assert response.json() == [{"n": "Müller"}]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../etc/pass wd.csv", "pass_wd.json"),
        ("Übersicht.csv", "bersicht.json"),
        ("....csv", "converted.json"),
    ],
)
def test_download_name_is_sanitised(client, filename, expected):
    response = client.post("/convert?from=csv&to=json", headers=AUTH, files=upload(filename))
    assert response.status_code == 200
    assert f'filename="{expected}"' in response.headers["content-disposition"]


def test_header_injection_in_filename_is_neutralised():
    assert _download_name('evil"\r\nX-Bad: 1.csv', Format.JSON) == "evil___X-Bad__1.json"


def test_lone_surrogate_does_not_crash(client):
    response = client.post(
        "/convert?from=json&to=json",
        headers=AUTH,
        files=upload("d.json", b'[{"a": "\\ud800"}]'),
    )
    assert response.status_code == 200


# --- error status codes --------------------------------------------------------------


def test_empty_file_is_400(client):
    response = client.post("/convert?from=csv&to=json", headers=AUTH, files=upload(content=b""))
    assert response.status_code == 400
    assert response.json()["errors"] == [{"row": None, "message": "File is empty."}]


def test_syntax_error_is_400_with_row(client):
    response = client.post(
        "/convert?from=json&to=csv", headers=AUTH, files=upload(content=b'[\n{"a": }\n]')
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["row"] == 2


def test_bad_rows_are_422_with_rows(client):
    response = client.post(
        "/convert?from=csv&to=json", headers=AUTH, files=upload(content=b"a,b\n1\n1,2,3\n")
    )
    assert response.status_code == 422
    assert [e["row"] for e in response.json()["errors"]] == [2, 3]


def test_unknown_format_is_422(client):
    response = client.post("/convert?from=csv&to=yaml", headers=AUTH, files=upload())
    assert response.status_code == 422


def test_missing_file_is_422(client):
    assert client.post("/convert?from=csv&to=json", headers=AUTH).status_code == 422


def test_file_over_limit_is_413(make_client):
    # Small enough to pass the body check, so this exercises the exact per-file check.
    client = make_client(max_upload_bytes=10)
    response = client.post(
        "/convert?from=csv&to=json", headers=AUTH, files=upload(content=b"a\n" + b"1\n" * 10)
    )
    assert response.status_code == 413


def test_file_at_limit_is_accepted(make_client):
    client = make_client(max_upload_bytes=8)
    response = client.post(
        "/convert?from=csv&to=json", headers=AUTH, files=upload(content=b"a,b\n1,2\n")
    )
    assert response.status_code == 200


def test_oversized_body_is_rejected_from_content_length(make_client):
    client = make_client(max_upload_bytes=10)
    response = client.post(
        "/convert?from=csv&to=json",
        headers=AUTH,
        files=upload(content=b"x" * (1024 * 1024)),
    )
    assert response.status_code == 413


def test_oversized_chunked_body_is_rejected_while_streaming(make_client):
    client = make_client(max_upload_bytes=10)

    def chunks():
        yield b"x" * (1024 * 1024)

    response = client.post(
        "/convert?from=csv&to=json",
        headers={**AUTH, "Content-Type": "multipart/form-data; boundary=b"},
        content=chunks(),
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 413


# --- /validate -----------------------------------------------------------------------


def test_validate_clean_file(client):
    response = client.post("/validate", headers=AUTH, files=upload())
    assert response.status_code == 200
    assert response.json() == {"valid": True, "format": "csv", "rows": 1, "errors": []}


def test_validate_lists_errors_with_rows(client):
    response = client.post("/validate", headers=AUTH, files=upload(content=b"a,b\n1,2\n3\n4,5,6\n"))
    body = response.json()
    assert response.status_code == 200
    assert body["valid"] is False
    assert body["rows"] == 1
    assert [e["row"] for e in body["errors"]] == [3, 4]


def test_validate_empty_file_is_a_report_not_an_error(client):
    response = client.post("/validate", headers=AUTH, files=upload(content=b""))
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_validate_format_param_beats_extension(client):
    response = client.post(
        "/validate?format=json", headers=AUTH, files=upload("data.txt", b'[{"a": 1}]')
    )
    assert response.json()["format"] == "json"


def test_validate_extension_is_case_insensitive(client):
    response = client.post("/validate", headers=AUTH, files=upload("DATA.JSON", b'[{"a": 1}]'))
    assert response.json()["format"] == "json"


def test_validate_unknown_extension_is_400(client):
    response = client.post("/validate", headers=AUTH, files=upload("data.txt"))
    assert response.status_code == 400


def test_validate_over_limit_is_413(make_client):
    client = make_client(max_upload_bytes=4)
    response = client.post("/validate", headers=AUTH, files=upload())
    assert response.status_code == 413


# --- auth ----------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/convert?from=csv&to=json", "/validate"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": "schlüssel"}])
def test_key_is_required(client, path, headers):
    encoded = {k: v.encode() for k, v in headers.items()}
    assert client.post(path, headers=encoded, files=upload()).status_code == 401


def test_startup_fails_without_api_key(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API_KEY"):
        create_app()


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "5")
    monkeypatch.setenv("RATE_LIMIT", "2/minute")
    assert Settings.from_env() == Settings(api_key="k", max_upload_bytes=5, rate_limit="2/minute")


# --- rate limiting -------------------------------------------------------------------


def test_rate_limit_is_429(make_client):
    client = make_client(rate_limit="2/minute")
    codes = [client.post("/validate", headers=AUTH, files=upload()).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_rotating_bad_keys_does_not_escape_the_limit(make_client):
    client = make_client(rate_limit="2/minute")
    codes = [
        client.post("/validate", headers={"X-API-Key": f"guess{i}"}, files=upload()).status_code
        for i in range(3)
    ]
    assert codes == [401, 401, 429]


# --- logging -------------------------------------------------------------------------


def test_every_response_has_a_request_id(client):
    assert len(client.get("/health").headers["x-request-id"]) == 32


def test_requests_are_logged_as_structured_fields(client, caplog):
    logging.getLogger().addHandler(caplog.handler)
    with caplog.at_level(logging.INFO, logger="data_converter.access"):
        client.get("/health")
    fields = caplog.records[-1].fields
    assert fields["path"] == "/health"
    assert fields["status"] == 200


def test_json_formatter_output():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("n", logging.ERROR, "f", 1, "hi %s", ("you",), sys.exc_info())
    record.fields = {"status": 500}
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "hi you"
    assert entry["status"] == 500
    assert entry["level"] == "ERROR"
    assert "boom" in entry["exception"]


def test_unhandled_error_is_a_plain_500(monkeypatch):
    monkeypatch.setattr("src.main.convert", lambda *a: 1 / 0)
    client = TestClient(create_app(Settings(api_key="test-key")), raise_server_exceptions=False)
    response = client.post("/convert?from=csv&to=json", headers=AUTH, files=upload())
    assert response.status_code == 500
