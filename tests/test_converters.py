"""Tricky-case tests for the conversion core. Written before the implementation."""

import json

import pytest

from src import converters
from src.converters import (
    Format,
    MalformedInputError,
    RowValidationError,
    convert,
    csv_to_json,
    csv_to_xml,
    decode_text,
    json_to_csv,
    json_to_xml,
    validate,
    xml_to_csv,
    xml_to_json,
)


def rows(output: str) -> list[dict]:
    return json.loads(output)


# --- empty input ---------------------------------------------------------------------


@pytest.mark.parametrize("fmt", list(Format))
@pytest.mark.parametrize("payload", [b"", b"   \n\t\n"])
def test_empty_input_is_rejected(fmt, payload):
    with pytest.raises(MalformedInputError, match="empty"):
        convert(payload, fmt, Format.JSON)


def test_header_only_csv_has_no_records():
    assert rows(csv_to_json(b"a,b\n")) == []


def test_empty_json_array_produces_empty_outputs():
    assert json_to_csv(b"[]") == ""
    assert xml_to_json(json_to_xml(b"[]").encode()) == "[]\n"


# --- encodings -----------------------------------------------------------------------


def test_utf8_german_text():
    assert rows(csv_to_json("name\nMüller\n".encode()))[0]["name"] == "Müller"


def test_latin1_german_text():
    assert rows(csv_to_json("name\nMüller\n".encode("latin-1")))[0]["name"] == "Müller"


def test_windows_1252_euro_sign():
    assert rows(csv_to_json("preis\n5 €\n".encode("cp1252")))[0]["preis"] == "5 €"


def test_utf8_bom_does_not_pollute_first_header():
    assert rows(csv_to_json("﻿name\nA\n".encode()))[0] == {"name": "A"}


def test_utf16_with_bom():
    assert rows(csv_to_json("name\nÄ\n".encode("utf-16")))[0]["name"] == "Ä"


def test_broken_utf16_is_malformed():
    with pytest.raises(MalformedInputError):
        decode_text(b"\xff\xfea")


def test_undefined_cp1252_bytes_fall_back_to_latin1():
    assert decode_text(b"caf\xe9\x81") == "café\x81"


def test_latin1_json():
    assert rows(convert('[{"ort": "Köln"}]'.encode("latin-1"), Format.JSON, Format.JSON)) == [
        {"ort": "Köln"}
    ]


def test_xml_honours_declared_encoding():
    xml = '<?xml version="1.0" encoding="ISO-8859-1"?><r><a>Köln</a><b>x</b></r>'
    assert rows(xml_to_json(xml.encode("latin-1"))) == [{"a": "Köln", "b": "x"}]


def test_xml_with_latin1_bytes_and_no_declaration():
    assert rows(xml_to_json("<r><a>Köln</a><b>x</b></r>".encode("latin-1")))[0]["a"] == "Köln"


def test_binary_upload_is_malformed():
    with pytest.raises(MalformedInputError, match="binary"):
        csv_to_json(b"PK\x03\x04\x00\x00\x00")


def test_text_input_is_accepted():
    assert rows(csv_to_json("a\n1\n")) == [{"a": "1"}]


# --- CSV dialect ---------------------------------------------------------------------


def test_comma_inside_quotes():
    assert rows(csv_to_json(b'name,city\n"Doe, Jane","Berlin, DE"\n')) == [
        {"name": "Doe, Jane", "city": "Berlin, DE"}
    ]


def test_escaped_quote_and_newline_inside_quotes():
    assert rows(csv_to_json(b'a,b\n"say ""hi""","line1\nline2"\n')) == [
        {"a": 'say "hi"', "b": "line1\nline2"}
    ]


def test_german_excel_semicolon_delimiter():
    assert rows(csv_to_json(b"name;preis\nBrot;1,50\n")) == [{"name": "Brot", "preis": "1,50"}]


def test_delimiter_inside_quoted_header_is_ignored():
    assert rows(csv_to_json(b'"a,b,c";d\n1;2\n')) == [{"a,b,c": "1", "d": "2"}]


