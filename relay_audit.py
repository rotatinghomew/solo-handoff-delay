#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联络接力：真实路径重建、逾期/长尾核对与下周窗口试案比较。

仅使用 Python 3.11 标准库。设计为可反复运行：
    python3 -I relay_audit.py
会读取 data/records.json，并把机器可读结果写入 analysis/results.json 与 CSV 表。

关键口径（与 README 的运行契约一致）：
* 办结以“已完成”事件及其凭据为准；同凭据“转抄”不是新回执，不改变办结日。
* 发出→接手之间仍记在原联络台名下，不计为站点处理时长。
* join=全部 取所有有效分支办结日的最大值（且任一分支被退回即不能按现状办结）；
  join=任一 取最早有效办结日；退回分支无有效履行。
* amendments 在其“知道日”当天及以后的截点视图中生效；最终真相视图采用补记。
* 共用凭据只对 shared_evidence 明确列出的 request 生效。
* 准时：可确认办结日 <= deadline；截点已过 deadline 仍无可确认办结 = 逾期；
  deadline 当天结束前仍算到期未逾期，次日起记为逾期。
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / "data" / "records.json"
ASOF = 36  # remaining_queue.still_open_asof；第 37 周编表前


def load_records(path: str | Path = DEFAULT_DATA) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 路径重建
# ---------------------------------------------------------------------------

def _branch_events(payload: dict):
    evb = defaultdict(list)
    for e in payload["events"]:
        evb[(e["request"], e["branch"])].append(e)
    return evb


def shared_completion_index(payload: dict):
    """evidence -> (day, request_of_event) for 已完成 events."""
    idx = {}
    for e in payload["events"]:
        if e["event"] == "已完成":
            idx[e["evidence"]] = (e["day"], e["request"])
    allowed = defaultdict(set)
    for s in payload.get("shared_evidence", []):
        allowed[s["evidence"]].add(s["request"])
    return idx, allowed


def effective_join(req_row: dict, amendments: dict, cut: int | None):
    rid = req_row["request"]
    if rid in amendments:
        a = amendments[rid]
        if cut is None or a["day"] <= cut:
            return a["new_join"], a
    return req_row["join"], None


def request_view(payload: dict, rid: str, cut: int | None = None,
                 use_amendment: bool = True, evb=None):
    """重建单个请求在截点 cut 的可观察路径与状态。cut=None 表示全部记录。"""
    horizon = cut if cut is not None else max(e["day"] for e in payload["events"])
    req = {r["request"]: r for r in payload["requests"]}[rid]
    amendments = {a["request"]: a for a in payload.get("amendments", [])}
    if evb is None:
        evb = _branch_events(payload)

    join, amend = effective_join(req, amendments if use_amendment else {}, cut)
    bids = sorted({b for (q, b) in evb if q == rid},
                  key=lambda x: int(x.rsplit("-", 1)[1]))

    branches = {}
    comp_days, reject_days, held, sent = [], [], [], []
    for b in bids:
        evs = [e for e in evb[(rid, b)] if cut is None or e["day"] <= cut]
        days = defaultdict(list)
        for e in evs:
            days[e["event"]].append(e)
        p = min((e["day"] for e in days["发出"]), default=None)
        h = min((e["day"] for e in days["接手"]), default=None)
        c_ev = sorted(days["已完成"], key=lambda e: e["day"])
        c = c_ev[0]["day"] if c_ev else None
        c_evid = c_ev[0]["evidence"] if c_ev else None
        t = min((e["day"] for e in days["转抄"]), default=None)
        f = min((e["day"] for e in days["退回"]), default=None)
        owner_h = next((e["owner"] for e in sorted(days["接手"], key=lambda e: e["day"])), None)
        row = {
            "branch": b, "dispatch": p, "accept": h, "complete": c,
            "complete_evidence": c_evid, "transcribe": t, "reject": f,
            "accept_owner": owner_h,
        }
        # 阶段间隔（按日历日差）
        row["lag_start_dispatch"] = (p - req["start"]) if p is not None else None
        row["lag_dispatch_accept"] = (h - p) if (p is not None and h is not None) else None
        if h is not None and c is not None:
            row["lag_accept_terminal"] = c - h
        elif h is not None and f is not None:
            row["lag_accept_terminal"] = f - h
        else:
            row["lag_accept_terminal"] = None
        branches[b] = row
        if c is not None:
            comp_days.append(c)
        elif f is not None:
            reject_days.append(f)
        elif h is not None:
            held.append((b, h, owner_h))
        elif p is not None:
            sent.append((b, p))

    # 共用凭据：仅登记，不覆盖更早的自有办结
    shared_usable = []
    comp_idx, allowed = shared_completion_index(payload)
    for s in payload.get("shared_evidence", []):
        if s["request"] == rid and s["evidence"] in comp_idx:
            d, origin = comp_idx[s["evidence"]]
            if cut is None or d <= cut:
                shared_usable.append({"evidence": s["evidence"], "day": d,
                                      "origin_request": origin,
                                      "listed_for": sorted(allowed[s["evidence"]])})

    status, close_day = _join_status(join, bids, branches, comp_days, reject_days,
                                     held, cut, req)
    deadline = req["deadline"]
    if close_day is not None:
        overdue = close_day > deadline
        overdue_since = deadline + 1 if overdue else None
    else:
        overdue = horizon > deadline
        overdue_since = deadline + 1  # 无 confirmable 办结：截止次日即逾期
    return {
        "request": rid, "household": req["household"], "recipient": req["recipient"],
        "start": req["start"], "deadline": deadline, "join_original": req["join"],
        "join_effective": join, "amendment": amend, "cut": cut,
        "branches": branches, "status": status, "close_day": close_day,
        "overdue": overdue, "overdue_since": overdue_since,
        "days_late_at_cut": (horizon - deadline) if overdue else None,
        "shared_evidence_usable": shared_usable,
        "held": [{"branch": b, "since": h, "owner": o,
                  "age_at_cut": horizon - h}
                 for b, h, o in held],
        "sent_unaccepted": [{"branch": b, "since": p,
                             "age_at_cut": horizon - p}
                            for b, p in sent],
    }


