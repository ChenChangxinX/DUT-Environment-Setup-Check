"""
excel_to_nodes.py
读取 Excel，生成 nodes.json

Excel 列（顺序不限，列名大小写不敏感）：
  Silicon | QDF | Camera | Device Name | RVP IP | RVP type | Rework RVP | Allocation

用法：
  python excel_to_nodes.py <excel_file.xlsx> [--sheet Sheet1] [--port 8000] [--out nodes.json]
"""

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict
from typing import Optional

try:
    import openpyxl
except ImportError:
    sys.exit("请先安装依赖: pip install openpyxl")


# Excel 列名 -> JSON 字段名 映射（小写匹配）
COL_MAP = {
    "silicon":     "silicon",
    "qdf":         "qdf",
    "camera":      "camera",
    "device name": "device_name",
    "rvp ip":      "name",          # name 字段与 RVP IP 保持一致
    "rvp type":    "rvp_type",
    "rework rvp":  "rework_rvp",
    "allocation":  "allocation",
}

REQUIRED_COLS = {"rvp ip", "rvp type"}


def parse_args():
    p = argparse.ArgumentParser(description="Excel -> nodes.json")
    p.add_argument("excel", help="Excel 文件路径")
    p.add_argument("--sheet", default=None, help="Sheet 名称（默认第一个）")
    p.add_argument("--port", type=int, default=8000, help="节点端口（默认 8000）")
    p.add_argument("--out", default="nodes.json", help="输出文件（默认 nodes.json）")
    return p.parse_args()


def load_sheet(excel_path: str, sheet_name: Optional[str]):
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
    return ws


def normalize(s) -> str:
    return str(s).strip().lower() if s is not None else ""


def build_nodes(ws, port: int) -> dict:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        sys.exit("Excel 为空")

    # 找列头（第一行）
    header = [normalize(c) for c in rows[0]]
    missing = REQUIRED_COLS - set(header)
    if missing:
        sys.exit(f"缺少必需列: {missing}  实际列: {header}")

    col_idx = {col: header.index(col) for col in header if col in COL_MAP}

    # 统计每种 rvp_type 出现次数，用于生成 GC-1, DC-1 ...
    type_counter: dict[str, int] = defaultdict(int)
    nodes = {}

    for row in rows[1:]:
        def cell(col_name: str) -> str:
            idx = col_idx.get(col_name)
            if idx is None:
                return ""
            v = row[idx]
            return str(v).strip() if v is not None else ""

        rvp_ip   = cell("rvp ip")
        rvp_type = cell("rvp type").upper()

        if not rvp_ip or not rvp_type:
            continue  # 跳过空行

        type_counter[rvp_type] += 1
        node_id = f"{rvp_type}-{type_counter[rvp_type]}"

        entry: dict = {
            "name":     rvp_ip,
            "base_url": f"http://{rvp_ip}:{port}",
        }

        # 追加其他字段
        for excel_col, json_field in COL_MAP.items():
            if excel_col in ("rvp ip", "rvp type") or excel_col not in col_idx:
                continue
            val = cell(excel_col)
            if val:
                entry[json_field] = val

        nodes[node_id] = entry

    return nodes


def main():
    args = parse_args()
    excel_path = Path(args.excel)
    if not excel_path.exists():
        sys.exit(f"找不到文件: {excel_path}")

    ws = load_sheet(str(excel_path), args.sheet)
    nodes = build_nodes(ws, args.port)

    if not nodes:
        sys.exit("未读取到任何节点，请检查 Excel 内容")

    out_path = Path(args.out)
    out_path.write_text(json.dumps(nodes, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ 已生成 {out_path}，共 {len(nodes)} 个节点：")
    for nid, info in nodes.items():
        print(f"  {nid}: {info['name']}")


if __name__ == "__main__":
    main()
