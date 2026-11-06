"""Pure conversion between CSV, JSON and XML. No web code lives here.

Every format is read into the same shape, a list of flat-or-nested dicts ("records"), and
written back out from it. That keeps N formats to N readers and N writers.

Decisions that affect output:
- CSV values stay strings. Guessing types would turn the postcode "01067" into 1067.
- Nested data becomes dotted CSV columns: {"a": {"b": 1}} -> column "a.b".
- XML attributes become "@name" keys and text beside attributes or children becomes "#text".
- An XML root whose children all share one tag is a list of records; its own attributes are
  dropped. Any other root is a single record.
- XML namespaces are stripped from names.
"""

import codecs
import csv
import io
import json
import re
import xml.etree.ElementTree as ET  # noqa: S405 - used to build output, never to parse
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import defusedxml.ElementTree as SafeET
from defusedxml.common import DefusedXmlException

Record = dict[str, Any]
Input = bytes | str

CSV_DELIMITERS = (",", ";", "\t", "|")
TEXT_KEY = "#text"
ATTRIBUTE_PREFIX = "@"

_NAME_START = "A-Za-z_À-ÖØ-öø-˿Ͱ-ͽͿ-῿‌‍⁰-↏Ⰰ-⿯、-퟿豈-﷏ﷰ-�\U00010000-\U000effff"
_NAME_CHAR = _NAME_START + r"\-.0-9·̀-ͯ‿⁀"
_INVALID_NAME_CHAR = re.compile(f"[^{_NAME_CHAR}]")
_VALID_NAME_START = re.compile(f"[{_NAME_START}]")
_INVALID_XML_CHAR = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


class Format(StrEnum):
    CSV = "csv"
    JSON = "json"
    XML = "xml"


@dataclass(frozen=True)
class Issue:
    """One problem found in a file.

    `row` is the line in the file for CSV and for JSON/XML syntax errors, the 1-based item
    position for items of a JSON array, and None when the problem concerns the whole file.
    """

    row: int | None
    message: str


class ConversionError(ValueError):
    """Base class. Carries every problem found, so callers can list them all."""

    def __init__(self, errors: list[Issue]):
        super().__init__(errors[0].message)
        self.errors = errors


class MalformedInputError(ConversionError):
    """The file cannot be read at all: empty, binary, or a syntax error."""

    def __init__(self, message: str, row: int | None = None):
        super().__init__([Issue(row, message)])


class RowValidationError(ConversionError):
    """The file was read, but individual rows are invalid."""


# --- text ----------------------------------------------------------------------------


def decode_text(data: Input) -> str:
    """Decode bytes, trying UTF-8 first, then the encodings German Excel exports use.

    Valid UTF-8 is very unlikely to be Latin-1 text by accident, so the order is safe.
    cp1252 goes before Latin-1 because it maps 0x80 to the euro sign. Latin-1 decodes
    anything, so it is the last resort.
    """
    if isinstance(data, str):
        return data
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise MalformedInputError("File has a UTF-16 marker but is not UTF-16.") from exc
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _load_text(data: Input) -> str:
    text = decode_text(data)
    if not text.strip():
        raise MalformedInputError("File is empty.")
    if "\x00" in text:
        raise MalformedInputError("File looks binary, not text.")
    return text


# --- readers -------------------------------------------------------------------------


def _detect_delimiter(text: str) -> str:
    """Pick the delimiter used most in the header line, ignoring quoted sections."""
    counts = dict.fromkeys(CSV_DELIMITERS, 0)
    in_quotes = False
    for char in text.lstrip("\r\n"):
        if char == '"':
            in_quotes = not in_quotes
        elif in_quotes:
            continue
        elif char in "\r\n":
            break
        elif char in counts:
            counts[char] += 1
    return max(counts, key=lambda delimiter: counts[delimiter])


def _header_issues(header: list[str], row: int) -> list[Issue]:
    issues = []
    seen = set()
    for position, name in enumerate(header, start=1):
        if not name.strip():
            issues.append(Issue(row, f"Column {position} has an empty header."))
        elif name in seen:
            issues.append(Issue(row, f"Duplicate column '{name}'."))
        seen.add(name)
    return issues


def read_csv(data: Input) -> tuple[list[Record], list[Issue]]:
    text = _load_text(data)
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=_detect_delimiter(text), strict=True
    )
    header: list[str] | None = None
    records: list[Record] = []
    issues: list[Issue] = []
    while True:
        # A quoted field can span lines; report the line the record starts on.
        row_number = reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            issues.append(Issue(row_number, f"Invalid CSV: {exc}."))
            break
        if not row:
            continue
        if header is None:
            header = row
            issues.extend(_header_issues(header, row_number))
        elif len(row) != len(header):
            issues.append(Issue(row_number, f"Found {len(row)} column(s), expected {len(header)}."))
        else:
            records.append(dict(zip(header, row, strict=True)))
    return records, issues


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def read_json(data: Input) -> tuple[list[Record], list[Issue]]:
    text = _load_text(data)
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise MalformedInputError(f"Invalid JSON: {exc.msg}.", row=exc.lineno) from exc
    except ValueError as exc:
        raise MalformedInputError(f"Invalid JSON: {exc}.") from exc
    if isinstance(parsed, dict):
        return [parsed], []
    if not isinstance(parsed, list):
        raise MalformedInputError("JSON must be an object or an array of objects.")
    issues = [
        Issue(position, "Item is not an object.")
        for position, item in enumerate(parsed, start=1)
        if not isinstance(item, dict)
    ]
    return [item for item in parsed if isinstance(item, dict)], issues


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _element_value(element: ET.Element) -> Any:
    children = list(element)
    attributes = {
        f"{ATTRIBUTE_PREFIX}{_local_name(key)}": value for key, value in element.attrib.items()
    }
    text = element.text or ""
    if not children and not attributes:
        return text if text.strip() else None

    value: Record = attributes
    for child in children:
        tag = _local_name(child.tag)
        child_value = _element_value(child)
        if tag not in value:
            value[tag] = child_value
        elif isinstance(value[tag], list):
            value[tag].append(child_value)
        else:
            value[tag] = [value[tag], child_value]
    if text.strip():
        value[TEXT_KEY] = text.strip()
    return value


