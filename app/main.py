"""Flask app: import survey CSV + JSON jump table, edit per household, export.

Key behaviours:

* Import walks the routing rules for every CSV row and keeps only the values
  on the questions the walk actually reached (stopping at the first blank).
* Every answer is stored under the path (question-id sequence) that reached
  it, so two routes into the same question keep separate answers and the
  earlier route's answers reappear when the walk returns to it.
* A batch can accept one roster CSV describing sampling slots: a slot number,
  a unique main household and optional substitute households.  Reviewers see
  and download a submission list that counts at most one valid questionnaire
  per slot (locked entries first, then qualified mains, then qualified
  substitutes in roster order).
* Reviewer locks are checked against the freshly resolved list, so a lock on a
  slot that has since changed household or become empty is refused.
* Every list view and CSV download is built from one database snapshot of the
  roster, lock state and answers, so all three always reflect the same saved
  state.
* Saves happen in one transaction; if anything fails, both the web page and
  the download keep showing the previously saved answers.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, url_for)

import engine
import importer
import roster
import storage

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.secret_key = os.environ.get("SURVEY_SECRET", "orchard-survey-dev")

DB_PATH = storage.db_path_from_env()


def get_db():
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    return storage.connect(DB_PATH)


@app.errorhandler(sqlite3.Error)
def database_unavailable(error):
    """A storage failure never exposes half-written answers.

    Saves run in a transaction, so the previously committed survey is still
    intact; both the pages and the CSV download keep serving it once storage
    is reachable again.
    """
    app.logger.exception("database error")
    return render_template(
        "error.html",
        title="数据库暂时不可用",
        message="本次操作没有保存，网页和下载都会继续使用上次成功保存的答卷。",
        detail=str(error)), 503


@app.errorhandler(413)
def too_large(error):
    flash("上传文件过大（上限 32 MB）", "error")
    return redirect(url_for("index"))


def load_batch_or_404(connection, batch_id):
    info = storage.get_batch(connection, batch_id)
    if info is None:
        abort(404)
    return info


def load_snapshot_or_404(connection, batch_id):
    snapshot = storage.load_snapshot(connection, batch_id)
    if snapshot is None:
        abort(404)
    return snapshot


def resolve_snapshot(snapshot):
    """Submission list for a storage snapshot (see ``roster.resolve``)."""
    return roster.resolve(
        snapshot["batch"]["definition"], snapshot["answers"],
        snapshot["slots"], snapshot["locks"])


@app.get("/")
def index():
    connection = get_db()
    batches = storage.list_batches(connection)
    connection.close()
    return render_template("index.html", batches=batches)


@app.post("/upload")
def upload():
    csv_file = request.files.get("csv_file")
    json_file = request.files.get("json_file")
    if csv_file is None or not csv_file.filename:
        flash("请选择答卷 CSV 文件", "error")
        return redirect(url_for("index"))
    if json_file is None or not json_file.filename:
        flash("请选择 JSON 跳题表", "error")
        return redirect(url_for("index"))

    try:
        definition, warnings, rows, _, _ = importer.parse(
            csv_file.read(), json_file.read())
    except (importer.ImportError_, engine.DefinitionError) as error:
        flash("导入失败：%s" % error, "error")
        return redirect(url_for("index"))

    answers = importer.to_import_answers(definition, rows)
    name = request.form.get("batch_name", "").strip() or \
        os.path.splitext(os.path.basename(csv_file.filename))[0]

    connection = get_db()
    try:
        batch_id = storage.create_batch(
            connection, name, definition,
            [(row["household_no"], row["row_number"]) for row in rows],
            answers)
    finally:
        connection.close()

    flash("已导入 %d 户，%d 道题" % (len(rows), len(definition)), "success")
    if warnings:
        flash("；".join(dict.fromkeys(warnings)), "warning")
    return redirect(url_for("batch", batch_id=batch_id))


@app.get("/batch/<int:batch_id>")
def batch(batch_id):
    connection = get_db()
    snapshot = load_snapshot_or_404(connection, batch_id)
    connection.close()

    definition = snapshot["batch"]["definition"]
    entries = resolve_snapshot(snapshot)
    households = []
    for household in snapshot["households"]:
        stored = snapshot["answers"].get(household["id"]) or {}
        steps = engine.walk_stored(definition, stored)
        missing = engine.first_missing(steps)
        households.append({
            "id": household["id"],
            "household_no": household["household_no"],
            "route": engine.route(steps),
            "missing": missing["qid"] if missing else None,
            "answered": sum(1 for step in steps if step["answered"]),
        })

    slots = None
    if snapshot["has_roster"]:
        slots = entries
    return render_template("batch.html", batch=snapshot["batch"],
                           households=households, slots=slots,
                           slot_total=len(entries),
                           filled=sum(1 for entry in entries
                                      if entry["household_id"] is not None),
                           question_count=len(definition))


@app.post("/batch/<int:batch_id>/roster")
def upload_roster(batch_id):
    roster_file = request.files.get("roster_file")
    if roster_file is None or not roster_file.filename:
        flash("请选择名册 CSV 文件", "error")
        return redirect(url_for("batch", batch_id=batch_id))

    connection = get_db()
    load_batch_or_404(connection, batch_id)
    if storage.has_roster(connection, batch_id):
        connection.close()
        flash("本批已经导入过名册，每批只能成功导入一份；"
              "如需更换请新建批次", "error")
        return redirect(url_for("batch", batch_id=batch_id))

    households = storage.household_index(connection, batch_id)
    try:
        entries, warnings = importer.parse_roster(
            roster_file.read(), households)
        storage.create_roster(connection, batch_id, entries)
    except (importer.ImportError_, engine.DefinitionError) as error:
        connection.close()
        flash("名册导入失败：%s" % error, "error")
        return redirect(url_for("batch", batch_id=batch_id))
    except sqlite3.IntegrityError:
        connection.close()
        flash("名册导入失败：本批已经导入过名册，每批只能成功导入一份",
              "error")
        return redirect(url_for("batch", batch_id=batch_id))
    connection.close()

    flash("已导入名册：%d 个抽样位" % len(entries), "success")
    if warnings:
        flash("；".join(dict.fromkeys(warnings)), "warning")
    return redirect(url_for("batch", batch_id=batch_id))


def _roster_context(snapshot):
    """Build what the lock controls need, from one snapshot."""
    entries = resolve_snapshot(snapshot)
    household_by_id = snapshot["household_map"]
    # A locked household shown in a different slot still needs its number.
    # Household references are fresh dicts, so this never mutates the roster.
    for entry in entries:
        household = entry["household"]
        if household is not None:
            household.setdefault("household_no", household_by_id.get(
                household["id"], {}).get("household_no", household["id"]))
    return entries


@app.post("/batch/<int:batch_id>/slots/<int:slot_id>/lock")
def lock_slot(batch_id, slot_id):
    expected = request.form.get("household_id", type=int)
    connection = get_db()
    try:
        # Resolve and write inside one write transaction: another reviewer's
        # change between the check and the lock cannot slip in.
        with storage.transaction(connection):
            snapshot = storage.load_snapshot(connection, batch_id)
            if snapshot is None:
                abort(404)
            entries = _roster_context(snapshot)
            slot_numbers = roster.slot_numbers(entries)
            if slot_id not in slot_numbers:
                abort(404)

            ok, message = roster.check_lock(entries, slot_id, expected)
            if ok:
                storage.set_lock(connection, batch_id, slot_id, expected)
    finally:
        connection.close()

    if not ok:
        flash(message, "error")
    else:
        flash("已锁定抽样位 %s 的户 %s" % (
            slot_numbers[slot_id], _household_no(snapshot, expected)),
            "success")
    return redirect(url_for("batch", batch_id=batch_id))


@app.post("/batch/<int:batch_id>/slots/<int:slot_id>/unlock")
def unlock_slot(batch_id, slot_id):
    connection = get_db()
    load_batch_or_404(connection, batch_id)
    removed = storage.delete_lock(connection, batch_id, slot_id)
    connection.close()
    if removed:
        flash("已解锁该抽样位", "success")
    else:
        flash("该抽样位当前没有锁定", "warning")
    return redirect(url_for("batch", batch_id=batch_id))


def _household_no(snapshot, household_id):
    row = snapshot.get("household_map", {}).get(household_id)
    return row["household_no"] if row else str(household_id)


@app.get("/batch/<int:batch_id>/household/<int:household_id>")
def household(batch_id, household_id):
    connection = get_db()
    try:
        with storage.read_transaction(connection):
            info = storage.get_batch(connection, batch_id)
            record = (storage.get_household(connection, batch_id, household_id)
                      if info is not None else None)
            if record is None:
                abort(404)
            stored = storage.load_answers(connection, household_id)
            lock = storage.lock_for_household(
                connection, batch_id, household_id)
    finally:
        connection.close()
    return _render_household(info, record, stored, lock)


@app.post("/batch/<int:batch_id>/household/<int:household_id>")
def save_household(batch_id, household_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    record = storage.get_household(connection, batch_id, household_id)
    if record is None:
        connection.close()
        abort(404)

    submitted = collect_submitted(request.form)
    try:
        # Answers, current lock state and the resulting qualification are read
        # and written in one transaction, so an interleaved lock/unlock cannot
        # let a disqualified edit overwrite a locked household's questionnaire.
        with storage.transaction(connection):
            stored = storage.load_answers(connection, household_id)
            changes, ignored = engine.plan_changes(
                info["definition"], stored, submitted)
            merged = dict(stored)
            for key, value in changes.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value

            # A locked household may still be edited.  The new answers either
            # qualify and are saved as the submitted questionnaire, or the save
            # is refused and the last saved answers remain in force.
            lock = storage.lock_for_household(
                connection, batch_id, household_id)
            refused = None
            if lock is not None:
                ok, reason = roster.qualify(info["definition"], merged)
                if not ok:
                    refused = ("保存失败：该户已锁定，本次答案%s，未保存"
                               "（网页和下载仍显示上次保存的答卷）" % reason)
            if refused is None:
                storage.save_answers(connection, household_id, changes)
    except engine.DefinitionError as error:
        connection.close()
        flash("保存失败：%s（网页和下载仍显示上次保存的答卷）" % error, "error")
        return redirect(url_for("household", batch_id=batch_id,
                                household_id=household_id))
    except Exception as error:  # transaction rolled back; nothing changed
        connection.close()
        app.logger.exception("save failed")
        flash("保存失败：%s（网页和下载仍显示上次保存的答卷）" % error, "error")
        return redirect(url_for("household", batch_id=batch_id,
                                household_id=household_id))

    if refused is not None:
        connection.close()
        flash(refused, "error")
        return redirect(url_for("household", batch_id=batch_id,
                                household_id=household_id))

    current = storage.load_answers(connection, household_id)
    lock = storage.lock_for_household(connection, batch_id, household_id)
    connection.close()
    if ignored:
        flash("题 %s 不在当前路径上，未保存" % "、".join(dict.fromkeys(ignored)),
              "warning")
    saved = sum(1 for value in changes.values() if value is not None)
    flash("已保存 %d 处修改" % saved, "success")
    return _render_household(info, record, current, lock)


def collect_submitted(form):
    """Turn the edit form into ``[(qid, prefix, value)]`` in page order."""
    entries = []
    index = 1
    while ("k_qid_%d" % index) in form:
        qid = form["k_qid_%d" % index]
        prefix = form.get("k_prefix_%d" % index, "[]")
        try:
            parsed = json.loads(prefix)
            prefix = tuple(str(item) for item in parsed)
        except (TypeError, ValueError):
            prefix = ()
        entries.append((qid, prefix, form.get("v_%d" % index, "")))
        index += 1
    return entries


def _render_household(info, record, stored, lock=None):
    definition = info["definition"]
    steps = engine.walk_stored(definition, stored)
    return render_template(
        "household.html",
        batch=info,
        household=record,
        steps=steps,
        missing=engine.first_missing(steps),
        others=engine.other_path_answers(definition, stored, steps),
        lock=lock,
    )


@app.get("/batch/<int:batch_id>/export.csv")
def export_csv(batch_id):
    """Download every household's effective answers (analysis CSV)."""
    connection = get_db()
    snapshot = load_snapshot_or_404(connection, batch_id)
    connection.close()
    definition = snapshot["batch"]["definition"]

    qids = [question["id"] for question in definition]
    titles = {question["id"]: question["title"] for question in definition}

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["户号", "当前路径", "已答题数"]
    header += ["%s %s" % (qid, titles[qid]) for qid in qids]
    header += ["首个漏答题", "漏答题干"]
    writer.writerow(header)

    for household in snapshot["households"]:
        stored = snapshot["answers"].get(household["id"]) or {}
        steps = engine.walk_stored(definition, stored)
        values = roster.effective_answers(steps)
        missing = engine.first_missing(steps)
        row = [household["household_no"], " → ".join(engine.route(steps)),
               len(values)]
        row += [values.get(qid, "") for qid in qids]
        row += [missing["qid"] if missing else "",
                missing["title"] if missing else ""]
        writer.writerow(row)

    data = output.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), mimetype="text/csv",
                     as_attachment=True,
                     download_name="survey_%d_export.csv" % batch_id)