def test_tab_delimiter():
    assert rows(csv_to_json(b"a\tb\n1\t2\n")) == [{"a": "1", "b": "2"}]


def test_values_stay_strings_so_leading_zeros_survive():
    assert rows(csv_to_json(b"plz\n01067\n")) == [{"plz": "01067"}]


def test_blank_lines_are_skipped():
    assert len(rows(csv_to_json(b"\na\n\n1\n\n2\n"))) == 2


# --- CSV validation ------------------------------------------------------------------


def test_row_numbers_are_file_lines():
    errors = validate(b"a,b\n1,2\n3\n4,5,6\n", Format.CSV)
    assert [e.row for e in errors] == [3, 4]
    assert "expected 2" in errors[0].message


def test_row_number_after_multiline_field_points_to_record_start():
    errors = validate(b'a,b\n"x\ny",2\nbad\n', Format.CSV)
    assert [e.row for e in errors] == [4]


def test_convert_refuses_bad_rows_and_lists_them():
    with pytest.raises(RowValidationError) as exc:
        csv_to_json(b"a,b\n1\n")
    assert exc.value.errors[0].row == 2


def test_duplicate_and_blank_headers_are_reported():
    messages = [e.message for e in validate(b"a,a,\n1,2,3\n", Format.CSV)]
    assert any("duplicate" in m.lower() for m in messages)
    assert any("empty" in m.lower() for m in messages)


def test_unterminated_quote_is_reported():
    errors = validate(b'a,b\n1,"oops\n', Format.CSV)
    assert errors and errors[0].row == 2


def test_clean_file_has_no_errors():
    assert validate(b"a,b\n1,2\n", Format.CSV) == []


def test_validate_reports_empty_file():
    errors = validate(b"", Format.CSV)
    assert len(errors) == 1 and errors[0].row is None


# --- JSON ----------------------------------------------------------------------------


def test_json_syntax_error_reports_line():
    errors = validate(b'[\n{"a": 1},\n{"a": }\n]', Format.JSON)
    assert errors[0].row == 3


def test_json_non_object_items_are_reported_by_position():
    errors = validate(b'[{"a": 1}, 2, {"a": 3}, "x"]', Format.JSON)
    assert [e.row for e in errors] == [2, 4]


def test_json_top_level_scalar_is_rejected():
    assert validate(b"42", Format.JSON)[0].row is None


def test_json_single_object_is_one_record():
    assert json_to_csv(b'{"a": 1}') == "a\n1\n"


def test_json_nan_is_rejected():
    with pytest.raises(MalformedInputError):
        convert(b'[{"a": NaN}]', Format.JSON, Format.JSON)


def test_deep_json_is_rejected_not_crashing():
    deep = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(MalformedInputError, match="deeply"):
        convert(deep, Format.JSON, Format.CSV)


def test_nested_json_flattens_to_dotted_columns():
    payload = b'[{"id": 1, "addr": {"city": "Bonn"}, "tags": ["x", "y"], "ok": true, "n": null}]'
    assert json_to_csv(payload) == "id,addr.city,tags.0,tags.1,ok,n\n1,Bonn,x,y,true,\n"


def test_csv_columns_are_the_union_of_all_records():
    assert json_to_csv(b'[{"a": 1}, {"b": 2}]') == "a,b\n1,\n,2\n"


def test_empty_containers_flatten_to_blank_cell():
    assert json_to_csv(b'[{"a": {}, "b": []}]') == "a,b\n,\n"


# --- XML reading ---------------------------------------------------------------------


def test_nested_xml_to_json():
    xml = b"""<orders>
      <order id="1"><customer><name>Anna</name></customer>
        <item>a</item><item>b</item></order>
      <order id="2"><customer><name>Ben</name></customer><item>c</item></order>
    </orders>"""
    assert rows(xml_to_json(xml)) == [
        {"@id": "1", "customer": {"name": "Anna"}, "item": ["a", "b"]},
        {"@id": "2", "customer": {"name": "Ben"}, "item": "c"},
    ]


def test_nested_xml_to_csv():
    xml = b"<r><p><a><b>1</b></a></p><p><a><b>2</b></a></p></r>"
    assert xml_to_csv(xml) == "a.b\n1\n2\n"