def _join_status(join, bids, branches, comp_days, reject_days, held, cut, req):
    if join == "任一":
        if comp_days:
            return "closed", min(comp_days)
        if len(reject_days) == len(bids):
            return "all_rejected", None
        return "open", None
    # 全部
    if reject_days:
        return "broken_rejected_branch", None
    if len(comp_days) == len(bids):
        return "closed", max(comp_days)
    return "open", None


def all_request_views(payload: dict, cut=None):
    evb = _branch_events(payload)
    return [request_view(payload, r["request"], cut=cut, evb=evb)
            for r in payload["requests"]]


# ---------------------------------------------------------------------------
# 批次 / 周界 / 家庭
# ---------------------------------------------------------------------------

def intake_week(start: int) -> int:
    return start // 7


def batch_table(views):
    groups = defaultdict(list)
    for v in views:
        groups[(intake_week(v["start"]), v["start"])].append(v)
    out = []
    for (wk, st), vs in sorted(groups.items()):
        out.append({
            "intake_week": wk, "start_day": st, "n": len(vs),
            "requests": [v["request"] for v in vs],
            "closed": sum(v["status"] == "closed" for v in vs),
            "overdue_or_lost": sum(v["status"] != "closed" for v in vs),
            "ids_overdue_or_lost": [v["request"] for v in vs if v["status"] != "closed"],
            "close_days": {v["request"]: v["close_day"] for v in vs},
        })
    return out


def weekly_batch_table(views):
    groups = defaultdict(list)
    for v in views:
        groups[intake_week(v["start"])].append(v)
    return [{
        "intake_week": wk,
        "day_range": [wk * 7, wk * 7 + 6],
        "n": len(vs),
        "closed": sum(v["status"] == "closed" for v in vs),
        "unfulfilled": [v["request"] for v in vs if v["status"] != "closed"],
    } for wk, vs in sorted(groups.items())]


def household_table(views):
    hh = defaultdict(list)
    for v in views:
        hh[v["household"]].append(v)
    out = []
    for h, vs in sorted(hh.items(), key=lambda kv: int(kv[0][1:])):
        done_days = [v["close_day"] for v in vs if v["status"] == "closed"]
        out.append({
            "household": h,
            "requests": [v["request"] for v in vs],
            "request_status": {v["request"]: v["status"] for v in vs},
            "all_requests_closed": len(done_days) == len(vs),
            "household_result_day": max(done_days) if len(done_days) == len(vs) else None,
            "max_deadline": max(v["deadline"] for v in vs),
        })
    return out


