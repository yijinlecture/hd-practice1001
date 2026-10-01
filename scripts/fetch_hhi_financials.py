#!/usr/bin/env python3
import csv
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATHS = [BASE_DIR / ".env"]
OUTPUT_JSON = BASE_DIR / "data" / "financials.json"
OUTPUT_CSV = BASE_DIR / "data" / "financials.csv"

CORP_CODE = "01390344"
CORP_NAME = "HD현대중공업"
BSNS_YEAR = "2025"
REPRT_CODE = "11011"
FS_DIV = "CFS"


class RowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.current_row = None
        self.current_cell = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.current_row = []
        elif tag in ("td", "th") and self.current_row is not None:
            self.current_cell = []

    def handle_data(self, data):
        if self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.current_row is not None:
            text = " ".join("".join(self.current_cell or []).split())
            self.current_row.append(text)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None


def load_env():
    values = {}
    for path in ENV_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def api_get(path, params, raw=False):
    url = "https://opendart.fss.or.kr/api/" + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read()
    if raw:
        return body
    return json.loads(body.decode("utf-8"))


def parse_amount(value):
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "-"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    digits = re.sub(r"[^0-9.]", "", text)
    if not digits:
        return None
    amount = int(float(digits))
    return -amount if negative else amount


def get_financial_statement_items(api_key):
    data = api_get(
        "fnlttSinglAcntAll.json",
        {
            "crtfc_key": api_key,
            "corp_code": CORP_CODE,
            "bsns_year": BSNS_YEAR,
            "reprt_code": REPRT_CODE,
            "fs_div": FS_DIV,
        },
    )
    if data.get("status") != "000":
        raise RuntimeError(f"OpenDART financial API error: {data.get('status')} {data.get('message')}")

    wanted = {
        "ifrs-full_Revenue": "매출액",
        "dart_OperatingIncomeLoss": "영업이익",
    }
    items = {}
    rcept_no = None
    for row in data.get("list", []):
        rcept_no = rcept_no or row.get("rcept_no")
        account_id = row.get("account_id")
        if account_id in wanted:
            items[wanted[account_id]] = {
                "label": wanted[account_id],
                "account_id": account_id,
                "account_nm": row.get("account_nm"),
                "statement": row.get("sj_nm"),
                "amount": parse_amount(row.get("thstrm_amount")),
                "unit": row.get("currency", "KRW"),
                "raw_amount": row.get("thstrm_amount"),
                "source": "OpenDART fnlttSinglAcntAll",
            }

    missing = [label for label in wanted.values() if label not in items]
    if missing:
        raise RuntimeError(f"Missing financial statement items: {', '.join(missing)}")
    if not rcept_no:
        raise RuntimeError("Missing receipt number from financial statement response")
    return items, rcept_no


def read_report_xml(api_key, rcept_no):
    body = api_get("document.xml", {"crtfc_key": api_key, "rcept_no": rcept_no}, raw=True)
    if not zipfile.is_zipfile(io.BytesIO(body)):
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenDART document API did not return a zip file: {text[:300]}")

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = sorted(zf.namelist(), key=lambda name: zf.getinfo(name).file_size, reverse=True)
        with zf.open(names[0]) as fp:
            return fp.read().decode("utf-8", errors="ignore")


def parse_order_backlog(xml_text):
    marker = "기말수주잔고"
    marker_pos = xml_text.find(marker)
    if marker_pos == -1:
        raise RuntimeError("Could not find order backlog table marker in report XML")

    table_start = xml_text.rfind("<TABLE", 0, marker_pos)
    table_end = xml_text.find("</TABLE>", marker_pos)
    if table_start == -1 or table_end == -1:
        raise RuntimeError("Could not isolate order backlog table")

    parser = RowParser()
    parser.feed(xml_text[table_start : table_end + len("</TABLE>")])

    rows = parser.rows
    body_rows = [row for row in rows if row and row[0] not in ("품목", "수량")]
    detail = []
    total_amount_million_krw = None

    for row in body_rows:
        name = row[0].replace(" ", "")
        last_amount = parse_amount(row[-1]) if row else None
        if name == "합계":
            total_amount_million_krw = last_amount
            continue
        detail.append(
            {
                "item": row[0],
                "due_date": row[2] if len(row) > 2 else None,
                "ending_backlog_million_krw": last_amount,
            }
        )

    if total_amount_million_krw is None:
        raise RuntimeError("Could not find total ending order backlog")

    return {
        "label": "수주잔고",
        "account_id": None,
        "account_nm": "기말수주잔고",
        "statement": "사업보고서 III. 재무에 관한 사항 외 수주상황",
        "amount": total_amount_million_krw * 1_000_000,
        "unit": "KRW",
        "raw_amount": f"{total_amount_million_krw:,}",
        "raw_unit": "백만원",
        "source": "OpenDART document.xml",
        "details": detail,
    }


def write_outputs(records, metadata):
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metadata": metadata, "financials": records}
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "company",
                "corp_code",
                "business_year",
                "report_name",
                "receipt_no",
                "metric",
                "amount",
                "unit",
                "raw_amount",
                "raw_unit",
                "source",
            ],
        )
        writer.writeheader()
        for row in records:
            writer.writerow(
                {
                    "company": metadata["company"],
                    "corp_code": metadata["corp_code"],
                    "business_year": metadata["business_year"],
                    "report_name": metadata["report_name"],
                    "receipt_no": metadata["receipt_no"],
                    "metric": row["label"],
                    "amount": row["amount"],
                    "unit": row["unit"],
                    "raw_amount": row["raw_amount"],
                    "raw_unit": row.get("raw_unit", row["unit"]),
                    "source": row["source"],
                }
            )


def main():
    api_key = load_env().get("DART_API_KEY")
    if not api_key:
        print("DART_API_KEY was not found in .env", file=sys.stderr)
        return 1

    items, rcept_no = get_financial_statement_items(api_key)
    report_xml = read_report_xml(api_key, rcept_no)
    backlog = parse_order_backlog(report_xml)

    records = [items["매출액"], items["영업이익"], backlog]
    metadata = {
        "company": CORP_NAME,
        "corp_code": CORP_CODE,
        "business_year": BSNS_YEAR,
        "report_name": "사업보고서 (2025.12)",
        "report_code": REPRT_CODE,
        "financial_statement_division": FS_DIV,
        "receipt_no": rcept_no,
        "currency_note": "매출액·영업이익은 연결재무제표 KRW, 수주잔고 원문 표는 백만원 단위를 KRW로 환산",
    }
    write_outputs(records, metadata)

    for row in records:
        print(f"{row['label']}: {row['amount']:,} {row['unit']}")
    print(f"Wrote {OUTPUT_JSON.relative_to(BASE_DIR)}")
    print(f"Wrote {OUTPUT_CSV.relative_to(BASE_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
