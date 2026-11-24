"""自测：算法规则 + Flask 端到端主流程。

以隔离模式运行：python -I tests/selftest.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db  # noqa: E402
from app.engine import evaluate_segment, solve, split_segments  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def d(order_no, start, end, shares):
    return {"id": abs(hash(order_no)) % 100000, "order_no": order_no,
            "start_date": start, "end_date": end, "shares": shares,
            "version": 1}


# ---------------- 算法层 ----------------

def test_engine() -> None:
    print("== 算法层 ==")

    # 1. 同权利人同日同值只计一次，合计 10000 可通过
    r = evaluate_segment(
        [d("A", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000}),
         d("B", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000})],
        "2026-01-01", "2026-02-01",
    )
    check("同值去重后合计 10000", r["ok"] and r["shares"] == {"甲": 6000, "乙": 4000})

    # 2. 同一权利人异值 -> 无法供数
    r = evaluate_segment(
        [d("A", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000}),
         d("B", "2026-01-01", "2026-02-01", {"甲": 5000, "乙": 5000})],
        "2026-01-01", "2026-02-01",
    )
    check("同段异值判失败", (not r["ok"]) and "异值" in r["reason"])

    # 3. 去重后合计不等于 10000
    r = evaluate_segment(
        [d("A", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 3000})],
        "2026-01-01", "2026-02-01",
    )
    check("合计 9000 判失败", not r["ok"] and "10000" in r["reason"])

    # 4. 无覆盖
    r = evaluate_segment([], "2026-01-01", "2026-02-01")
    check("无覆盖判失败", not r["ok"] and "覆盖" in r["reason"])

    # 5. 最少退单：3 单中 1 单异值，应恰好退回该单
    decls = [
        d("D001", "2026-01-01", "2026-04-01", {"甲": 6000, "乙": 4000}),
        d("D002", "2026-04-01", "2026-07-01", {"甲": 7000, "乙": 3000}),
        d("D003", "2026-03-01", "2026-04-01", {"甲": 5000, "乙": 5000}),
        d("D004", "2026-02-01", "2026-03-01", {"甲": 6000, "乙": 4000}),
    ]
    sol = solve(decls, "2026-01-01", "2026-07-01")
    check("最少退单恰好 1 单 D003",
          sol.feasible and sol.returned == ["D003"], str(sol.returned))
    check("保留 3 单", sol.retain == ["D001", "D002", "D004"], str(sol.retain))
    check("分段按区间切分（无逐日展开）", len(sol.segments) == 4,
          f"segments={len(sol.segments)}")
    check("每段合计 10000",
          all(sum(s["shares"].values()) == 10000 for s in sol.segments))

    # 6. 覆盖空档 -> 无解
    gap = [
        d("G1", "2026-01-01", "2026-04-01", {"甲": 8000, "乙": 2000}),
        d("G2", "2026-05-01", "2026-07-01", {"甲": 5500, "乙": 4500}),
    ]
    sol = solve(gap, "2026-01-01", "2026-07-01")
    check("空档场景无解", not sol.feasible)
    check("无解诊断含无覆盖段", any("覆盖" in (s["reason"] or "") for s in sol.segments))

    # 7. 必须退单且多个同规模方案时，单号字典序最小的保留集
    tie = [
        d("X1", "2026-01-01", "2026-02-01", {"甲": 6000, "乙": 4000}),
        d("X2", "2026-01-01", "2026-02-01", {"甲": 7000, "乙": 3000}),
        d("X3", "2026-01-01", "2026-02-01", {"甲": 8000, "乙": 2000}),
    ]
    # 三单两两异值，最多保留一单，应保留字典序最小的 X1，退回 X2、X3
    sol = solve(tie, "2026-01-01", "2026-02-01")
    check("平手取字典序最小保留集", sol.feasible and sol.retain == ["X1"]
          and sol.returned == ["X2", "X3"], str((sol.retain, sol.returned)))

    # 8. 部分相交的申报也会产生边界分段
    segs = split_segments("2026-01-01", "2026-07-01",
                          [d("P", "2025-12-01", "2026-02-01", {"甲": 10000})])
    check("越界相交申报按结算期截断", segs[0] == ("2026-01-01", "2026-02-01"))


# ---------------- Web 端到端 ----------------

def make_app(tmpdir):
    app = create_app({"TESTING": True, "DATABASE": os.path.join(tmpdir, "test.db")})
    with app.app_context():
        db.init_db()
        from app.seed import seed_demo_data
        seed_demo_data()
    return app


def form_post(client, path, data):
    return client.post(path, data=data, follow_redirects=False)


def test_web() -> None:
    print("== Web 端到端 ==")
    tmpdir = tempfile.mkdtemp()
    app = make_app(tmpdir)
    client = app.test_client()

    # 首页
    resp = client.get("/")
    check("首页 200 且列出演示录音", resp.status_code == 200 and "深夜电台" in resp.text)

    with app.app_context():
        recordings = {r["title"]: r["id"] for r in db.list_recordings()}
        rid1 = recordings["深夜电台（可核定）"]
        rid2 = recordings["清晨钟声（无解·回退上一版）"]
        rid3 = recordings["午后回廊（版本递增演示）"]

    # 录音详情
    resp = client.get(f"/recordings/{rid1}")
    check("录音详情含 4 单演示申报", resp.status_code == 200 and "D003" in resp.text)

    # 预览 R1
    resp = form_post(client, f"/recordings/{rid1}/preview",
                     {"period_start": "2026-01-01", "period_end": "2026-07-01"})
    check("预览提交 302", resp.status_code == 302)
    token = resp.headers["Location"].rsplit("/", 1)[-1]
    resp = client.get(f"/previews/{token}")
    check("预览显示最少退单 D003", "D003" in resp.text and "退回 1 单" in resp.text)
    check("预览显示分段份额", "2026-04-01" in resp.text)

    # 确认发布
    resp = form_post(client, f"/previews/{token}/confirm", {})
    check("确认发布 302 到版本页", resp.status_code == 302 and "/versions/" in resp.headers["Location"])
    version_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    resp = client.get(f"/versions/{version_id}")
    check("版本页含保留/退单与份额", "D003" in resp.text and "10000" in resp.text)
    resp = client.get(f"/api/versions/{version_id}")
    data = resp.get_json()
    check("JSON 取数结构完整",
          resp.status_code == 200 and data["returned_orders"] == ["D003"]
          and all(sum(s["shares"].values()) == 10000 for s in data["segments"]))

    # 原申报仍留档
    with app.app_context():
        check("发布后原申报保留", len(db.list_declarations(rid1)) == 4)

    # 预览期间更正申报 -> 旧预览签名失效，确认被拒
    with app.app_context():
        d001 = db.find_declaration(rid1, "D001")
        edit_id = d001["id"]
    form_post(client, f"/declarations/{edit_id}/edit", {
        "start_date": "2026-01-01", "end_date": "2026-04-01",
        "holder_name": ["甲", "乙"], "holder_value": ["6500", "3500"],
    })
    resp = form_post(client, f"/previews/{token}/confirm", {})
    check("资料不一致时拒绝确认并重算提示",
          resp.status_code == 302)
    with client.session_transaction() as sess:
        flashes = [f[1] for f in sess.get("_flashes", [])]
    check("重算提示文案正确", any("不一致" in m for m in flashes), str(flashes))
    with app.app_context():
        check("更正后版本号递增且旧版留档",
              db.get_declaration(edit_id)["version"] == 2
              and len(db.list_history(edit_id)) == 1)
    resp = client.get(f"/previews/{token}")
    check("预览页标记资料不一致", "资料不一致" in resp.text)

    # 新增页面不允许用同号覆盖已有申报
    resp = form_post(client, f"/recordings/{rid1}/declarations/new", {
        "order_no": "D001", "start_date": "2026-01-01", "end_date": "2026-02-01",
        "holder_name": ["甲", "乙"], "holder_value": ["5000", "5000"],
    })
    check("新增页面遇同号只提示已存在",
          resp.status_code == 200 and "已存在" in resp.text)
    with app.app_context():
        after = db.get_declaration(edit_id)
        check("同号新增未改动原申报与留档",
              after["version"] == 2 and len(db.list_history(edit_id)) == 1
              and after["shares"] == {"甲": 6500, "乙": 3500}, str(after["shares"]))

    # 重新预览并发布 v2
    resp = form_post(client, f"/recordings/{rid1}/preview",
                     {"period_start": "2026-01-01", "period_end": "2026-07-01"})
    token2 = resp.headers["Location"].rsplit("/", 1)[-1]
    resp = form_post(client, f"/previews/{token2}/confirm", {})
    vid2 = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        check("同结算期版本号递增为 v2",
              db.get_version(vid2)["version_no"] == 2)

    # 模拟写入失败：不产生新版，上一版仍可取
    with app.app_context():
        before = len(db.list_versions(rid1))
    resp = form_post(client, f"/previews/{token2}/confirm", {"simulate_fail": "1"})
    check("写入失败 302 回预览页", resp.status_code == 302 and "/previews/" in resp.headers["Location"])
    with client.session_transaction() as sess:
        flashes = [f[1] for f in sess.get("_flashes", [])]
    check("写入失败提示回退上一版", any("写入失败" in m for m in flashes))
    with app.app_context():
        check("失败后未新增版本", len(db.list_versions(rid1)) == before)
    resp = client.get(f"/versions/{version_id}")
    check("上一版仍可正常取数", resp.status_code == 200 and "D003" in resp.text)

    # 无解录音
    resp = form_post(client, f"/recordings/{rid2}/preview",
                     {"period_start": "2026-01-01", "period_end": "2026-07-01"})
    token3 = resp.headers["Location"].rsplit("/", 1)[-1]
    resp = client.get(f"/previews/{token3}")
    check("无解预览展示原因与分段诊断", "无解" in resp.text and "无申报覆盖" in resp.text)
    resp = form_post(client, f"/previews/{token3}/confirm", {})
    check("无解禁止发布", resp.status_code == 302)
    with app.app_context():
        versions_r2 = db.list_versions(rid2)
        check("预置历史版可取数", len(versions_r2) >= 1)
        old_vid = versions_r2[0]["id"]
    check("上一版 JSON 可取", client.get(f"/api/versions/{old_vid}").status_code == 200)

    # 录入校验（非法输入 200 重渲染，flash 已在响应页面中展示）
    resp = form_post(client, f"/recordings/{rid3}/declarations/new", {
        "order_no": "BAD1", "start_date": "2026-01-01", "end_date": "2026-01-01",
        "holder_name": ["丙"], "holder_value": ["10000"],
    })
    check("起日=止日被拒", resp.status_code == 200 and "起日早于止日" in resp.text)

    resp = form_post(client, f"/recordings/{rid3}/declarations/new", {
        "order_no": "BAD2", "start_date": "2026-01-01", "end_date": "2026-02-01",
        "holder_name": ["丙"], "holder_value": ["9999"],
    })
    check("单内合计非 10000 被拒", resp.status_code == 200 and "10000" in resp.text)

    # 每首最多 12 单：补齐到上限再试第 13 单
    with app.app_context():
        existing = len(db.list_declarations(rid3))
    for i in range(existing, 12):
        form_post(client, f"/recordings/{rid3}/declarations/new", {
            "order_no": f"L{i:02d}", "start_date": "2026-01-01", "end_date": "2026-02-01",
            "holder_name": ["丙"], "holder_value": ["10000"],
        })
    resp = form_post(client, f"/recordings/{rid3}/declarations/new", {
        "order_no": "L13", "start_date": "2026-01-01", "end_date": "2026-02-01",
        "holder_name": ["丙"], "holder_value": ["10000"],
    })
    check("第 13 单被拒（上限 12）", resp.status_code == 200 and "12" in resp.text)

    # 版本列表页
    resp = client.get("/versions")
    check("核定版列表页 200", resp.status_code == 200 and "清晨钟声" in resp.text)


# ---------------- 并发与一致性 ----------------

def run_concurrently(app, tasks):
    """让多个任务尽量同时进入服务端逻辑，收集结果或异常。"""
    barrier = threading.Barrier(len(tasks))
    results: list = [None] * len(tasks)
    errors: list = [None] * len(tasks)

    def worker(i, fn):
        with app.app_context():
            try:
                barrier.wait(timeout=10)
                results[i] = fn(i)
            except BaseException as exc:  # noqa: BLE001 - 自测需要收集所有异常
                errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i, fn))
               for i, fn in enumerate(tasks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


def test_concurrency() -> None:
    print("== 并发与一致性 ==")
    tmpdir = tempfile.mkdtemp()
    app = make_app(tmpdir)

    with app.app_context():
        rec = db.get_or_create_recording("并发台")
        decl = db.create_declaration(
            rec, "C001", "2026-01-01", "2026-03-01", {"甲": 6000, "乙": 4000}
        )
        decl_id = decl["id"]
        rid = rec

    # 1) 两次并发更正：现行 v3，留档必须恰好是 v1、v2 且内容对应各自被替换的版本
    edits = [
        ("2026-01-01", "2026-03-01", {"甲": 6100, "乙": 3900}),
        ("2026-01-01", "2026-03-01", {"甲": 6200, "乙": 3800}),
    ]

    def do_edit(i):
        db.update_declaration(decl_id, *edits[i])

    _results, errors = run_concurrently(app, [do_edit, do_edit])
    check("两次并发更正均成功", all(e is None for e in errors), str(errors))
    with app.app_context():
        current, history = db.get_declaration_with_history(decl_id)
    check("并发两次更正后现行版号为 3", current["version"] == 3, str(current["version"]))
    check("并发两次更正后留档恰为两条", len(history) == 2, str(len(history)))
    check("留档版号依次为 v1、v2",
          [h["version"] for h in history] == [1, 2],
          str([h["version"] for h in history]))
    check("留档保留了实际被替换的起止日与份额",
          history[0]["shares"] == {"甲": 6000, "乙": 4000}
          and history[0]["start_date"] == "2026-01-01"
          and history[1]["shares"] in (edits[0][2], edits[1][2]),
          str([(h["version"], h["shares"]) for h in history]))
    # 第一次更正的内容被第二次替换后进入留档，现行版即第二次提交的内容；
    # 两者合起来必须正好是两次提交，证明没有丢失或重复入账。
    check("现行版与第二条留档合起来恰为两次提交",
          {json.dumps(history[1]["shares"], sort_keys=True),
           json.dumps(current["shares"], sort_keys=True)}
          == {json.dumps(edits[0][2], sort_keys=True),
              json.dumps(edits[1][2], sort_keys=True)},
          str((history[1]["shares"], current["shares"])))

    # 2) 两个窗口同时给同一首录音新录同号申报：只入账一份，另一份提示已存在
    def do_create(i):
        try:
            return ("ok", db.create_declaration(
                rid, "C002", "2026-01-01", "2026-02-01",
                {"甲": 5000 + i, "乙": 5000 - i},
            ))
        except db.DuplicateOrderError:
            return ("dup", None)

    results, errors = run_concurrently(app, [do_create, do_create])
    check("并发同号新增无未捕获异常", all(e is None for e in errors), str(errors))
    outcomes = sorted(r[0] for r in results)
    check("并发同号新增恰好一份入账、一份判重",
          outcomes == ["dup", "ok"], str(outcomes))
    with app.app_context():
        rows = [d for d in db.list_declarations(rid) if d["order_no"] == "C002"]
        check("库中同号申报只有一条", len(rows) == 1, str(len(rows)))
        check("入账的一份保持首份内容且未被覆盖",
              rows[0]["version"] == 1 and not db.list_history(rows[0]["id"]))

    # 3) 并发同号新增走 Web：后到的一份显示“已存在”，原申报与留档照常可查
    app2 = make_app(tempfile.mkdtemp())
    client = app2.test_client()
    with app2.app_context():
        rid2 = db.get_or_create_recording("并发网页台")
    payload = {"order_no": "W001", "start_date": "2026-01-01",
               "end_date": "2026-02-01",
               "holder_name": "甲", "holder_value": "10000"}
    bodies = [None, None]
    statuses = [None, None]
    barrier = threading.Barrier(2)

    def hit(i):
        with app2.test_client() as c:
            barrier.wait(timeout=10)
            resp = c.post(f"/recordings/{rid2}/declarations/new", data=payload)
            statuses[i] = resp.status_code
            bodies[i] = resp.get_data(as_text=True)

    web_threads = [threading.Thread(target=hit, args=(i,)) for i in range(2)]
    for t in web_threads:
        t.start()
    for t in web_threads:
        t.join(timeout=60)
    dup_bodies = [b for b in bodies if b and "已存在" in b]
    check("并发网页同号新增：一份提示单号已存在", len(dup_bodies) == 1,
          str(statuses))
    with app2.app_context():
        rows = [d for d in db.list_declarations(rid2) if d["order_no"] == "W001"]
        check("并发网页同号新增：库中仅一条", len(rows) == 1, str(len(rows)))
        web_id = rows[0]["id"]
    resp = client.get(f"/recordings/{rid2}")
    check("原申报照常可查", resp.status_code == 200 and "W001" in resp.text)
    resp = client.get(f"/declarations/{web_id}/edit")
    check("留档页照常可查",
          resp.status_code == 200 and "W001" in resp.text and "更正申报" in resp.text)

    # 4) 更正页现值与留档来自同一快照；查档期间仍可保存更正。
    #    先把 C001 改成 v4，并在读快照挂起时由另一连接写入 v5，
    #    页面仍应呈现同一提交状态（v4 与其对应的三条留档）。
    with app.app_context():
        db.update_declaration(decl_id, "2026-01-01", "2026-03-01",
                              {"甲": 6250, "乙": 3750})
    gate = threading.Event()

    def slow_render():
        with app.app_context():
            with db.read_snapshot() as conn:
                row = conn.execute(
                    "SELECT * FROM declaration WHERE id = ?", (decl_id,)
                ).fetchone()
                hist = conn.execute(
                    "SELECT version FROM declaration_history "
                    "WHERE declaration_id = ? ORDER BY version", (decl_id,)
                ).fetchall()
                gate.set()
                time.sleep(0.3)  # 模拟查档耗时，期间另一请求写入
                return (row["version"], [h["version"] for h in hist],
                        json.loads(row["shares_json"]))

    clock = {}

    def concurrent_edit():
        gate.wait(timeout=10)
        t0 = time.time()
        with app.app_context():
            db.update_declaration(decl_id, "2026-01-01", "2026-03-01",
                                  {"甲": 6300, "乙": 3700})
        clock["write"] = time.time() - t0

    rendered = {}
    th = threading.Thread(target=lambda: rendered.update(
        snap=slow_render()))
    tw = threading.Thread(target=concurrent_edit)
    th.start()
    tw.start()
    th.join(timeout=30)
    tw.join(timeout=30)
    snap = rendered.get("snap")
    check("查档期间另一请求的更正照常保存成功", "write" in clock, str(clock))
    check("查档读到的现值与留档为同一提交状态",
          snap is not None and snap[0] == 4 and snap[1] == [1, 2, 3],
          str(snap))
    with app.app_context():
        cur, hist = db.get_declaration_with_history(decl_id)
    check("挂起期间的更正已入账（现行 v5、留档 4 条）",
          cur["version"] == 5 and [h["version"] for h in hist] == [1, 2, 3, 4],
          str((cur["version"], [h["version"] for h in hist])))

    # 5) 留档与现行版同事务：写入中途失败时两处都保留原值
    before_cur, before_hist = cur, hist
    original = db._insert_history

    def boom(conn, existing):
        raise sqlite3.OperationalError("模拟留档失败")

    db._insert_history = boom
    try:
        with app.app_context():
            try:
                db.update_declaration(decl_id, "2026-01-01", "2026-03-01",
                                      {"甲": 9999, "乙": 1})
            except sqlite3.OperationalError:
                failed = True
            else:
                failed = False
    finally:
        db._insert_history = original
    with app.app_context():
        cur2, hist2 = db.get_declaration_with_history(decl_id)
    check("更正写入失败向上抛出", failed)
    check("失败后现行版保持原值", cur2["version"] == before_cur["version"]
          and cur2["shares"] == before_cur["shares"])
    check("失败后留档保持原值", len(hist2) == len(before_hist)
          and [h["version"] for h in hist2] == [h["version"] for h in before_hist])


if __name__ == "__main__":
    test_engine()
    test_web()
    test_concurrency()
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)
