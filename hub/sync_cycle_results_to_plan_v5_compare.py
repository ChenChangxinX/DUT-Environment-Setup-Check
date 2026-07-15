# -*- coding: utf-8 -*-
"""
SYNC mode per your rule:
- If target(plan cycle) case ALREADY has result -> DO NOT update, but COMPARE with source and WARN if different.
- If target case has NO result -> UPDATE it with source result (dry-run supported).

Auth: Bearer token OR Basic user/password.

GET /rest/atm/1.0/testrun/{testRunKey}/testresults/ is used to read.
POST /rest/atm/1.0/testrun/{testRunKey}/testresults is used to write (your environment already works).

How to run
1.
python .\sync_cycle_results_to_plan_v5_compare.py `
  --base https://jira.devtools.intel.com `
  --user your_alias `
  --password your_password_or_pat `
  --insecure `
  --source-cycle ICWF-C704 `
  --plan ICWF-P36 `
  --log-level DEBUG `
  --dry-run
2.
python .\sync_cycle_results_to_plan_v5_compare.py `
  --base https://jira.devtools.intel.com `
  --token $env:ZEPHYR_TOKEN `
  --insecure `
  --source-cycle ICWF-C704 `
  --plan ICWF-P36 `
  --log-level DEBUG `
  --dry-run

3. (NEW) sync source cycle to specific target cycle(s), no plan needed
python .\sync_cycle_results_to_plan_v5_compare.py `
    --base https://jira.devtools.intel.com `
    --token $env:ZEPHYR_TOKEN `
    --insecure `
    --source-cycle ICL2H-C151 `
    --target-cycle ICL2H-C161 `
    --log-level DEBUG `
    --dry-run

4. (NEW) For all cases of a specified cycle, batch write the HSD ID/Title/Type you passed in
python .\JIRA_CURD\sync_cycle_results_to_plan_v5_compare.py `
  --base https://jira.devtools.intel.com `
  --token $env:ZEPHYR_TOKEN `
  --insecure `
  --update-hsd-cycle ICWF-C1133 `
  --set-hsd-id "15019437660" `
  --set-hsd-title "[NVL-Hx][A1][RVP01][RVP04][Regression] Preview will be brighter after setting zoom value" `
  --set-hsd-type "Known" `
  --dry-run `
  --log-level DEBUG

dry-run see 'what will be updated with no results and which will trigger warnings'
Real update (only update cases where 'plan has no result'; for cases with results, only compare alerts, do not overwrite)
You can remove --dry-run.

Enhancement:
  --report-json .\per_cycle_report.json `
  --warnings-json .\warnings.json

- Per-cycle summary: updated/warnings counts
- Warnings summary: grouped output (by cycle & by type)
"""


import argparse
import json
import os
import sys
import time
import logging
from datetime import datetime
from typing import Dict, List, Any, Tuple

import requests
import urllib3


