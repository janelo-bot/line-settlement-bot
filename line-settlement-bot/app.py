from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

LIFF_ID = "2009616560-k85q2AlU"


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(str(value).replace(",", "").strip())
    except Exception:
        return default


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

    result = {
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
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    return jsonify({
        "ok": True,
        "result": result
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)