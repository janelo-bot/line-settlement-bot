from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime
from typing import Any

import gspread
import requests
from flask import Flask, abort, jsonify, render_template, request
from google.oauth2.service_account import Credentials

app = Flask(__name__)

LIFF_ID = "2009616560-k85q2AlU"
GOOGLE_SHEET_NAME = "便當店每日結算系統"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(str(value).replace(",", "").strip())
    except Exception:
        return default


def get_gspread_client():
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


def verify_line_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return False

    hash_bytes = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected_signature = base64.b64encode(hash_bytes).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


def push_line_message(to: str, text: str):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise ValueError("缺少 LINE_CHANNEL_ACCESS_TOKEN")

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": to,
        "messages": [{"type": "text", "text": text}]
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"LINE push 失敗：{resp.status_code} {resp.text}")


def upsert_group_binding(store_name: str, group_id: str):
    client = get_gspread_client()
    spreadsheet = client.open(GOOGLE_SHEET_NAME)
    ws = spreadsheet.worksheet("group_bindings")

    values = ws.get_all_values()

    # 第一列是標題，從第二列開始找
    found_row = None
    for idx, row in enumerate(values[1:], start=2):
        if len(row) >= 1 and row[0].strip() == store_name:
            found_row = idx
            break

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if found_row:
        ws.update(f"A{found_row}:C{found_row}", [[store_name, group_id, now_str]])
    else:
        ws.append_row([store_name, group_id, now_str])


def get_group_id_by_store(store_name: str) -> str:
    client = get_gspread_client()
    spreadsheet = client.open(GOOGLE_SHEET_NAME)
    ws = spreadsheet.worksheet("group_bindings")

    values = ws.get_all_values()
    for row in values[1:]:
        if len(row) >= 2 and row[0].strip() == store_name:
            return row[1].strip()
    return ""


def build_settlement_text(result: dict) -> str:
    lines = [
        f'{result["store"]}｜{result["date"]} 每日結算完成',
        "",
        f'營業額(A)：{result["revenue_a"]:,}',
        f'支出(B)：{result["expense_total_b"]:,}',
        f'實際現金(D)：{result["actual_cash_d"]:,}',
        "",
        f'應收現金(C)：{result["should_cash_c"]:,}',
        f'溢短收(E)：{result["diff_e"]:,}',
        f'狀態：{result["status"]}',
    ]

    if result["note"]:
        lines.extend(["", f'備註：{result["note"]}'])

    lines.extend(["", f'填表人：{result["operator_name"]}'])
    return "\n".join(lines)


@app.route("/")
def home():
    return "LINE Settlement Bot is running."


@app.route("/liff/settlement")
def liff_settlement():
    return render_template("settlement.html", liff_id=LIFF_ID)


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_line_signature(body, signature):
        abort(400, "Invalid signature")

    payload = request.get_json(silent=True) or {}
    events = payload.get("events", [])

    for event in events:
        event_type = event.get("type")
        source = event.get("source", {})
        source_type = source.get("type")
        group_id = source.get("groupId", "")

        # bot 被加入群組時
        if event_type == "join" and source_type == "group" and group_id:
            try:
                push_line_message(group_id, "已加入群組。請輸入：綁定 后庄店 或 綁定 霧峰店")
            except Exception:
                pass

        # 收群組文字訊息
        if event_type == "message" and source_type == "group" and group_id:
            message = event.get("message", {})
            if message.get("type") != "text":
                continue

            text = (message.get("text") or "").strip()

            if text.startswith("綁定 "):
                store_name = text.replace("綁定 ", "", 1).strip()

                if store_name not in ["后庄店", "霧峰店"]:
                    push_line_message(group_id, "店名只能綁定：后庄店 或 霧峰店")
                    continue

                upsert_group_binding(store_name, group_id)
                push_line_message(group_id, f"綁定完成：{store_name}")

    return "OK", 200


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

    # 不再依賴 LIFF 的 groupId，改由店別去查綁定表
    line_group_id = get_group_id_by_store(store)

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

    if line_group_id:
        message_text = build_settlement_text(result)
        push_line_message(line_group_id, message_text)

    return jsonify({
        "ok": True,
        "result": result
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
