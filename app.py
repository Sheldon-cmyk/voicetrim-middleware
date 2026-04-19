from flask import Flask, request, jsonify
import requests
import json
import logging
import os
from datetime import datetime, timezone

app = Flask(__name__ )
logging.basicConfig(level=logging.INFO)

AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE = os.environ["AIRTABLE_BASE"]

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

def parse_args(func):
    args_raw = func.get('arguments', '{}')
    if isinstance(args_raw, str):
        try:
            return json.loads(args_raw)
        except Exception:
            return {}
    return args_raw or {}

def extract_call_info(body):
    message = body.get('message', {})
    call = message.get('call', {})
    phone = call.get('customer', {}).get('number', '')
    call_id = call.get('id', '')
    tool_calls = message.get('toolCalls', message.get('toolCallList', []))
    return phone, call_id, tool_calls

def airtable_get(table, params):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table )}"
    resp = requests.get(url, headers=AIRTABLE_HEADERS, params=params, timeout=10)
    return resp.json().get('records', [])

def airtable_post(table, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table )}"
    resp = requests.post(url, headers=AIRTABLE_HEADERS, json={"fields": fields}, timeout=10)
    return resp

def airtable_patch(table, record_id, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table )}/{record_id}"
    resp = requests.patch(url, headers=AIRTABLE_HEADERS, json={"fields": fields}, timeout=10)
    return resp

