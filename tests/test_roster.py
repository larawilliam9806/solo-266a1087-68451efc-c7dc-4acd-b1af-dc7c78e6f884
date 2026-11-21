"""Roster submission-slot rules: resolution, locks, and locked edits."""

import csv
import io
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import engine  # noqa: E402
import importer  # noqa: E402
import roster  # noqa: E402


def definition():
    return [
        {"id": "Q1", "title": "设备来源", "type": "single",
         "options": ["自购", "租赁"], "jumps": {"租赁": "Q5"}},
        {"id": "Q2", "title": "自购类型", "type": "single",
         "options": ["喷药机"], "jumps": {}},
        {"id": "Q3", "title": "台数", "type": "single", "options": ["1"],
         "jumps": {"1": "Q7"}},
        {"id": "Q5", "title": "租赁方式", "type": "single",
         "options": ["按年"], "jumps": {}},
        {"id": "Q7", "title": "费用", "type": "number", "options": [],
         "jumps": {}},
    ]


DEFN = definition()


def complete(prefix, route):
    """Stored answers that walk exactly ``route`` and finish it."""
    stored = {}
    order = [question["id"] for question in DEFN]
    for position, qid in enumerate(route):
        question = next(q for q in DEFN if q["id"] == qid)
        if question["type"] == engine.SINGLE:
            following = route[position + 1] if position + 1 < len(route) else None
            value = question["options"][0]
            for option in question["options"]:
                target = question["jumps"].get(option)
                next_question = (target if target else
                                 order[order.index(qid) + 1])
                if next_question == following:
                    value = option
                    break
        else:
            value = "100"
        stored[(tuple(route[:position]), qid)] = value
    return stored


SELF = ["Q1", "Q2", "Q3", "Q7"]
RENT = ["Q1", "Q5", "Q7"]


def full(route):
    return complete([], route)


def slots(*rows):
    """rows: (slot_no, main, [substitutes]) with household ids as numbers."""
    return [{"id": index + 1, "slot_no": slot_no, "position": index,
             "main": {"id": main, "household_no": "H%d" % main},
             "substitutes": [{"id": sub, "household_no": "H%d" % sub}
                             for sub in subs]}
            for index, (slot_no, main, subs) in enumerate(rows)]


def resolve(answers, slot_list, locks=None):
    return roster.resolve(DEFN, answers, slot_list, locks or {})


def by_slot(entries):
    return {entry["slot_no"]: entry for entry in entries}


# ---------------------------------------------------------------------------
# Qualification


def test_qualified_only_when_path_finished_and_options_valid():
    ok, reason = roster.qualify(DEFN, full(SELF))
    assert ok, reason

    partial = {((), "Q1"): "自购"}
    ok, reason = roster.qualify(DEFN, partial)
    assert not ok and "未答完" in reason and "Q2" in reason

    invalid = full(SELF)
    invalid[((), "Q1")] = "丢弃"
    ok, reason = roster.qualify(DEFN, invalid)
    assert not ok and "不在选项中" in reason


# ---------------------------------------------------------------------------
# Resolution order


def test_locked_kept_then_mains_then_substitutes_in_roster_order():
    answers = {
        1: full(RENT),     # main of S1, locked
        2: {((), "Q1"): "自购"},   # main of S2, incomplete
        3: full(SELF),     # substitute at S2
        4: full(SELF),     # substitute at S3
    }
    slot_list = slots(("S1", 1, []), ("S2", 2, [3, 4]),
                      ("S3", 5, [3]), ("S4", 6, []))
    # H1 is locked in S1 even though it is already a main there.
    entries = by_slot(resolve(answers, slot_list, {1: 1}))
    assert entries["S1"]["status"] == "locked"
    assert entries["S2"]["status"] == "substitute"
    assert entries["S2"]["household_id"] == 3
    assert entries["S3"]["status"] == "empty"
    assert "其他抽样位" in entries["S3"]["reason"]
    assert entries["S4"]["status"] == "empty"
    assert "未答完" in entries["S4"]["reason"]


def test_same_substitute_may_fill_only_one_slot():
    incomplete = {((), "Q1"): "自购"}
    answers = {1: full(SELF), 2: full(SELF), 3: incomplete,
               4: incomplete, 5: incomplete}
    slot_list = slots(("S1", 3, [1]), ("S2", 4, [1]), ("S3", 5, [1, 2]))
    entries = resolve(answers, slot_list)
    assert entries[0]["household_id"] == 1       # H1 takes S1
    assert entries[1]["status"] == "empty"       # S2 does not count it twice
    assert "其他抽样位" in entries[1]["reason"]
    assert entries[2]["household_id"] == 2       # H2 still available at S3


def test_main_qualified_before_substitute_filling():
    answers = {1: full(SELF), 2: full(SELF), 3: full(RENT)}
    slot_list = slots(("S1", 3, [1]), ("S2", 4, [1, 2]))
    entries = resolve(answers, slot_list)
    assert entries[0]["household_id"] == 3       # qualified main first
    assert entries[1]["household_id"] == 1       # then the first candidate