# ---------------------------
# Logging
# ---------------------------
def setup_logger(log_file: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("sync_hybrid_report")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# ---------------------------
# HTTP + auth
# ---------------------------
def build_session(insecure: bool) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    if insecure:
        s.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


def apply_bearer(s: requests.Session, token: str):
    s.headers.update({"Authorization": f"Bearer {token}"})


def apply_basic(s: requests.Session, username: str, password: str):
    s.auth = (username, password)


def request_json(method: str, url: str, s: requests.Session,
                 json_body: Any = None, timeout: int = 60) -> Tuple[int, str, Any]:
    r = s.request(method, url, json=json_body, timeout=timeout)
    text = r.text or ""
    try:
        data = r.json() if text.strip() else None
    except Exception:
        data = None
    return r.status_code, text, data


# ---------------------------
# API wrappers
# ---------------------------
def get_cycle_testresults(base: str, s: requests.Session, cycle_key: str, timeout: int = 120) -> List[Dict[str, Any]]:
    url = f"{base}/rest/atm/1.0/testrun/{cycle_key}/testresults/"
    code, text, data = request_json("GET", url, s, timeout=timeout)
    if code >= 400:
        raise RuntimeError(f"[GET testresults] {cycle_key} HTTP {code}\nURL: {url}\n{text[:2000]}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        return data["values"]
    return []


def post_cycle_testresults(base: str, s: requests.Session, cycle_key: str,
                           items: List[Dict[str, Any]], timeout: int = 120) -> None:
    url = f"{base}/rest/atm/1.0/testrun/{cycle_key}/testresults"
    code, text, _ = request_json("POST", url, s, json_body=items, timeout=timeout)
    if code >= 400:
        raise RuntimeError(f"[POST testresults] {cycle_key} HTTP {code}\nURL: {url}\n{text[:2000]}")


def get_plan_cycles_from_api(base: str, s: requests.Session, plan_key: str) -> List[str]:
    url = f"{base}/rest/atm/1.0/testplan/{plan_key}"
    code, text, data = request_json("GET", url, s, timeout=60)
    if code >= 400:
        raise RuntimeError(f"[GET testplan] HTTP {code}\nURL: {url}\n{text[:2000]}")

    cycles = []
    if isinstance(data, dict):
        for k in ("testRuns", "testRunKeys", "testruns", "testcycles", "cycles"):
            v = data.get(k)
            if not v:
                continue
            if isinstance(v, list):
                for e in v:
                    if isinstance(e, str):
                        cycles.append(e)
                    elif isinstance(e, dict):
                        cycles.append(e.get("key") or e.get("testRunKey") or e.get("testrunKey") or e.get("id"))

    cycles = [str(x) for x in cycles if x]
    seen, uniq = set(), []
    for c in cycles:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq


# ---------------------------
# Data helpers
# ---------------------------
def pick_testcase_key(r: Dict[str, Any]) -> str:
    return str(
        r.get("testCaseKey")
        or (r.get("testCase") or {}).get("key")
        or (r.get("testCase") or {}).get("testCaseKey")
        or ""
    ).strip()


def pick_status(r: Dict[str, Any]) -> str:
    v = r.get("status") or r.get("statusName") or r.get("executionStatus") or ""
    return str(v).strip()


def normalize_status(status: Any) -> str:
    # Normalize status text for robust comparison across spacing/case variants.
    return " ".join(str(status or "").strip().split()).lower()


def extract_hsd_custom_fields(r: Dict[str, Any]) -> Dict[str, Any]:
    cf = r.get("customFields") or {}
    if not isinstance(cf, dict):
        return {}
    out = {}
    if cf.get("HSD ID"):
        out["HSD ID"] = cf.get("HSD ID")
    if cf.get("HSD Title"):
        out["HSD Title"] = cf.get("HSD Title")
    if cf.get("HSD Type"):
        out["HSD Type"] = cf.get("HSD Type")
    return out


def build_hsd_custom_fields(hsd_id: str, hsd_title: str, hsd_type: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if hsd_id.strip():
        out["HSD ID"] = hsd_id.strip()
    if hsd_title.strip():
        out["HSD Title"] = hsd_title.strip()
    if hsd_type.strip():
        out["HSD Type"] = hsd_type.strip()
    return out


def slim_result_record(r: Dict[str, Any]) -> Dict[str, Any]:
    keys = ["id", "key", "testCaseKey", "status", "executionDate", "assignedTo", "executedBy"]
    out = {k: r.get(k) for k in keys if k in r}
    out["testCaseKey"] = pick_testcase_key(r)
    out["status"] = pick_status(r)
    hsd = extract_hsd_custom_fields(r)
    if hsd:
        out["customFields"] = hsd
    return out


def build_maps(results: List[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    status_map, record_map = {}, {}
    for r in results:
        tck = pick_testcase_key(r)
        if not tck:
            continue
        status_map[tck] = pick_status(r)
        record_map[tck] = r
    return status_map, record_map


def has_result(status: str, no_result_statuses_norm: set) -> bool:
    s = normalize_status(status)
    if not s:
        return False
    return s not in no_result_statuses_norm


def diff_custom_fields(src_cf: Dict[str, Any], tgt_cf: Dict[str, Any]) -> Dict[str, Tuple[Any, Any]]:
    diffs = {}
    keys = set(src_cf.keys()) | set(tgt_cf.keys())
    for k in keys:
        if src_cf.get(k) != tgt_cf.get(k):
            diffs[k] = (src_cf.get(k), tgt_cf.get(k))
    return diffs


# ---------------------------
# Reporting helpers
# ---------------------------
def init_cycle_stat() -> Dict[str, int]:
    return {
        "overlap": 0,
        "existing_compared": 0,
        "warn_status": 0,
        "warn_hsd": 0,
        "update_missing": 0,
        "updated": 0,
        "update_failed": 0,
    }


def format_cycle_line(cycle: str, st: Dict[str, int], dry_run: bool) -> str:
    upd_label = "would_update" if dry_run else "updated"
    upd_value = st["update_missing"] if dry_run else st["updated"]
    return (
        f"{cycle}: overlap={st['overlap']}, "
        f"existing_compared={st['existing_compared']}, "
        f"warn(status)={st['warn_status']}, warn(hsd)={st['warn_hsd']}, "
        f"update_missing={st['update_missing']}, {upd_label}={upd_value}"
        + (f", update_failed={st['update_failed']}" if not dry_run else "")
    )


def log_dry_run_preview(logger: logging.Logger, would_update_preview: List[Dict[str, Any]]) -> None:
    logger.info("=== DRY-RUN DETAILS (grouped by cycle) ===")
    if not would_update_preview:
        logger.info("No updates would be performed.")
        return

    by_cycle: Dict[str, List[Dict[str, Any]]] = {}
    for item in would_update_preview:
        by_cycle.setdefault(item["cycle"], []).append(item)

    for cyc in sorted(by_cycle.keys()):
        rows = by_cycle[cyc]
        logger.info(f"{cyc}: would_update={len(rows)}")
        for x in rows:
            cf_tag = " +customFields" if x.get("payload_has_customFields") else ""
            logger.info(
                f"  case={x['testCaseKey']}: {x['from']!r} -> {x['to']!r}{cf_tag}"
            )


# ---------------------------
# Main
# ---------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--insecure", action="store_true")

    ap.add_argument("--token", default=os.environ.get("ZEPHYR_TOKEN", ""))
    ap.add_argument("--user", default=os.environ.get("JIRA_USER", ""))
    ap.add_argument("--password", default=os.environ.get("JIRA_PASS", ""))

    ap.add_argument("--source-cycle", default="", help="Source cycle key for sync mode.")
    ap.add_argument("--plan", default="", help="Test plan key. Required only when --target-cycle/--target-cycles are not provided.")
    ap.add_argument("--cycles", default="", help="Fallback cycle list for plan mode, comma separated.")
    ap.add_argument("--target-cycle", default="", help="Direct target cycle key (single).")
    ap.add_argument("--target-cycles", default="", help="Direct target cycle keys (comma separated).")

    # Standalone HSD bulk update mode (single cycle)
    ap.add_argument("--update-hsd-cycle", default="", help="Update all cases in this cycle with provided HSD fields.")
    ap.add_argument("--set-hsd-id", default="", help="HSD ID value to set in --update-hsd-cycle mode.")
    ap.add_argument("--set-hsd-title", default="", help="HSD Title value to set in --update-hsd-cycle mode.")
    ap.add_argument("--set-hsd-type", default="", help="HSD Type value to set in --update-hsd-cycle mode.")

    ap.add_argument("--only-status", default="")
    ap.add_argument("--compare-hsd", dest="compare_hsd", action="store_true",
                    help="Enable HSD custom field comparison for cases with existing results.")
    ap.add_argument("--no-compare-hsd", dest="compare_hsd", action="store_false",
                    help="Disable HSD custom field comparison for cases with existing results.")
    ap.set_defaults(compare_hsd=True)

    ap.add_argument("--no-result-statuses", default="Not Executed,Not Run,NotRun,To Do")
    ap.add_argument("--dry-run", action="store_true")

    ap.add_argument("--log-file", default="")
    ap.add_argument("--log-level", default="INFO")

    # Optional: dump json report files
    ap.add_argument("--report-json", default="", help="Write per-cycle summary to JSON file (optional).")
    ap.add_argument("--warnings-json", default="", help="Write warnings list to JSON file (optional).")

    args = ap.parse_args()

    base = args.base.strip().rstrip("/")
    token = (args.token or "").strip()
    user = (args.user or "").strip()
    password = (args.password or "").strip()
    if not token and not (user and password):
        raise RuntimeError("Auth required: provide --token OR (--user and --password).")

    target_label = args.plan or "direct_targets"
    if args.update_hsd_cycle.strip():
        target_label = f"hsd_{args.update_hsd_cycle.strip()}"

    if not args.log_file:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.log_file = f"sync_report_{target_label}_{args.source_cycle}_{ts}.log"

    logger = setup_logger(args.log_file, args.log_level)
    logger.info(
        f"Start SYNC+REPORT. plan={args.plan or '(none)'}, "
        f"source_cycle={args.source_cycle}, dry_run={args.dry_run}"
    )
    logger.info(f"Log file: {args.log_file}")

    no_result_statuses = [x.strip() for x in args.no_result_statuses.split(",") if x.strip()]
    no_result_statuses_norm = {normalize_status(x) for x in no_result_statuses}
    logger.info(f"Target no-result statuses(raw): {no_result_statuses}")
    logger.info(f"Target no-result statuses(normalized): {sorted(no_result_statuses_norm)}")
    logger.info(f"Compare HSD enabled: {args.compare_hsd}")

    s = build_session(args.insecure)
    if token:
        apply_bearer(s, token)
        logger.info("Auth mode: bearer (token hidden)")
    else:
        apply_basic(s, user, password)
        logger.info("Auth mode: basic (password hidden)")

    # ---------------------------
    # Standalone mode: bulk update HSD fields in one cycle
    # ---------------------------
    if args.update_hsd_cycle.strip():
        cycle_key = args.update_hsd_cycle.strip()
        hsd_fields = build_hsd_custom_fields(args.set_hsd_id, args.set_hsd_title, args.set_hsd_type)
        if not hsd_fields:
            raise RuntimeError("--update-hsd-cycle mode requires at least one of --set-hsd-id/--set-hsd-title/--set-hsd-type.")

        logger.info(
            f"Start HSD bulk update mode. cycle={cycle_key}, dry_run={args.dry_run}, "
            f"fields={json.dumps(hsd_fields, ensure_ascii=False)}"
        )

        cycle_results = get_cycle_testresults(base, s, cycle_key)
        payloads: List[Dict[str, Any]] = []
        preview: List[Dict[str, Any]] = []

        for r in cycle_results:
            tck = pick_testcase_key(r)
            if not tck:
                continue

            status = pick_status(r)
            item = {
                "testCaseKey": tck,
                "status": status,
                "customFields": dict(hsd_fields)
            }
            payloads.append(item)

            if args.dry_run:
                preview.append({
                    "testCaseKey": tck,
                    "status": status,
                    "customFields": hsd_fields
                })

        logger.info(f"Cycle {cycle_key}: total_results={len(cycle_results)}, writable_cases={len(payloads)}")

        if args.dry_run:
            logger.info("=== HSD BULK UPDATE DRY-RUN PREVIEW ===")
            for row in preview:
                logger.info(
                    f"case={row['testCaseKey']} status={row['status']!r} customFields={json.dumps(row['customFields'], ensure_ascii=False)}"
                )
            print(f"[DONE][DRY-RUN] cycle={cycle_key} would_update={len(payloads)}")
            return

        if not payloads:
            logger.info(f"No writable cases found in cycle {cycle_key}.")
            print(f"[DONE] cycle={cycle_key} updated=0")
            return

        post_cycle_testresults(base, s, cycle_key, payloads)
        logger.info(f"[OK] {cycle_key}: HSD fields updated for {len(payloads)} cases")
        print(f"[DONE] cycle={cycle_key} updated={len(payloads)}")
        return

    if not args.source_cycle.strip():
        raise RuntimeError("--source-cycle is required in sync mode.")

    # Load source
    src_results = get_cycle_testresults(base, s, args.source_cycle)
    src_status_map, src_record_map = build_maps(src_results)

    if args.only_status:
        want = normalize_status(args.only_status)
        src_status_map = {k: v for k, v in src_status_map.items() if normalize_status(v) == want}
        src_record_map = {k: src_record_map[k] for k in src_status_map.keys() if k in src_record_map}

    logger.info(f"Source loaded: {len(src_status_map)} cases")

    # Resolve target cycles (direct mode or plan mode)
    direct_cycles: List[str] = []
    if args.target_cycle.strip():
        direct_cycles.append(args.target_cycle.strip())
    if args.target_cycles.strip():
        direct_cycles.extend([x.strip() for x in args.target_cycles.split(",") if x.strip()])

    # de-duplicate direct targets while preserving order
    if direct_cycles:
        seen_direct, uniq_direct = set(), []
        for c in direct_cycles:
            if c not in seen_direct:
                uniq_direct.append(c)
                seen_direct.add(c)
        direct_cycles = uniq_direct

    # Load target cycles
    plan_cycles: List[str] = []
    if direct_cycles:
        plan_cycles = direct_cycles
        logger.info(f"Using direct target cycle mode. target_cycles={plan_cycles}")
    else:
        if not args.plan:
            raise RuntimeError("Target required: provide --target-cycle/--target-cycles OR provide --plan.")

        try:
            plan_cycles = get_plan_cycles_from_api(base, s, args.plan)
        except Exception as e:
            logger.warning(f"Cannot list cycles from plan API: {e}")

        if not plan_cycles and args.cycles:
            plan_cycles = [x.strip() for x in args.cycles.split(",") if x.strip()]
            logger.info("Using --cycles fallback list")

    if not plan_cycles:
        raise RuntimeError("No target cycles found.")

    logger.info(f"Target cycles found: {len(plan_cycles)}")

    # --------- NEW: per-cycle stats + warnings list ----------
    per_cycle_stats: Dict[str, Dict[str, int]] = {}
    warnings: List[Dict[str, Any]] = []
    would_update_preview: List[Dict[str, Any]] = []

    # Overall summary
    summary = {
        "cycles_total": len(plan_cycles),
        "cycles_processed": 0,
        "warnings_status_diff": 0,
        "warnings_hsd_diff": 0,
        "cases_to_update_missing": 0,
        "cases_updated": 0,
        "cases_update_failed": 0,
        "cycles_cf_fallback": 0
    }

    for c in plan_cycles:
        per_cycle_stats.setdefault(c, init_cycle_stat())
        logger.info(f"=== Cycle {c} ===")
        try:
            tgt_results = get_cycle_testresults(base, s, c)
        except Exception as e:
            logger.error(f"Skip cycle {c} (cannot GET testresults): {e}")
            continue

        summary["cycles_processed"] += 1
        tgt_status_map, tgt_record_map = build_maps(tgt_results)

        overlap_keys = [k for k in src_status_map.keys() if k in tgt_record_map]
        per_cycle_stats[c]["overlap"] = len(overlap_keys)

        if not overlap_keys:
            logger.info(f"[INFO] {c}: no overlapping cases with source")
            continue

        items_to_update: List[Dict[str, Any]] = []
        cycle_warn_status = 0
        cycle_warn_hsd = 0

        for tck in overlap_keys:
            src_status = src_status_map.get(tck, "")
            tgt_status = tgt_status_map.get(tck, "")
            src_full = src_record_map.get(tck, {})
            tgt_full = tgt_record_map.get(tck, {})

            if has_result(tgt_status, no_result_statuses_norm):
                # existing result => compare, warn if different
                per_cycle_stats[c]["existing_compared"] += 1

                if normalize_status(src_status) != normalize_status(tgt_status):
                    cycle_warn_status += 1
                    summary["warnings_status_diff"] += 1
                    warnings.append({
                        "type": "STATUS_DIFF",
                        "cycle": c,
                        "testCaseKey": tck,
                        "src_status": src_status,
                        "tgt_status": tgt_status,
                        "src_status_normalized": normalize_status(src_status),
                        "tgt_status_normalized": normalize_status(tgt_status),
                        "src": slim_result_record(src_full),
                        "tgt": slim_result_record(tgt_full),
                    })
                    logger.warning(f"[STATUS DIFF] cycle={c} case={tck} src={src_status!r} tgt={tgt_status!r}")

                if args.compare_hsd:
                    src_cf = extract_hsd_custom_fields(src_full)
                    tgt_cf = extract_hsd_custom_fields(tgt_full)
                    diffs = diff_custom_fields(src_cf, tgt_cf)
                    if diffs:
                        cycle_warn_hsd += 1
                        summary["warnings_hsd_diff"] += 1
                        warnings.append({
                            "type": "HSD_DIFF",
                            "cycle": c,
                            "testCaseKey": tck,
                            "diffs": diffs
                        })
                        logger.warning(f"[HSD DIFF] cycle={c} case={tck} diffs={json.dumps(diffs, ensure_ascii=False)}")

                continue

            # no result => update
            payload = {"testCaseKey": tck, "status": src_status}
            if normalize_status(src_status) == "fail":
                hsd_cf = extract_hsd_custom_fields(src_full)
                if hsd_cf:
                    payload["customFields"] = hsd_cf

            items_to_update.append(payload)
            per_cycle_stats[c]["update_missing"] += 1
            summary["cases_to_update_missing"] += 1

            if args.dry_run:
                would_update_preview.append({
                    "cycle": c,
                    "testCaseKey": tck,
                    "from": tgt_status or "(empty)",
                    "to": src_status,
                    "payload_has_customFields": ("customFields" in payload)
                })

        per_cycle_stats[c]["warn_status"] += cycle_warn_status
        per_cycle_stats[c]["warn_hsd"] += cycle_warn_hsd

        logger.info(f"Cycle {c}: overlap={len(overlap_keys)}, update_missing={len(items_to_update)}, warn_status={cycle_warn_status}, warn_hsd={cycle_warn_hsd}")

        if args.dry_run or not items_to_update:
            continue

        # do update
        try:
            post_cycle_testresults(base, s, c, items_to_update)
            per_cycle_stats[c]["updated"] += len(items_to_update)
            summary["cases_updated"] += len(items_to_update)
            logger.info(f"[OK] {c}: updated {len(items_to_update)} missing-result cases")
        except Exception as e:
            has_cf = any("customFields" in x for x in items_to_update)
            if has_cf:
                logger.warning(f"[WARN] {c}: update failed (maybe customFields not accepted). Retry status-only. err={e}")
                summary["cycles_cf_fallback"] += 1
                status_only = [{"testCaseKey": x["testCaseKey"], "status": x["status"]} for x in items_to_update]
                try:
                    post_cycle_testresults(base, s, c, status_only)
                    per_cycle_stats[c]["updated"] += len(status_only)
                    summary["cases_updated"] += len(status_only)
                    logger.info(f"[OK] {c}: fallback status-only updated {len(status_only)} cases")
                except Exception as e2:
                    per_cycle_stats[c]["update_failed"] += len(items_to_update)
                    summary["cases_update_failed"] += len(items_to_update)
                    logger.error(f"[FAIL] {c}: fallback also failed. err={e2}")
            else:
                per_cycle_stats[c]["update_failed"] += len(items_to_update)
                summary["cases_update_failed"] += len(items_to_update)
                logger.error(f"[FAIL] {c}: update failed. err={e}")

        time.sleep(0.2)

    # ---------------------------
    # NEW OUTPUT: per-cycle summary
    # ---------------------------
    logger.info("=== PER-CYCLE SUMMARY (update/warn per cycle) ===")
    for cyc in sorted(per_cycle_stats.keys()):
        st = per_cycle_stats[cyc]
        # only print cycles that actually mattered (overlap > 0 or warnings/update_missing >0)
        if st["overlap"] == 0 and st["warn_status"] == 0 and st["warn_hsd"] == 0 and st["update_missing"] == 0:
            continue
        logger.info(format_cycle_line(cyc, st, args.dry_run))

    # ---------------------------
    # NEW OUTPUT: warnings summary
    # ---------------------------
    logger.info("=== WARNINGS SUMMARY (grouped) ===")
    if not warnings:
        logger.info("No warnings.")
    else:
        # group by cycle
        by_cycle: Dict[str, List[Dict[str, Any]]] = {}
        for w in warnings:
            by_cycle.setdefault(w["cycle"], []).append(w)

        for cyc in sorted(by_cycle.keys()):
            ws = by_cycle[cyc]
            status_ws = [x for x in ws if x["type"] == "STATUS_DIFF"]
            hsd_ws = [x for x in ws if x["type"] == "HSD_DIFF"]
            logger.info(f"{cyc}: warnings_total={len(ws)} (status={len(status_ws)}, hsd={len(hsd_ws)})")
            # list details (compact)
            for x in status_ws:
                logger.info(f"  [STATUS_DIFF] {x['testCaseKey']}: tgt={x['tgt_status']!r} vs src={x['src_status']!r}")
            for x in hsd_ws:
                logger.info(f"  [HSD_DIFF] {x['testCaseKey']}: {json.dumps(x['diffs'], ensure_ascii=False)}")

    # dump JSON (optional)
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as f:
            json.dump(per_cycle_stats, f, ensure_ascii=False, indent=2)
        logger.info(f"Wrote per-cycle report json -> {args.report_json}")

    if args.warnings_json:
        with open(args.warnings_json, "w", encoding="utf-8") as f:
            json.dump(warnings, f, ensure_ascii=False, indent=2)
        logger.info(f"Wrote warnings json -> {args.warnings_json}")

    logger.info(f"=== DONE SUMMARY === {json.dumps(summary, ensure_ascii=False)}")
    if args.dry_run:
        logger.info(f"[DRY-RUN PREVIEW] would_update_count={len(would_update_preview)}")
        log_dry_run_preview(logger, would_update_preview)

    print(
        f"[DONE] updated={summary['cases_updated']} "
        f"warn_status={summary['warnings_status_diff']} warn_hsd={summary['warnings_hsd_diff']} "
        f"(log: {args.log_file})"
    )


if __name__ == "__main__":
    main()