@app.get("/batch/<int:batch_id>/slots.csv")
def export_slots(batch_id):
    """Download the resolved submission list.

    One row per sampling slot; empty slots carry the reason instead of a
    household and its answers.  Built from one storage snapshot, exactly like
    the on-screen list.
    """
    connection = get_db()
    snapshot = load_snapshot_or_404(connection, batch_id)
    connection.close()

    definition = snapshot["batch"]["definition"]
    entries = _roster_context(snapshot)

    qids = [question["id"] for question in definition]
    titles = {question["id"]: question["title"] for question in definition}

    output = io.StringIO()
    writer = csv.writer(output)
    header = ["抽样位", "入样户号", "入样方式", "锁定", "当前路径", "已答题数"]
    header += ["%s %s" % (qid, titles[qid]) for qid in qids]
    header += ["首个漏答题", "漏答题干", "空缺原因"]
    writer.writerow(header)

    for entry in entries:
        household_no = entry["household"]["household_no"] \
            if entry["household"] else ""
        values = entry["answers"]
        row = [entry["slot_no"], household_no, entry["status_label"],
               "是" if entry.get("is_locked") else "否",
               " → ".join(entry["route"]), entry["answered"]]
        row += [values.get(qid, "") for qid in qids]
        row += [entry["missing"] or "", entry["missing_title"] or "",
                entry["reason"]]
        writer.writerow(row)

    data = output.getvalue().encode("utf-8-sig")
    return send_file(io.BytesIO(data), mimetype="text/csv",
                     as_attachment=True,
                     download_name="survey_%d_slots.csv" % batch_id)


@app.get("/batch/<int:batch_id>/definition.json")
def export_definition(batch_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    connection.close()
    return info["definition"]


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
            debug=os.environ.get("SURVEY_DEBUG") == "1")
