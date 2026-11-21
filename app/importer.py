"""CSV parsing and questionnaire building from uploaded files."""

from __future__ import annotations

import csv
import io
import json
import re

import engine

HOUSEHOLD_HEADERS = ("户号", "户编号", "户主编号", "农户编号", "household_no",
                     "household", "household_id")
_SEPARATORS = re.compile(r"[\s:：、，,|/\\\-—_（(]+")
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")


class ImportError_(ValueError):
    """An uploaded file cannot be imported."""


def decode(raw):
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_("无法识别 CSV 编码，请另存为 UTF-8 或 GBK")


def read_csv(raw):
    text = decode(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader]
    if not rows:
        raise ImportError_("CSV 是空的")
    return rows


def _clean_header(header):
    return (header or "").replace("﻿", "").strip()


def find_household_column(headers):
    cleaned = [_clean_header(h) for h in headers]
    for position, header in enumerate(cleaned):
        lowered = header.lower()
        for candidate in HOUSEHOLD_HEADERS:
            if lowered == candidate.lower():
                return position
    for position, header in enumerate(cleaned):
        if "户号" in header or "编号" in header:
            return position
    return 0


def header_key(header):
    """``A1 设备来源`` -> ``("A1", "设备来源")``; ``设备来源`` -> ``(None, 设备来源)``."""
    cleaned = _clean_header(header)
    parts = _SEPARATORS.split(cleaned, maxsplit=1)
    first = parts[0].strip()
    if parts[1:]:
        rest = cleaned[len(first):].strip()
        rest = rest.lstrip("".join(" \t:：、，,|/\\-—_（）()"))
        return (first or None), rest.strip()
    return first or None, cleaned


def question_id_for_header(header, definition):
    """Map a CSV header to a question id.

    Accepts the bare id, ``A1 设备来源``, ``A1：设备来源`` and the title.
    """
    cleaned = _clean_header(header)
    ids = [question["id"] for question in definition]
    if cleaned in ids:
        return cleaned
    first = _SEPARATORS.split(cleaned, maxsplit=1)[0].strip()
    if first in ids:
        return first
    for question in definition:
        if cleaned == question["title"]:
            return question["id"]
    return None


def read_json(raw):
    try:
        return json.loads(decode(raw))
    except (ValueError, TypeError) as error:
        raise ImportError_("JSON 跳题表解析失败: %s" % error)


def parse(raw_csv, raw_json, household_column=None):
    """Parse uploads into ``(definition, warnings, rows)``.

    ``rows`` is a list of ``{household_no, row_number, answers}`` where
    ``answers`` maps question id to the cell text.
    """
    rows = read_csv(raw_csv)
    headers = [_clean_header(h) for h in rows[0]]
    if not any(headers):
        raise ImportError_("CSV 表头为空")

    household_position = (find_household_column(headers)
                          if household_column in (None, "")
                          else int(household_column))
    if household_position < 0 or household_position >= len(headers):
        raise ImportError_("户号列位置超出范围")
    if len(headers) < 2:
        raise ImportError_("CSV 至少需要户号列和一个题目列")

    data = read_json(raw_json) if raw_json else None

    # Question order and titles come from the JSON when it declares them.
    # A bare jump table leaves the order to the CSV columns.
    json_columns = engine.json_columns(data) if data is not None else None
    if json_columns:
        columns = [{"id": column["id"],
                    "title": column["title"] or column["id"]}
                   for column in json_columns]
    else:
        columns = []
        for position, header in enumerate(headers):
            if position == household_position:
                continue
            key, title = header_key(header)
            columns.append({"id": key or title, "title": title})

    column_map = {}
    for position, header in enumerate(headers):
        if position == household_position:
            continue
        qid = question_id_for_header(header, columns)
        if qid is None:
            continue
        column_map[position] = qid

    if not column_map:
        raise ImportError_("CSV 中没有能对应到题号的列")

    parsed_rows = []
    observed = {}
    seen = set()
    warnings = []
    for row_number, raw_row in enumerate(rows[1:], start=1):
        if not any((cell or "").strip() for cell in raw_row):
            continue
        household_no = (raw_row[household_position]
                        if household_position < len(raw_row) else "").strip()
        if not household_no:
            warnings.append("CSV 第 %d 行缺少户号，已跳过" % (row_number + 1))
            continue
        if household_no in seen:
            warnings.append("户号 %s 重复出现，只保留第一次" % household_no)
            continue
        seen.add(household_no)
        answers = {}
        for position, qid in column_map.items():
            cell = (raw_row[position]
                    if position < len(raw_row) else "").strip()
            answers[qid] = cell
            if cell:
                observed.setdefault(qid, [])
                if cell not in observed[qid]:
                    observed[qid].append(cell)
        parsed_rows.append({
            "household_no": household_no,
            "row_number": row_number,
            "answers": answers,
        })

    if not parsed_rows:
        raise ImportError_("CSV 中没有数据行")

    column_definitions = [{"id": qid, "title": next(
        (header for position, header in enumerate(headers)
         if column_map.get(position) == qid), qid)}
        for qid in dict.fromkeys(column_map.values())]

    if data is None:
        definition = engine.build_definition_from_columns(column_definitions)
        warnings.append("未上传 JSON 跳题表：按 CSV 列顺序生成纯文本问卷")
    else:
        try:
            definition, build_warnings = engine.build_definition(
                data, column_definitions, observed)
        except engine.DefinitionError as error:
            raise ImportError_(str(error))
        warnings.extend(build_warnings)

    return definition, warnings, parsed_rows, household_position, headers


