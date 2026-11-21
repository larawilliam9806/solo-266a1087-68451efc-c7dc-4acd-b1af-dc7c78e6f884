import csv
import html
import io
import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DB", str(tmp_path / "survey.sqlite3"))
    monkeypatch.setenv("SURVEY_SECRET", "test")
    for module in ("storage", "engine", "importer", "main"):
        sys.modules.pop(module, None)
    import main
    main.app.config.update(TESTING=True)
    with main.app.test_client() as test_client:
        yield test_client


CSV_TEXT = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）
H1,自购,喷药机,2,无,,,12000
H2,租赁,喷药机,2,有,按年整租,3,8000
H3,自购,,,,,,9000
H4,租赁,,,有,按季短租,,,
"""

JUMPS = {
    "order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
    "titles": {
        "Q1": "设备来源", "Q2": "自购设备类型", "Q3": "自购台数",
        "Q4": "是否享受购置补贴", "Q5": "租赁方式", "Q6": "租赁台数",
        "Q7": "年费用合计（元）",
    },
    "types": {"Q1": "single", "Q2": "single", "Q4": "single",
              "Q5": "single", "Q3": "number", "Q6": "number",
              "Q7": "number"},
    "options": {"Q1": ["自购", "租赁"], "Q2": ["喷药机", "采摘平台"],
                "Q4": ["有", "无"], "Q5": ["按年整租", "按季短租"]},
    "jumps": {"Q1": {"自购": "Q2", "租赁": "Q5"},
              "Q2": {"采摘平台": "Q7"},
              "Q4": {"有": "Q7", "无": "Q7"}},
}


def upload(client, csv_text=CSV_TEXT, jumps=JUMPS):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(jumps).encode("utf-8")),
                      "jumps.json"),
        "batch_name": "测试批次",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    return response


ROSTER_TEXT = """抽样位,主户号,候补户号
S1,H1,H3
S2,H2,
S3,H4,
"""


def upload_roster(client, batch_id=1, roster_text=ROSTER_TEXT):
    return client.post("/batch/%d/roster" % batch_id, data={
        "roster_file": (io.BytesIO(roster_text.encode("utf-8")), "roster.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)


def export_rows(client, batch_id=1):
    text = client.get("/batch/%d/slots.csv" % batch_id).data.decode(
        "utf-8-sig")
    lines = text.strip().splitlines()
    header = next(csv.reader([lines[0]]))
    return header, [dict(zip(header, row))
                    for row in csv.reader(lines[1:])]


def export_answers(client, batch_id=1):
    """Interim helper: effective answers keyed by household number.

    Households are only present when their slot is entered, so this is used
    for the slot-list downloads after uploading a roster.
    """
    _, rows = export_rows(client, batch_id)
    return {row["入样户号"]: row for row in rows if row["入样户号"]}


def download_all_answers(client, batch_id=1):
    """Effective answers for every household (from the editor pages)."""
    out = {}
    body = client.get("/batch/%d" % batch_id).get_data(as_text=True)
    for match in re.finditer(
            r'href="(/batch/%d/household/(\d+))"' % batch_id, body):
        page = client.get(match.group(1)).get_data(as_text=True)
        household_no = re.search(r"<h1>户号 ([^<]+)</h1>", page).group(1)
        out[household_no] = values(page)
    return out


def form_fields(body):
    """(qid, prefix, value) for every question rendered on the edit page."""
    entries = []
    pattern = re.compile(
        r'name="k_qid_(\d+)"\s+value="([^"]*)">\s*'
        r'<input\s+type="hidden"\s+name="k_prefix_\1"\s+value="([^"]*)"',
        re.S)
    for index, qid, prefix in pattern.findall(body):
        tags = re.findall(r'<input[^>]*name="v_%s"[^>]*>' % index, body, re.S)
        chosen = next((tag for tag in tags if "checked" in tag),
                      tags[0] if tags else "")
        value_match = re.search(r'data-value="([^"]*)"', chosen)
        entries.append((html.unescape(qid), json.loads(html.unescape(prefix)),
                        html.unescape(value_match.group(1)) if value_match
                        else ""))
    return entries


def values(body):
    return {(qid, tuple(prefix)): value
            for qid, prefix, value in form_fields(body)}


def submit(client, entries, household=2):
    data = {}
    for position, (qid, prefix, value) in enumerate(entries, start=1):
        data["k_qid_%d" % position] = qid
        data["k_prefix_%d" % position] = json.dumps(list(prefix))
        data["v_%d" % position] = value
    return client.post("/batch/1/household/%d" % household, data=data,
                       follow_redirects=True).get_data(as_text=True)


def replace(entries, qid, prefix, value):
    return [(q, tuple(p), value if (q == qid and tuple(p) == tuple(prefix))
             else v) for q, p, v in entries]


SELF = ("Q1", "Q2", "Q3", "Q4")
RENT = ("Q1", "Q5", "Q6")


def test_import_and_list(client):
    body = upload(client).get_data(as_text=True)
    assert "测试批次" in body
    for household in ("H1", "H2", "H3", "H4"):
        assert household in body


def test_self_buy_path_and_shared_cost(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/1").get_data(as_text=True))
    assert [qid for qid, _, _ in fields] == ["Q1", "Q2", "Q3", "Q4", "Q7"]
    assert dict((q, v) for q, _, v in fields)["Q7"] == "12000"


def test_rental_path_skips_self_buy_questions(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    assert [qid for qid, _, _ in fields] == ["Q1", "Q5", "Q6", "Q7"]
    assert dict((q, v) for q, _, v in fields)["Q7"] == "8000"


def test_import_ignores_unreached_answers(client):
    upload(client)
    stored = download_all_answers(client)
    # H2 is a rental household; its self-buy columns must not be imported.
    h2 = stored["H2"]
    assert ("Q2", ("Q1",)) not in h2
    assert ("Q3", ("Q1", "Q2")) not in h2
    assert ("Q4", ("Q1", "Q2", "Q3")) not in h2
    assert h2[("Q1", ())] == "租赁"
    assert h2[("Q5", ("Q1",))] == "按年整租"


def test_switch_branch_saves_separate_answers(client):
    upload(client)
    # H2 starts on the rental route.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "喷药机",
        "k_qid_3": "Q3", "k_prefix_3": '["Q1", "Q2"]', "v_3": "4",
        "k_qid_4": "Q4", "k_prefix_4": '["Q1", "Q2", "Q3"]', "v_4": "无",
        "k_qid_5": "Q7", "k_prefix_5": '["Q1", "Q2", "Q3", "Q4"]',
        "v_5": "15000",
    }, follow_redirects=True).get_data(as_text=True)

    assert "其他路径上保存的答案" in body
    current = values(body)
    assert current[("Q1", ())] == "自购"
    assert current[("Q7", SELF)] == "15000"
    # The rental path's answers are still stored.
    assert current[("Q5", ("Q1",))] if ("Q5", ("Q1",)) in current else True
    assert "按年整租" in body and "8000" in body

    # Switch back to rental: its own answers return, including its cost.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert current[("Q5", ("Q1",))] == "按年整租"
    assert current[("Q6", ("Q1", "Q5"))] == "3"
    assert current[("Q7", RENT)] == "8000"


def test_shared_cost_question_is_per_path(client):
    upload(client)
    # Change the rental household's cost.
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q7", RENT, "9000"))
    assert values(body)[("Q7", RENT)] == "9000"

    # Move to self-buy: the cost shows the self-buy value, not 9000.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "喷药机",
        "k_qid_3": "Q3", "k_prefix_3": '["Q1", "Q2"]', "v_3": "4",
        "k_qid_4": "Q4", "k_prefix_4": '["Q1", "Q2", "Q3"]', "v_4": "无",
        "k_qid_5": "Q7", "k_prefix_5": '["Q1", "Q2", "Q3", "Q4"]',
        "v_5": "",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert current[("Q7", SELF)] == ""
    assert "9000" in body  # still visible as another path's answer

    # Return to rental: 9000 is intact.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True).get_data(as_text=True)
    assert values(body)[("Q7", RENT)] == "9000"


def test_stale_fields_of_old_route_are_not_saved(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    # Keep every rental field but change the route: they are now stale.
    body = submit(client, [("Q1", (), "自购")] + fields[1:])
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2"]
    assert current[("Q2", ("Q1",))] == ""


def test_invalid_single_option_rejected_and_previous_answers_kept(client):
    upload(client)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "丢弃",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q1", ())] == "租赁"
    assert values(page)[("Q7", RENT)] == "8000"


def test_export_lists_effective_answers_and_first_missing(client):
    upload(client)
    upload_roster(client)
    header, rows = export_rows(client)
    assert header[0] == "抽样位"
    assert header[1] == "入样户号"
    assert "当前路径" in header
    assert "Q7 年费用合计（元）" in header
    assert header[-1] == "空缺原因"

    h2 = next(row for row in rows if row["入样户号"] == "H2")
    assert h2["首个漏答题"] == ""
    assert h2["入样方式"] == "主户"
    assert h2["Q1 设备来源"] == "租赁"
    assert h2["Q5 租赁方式"] == "按年整租"
    assert h2["Q7 年费用合计（元）"] == "8000"
    assert h2["当前路径"] == "Q1 → Q5 → Q6 → Q7"


def test_missing_answer_is_reported(client):
    upload(client)
    upload_roster(client)
    stored = download_all_answers(client)
    h3 = stored["H3"]
    assert h3[("Q1", ())] == "自购"
    # Nothing after the first blank is imported, even though Q7 was filled.
    assert ("Q7", ("Q1", "Q2", "Q3", "Q4")) not in h3

    h4 = stored["H4"]
    assert h4[("Q5", ("Q1",))] == "按季短租"

    _, rows = export_rows(client)
    h3_slot = next(row for row in rows if row["抽样位"] == "S3")
    assert h3_slot["入样户号"] == ""
    assert "未答完" in h3_slot["空缺原因"]
    # H3 is registered as a candidate at S1 but has not answered past Q1.
    s1 = next(row for row in rows if row["抽样位"] == "S1")
    assert s1["入样户号"] == "H1"
    h4_slot = next(row for row in rows if row["抽样位"] == "S3")
    assert h4_slot["入样户号"] == ""


def test_clearing_answer_stops_path(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q5", ("Q1",), ""))
    assert [qid for qid, _, _ in form_fields(body)] == ["Q1", "Q5"]


def test_jump_directly_to_shared_cost_question(client):
    upload(client)
    # 自购 + 采摘平台 jumps straight to Q7, skipping Q3/Q4.
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "采摘平台",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2", "Q7"]
    assert current[("Q7", ("Q1", "Q2"))] == ""


def test_save_failure_keeps_last_saved_answers(client, monkeypatch):
    upload(client)
    import main
    original = main.storage.save_answers
    monkeypatch.setattr(main.storage, "save_answers",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("disk full")))
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
    }, follow_redirects=True)
    assert "disk full" in response.get_data(as_text=True)

    stored = download_all_answers(client)
    assert stored["H2"][("Q1", ())] == "租赁"
    assert stored["H2"][("Q7", RENT)] == "8000"
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q1", ())] == "租赁"
    assert values(page)[("Q7", RENT)] == "8000"
    monkeypatch.setattr(main.storage, "save_answers", original)


def test_database_error_keeps_download_on_last_saved(client, monkeypatch):
    upload(client)
    import main

    def explode(connection, household_id, changes):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main.storage, "save_answers", explode)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q1", ())] == "租赁"
    assert values(page)[("Q7", RENT)] == "8000"


def test_batch_route_returns_404_for_missing_batch(client):
    assert client.get("/batch/99").status_code == 404
    assert client.get("/batch/1/household/99").status_code == 404


def test_import_rejects_bad_json(client):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(CSV_TEXT.encode()), "answers.csv"),
        "json_file": (io.BytesIO(b"{not json"), "jumps.json"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "导入失败" in response.get_data(as_text=True)


def test_import_rejects_bad_jump_target(client):
    jumps = dict(JUMPS, jumps={"Q1": {"租赁": "Q99"}})
    response = upload(client, jumps=jumps)
    assert "导入失败" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Roster and submission slots


def slot_id_for(body, slot_no):
    pattern = re.compile(
        r'data-slot-id="(\d+)">%s</td>' % re.escape(slot_no))
    return int(pattern.search(body).group(1))


def lock(client, slot_id, household_id):
    return client.post("/batch/1/slots/%d/lock" % slot_id,
                       data={"household_id": household_id},
                       follow_redirects=True).get_data(as_text=True)


def unlock(client, slot_id):
    return client.post("/batch/1/slots/%d/unlock" % slot_id,
                       follow_redirects=True).get_data(as_text=True)


def test_roster_resolves_locked_mains_and_substitutes(client):
    upload(client)
    body = upload_roster(client).get_data(as_text=True)
    assert "已导入名册" in body

    _, rows = export_rows(client)
    by_slot = {row["抽样位"]: row for row in rows}
    assert by_slot["S1"]["入样户号"] == "H1"
    assert by_slot["S1"]["入样方式"] == "主户"
    assert by_slot["S2"]["入样户号"] == "H2"
    assert by_slot["S3"]["入样户号"] == ""
    assert "未答完" in by_slot["S3"]["空缺原因"]


def test_roster_imports_once_per_batch(client):
    upload(client)
    upload_roster(client)
    body = upload_roster(client).get_data(as_text=True)
    assert "每批只能成功导入一份" in body


def test_invalid_roster_does_not_consume_the_quota(client):
    upload(client)
    bad = "抽样位,主户号,候补户号\nS1,H9,\n"
    body = upload_roster(client, roster_text=bad).get_data(as_text=True)
    assert "不在本批" in body
    body = upload_roster(client).get_data(as_text=True)
    assert "已导入名册" in body


def test_lock_and_unlock_a_slot(client):
    upload(client)
    upload_roster(client)
    body = client.get("/batch/1").get_data(as_text=True)
    s1 = slot_id_for(body, "S1")
    body = lock(client, s1, 1)
    assert "已锁定抽样位 S1 的户 H1" in body
    assert "解锁" in body

    body = unlock(client, s1)
    assert "已解锁" in body


def test_lock_refused_when_slot_changed_or_empty(client):
    upload(client)
    upload_roster(client)
    body = client.get("/batch/1").get_data(as_text=True)
    s1 = slot_id_for(body, "S1")
    s3 = slot_id_for(body, "S3")

    body = lock(client, s1, 2)      # S1 currently holds H1, not H2
    assert "已换户" in body and "刷新" in body

    body = lock(client, s3, 4)      # S3 is empty
    assert "空缺" in body and "刷新" in body


def test_locked_substitute_not_displaced_when_main_finishes(client):
    upload(client)
    # H3 is the main but has only answered Q1; H2 is a complete candidate.
    roster_text = "抽样位,主户号,候补户号\nS1,H3,H2\n"
    upload_roster(client, roster_text=roster_text)
    _, rows = export_rows(client)
    assert rows[0]["入样户号"] == "H2"
    assert rows[0]["入样方式"] == "候补"

    body = client.get("/batch/1").get_data(as_text=True)
    s1 = slot_id_for(body, "S1")
    body = lock(client, s1, 2)
    assert "已锁定" in body

    # The main household H3 now answers the whole self-buy route.
    body = submit(client, [
        ("Q1", (), "自购"), ("Q2", ("Q1",), "喷药机"),
        ("Q3", ("Q1", "Q2"), "2"), ("Q4", ("Q1", "Q2", "Q3"), "无"),
        ("Q7", SELF, "5000"),
    ], household=3)
    assert "已保存" in body

    _, rows = export_rows(client)
    assert rows[0]["入样户号"] == "H2"        # the lock still wins
    assert rows[0]["入样方式"] == "已锁定"
    assert rows[0]["Q7 年费用合计（元）"] == "8000"


def test_locked_household_can_resubmit_qualified_answers(client):
    upload(client)
    upload_roster(client)
    body = client.get("/batch/1").get_data(as_text=True)
    s2 = slot_id_for(body, "S2")
    lock(client, s2, 2)

    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    assert "该户已锁定在抽样位 S2" in client.get(
        "/batch/1/household/2").get_data(as_text=True)
    body = submit(client, replace(fields, "Q7", RENT, "9500"))
    assert "已保存" in body
    assert values(body)[("Q7", RENT)] == "9500"

    _, rows = export_rows(client)
    by_slot = {row["抽样位"]: row for row in rows}
    assert by_slot["S2"]["Q7 年费用合计（元）"] == "9500"


def test_locked_household_disqualified_edit_is_refused(client):
    upload(client)
    upload_roster(client)
    body = client.get("/batch/1").get_data(as_text=True)
    s2 = slot_id_for(body, "S2")
    lock(client, s2, 2)

    # Clearing the rental method stops the path before Q7: disqualified.
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q5", ("Q1",), ""))
    assert "已锁定" in body and "未保存" in body

    # The previously saved questionnaire is still in force.
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q5", ("Q1",))] == "按年整租"
    assert values(page)[("Q7", RENT)] == "8000"
    _, rows = export_rows(client)
    by_slot = {row["抽样位"]: row for row in rows}
    assert by_slot["S2"]["入样户号"] == "H2"
    assert by_slot["S2"]["Q7 年费用合计（元）"] == "8000"


def test_unlocked_household_may_be_saved_incomplete(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q5", ("Q1",), ""))
    assert "已保存" in body
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q5", ("Q1",))] == ""


def test_list_and_download_use_the_same_saved_state(client):
    upload(client)
    upload_roster(client)
    body = client.get("/batch/1").get_data(as_text=True)
    s1 = slot_id_for(body, "S1")
    lock(client, s1, 1)

    page = client.get("/batch/1").get_data(as_text=True)
    _, rows = export_rows(client)
    by_slot = {row["抽样位"]: row for row in rows}
    # Both the page and the download show the lock on S1.
    assert "已锁定" in page
    assert by_slot["S1"]["入样方式"] == "已锁定"
    for slot_no in ("S1", "S2", "S3"):
        assert slot_no in page and slot_no in by_slot
