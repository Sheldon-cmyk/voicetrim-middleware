"""
VoiceTrim Middleware Server
Receives Vapi tool calls and writes directly to Airtable.
"""
from flask import Flask, request, jsonify
import requests
import json
import logging
import os
from datetime import datetime, timezone

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "REDACTED_TOKEN")
AIRTABLE_BASE = os.environ.get("AIRTABLE_BASE", "appJbb8o6E2TmCHNR")

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

MAKE_WEBHOOKS = {
    "get_totals":         "https://hook.us2.make.com/q1hare1nb12u8lblgxrpp2pzv8di87fz",
    "save_usual":         "https://hook.us2.make.com/u1d41yyzb3uacyn3ye3o9sn3k0bzaxp2",
    "log_usual":          "https://hook.us2.make.com/ra7zfl46u51x22ju1djxuixl7orrzwv1",
    "log_shopping_item":  "https://hook.us2.make.com/p5j4ulkoqhp3lgd6elywg63x24tmkptr",
    "save_meal_plan":     "https://hook.us2.make.com/vxlfupd3uq5ddupdxqqj0bx53owzm4px",
    "send_summary_email": "https://hook.us2.make.com/xjctrvmkg359ljkrgvc4bu3hl781vvco",
}

def parse_args(func):
    args_raw = func.get('arguments', '{}')
    if isinstance(args_raw, str):
        try:
            return json.loads(args_raw)
        except:
            return {}
    return args_raw or {}

def extract_call_info(body):
    message = body.get('message', {})
    call = message.get('call', {})
    phone = call.get('customer', {}).get('number', '')
    call_id = call.get('id', '')
    tool_calls = message.get('toolCalls', message.get('toolCallList', []))
    return phone, call_id, tool_calls

def log_food_to_airtable(phone, call_id, args):
    now = datetime.now(timezone.utc).isoformat()
    fields = {
        "Phone": phone,
        "Food Name": str(args.get('food_name', '')),
        "Logged At": now,
    }
    for field, key in [("Calories", "calories"), ("Protein", "protein"),
                        ("Carbs", "carbs"), ("Fat", "fat")]:
        val = args.get(key)
        if val is not None:
            try:
                fields[field] = float(val)
            except (ValueError, TypeError):
                pass
    meal_type = args.get('meal_type')
    if meal_type:
        fields["Meal Type"] = meal_type
    if call_id:
        fields["Call ID"] = call_id

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/Food%20Log"
    resp = requests.post(url, headers=AIRTABLE_HEADERS, json={"fields": fields}, timeout=10)
    app.logger.info(f"Food Log: {resp.status_code} - {fields.get('Food Name')} {fields.get('Calories')} cal")
    return resp.status_code in (200, 201)

def update_daily_log(phone, args):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    search_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/Daily%20Logs"
    params = {
        "filterByFormula": f"AND({{Phone}}='{phone}', DATETIME_FORMAT({{Date}}, 'YYYY-MM-DD')='{today}')",
        "maxRecords": 1
    }
    try:
        calories = float(args.get('calories', 0) or 0)
        protein = float(args.get('protein', 0) or 0)
        carbs = float(args.get('carbs', 0) or 0)
        fat = float(args.get('fat', 0) or 0)

        search_resp = requests.get(search_url, headers=AIRTABLE_HEADERS, params=params, timeout=10)
        existing = search_resp.json().get('records', [])

        if existing:
            record_id = existing[0]['id']
            current = existing[0].get('fields', {})
            update_fields = {
                "Total Calories": float(current.get('Total Calories', 0) or 0) + calories,
                "Total Protein": float(current.get('Total Protein', 0) or 0) + protein,
                "Total Carbs": float(current.get('Total Carbs', 0) or 0) + carbs,
                "Total Fat": float(current.get('Total Fat', 0) or 0) + fat,
            }
            requests.patch(f"{search_url}/{record_id}", headers=AIRTABLE_HEADERS, json={"fields": update_fields}, timeout=10)
        else:
            new_fields = {
                "Phone": phone,
                "Date": today,
                "Total Calories": calories,
                "Total Protein": protein,
                "Total Carbs": carbs,
                "Total Fat": fat,
            }
            requests.post(search_url, headers=AIRTABLE_HEADERS, json={"fields": new_fields}, timeout=10)
    except Exception as e:
        app.logger.error(f"Daily log error: {e}")

@app.route('/tool/<tool_name>', methods=['POST'])
def handle_tool(tool_name):
    try:
        body = request.json or {}
        phone, call_id, tool_calls = extract_call_info(body)
        results = []

        for tc in tool_calls:
            tc_id = tc.get('id', 'unknown')
            func = tc.get('function', {})
            name = func.get('name', tool_name)
            args = parse_args(func)

            app.logger.info(f"Tool: {name}, Phone: {phone}, Args: {args}")

            if name == 'log_food':
                success = log_food_to_airtable(phone, call_id, args)
                if success:
                    update_daily_log(phone, args)
                food = args.get('food_name', 'item')
                cal = args.get('calories', '')
                result_text = f"Logged {food}" + (f" at {cal} calories" if cal else "")
            else:
                make_url = MAKE_WEBHOOKS.get(name)
                if make_url:
                    payload = {"phone": phone, "call_id": call_id, "tool_name": name, **args}
                    try:
                        resp = requests.post(make_url, json=payload, timeout=10)
                        result_text = resp.text[:200] if resp.text and name == 'get_totals' else "done"
                    except:
                        result_text = "done"
                else:
                    result_text = "done"

            results.append({"toolCallId": tc_id, "result": result_text})

        return jsonify({"results": results})

    except Exception as e:
        app.logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"results": [{"toolCallId": "error", "result": "logged"}]}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "VoiceTrim Middleware"})

@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": "VoiceTrim Middleware", "status": "running"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5556))
    app.run(host='0.0.0.0', port=port)