def snapshot(payload, cut: int):
    views = all_request_views(payload, cut=cut)
    overdue = [v["request"] for v in views if v["overdue"]]
    due_today = [v["request"] for v in views
                 if v["status"] != "closed" and v["deadline"] == cut]
    certain_miss = [v["request"] for v in views
                    if v["status"].startswith("broken") and v["deadline"] >= cut]
    open_held = []
    for v in views:
        for h in v["held"]:
            open_held.append({"request": v["request"], **h,
                              "deadline": v["deadline"],
                              "days_to_deadline": v["deadline"] - cut})
    return {"cut_day": cut, "overdue": overdue, "due_today_but_unconfirmed": due_today,
            "broken_not_yet_overdue": certain_miss,
            "held_branches": open_held}


# ---------------------------------------------------------------------------
# 阶段用时（旧的“站点平均”口径 vs 真实路径）
# ---------------------------------------------------------------------------

def phase_metrics(payload):
    evb = _branch_events(payload)
    station_done = defaultdict(list)
    station_reject = defaultdict(list)
    accept_lag = defaultdict(list)
    dispatch_lag = []
    transcribe_lag = []
    for (rid, b), evs in evb.items():
        days = defaultdict(list)
        for e in evs:
            days[e["event"]].append(e["day"])
        p = min(days["发出"], default=None)
        h = min(days["接手"], default=None)
        c = min(days["已完成"], default=None)
        f = min(days["退回"], default=None)
        t = min(days["转抄"], default=None)
        req = next(r for r in payload["requests"] if r["request"] == rid)
        if p is not None:
            dispatch_lag.append(p - req["start"])
        if p is not None and h is not None:
            owner = next(e["owner"] for e in evs if e["event"] == "接手")
            accept_lag[owner].append(h - p)
        owner = "站0" if b.endswith("-0") else "站1"
        if h is not None and c is not None:
            station_done[owner].append(c - h)
        if h is not None and f is not None:
            station_reject[owner].append(f - h)
        if c is not None and t is not None:
            transcribe_lag.append(t - c)

    def mean(xs):
        return round(statistics.mean(xs), 3) if xs else None

    rows = {}
    for s in ("站0", "站1"):
        terminal = station_done[s] + station_reject[s]
        rows[s] = {
            "accept_lag_days_counter": dict(Counter(accept_lag[s])),
            "handle_to_complete_mean": mean(station_done[s]),
            "handle_to_complete_n": len(station_done[s]),
            "handle_to_reject_days": sorted(station_reject[s]),
            "terminal_handling_mean_all_terminal": mean(terminal),
            "terminal_n": len(terminal),
            "open_branches_excluded_from_average":
                sum(1 for (rid, b), evs in evb.items()
                    if ("站0" if b.endswith("-0") else "站1") == s
                    and not any(e["event"] in ("已完成", "退回") for e in evs)),
        }
    return {
        "start_to_dispatch_lag_counter": dict(Counter(dispatch_lag)),
        "complete_to_transcribe_lag_counter": dict(Counter(transcribe_lag)),
        "stations": rows,
        "warning": "旧均值只统计已终结分支；无限期挂起的长尾被剔除，且不含 join/截止日信息。",
    }


# ---------------------------------------------------------------------------
# 机构承诺 / 家庭约定 / 实际接手 / 可确认办结 四本账
# ---------------------------------------------------------------------------

def four_ledger(payload):
    views = {v["request"]: v for v in all_request_views(payload)}
    windows = defaultdict(list)
    for w in payload["windows"]:
        windows[w["request"]].append(w)
    pauses = defaultdict(list)
    for p in payload["pauses"]:
        pauses[p["request"]].append(p)
    rebooks = defaultdict(list)
    for r in payload["rebook_requests"]:
        rebooks[r["request"]].append(r)

    out = []
    for rid, v in views.items():
        req = next(r for r in payload["requests"] if r["request"] == rid)
        # 家庭约定暂停是否真的落在该请求可观察等待区间内
        pause_rows = []
        active_lo = req["start"]
        active_hi = v["close_day"] if v["close_day"] is not None else ASOF
        for p in pauses.get(rid, []):
            overlap_elapsed = max(0, min(p["hi"], active_hi) - max(p["lo"], active_lo))
            applies = ("家庭" in p["basis"]) and overlap_elapsed > 0 \
                and p["recipient"] == req["recipient"]
            pause_rows.append({**p, "overlap_days": overlap_elapsed,
                               "applies_to_wait": applies,
                               "reason_if_not": None if applies else
                               "约定日段与该请求等待区间不重叠（或对象不符），不得核减等待"})
        out.append({
            "request": rid,
            "institution_promise": {"deadline": req["deadline"],
                                    "deadline_rule": "可确认办结日<=deadline 为准时",
                                    "notified_windows": windows.get(rid, [])},
            "household_agreement": {"pauses": pause_rows,
                                    "window_consent_days":
                                        [w["consent_day"] for w in windows.get(rid, [])],
                                    "rebook_drafts": rebooks.get(rid, [])},
            "actual_acceptance": [
                {"branch": b, "day": br["accept"], "owner": br["accept_owner"]}
                for b, br in v["branches"].items() if br["accept"] is not None],
            "confirmable_completion": (
                {"day": v["close_day"],
                 "evidence": next((br["complete_evidence"] for br in v["branches"].values()
                                   if br["complete"] == v["close_day"]), None),
                 "basis": "已完成事件凭据日；转抄不另算"}
                if v["close_day"] is not None else None),
            "status_final_corrected": v["status"],
        })
    return out


