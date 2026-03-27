from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

import gspread
from flask import Flask, jsonify, render_template, request
from google.oauth2.service_account import Credentials

app = Flask(__name__)

LIFF_ID = "2009616560-k85q2AlU"
GOOGLE_SHEET_NAME = "蛋白每日結算系統"


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(str(value).replace(",", "").strip())
    except Exception:
        return default


def get_gspread_client():
    """
    兩種方式擇一：
    1. 本機測試：用 credentials.json 檔案
    2. Render：用環境變數 GOOGLE_SERVICE_ACCOUNT_JSON
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    if service_account_json:
        info = json.loads(service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)

    return gspread.authorize(creds)


def write_to_google_sheets(result: dict):
    client = get_gspread_client()
    spreadsheet = client.open(GOOGLE_SHEET_NAME)

    daily_ws = spreadsheet.worksheet("daily_settlement")
    expense_ws = spreadsheet.worksheet("expense_details")

    daily_ws.append_row([
        result["id"],
        result["date"],
        result["store"],
        result["operator_name"],
        result["revenue_a"],
        result["expense_total_b"],
        result["should_cash_c"],
        result["actual_cash_d"],
        result["diff_e"],
        result["status"],
        result["note"],
        result["line_user_id"],
        result["line_group_id"],
        result["submitted_at"],
    ])

    for item in result["expenses"]:
        expense_ws.append_row([
            result["id"],
            result["date"],
            result["store"],
            item["name"],
            item["amount"],
            result["operator_name"],
            result["submitted_at"],
        ])


@app.route("/")
def home():
    return "LINE Settlement Bot is running."


@app.route("/liff/settlement")
def liff_settlement():
    return render_template("settlement.html", liff_id=LIFF_ID)


@app.route("/api/settlement/submit", methods=["POST"])
def submit_settlement():
    data = request.get_json(silent=True) or {}

    store = data.get("store", "")
    date = data.get("date", "")
    operator_name = data.get("operator_name", "")
    revenue_a = to_int(data.get("revenue_a"))
    actual_cash_d = to_int(data.get("actual_cash_d"))
    note = (data.get("note") or "").strip()
    line_user_id = (data.get("line_user_id") or "").strip()
    line_group_id = (data.get("line_group_id") or "").strip()

    expenses = data.get("expenses", [])
    expense_total_b = 0
    normalized_expenses = []

    for item in expenses:
        name = (item.get("name") or "").strip()
        amount = to_int(item.get("amount"))
        if not name and amount == 0:
            continue
        normalized_expenses.append({
            "name": name,
            "amount": amount
        })
        expense_total_b += amount

    should_cash_c = revenue_a - expense_total_b
    diff_e = actual_cash_d - should_cash_c
    status = "異常提醒" if abs(diff_e) > 300 else "正常"

    settlement_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]

    result = {
        "id": settlement_id,
        "store": store,
        "date": date,
        "operator_name": operator_name,
        "revenue_a": revenue_a,
        "expense_total_b": expense_total_b,
        "should_cash_c": should_cash_c,
        "actual_cash_d": actual_cash_d,
        "diff_e": diff_e,
        "status": status,
        "note": note,
        "expenses": normalized_expenses,
        "line_user_id": line_user_id,
        "line_group_id": line_group_id,
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    write_to_google_sheets(result)

    return jsonify({
        "ok": True,
        "result": result
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)