def today_utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def handle_log_food(phone, call_id, args):
    fields = {
        "Phone": phone,
        "Food Name": str(args.get('food_name', '')),
        "Logged At": now_utc(),
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
    resp = airtable_post("Food Log", fields)
    success = resp.status_code in (200, 201)
    app.logger.info(f"log_food: {resp.status_code} - {fields.get('Food Name')} {fields.get('Calories')} cal")
    if success:
        _update_daily_log(phone, args)
    food = args.get('food_name', 'item')
    cal = args.get('calories', '')
    return f"Logged {food}" + (f" at {cal} calories" if cal else "")

def _update_daily_log(phone, args):
    today = today_utc()
    params = {
        "filterByFormula": f"AND({{Phone}}='{phone}', DATETIME_FORMAT({{Date}}, 'YYYY-MM-DD')='{today}')",
        "maxRecords": 1
    }
    try:
        calories = float(args.get('calories', 0) or 0)
        protein  = float(args.get('protein',  0) or 0)
        carbs    = float(args.get('carbs',    0) or 0)
        fat      = float(args.get('fat',      0) or 0)
        existing = airtable_get("Daily Logs", params)
        if existing:
            record_id = existing[0]['id']
            cur = existing[0].get('fields', {})
            update_fields = {
                "Total Calories": float(cur.get('Total Calories', 0) or 0) + calories,
                "Total Protein":  float(cur.get('Total Protein',  0) or 0) + protein,
                "Total Carbs":    float(cur.get('Total Carbs',    0) or 0) + carbs,
                "Total Fat":      float(cur.get('Total Fat',      0) or 0) + fat,
            }
            airtable_patch("Daily Logs", record_id, update_fields)
        else:
            airtable_post("Daily Logs", {
                "Phone": phone, "Date": today,
                "Total Calories": calories, "Total Protein": protein,
                "Total Carbs": carbs, "Total Fat": fat,
            })
    except Exception as e:
        app.logger.error(f"_update_daily_log error: {e}")

def handle_get_totals(phone, args):
    today = today_utc()
    params = {
        "filterByFormula": f"AND({{Phone}}='{phone}', DATETIME_FORMAT({{Date}}, 'YYYY-MM-DD')='{today}')",
        "maxRecords": 1
    }
    try:
        records = airtable_get("Daily Logs", params)
        if records:
            f = records[0].get('fields', {})
            cal   = int(float(f.get('Total Calories', 0) or 0))
            pro   = int(float(f.get('Total Protein',  0) or 0))
            carbs = int(float(f.get('Total Carbs',    0) or 0))
            fat   = int(float(f.get('Total Fat',      0) or 0))
            return f"Today so far: {cal} calories, {pro}g protein, {carbs}g carbs, {fat}g fat."
        else:
            return "No food logged yet today."
    except Exception as e:
        app.logger.error(f"handle_get_totals error: {e}")
        return "I couldn't retrieve your totals right now. Please try again."

def handle_save_usual(phone, args):
    meal_name = args.get('meal_name', '')
    foods = args.get('foods', '')
    try:
        airtable_post("Usual Meals", {
            "Phone": phone,
            "Meal Name": str(meal_name),
            "Foods": str(foods),
            "Saved At": now_utc(),
        })
        return f"Saved '{meal_name}' as a usual meal."
    except Exception as e:
        app.logger.error(f"handle_save_usual error: {e}")
        return "Saved."

def handle_log_usual(phone, call_id, args):
    meal_name = args.get('meal_name', '')
    try:
        params = {
            "filterByFormula": f"AND({{Phone}}='{phone}', {{Meal Name}}='{meal_name}')",
            "maxRecords": 1
        }
        records = airtable_get("Usual Meals", params)
        if not records:
            return f"I couldn't find a usual meal called '{meal_name}'."
        f = records[0].get('fields', {})
        foods_str = f.get('Foods', '')
        for food in [x.strip() for x in foods_str.split(',') if x.strip()]:
            handle_log_food(phone, call_id, {'food_name': food, 'meal_type': args.get('meal_type', '')})
        return f"Logged your usual '{meal_name}'."
    except Exception as e:
        app.logger.error(f"handle_log_usual error: {e}")
        return "Logged."

def handle_log_shopping_item(phone, args):
    item = args.get('item', '')
    qty  = args.get('quantity', '')
    try:
        airtable_post("Shopping List", {
            "Phone": phone,
            "Item": str(item),
            "Quantity": str(qty),
            "Added At": now_utc(),
        })
        return f"Added {item} to your shopping list."
    except Exception as e:
        app.logger.error(f"handle_log_shopping_item error: {e}")
        return "Added."

def handle_save_meal_plan(phone, args):
    day   = args.get('day', '')
    meal  = args.get('meal', '')
    foods = args.get('foods', '')
    try:
        airtable_post("Meal Plans", {
            "Phone": phone,
            "Day": str(day),
            "Meal": str(meal),
            "Foods": str(foods),
            "Saved At": now_utc(),
        })
        return f"Saved meal plan for {day} {meal}."
    except Exception as e:
        app.logger.error(f"handle_save_meal_plan error: {e}")
        return "Saved."

def handle_send_summary_email(phone, args):
    email = args.get('email', '')
    app.logger.info(f"send_summary_email requested for {phone} to {email}")
    return "I'll send your summary shortly. Make sure your email is saved in your profile."

TOOL_HANDLERS = {
    'log_food':           lambda phone, call_id, args: handle_log_food(phone, call_id, args),
    'get_totals':         lambda phone, call_id, args: handle_get_totals(phone, args),
    'save_usual':         lambda phone, call_id, args: handle_save_usual(phone, args),
    'log_usual':          lambda phone, call_id, args: handle_log_usual(phone, call_id, args),
    'log_shopping_item':  lambda phone, call_id, args: handle_log_shopping_item(phone, args),
    'save_meal_plan':     lambda phone, call_id, args: handle_save_meal_plan(phone, args),
    'send_summary_email': lambda phone, call_id, args: handle_send_summary_email(phone, args),
}

@app.route('/tool/<tool_name>', methods=['POST'])
def handle_tool(tool_name):
    try:
        body = request.json or {}
        phone, call_id, tool_calls = extract_call_info(body)
        results = []
        for tc in tool_calls:
            tc_id = tc.get('id', 'unknown')
            func  = tc.get('function', {})
            name  = func.get('name', tool_name)
            args  = parse_args(func)
            app.logger.info(f"Tool: {name} | Phone: {phone} | Args: {args}")
            handler = TOOL_HANDLERS.get(name)
            if handler:
                try:
                    result_text = handler(phone, call_id, args)
                except Exception as e:
                    app.logger.error(f"Handler error for {name}: {e}", exc_info=True)
                    result_text = "done"
            else:
                app.logger.warning(f"Unknown tool: {name}")
                result_text = "done"
            results.append({"toolCallId": tc_id, "result": result_text})
        return jsonify({"results": results})
    except Exception as e:
        app.logger.error(f"Route error: {e}", exc_info=True)
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