# ---------------------------------------------------------------------------
# 第 37/38 周：在等谁、容量、试案
# ---------------------------------------------------------------------------

def waiting_asof(payload, cut=ASOF):
    queue = payload["remaining_queue"]
    evb = _branch_events(payload)
    out = []
    for q in queue:
        rid = q["request"]
        v = request_view(payload, rid, cut=cut, evb=evb)
        row = {"request": rid, "linked": q["linked"], "earliest_week": q["earliest_week"],
               "deadline": v["deadline"], "days_to_deadline": v["deadline"] - cut,
               "join_effective": v["join_effective"], "waiting_on": [],
               "structural_gaps": []}
        for b in q["linked"]:
            br = v["branches"][b]
            if br["complete"] is not None:
                continue
            if br["reject"] is not None and br["reject"] <= cut:
                row["waiting_on"].append({"branch": b, "party": "联络台需重新派出",
                                         "since": br["reject"],
                                         "note": "已退回，现状无有效履行，不在等站点"})
            elif br["accept"] is not None:
                row["waiting_on"].append({"branch": b, "party": br["accept_owner"],
                                          "since": br["accept"],
                                          "business_days_age": cut - br["accept"]})
            elif br["dispatch"] is not None:
                row["waiting_on"].append({"branch": b, "party": "原联络台/待接手",
                                          "since": br["dispatch"]})
        # 未列入 linked 的分支若已退回，是结构性缺口
        for b, br in v["branches"].items():
            if b not in q["linked"] and br["reject"] is not None:
                row["structural_gaps"].append(
                    f"{b} 已于 d{br['reject']} 被{br['accept_owner']}退回，"
                    f"全部依赖下必须重新派出，否则 {rid} 不能办结")
        out.append(row)
    return out


def proposal_ledger(payload):
    limits = payload["extra_limits"]
    sets = payload["proposal_sets"]
    agg = defaultdict(int)
    by_name = defaultdict(list)
    for s in sets:
        agg[(s["proposal"], str(s["week"]))] += s["extra"]
        agg[(s["proposal"], "total")] += s["extra"]
        by_name[s["proposal"]].append(s)
    proposals = {}
    for name, rows in by_name.items():
        w37 = sum(r["extra"] for r in rows if r["week"] == 37)
        w38 = sum(r["extra"] for r in rows if r["week"] == 38)
        total = w37 + w38
        proposals[name] = {
            "rows": rows, "extra_week37_cross_station": w37,
            "extra_week38_cross_station": w38, "extra_total": total,
            "within_week37_cap": w37 <= limits["37"],
            "within_week38_cap": w38 <= limits["38"],
            "within_weeks_total_cap_reading": total <= limits["weeks_total"],
            "cap_note": ("若 extra_limits.weeks_total 是跨周总额度，则超出；"
                         "若其仅表示覆盖周数=2，则只受各周跨站上限约束。需向维护者确认。"),
        }

    # 改约草稿对明确 capacity 的占用
    caps = payload["capacity"]
    cap_rows = []
    rebooks = payload["rebook_requests"]
    for c in caps:
        contenders = [r for r in rebooks
                      if r["new_owner"] == c["provider"]
                      and r["lo"] <= c["hi"] and r["hi"] >= c["lo"]]
        cap_rows.append({
            "provider": c["provider"], "window": [c["lo"], c["hi"]],
            "optional_slots": c["optional_slots"],
            "contending_drafts": [{"option": r["option"], "request": r["request"],
                                   "branch": r["branch"], "slot": [r["lo"], r["hi"]],
                                   "recipient": r["recipient"]} for r in contenders],
            "contender_count": len(contenders),
            "allocatable_at_most": min(c["optional_slots"], len(contenders)),
            "conflict": len(contenders) > c["optional_slots"],
        })

    # 每份草稿的同意/日历/前置条件门禁
    req = {r["request"]: r for r in payload["requests"]}
    consent_windows = defaultdict(list)
    for w in payload["windows"]:
        consent_windows[w["request"]].append(w)
    prereq = {(p["later"]): p for p in payload["prerequisites"]}
    draft_gates = []
    for r in rebooks:
        rid = r["request"]
        deadline = req[rid]["deadline"]
        notified = consent_windows.get(rid, [])
        needs = r["recipient"]
        gate = {
            "option": r["option"], "request": rid, "branch": r["branch"],
            "recipient": needs, "proposed": [r["lo"], r["hi"]],
            "new_owner": r["new_owner"], "deadline": deadline,
            "earliest_day_vs_deadline": r["lo"] - deadline,
            "calendar_ontime_possible": r["lo"] <= deadline,
            "consent_required_from": needs,
            "consent_on_record": False,
            "consent_note": ("所有 windows.consent_day 均为 null；未取得"
                             + ("学生本人同意，家长回执不能替代；" if needs == "学生"
                                else "相应对象同意；")
                             + "已通知窗口须照约保留，草稿不得视为已改约。"),
            "notified_windows": notified,
            "prerequisite": prereq.get(r["branch"]),
        }
        draft_gates.append(gate)
    return {"extra_limits": limits, "proposals": proposals,
            "capacity_conflicts": cap_rows, "draft_gates": draft_gates}


