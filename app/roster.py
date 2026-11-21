"""Submission-slot rules for a batch roster.

A batch may have one roster CSV listing sampling slots.  Each slot has a
unique main household and any number of optional substitute households.  The
submission list shown to reviewers and downloaded as CSV is resolved from the
roster, the lock state and the saved answers of the whole batch, and the result
only counts one valid questionnaire per slot:

1. households explicitly locked by a reviewer keep their slot, even if their
   answers would no longer qualify;
2. then every qualified main household takes its own slot;
3. remaining slots are filled, in roster order, with qualified substitutes
   that are not already entered.  The same substitute may serve several
   slots, but only until it is counted once.

A household is qualified when its stored answers walk to the end of the
current path and every single-choice answer on that path is one of the
question's options.

The module is framework and database free; callers pass plain dicts.
"""

from __future__ import annotations

import engine

LOCKED = "locked"
MAIN = "main"
SUBSTITUTE = "substitute"
EMPTY = "empty"

STATUS_LABELS = {
    LOCKED: "已锁定",
    MAIN: "主户",
    SUBSTITUTE: "候补",
    EMPTY: "空缺",
}


def qualify(definition, stored):
    """``(ok, reason)`` for one household's saved answers.

    ``reason`` is empty when qualified, otherwise a short Chinese explanation
    naming the question that lost the slot.
    """
    steps = engine.walk_stored(definition, stored or {})
    for step in steps:
        if step["invalid"]:
            return False, "题 %s 的答案「%s」不在选项中" % (
                step["qid"], step["value"])
    missing = engine.first_missing(steps)
    if missing is not None:
        return False, "未答完当前路径（题 %s %s）" % (
            missing["qid"], missing["title"])
    return True, ""


def effective_answers(steps):
    """Values on the questions actually answered on the current path."""
    return {step["qid"]: step["value"] for step in steps
            if step["answered"]}


def _snapshot_slot(slot, household):
    return {"id": household["id"], "household_no": household["household_no"]}


def _pool(slot):
    members = [slot["main"]]
    members.extend(slot.get("substitutes") or [])
    return members


def resolve(definition, answers_by_household, slots, locks):
    """Resolve every slot into the submission list.

    ``answers_by_household`` maps household id to stored answers;
    ``locks`` maps slot id to household id.  Returns a list in roster order;
    each entry carries the household (or ``None``), ``status``, a ``reason``
    for empty slots, and the effective answers when a household is entered.
    """
    cache = {}

    def household_info(household_id):
        if household_id not in cache:
            stored = answers_by_household.get(household_id) or {}
            steps = engine.walk_stored(definition, stored)
            ok, reason = qualify(definition, stored)
            cache[household_id] = {
                "ok": ok,
                "reason": reason,
                "steps": steps,
                "answers": effective_answers(steps),
                "route": engine.route(steps),
                "missing": engine.first_missing(steps),
            }
        return cache[household_id]

    taken_by = {}       # household id -> slot number already counting it
    entries = {}

    def enter(slot, household, status):
        info = household_info(household["id"])
        missing = info["missing"]
        entry = {
            "slot_id": slot["id"],
            "slot_no": slot["slot_no"],
            "position": slot["position"],
            "household": _snapshot_slot(slot, household),
            "household_id": household["id"],
            "status": status,
            "status_label": STATUS_LABELS[status],
            "reason": "",
            "route": info["route"],
            "answers": info["answers"],
            "answered": len(info["answers"]),
            "missing": missing["qid"] if missing else None,
            "missing_title": missing["title"] if missing else None,
        }
        entries[slot["id"]] = entry
        taken_by[household["id"]] = slot["slot_no"]
        return entry

    # 1. Locked households keep their slot.  A household already counted by
    #    an earlier lock is not counted twice.
    for slot in slots:
        locked_id = locks.get(slot["id"])
        if locked_id is None:
            continue
        if locked_id in taken_by:
            continue
        pool = {member["id"]: member for member in _pool(slot)}
        household = pool.get(locked_id)
        if household is None:
            # The lock no longer matches the roster; show the household as
            # entered so the reviewer can unlock it.
            household = {"id": locked_id,
                         "household_no": str(locked_id)}
        enter(slot, household, LOCKED)

    # 2. Qualified main households.
    for slot in slots:
        if slot["id"] in entries:
            continue
        main = slot["main"]
        if main["id"] in taken_by:
            continue
        if household_info(main["id"])["ok"]:
            enter(slot, main, MAIN)

    # 3. Qualified substitutes, in roster order, skipping households that are
    #    already counted or locked in another slot.
    for slot in slots:
        if slot["id"] in entries:
            continue
        for substitute in slot.get("substitutes") or []:
            if substitute["id"] in taken_by:
                continue
            if household_info(substitute["id"])["ok"]:
                enter(slot, substitute, SUBSTITUTE)
                break

    # 4. Remaining slots are empty; explain why.
    for slot in slots:
        if slot["id"] in entries:
            continue
        entries[slot["id"]] = _empty_entry(slot, taken_by, household_info)

    resolved = [entries[slot["id"]] for slot in slots]

    # A slot whose stored lock could not be counted (e.g. the same household
    # is already locked into an earlier slot) is still flagged as locked so
    # the reviewer can unlock it.
    for slot in slots:
        entry = entries[slot["id"]]
        entry["is_locked"] = slot["id"] in locks
        if entry["household_id"] is not None or slot["id"] not in locks:
            continue
        holder = taken_by.get(locks[slot["id"]])
        if holder:
            entry["reason"] = "该户已在抽样位 %s 锁定入样，此处不重复计入" % holder
    return resolved