def _parse_xml(data: Input, text: str) -> ET.Element:
    """Parse the raw bytes so a declared encoding is honoured, else the decoded text."""
    try:
        return SafeET.fromstring(data)
    except (SafeET.ParseError, LookupError):
        if isinstance(data, str):
            raise
    return SafeET.fromstring(text)


def read_xml(data: Input) -> tuple[list[Record], list[Issue]]:
    text = _load_text(data)
    try:
        root = _parse_xml(data, text)
    except SafeET.ParseError as exc:
        line = exc.position[0] if exc.position else None
        raise MalformedInputError(f"Invalid XML: {exc}.", row=line) from exc
    except DefusedXmlException as exc:
        raise MalformedInputError("XML entity declarations are not allowed.") from exc

    children = list(root)
    if not children and not root.attrib and not (root.text or "").strip():
        return [], []
    same_tag = children and len({child.tag for child in children}) == 1
    if same_tag and (len(children) > 1 or len(children[0]) or children[0].attrib):
        records = [(child.tag, _element_value(child)) for child in children]
    else:
        records = [(root.tag, _element_value(root))]
    return [
        value if isinstance(value, dict) else {_local_name(tag): value} for tag, value in records
    ], []


# --- writers -------------------------------------------------------------------------


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten(value: Any, prefix: str = "") -> Record:
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return {prefix: _scalar(value)}
    if not value and prefix:
        return {prefix: ""}
    flat: Record = {}
    for key, item in items:
        flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    return flat


def write_csv(records: list[Record]) -> str:
    rows = [_flatten(record) for record in records]
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    if not fieldnames:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, restval="", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def write_json(records: list[Record]) -> str:
    return json.dumps(records, ensure_ascii=False, indent=2) + "\n"


def xml_name(key: str) -> str:
    """Turn any string into a valid XML name, e.g. "First Name" -> "First_Name"."""
    name = _INVALID_NAME_CHAR.sub("_", key)
    if not _VALID_NAME_START.match(name):
        name = f"_{name}"
    return name


def _xml_text(value: Any) -> str | None:
    # XML 1.0 cannot represent most control characters, even escaped.
    return _INVALID_XML_CHAR.sub("", _scalar(value)) or None


def _add_fields(element: ET.Element, record: Record) -> None:
    for key, value in record.items():
        if key == TEXT_KEY:
            element.text = _xml_text(value)
        elif key.startswith(ATTRIBUTE_PREFIX) and not isinstance(value, dict | list):
            element.set(xml_name(key[1:]), _xml_text(value) or "")
        else:
            _append_value(element, xml_name(key), value)


def _append_value(parent: ET.Element, tag: str, value: Any) -> None:
    if isinstance(value, list):
        if not value:
            ET.SubElement(parent, tag)
        for item in value:
            if isinstance(item, list):
                _append_value(ET.SubElement(parent, tag), "item", item)
            else:
                _append_value(parent, tag, item)
        return
    child = ET.SubElement(parent, tag)
    if isinstance(value, dict):
        _add_fields(child, value)
    else:
        child.text = _xml_text(value)


def write_xml(records: list[Record]) -> str:
    root = ET.Element("records")
    for record in records:
        _add_fields(ET.SubElement(root, "record"), record)
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


# --- public API ----------------------------------------------------------------------

_READERS = {Format.CSV: read_csv, Format.JSON: read_json, Format.XML: read_xml}
_WRITERS = {Format.CSV: write_csv, Format.JSON: write_json, Format.XML: write_xml}
_TOO_DEEP = "Data is nested too deeply."


def _read(data: Input, source: Format) -> tuple[list[Record], list[Issue]]:
    try:
        return _READERS[source](data)
    except RecursionError as exc:
        raise MalformedInputError(_TOO_DEEP) from exc


def check(data: Input, source: Format) -> tuple[int, list[Issue]]:
    """Return the number of readable records and every problem in the file."""
    try:
        records, issues = _read(data, source)
    except MalformedInputError as exc:
        return 0, exc.errors
    return len(records), issues


def validate(data: Input, source: Format) -> list[Issue]:
    """Return every problem in the file; an empty list means `convert` will accept it."""
    return check(data, source)[1]


def convert(data: Input, source: Format, target: Format) -> str:
    """Convert a file, refusing it if any row is invalid rather than dropping rows."""
    records, issues = _read(data, source)
    if issues:
        raise RowValidationError(issues)
    try:
        return _WRITERS[target](records)
    except RecursionError as exc:
        raise MalformedInputError(_TOO_DEEP) from exc


def csv_to_json(data: Input) -> str:
    return convert(data, Format.CSV, Format.JSON)


def csv_to_xml(data: Input) -> str:
    return convert(data, Format.CSV, Format.XML)


def json_to_csv(data: Input) -> str:
    return convert(data, Format.JSON, Format.CSV)


def json_to_xml(data: Input) -> str:
    return convert(data, Format.JSON, Format.XML)


def xml_to_csv(data: Input) -> str:
    return convert(data, Format.XML, Format.CSV)


def xml_to_json(data: Input) -> str:
    return convert(data, Format.XML, Format.JSON)