def cross_week_headroom(payload):
    out = []
    for f in payload["weekly_forecasts"]:
        out.append({**f, "free_regular_slots": f["regular"] - f["already_taken"]})
    return out


# ---------------------------------------------------------------------------
# 试点证据可比性
# ---------------------------------------------------------------------------

def pilot_evidence(payload):
    p = payload["pilot"]

    def ridx(x):
        return int(x["request"].split("-")[1])

    def critical(x):
        ds = [v for v in x["delays"] if v is not None]
        if not ds:
            return None
        return max(ds) if x["join"] == "全部" else min(ds)

    busy_weeks = {
        "加开": sorted({x["week"] for x in p if x["load_flag"] == 1 and x["arrangement"] == "加开"}),
        "普通": sorted({x["week"] for x in p if x["load_flag"] == 1 and x["arrangement"] == "普通"}),
    }
    calm_weeks = {
        "加开": sorted({x["week"] for x in p if x["load_flag"] == 0 and x["arrangement"] == "加开"}),
        "普通": sorted({x["week"] for x in p if x["load_flag"] == 0 and x["arrangement"] == "普通"}),
    }

    strata = {}
    meta = {i: next(x for x in p if ridx(x) == i) for i in range(12)}
    for i in range(12):
        m = meta[i]
        key = f"route_index_{i}"
        strata[key] = {"root": m["root"], "join": m["join"], "pilot_deadline": m["deadline"],
                       "initially_visible_all": m["initially_visible"],
                       "complete_source_all": m["complete_source"]}
        for arr in ("普通", "加开"):
            for lf in (0, 1):
                ys = [x for x in p if ridx(x) == i and x["arrangement"] == arr
                      and x["load_flag"] == lf]
                T = [critical(x) for x in ys]
                T = [t for t in T if t is not None]
                label = f"L{lf}_{arr}"
                strata[key][label] = {
                    "weeks": sorted({x["week"] for x in ys}), "n": len(ys),
                    "critical_delay_values": T,
                    "min": min(T) if T else None, "max": max(T) if T else None,
                    "median": statistics.median(T) if T else None,
                }

    borrow_groups = [
        {"applies_to": "R25-0（站0，全部）与 Z2 改入站0的 R8-1",
         "best_stratum": "route_index_6",
         "why": "站0根、全部、双分支延迟完整且 initially_visible/complete_source 均为真",
         "busy_extra_weeks": busy_weeks["加开"], "busy_normal_weeks": busy_weeks["普通"]},
        {"applies_to": "R5-1、R28-1、R7-1 及 Z3 拟入站1的 R7-0（站1，全部）",
         "best_stratum": "route_index_3",
         "secondary_stratum": "route_index_9",
         "why": "idx3 双延迟完整；idx9 只有单延迟（第二值恒 null），"
                "对“全部”的 max 关键路径会低估，只能作旁证",
         "busy_extra_weeks": busy_weeks["加开"], "busy_normal_weeks": busy_weeks["普通"]},
    ]
    unusable = [
        {"stratum": "route_index_0",
         "reason": "initially_visible=false 且 complete_source=false，第二延迟 null；"
                   "当时不可见、来源不完整，不能作为支持证据"},
        {"stratum": "route_index_8",
         "reason": "complete_source=false：数字虽在，来源不完整，不能作为可靠材料"},
        {"stratum": "route_index_11",
         "reason": "initially_visible=false：不能算作当时决策可引用的轮次，"
                   "今天做事后复核可作验证"},
        {"stratum": "route_index_9",
         "reason": "第二延迟恒 null：全部依赖的关键路径只能取单支，范围偏乐观/不完整"},
    ]
    no_analog = [
        "退回后重新派出的分支（R28-0；R7-0 在 d37 实际退回）：pilot 无退回/重派字段",
        "prerequisites 登记的顺序接力与最短间隔（R7-0→R7-1≥1；R8-0→R8-1≥2）："
        "pilot 只有并行两支的延迟，没有顺序链样本，顺序工期需相加而非取 max",
        "未取得同意的改约（尤其 R8 学生本人同意）：属门禁问题，不是用时分布问题",
        "跨共用凭据办结（R4/R5 共用 R5-0C）：pilot 无对应结构",
    ]
    correlation = ("每个 pilot 周的 load_flag 对两站同时生效；忙周两站周均延迟同时抬升"
                   "（如 w4 5.2/5.2、w11 5.5/5.5、w16 4.8/6.0），"
                   "不能把两站当作相互独立的最好情况相乘/相加。")
    return {"busy_weeks": busy_weeks, "calm_weeks": calm_weeks,
            "strata": strata, "borrow_groups": borrow_groups,
            "unusable_or_limited": unusable, "no_analog_paths": no_analog,
            "correlated_busy_weeks": correlation}