def to_import_answers(definition, rows):
    """Key answers by ``(prefix, qid)`` using the routing rules."""
    stored = {}
    for row in rows:
        _, answers = engine.import_row(definition, row["answers"])
        stored[row["household_no"]] = answers
    return stored


# ---------------------------------------------------------------------------
# Roster CSV: one row per sampling slot


SLOT_HEADERS = ("抽样位", "抽样位号", "样本位", "样本位号", "位号",
                "slot", "slot_no", "position")
MAIN_HEADERS = ("主户号", "主户", "主户编号", "main", "main_household",
                "main_household_no", "household_no")
SUBSTITUTE_PREFIXES = ("候补户号", "候补户", "候补", "substitute",
                       "substitute_household", "backup")
_SUBSTITUTE_SPLIT = re.compile(r"[\s、,，;；/|]+")


def _column_position(headers, candidates, prefixes=()):
    for position, header in enumerate(headers):
        lowered = _clean_header(header).lower()
        if lowered in {candidate.lower() for candidate in candidates}:
            return position
    for position, header in enumerate(headers):
        lowered = _clean_header(header).lower()
        if any(lowered.startswith(prefix.lower()) for prefix in prefixes):
            return position
    return None


def _substitute_positions(headers):
    positions = []
    for position, header in enumerate(headers):
        cleaned = _clean_header(header)
        lowered = cleaned.lower()
        if any(lowered.startswith(prefix.lower())
               for prefix in SUBSTITUTE_PREFIXES) or cleaned.startswith("候补"):
            positions.append(position)
    return positions


def parse_roster(raw_csv, households):
    """Parse a roster CSV into ordered slot entries.

    ``households`` maps household number to its id in the batch.  The roster
    must name every household; a slot number and a main household may each
    appear only once.  One main household may not also be its own slot's
    substitute, but the same substitute may be listed at several slots.

    Returns ``(entries, warnings)`` where each entry is
    ``{"slot_no", "main_id", "substitute_ids"}``.
    """
    rows = read_csv(raw_csv)
    headers = [_clean_header(header) for header in rows[0]]
    if not any(headers):
        raise ImportError_("名册表头为空")

    slot_position = _column_position(
        headers, SLOT_HEADERS, prefixes=("抽样位", "样本位"))
    main_position = _column_position(
        headers, MAIN_HEADERS, prefixes=("主户号", "主户"))
    substitute_positions = _substitute_positions(headers)
    if slot_position is None:
        raise ImportError_("名册缺少「抽样位」列")
    if main_position is None:
        raise ImportError_("名册缺少「主户号」列")
    if not substitute_positions:
        raise ImportError_("名册缺少「候补户号」列")

    entries = []
    warnings = []
    seen_slots = set()
    seen_mains = set()
    for row_number, raw_row in enumerate(rows[1:], start=2):
        if not any((cell or "").strip() for cell in raw_row):
            continue

        def cell(position):
            return (raw_row[position] if position < len(raw_row) else "").strip()

        slot_no = cell(slot_position)
        main_no = cell(main_position)
        if not slot_no:
            raise ImportError_("名册第 %d 行缺少抽样位" % row_number)
        if not main_no:
            raise ImportError_("名册第 %d 行（抽样位 %s）缺少主户号"
                               % (row_number, slot_no))
        if slot_no in seen_slots:
            raise ImportError_("名册第 %d 行抽样位 %s 重复"
                               % (row_number, slot_no))
        seen_slots.add(slot_no)
        if main_no not in households:
            raise ImportError_("名册第 %d 行主户号 %s 不在本批"
                               % (row_number, main_no))
        if main_no in seen_mains:
            raise ImportError_("主户号 %s 在名册中出现了多个抽样位"
                               % main_no)
        seen_mains.add(main_no)

        substitute_nos = []
        for position in substitute_positions:
            for value in _SUBSTITUTE_SPLIT.split(cell(position)):
                if value and value not in substitute_nos:
                    substitute_nos.append(value)
        for substitute_no in substitute_nos:
            if substitute_no not in households:
                raise ImportError_(
                    "名册第 %d 行候补户号 %s 不在本批"
                    % (row_number, substitute_no))
            if substitute_no == main_no:
                raise ImportError_(
                    "名册第 %d 行主户号 %s 不能同时作为本抽样位的候补"
                    % (row_number, main_no))

        entries.append({
            "slot_no": slot_no,
            "main_id": households[main_no],
            "substitute_ids": [households[no] for no in substitute_nos],
        })

    if not entries:
        raise ImportError_("名册中没有数据行")

    # A main household may not also be a candidate substitute anywhere in the
    # roster; candidates may be shared between slots.
    mains = {entry["main_id"]: entry["slot_no"] for entry in entries}
    for entry in entries:
        for substitute_id in entry["substitute_ids"]:
            if substitute_id in mains:
                raise ImportError_(
                    "户 %s 是抽样位 %s 的主户，不能兼作候补"
                    % (_household_name(households, substitute_id),
                       mains[substitute_id]))
    return entries, warnings


def _household_name(households, household_id):
    for household_no, candidate in households.items():
        if candidate == household_id:
            return household_no
    return str(household_id)