def test_single_repeated_element_is_still_a_record():
    assert rows(xml_to_json(b"<rows><row><a>1</a></row></rows>")) == [{"a": "1"}]


def test_root_with_distinct_children_is_one_record():
    assert rows(xml_to_json(b"<p><a>1</a><b>2</b></p>")) == [{"a": "1", "b": "2"}]


def test_empty_root_has_no_records():
    assert rows(xml_to_json(b"<r/>")) == []


def test_text_only_root():
    assert rows(xml_to_json(b"<r>hi</r>")) == [{"r": "hi"}]


def test_empty_element_is_null_and_text_beside_attribute_is_kept():
    assert rows(xml_to_json(b'<r><a/><b k="v">t</b></r>')) == [
        {"a": None, "b": {"@k": "v", "#text": "t"}}
    ]


def test_namespaces_are_stripped():
    xml = b'<r xmlns="urn:x" xmlns:y="urn:y"><a y:k="v">1</a><b>2</b></r>'
    assert rows(xml_to_json(xml)) == [{"a": {"@k": "v", "#text": "1"}, "b": "2"}]


def test_xml_syntax_error_reports_line():
    errors = validate(b"<r>\n<a>1</a>\n<b>\n</r>", Format.XML)
    assert errors[0].row == 4


def test_unknown_declared_encoding_falls_back_to_decoded_text():
    xml = b'<?xml version="1.0" encoding="nope"?><r><a>1</a><b>2</b></r>'
    assert rows(xml_to_json(xml)) == [{"a": "1", "b": "2"}]


def test_entity_expansion_is_blocked():
    bomb = b"""<!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><r>&b;</r>"""
    with pytest.raises(MalformedInputError):
        xml_to_json(bomb)


def test_external_entity_is_blocked():
    xxe = b"""<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>"""
    with pytest.raises(MalformedInputError):
        xml_to_json(xxe)


def test_deep_xml_is_rejected_not_crashing():
    deep = b"<a>" * 200_000 + b"</a>" * 200_000
    with pytest.raises(MalformedInputError, match="deeply"):
        xml_to_json(deep)


# --- XML writing ---------------------------------------------------------------------


def test_csv_headers_become_valid_xml_names():
    out = csv_to_xml("First Name,1st,Fläche m²,a:b\nx,y,z,w\n".encode())
    assert rows(xml_to_json(out.encode())) == [
        {"First_Name": "x", "_1st": "y", "Fläche_m_": "z", "a_b": "w"}
    ]


def test_special_characters_are_escaped():
    out = csv_to_xml(b'a\n"<b> & c"\n')
    assert "&lt;b&gt; &amp; c" in out
    assert rows(xml_to_json(out.encode())) == [{"a": "<b> & c"}]


def test_control_characters_cannot_break_xml_output():
    out = json_to_xml(b'[{"a": "x\\u0001y"}]')
    assert rows(xml_to_json(out.encode())) == [{"a": "xy"}]


def test_attributes_and_text_round_trip():
    payload = {"@id": "1", "b": {"@k": "v", "#text": "t"}, "c": ["x", "y"], "d": None}
    out = json_to_xml(json.dumps([payload, payload]).encode())
    assert rows(xml_to_json(out.encode())) == [payload, payload]


def test_nested_lists_and_scalars_in_xml():
    out = json_to_xml(b'[{"m": [[1, 2], [3]], "e": [], "t": true}]')
    assert "<m>" in out and "<item>1</item>" in out and "<t>true</t>" in out and "<e />" in out


def test_blank_key_gets_a_usable_tag():
    assert converters.xml_name("") == "_"


# --- round trips ---------------------------------------------------------------------


@pytest.mark.parametrize("via", list(Format))
def test_flat_records_survive_any_format(via):
    source = b'name,note\n"Doe, Jane",M\xc3\xbcller\n'
    middle = convert(source, Format.CSV, via).encode()
    assert convert(middle, via, Format.CSV) == 'name,note\n"Doe, Jane",Müller\n'