# ---------------------------------------------------------------------------
# 主流程：写 results.json 与 CSV
# ---------------------------------------------------------------------------

def build_full_result(payload):
    final_views = all_request_views(payload)
    return {
        "anchor_asof": ASOF,
        "request_paths_final_corrected": final_views,
        "batches_by_start_cohort": batch_table(final_views),
        "weekly_intake_batches": weekly_batch_table(final_views),
        "households": household_table(final_views),
        "snapshots": {str(c): snapshot(payload, c) for c in (20, 27, ASOF, 48)},
        "phase_metrics": phase_metrics(payload),
        "four_ledger": four_ledger(payload),
        "waiting_asof36": waiting_asof(payload),
        "proposals": proposal_ledger(payload),
        "cross_week_headroom": cross_week_headroom(payload),
        "pilot": pilot_evidence(payload),
    }


def write_csv_tables(result, outdir: Path):
    tdir = outdir / "tables"
    tdir.mkdir(parents=True, exist_ok=True)

    with open(tdir / "request_paths.csv", "w", newline="", encoding="utf-8-sig") as fh:
        cols = ["request", "household", "recipient", "start", "deadline",
                "join_original", "join_effective", "status", "close_day", "overdue"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for v in result["request_paths_final_corrected"]:
            w.writerow({k: v[k] for k in cols})

    with open(tdir / "branch_phases.csv", "w", newline="", encoding="utf-8-sig") as fh:
        cols = ["request", "branch", "dispatch", "accept", "accept_owner",
                "complete", "transcribe", "reject",
                "lag_start_dispatch", "lag_dispatch_accept", "lag_accept_terminal"]
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for v in result["request_paths_final_corrected"]:
            for b, br in v["branches"].items():
                row = {k: br.get(k) for k in cols if k in br}
                row["request"] = v["request"]
                w.writerow(row)


def main():
    payload = load_records()
    result = build_full_result(payload)
    outdir = ROOT / "analysis"
    outdir.mkdir(exist_ok=True)
    (outdir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_csv_tables(result, outdir)

    # 控制台摘要
    vs = result["request_paths_final_corrected"]
    n_over = sum(v["status"] != "closed" for v in vs)
    print(f"requests={len(vs)} unfulfilled_final={n_over}")
    print("unfulfilled:", [v["request"] for v in vs if v["status"] != "closed"])
    for c in (20, 27, 36, 48):
        s = result["snapshots"][str(c)]
        print(f"day{c}: overdue={s['overdue']} due_today={s['due_today_but_unconfirmed']} "
              f"broken_not_yet={s['broken_not_yet_overdue']}")
    print("wrote", outdir / "results.json")


if __name__ == "__main__":
    main()
