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
GOOGLE_SHEET_NAME = "蛋白每日結算系統"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_LOGIN_CHANNEL_ID = os.getenv("LINE_LOGIN_CHANNEL_ID", "").strip()


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


def verify_line_id_token(id_token: str) -> dict:
    if not id_token:
        return {}

    if not LINE_LOGIN_CHANNEL_ID:
        raise RuntimeError("缺少 LINE_LOGIN_CHANNEL_ID")

    url = "https://api.line.me/oauth2/v2.1/verify"
    data = {
        "id_token": id_token,
        "client_id": LINE_LOGIN_CHANNEL_ID,
    }

    resp = requests.post(url, data=data, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"驗證 ID Token 失敗：{resp.status_code} {resp.text}")

    return resp.json()
    

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
        f'今日營業額：{result["revenue_a"]:,}',
        f'現金收入(A)：{result["cash_a"]:,}',
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
                
            elif text == "結算":
                bound_store = ""
                client = get_gspread_client()
                spreadsheet = client.open(GOOGLE_SHEET_NAME)
                ws = spreadsheet.worksheet("group_bindings")
                values = ws.get_all_values()

                for row in values[1:]:
                    if len(row) >= 2 and row[1].strip() == group_id:
                        bound_store = row[0].strip()
                        break

                if not bound_store:
                    push_line_message(group_id, "此群組尚未綁定分店，請先輸入：綁定 后庄店 或 綁定 霧峰店")
                    continue

                liff_url = f"https://liff.line.me/{LIFF_ID}?store={bound_store}"
                push_line_message(
                    group_id,
                    "請點以下連結填寫每日結算：\n"
                    f"{liff_url}\n\n"
                    f"目前分店：{bound_store}\n"
                    "請務必從 LINE 群組內開啟，不要複製到外部瀏覽器。"
                )

    return "OK", 200


@app.route("/api/settlement/submit", methods=["POST"])
def submit_settlement():
    data = request.get_json(silent=True) or {}

    store = (data.get("store") or "").strip()
    date = (data.get("date") or "").strip()
    frontend_operator_name = (data.get("operator_name") or "").strip()
    revenue_a = to_int(data.get("revenue_a"))
    cash_a = to_int(data.get("cash_a"))
    linepay_a = to_int(data.get("linepay_a"))
    ubereats_a = to_int(data.get("ubereats_a"))
    foodpanda_a = to_int(data.get("foodpanda_a"))
    ocard_a = to_int(data.get("ocard_a"))
    actual_cash_d = to_int(data.get("actual_cash_d"))
    note = (data.get("note") or "").strip()
    id_token = (data.get("id_token") or "").strip()

    if store not in ["后庄店", "霧峰店"]:
        return jsonify({
            "ok": False,
            "error": "分店資料錯誤"
        }), 400

    if not date:
        return jsonify({
            "ok": False,
            "error": "缺少日期"
        }), 400

    if revenue_a <= 0:
        return jsonify({
            "ok": False,
            "error": "營業額必須大於 0"
        }), 400

    if actual_cash_d < 0:
        return jsonify({
            "ok": False,
            "error": "實際現金不可小於 0"
        }), 400

    if not id_token:
        return jsonify({
            "ok": False,
            "error": "請從 LINE 群組內點擊「結算」開啟表單再送出"
        }), 400

    verified_user_id = ""
    verified_user_name = frontend_operator_name

    try:
        token_info = verify_line_id_token(id_token)
        verified_user_id = (token_info.get("sub") or "").strip()
        verified_user_name = (token_info.get("name") or "").strip() or frontend_operator_name
    except Exception as e:
        print(f"ID Token 驗證失敗：{e}")
        return jsonify({
            "ok": False,
            "error": "LINE 身分驗證失敗，請從 LINE 群組重新開啟表單"
        }), 400

    if not verified_user_id:
        return jsonify({
            "ok": False,
            "error": "無法取得填表人身分，請從 LINE 群組重新開啟表單"
        }), 400

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

    should_cash_c = cash_a - expense_total_b
    diff_e = actual_cash_d - should_cash_c
    status = "異常提醒" if abs(diff_e) > 100 else "正常"

    settlement_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]

    # 不再依賴 LIFF 的 groupId，改由店別去查綁定表
    line_group_id = get_group_id_by_store(store)

    result = {
        "id": settlement_id,
        "store": store,
        "date": date,
        "operator_name": verified_user_name,
        "revenue_a": revenue_a,
        "cash_a": cash_a,
        "linepay_a": linepay_a,
        "ubereats_a": ubereats_a,
        "foodpanda_a": foodpanda_a,
        "ocard_a": ocard_a,
        "expense_total_b": expense_total_b,
        "should_cash_c": should_cash_c,
        "actual_cash_d": actual_cash_d,
        "diff_e": diff_e,
        "status": status,
        "note": note,
        "expenses": normalized_expenses,
        "line_user_id": verified_user_id,
        "line_group_id": line_group_id,
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        write_to_google_sheets(result)
    except Exception as e:
        print(f"寫入 Google Sheets 失敗：{e}")
        return jsonify({
            "ok": False,
            "error": "寫入 Google Sheets 失敗"
        }), 500

    try:
        if line_group_id:
            message_text = build_settlement_text(result)
            push_line_message(line_group_id, message_text)
    except Exception as e:
        print(f"LINE push 失敗：{e}")
        return jsonify({
            "ok": False,
            "error": "已寫入資料，但回傳群組失敗"
        }), 500

    return jsonify({
        "ok": True,
        "result": result
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