def _empty_entry(slot, taken_by, household_info):
    main = slot["main"]
    parts = []
    if main["id"] in taken_by:
        parts.append("主户 %s 已在抽样位 %s 入样" % (
            main["household_no"], taken_by[main["id"]]))
    else:
        reason = household_info(main["id"])["reason"]
        parts.append("主户 %s %s" % (main["household_no"], reason))

    substitutes = slot.get("substitutes") or []
    if not substitutes:
        if not parts:
            parts.append("无候补")
    else:
        ready = [sub for sub in substitutes
                 if household_info(sub["id"])["ok"]]
        if not ready:
            parts.append("候补均未答完或答案不合格")
        elif all(sub["id"] in taken_by for sub in ready):
            parts.append("合格候补已在其他抽样位入样")
        else:
            parts.append("候补未入样")

    return {
        "slot_id": slot["id"],
        "slot_no": slot["slot_no"],
        "position": slot["position"],
        "household": None,
        "household_id": None,
        "status": EMPTY,
        "status_label": STATUS_LABELS[EMPTY],
        "is_locked": False,
        "reason": "；".join(part for part in parts if part),
        "route": [],
        "answers": {},
        "answered": 0,
        "missing": None,
        "missing_title": None,
    }


def current_occupant(resolved, slot_id):
    """The household id currently entered at a slot, or ``None``."""
    for entry in resolved:
        if entry["slot_id"] == slot_id:
            return entry["household_id"]
    return None


def slot_numbers(resolved):
    return {entry["slot_id"]: entry["slot_no"] for entry in resolved}


def check_lock(resolved, slot_id, expected_household_id):
    """Validate a reviewer's lock request against the freshly resolved list.

    Returns ``(ok, message)``.  A lock is only accepted while the slot still
    holds the household the reviewer saw; an empty or changed slot must be
    refreshed instead.
    """
    occupant = current_occupant(resolved, slot_id)
    slot_no = slot_numbers(resolved).get(slot_id, slot_id)
    if occupant is None:
        return False, "抽样位 %s 当前空缺，名单已变化，请刷新后重试" % slot_no
    if expected_household_id is None or occupant != expected_household_id:
        return False, "抽样位 %s 已换户，名单已变化，请刷新后重试" % slot_no
    return True, ""