def test_main_cannot_double_count_as_substitute():
    answers = {1: full(SELF), 2: full(SELF), 3: full(RENT)}
    slot_list = slots(("S1", 1, [1]), ("S2", 2, [3]), ("S3", 4, [3]))
    entries = resolve(answers, slot_list)
    assert entries[0]["household_id"] == 1
    assert entries[1]["household_id"] == 2
    assert entries[2]["household_id"] == 3


def test_locked_applies_even_if_answers_would_no_longer_qualify():
    answers = {1: full(SELF), 2: full(RENT)}
    # H1's Q3 is later cleared; it stays in the list because it is locked.
    broken = dict(answers)
    broken[1] = {((), "Q1"): "自购"}
    slot_list = slots(("S1", 1, [2]), ("S2", 3, []))
    entries = by_slot(resolve(broken, slot_list, {1: 1}))
    assert entries["S1"]["status"] == "locked"
    assert entries["S1"]["household_id"] == 1
    assert entries["S1"]["missing"] == "Q2"


def test_empty_slot_explains_unqualified_candidates():
    answers = {1: {((), "Q1"): "自购"}}
    slot_list = slots(("S1", 1, [2]))
    entries = resolve(answers, slot_list)
    assert entries[0]["status"] == "empty"
    assert "主户 H1 未答完" in entries[0]["reason"]
    assert "候补均未答完" in entries[0]["reason"]


def test_late_qualifying_main_does_not_displace_locked_substitute():
    # S1's candidate H2 was locked first; H1 (the main) only finishes later.
    answers = {1: full(SELF), 2: full(RENT)}
    slot_list = slots(("S1", 1, [2]))
    entries = resolve(answers, slot_list, {1: 2})
    assert entries[0]["household_id"] == 2
    assert entries[0]["status"] == "locked"


def test_duplicate_locks_count_one_household_once():
    answers = {1: full(SELF)}
    slot_list = slots(("S1", 2, [1]), ("S2", 3, [1]))
    entries = by_slot(resolve(answers, slot_list, {1: 1, 2: 1}))
    assert entries["S1"]["status"] == "locked"
    assert entries["S2"]["status"] == "empty"
    assert "S1" in entries["S2"]["reason"]


def test_every_slot_has_one_entry_in_roster_order():
    answers = {1: full(SELF)}
    slot_list = slots(("A", 2, []), ("B", 1, []), ("C", 3, []))
    entries = resolve(answers, slot_list)
    assert [entry["slot_no"] for entry in entries] == ["A", "B", "C"]
    assert entries[1]["household_id"] == 1


# ---------------------------------------------------------------------------
# Lock validation


def test_lock_refused_when_slot_is_empty_or_changed():
    answers = {1: full(SELF), 2: full(RENT)}
    slot_list = slots(("S1", 1, [2]), ("S2", 3, []))
    entries = resolve(answers, slot_list)
    ok, message = roster.check_lock(entries, 1, 1)
    assert ok, message

    ok, message = roster.check_lock(entries, 2, None)
    assert not ok and "空缺" in message and "刷新" in message

    ok, message = roster.check_lock(entries, 1, 2)
    assert not ok and "换户" in message and "刷新" in message


# ---------------------------------------------------------------------------
# Roster import validation


def households(**pairs):
    return {name: number for name, number in pairs.items()}


def parse_roster(text, batch_households):
    return importer.parse_roster(text.encode("utf-8"), batch_households)


def test_roster_accepts_blank_substitute_cells_and_shared_candidates():
    text = ("抽样位,主户号,候补户号\n"
            "S1,H1,\n"
            "S2,H2,H3\n"
            "S3,H4,H3\n")
    entries, _ = parse_roster(text, households(H1=1, H2=2, H3=3, H4=4))
    assert [entry["slot_no"] for entry in entries] == ["S1", "S2", "S3"]
    assert entries[0]["substitute_ids"] == []
    assert entries[1]["substitute_ids"] == [3]
    assert entries[2]["substitute_ids"] == [3]


def test_roster_rejects_duplicate_slot_and_main():
    bad_slot = "抽样位,主户号,候补户号\nS1,H1,\nS1,H2,\n"
    with pytest.raises(importer.ImportError_, match="抽样位 S1 重复"):
        parse_roster(bad_slot, households(H1=1, H2=2))

    bad_main = "抽样位,主户号,候补户号\nS1,H1,\nS2,H1,\n"
    with pytest.raises(importer.ImportError_, match="H1"):
        parse_roster(bad_main, households(H1=1))


def test_roster_rejects_main_as_substitute():
    text = "抽样位,主户号,候补户号\nS1,H1,H2\nS2,H2,\n"
    with pytest.raises(importer.ImportError_, match="不能兼作候补"):
        parse_roster(text, households(H1=1, H2=2))


def test_roster_requires_households_in_batch():
    text = "抽样位,主户号,候补户号\nS1,H1,H9\n"
    with pytest.raises(importer.ImportError_, match="H9 不在本批"):
        parse_roster(text, households(H1=1))


def test_roster_supports_multiple_substitute_columns():
    text = "抽样位,主户号,候补户号1,候补户号2\nS1,H1,H2,H3\n"
    entries, _ = parse_roster(text, households(H1=1, H2=2, H3=3))
    assert entries[0]["substitute_ids"] == [2, 3]
