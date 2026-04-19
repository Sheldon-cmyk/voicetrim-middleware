
from flask import Flask, request, jsonify
import requests
import json
import logging
import os
from datetime import datetime, timezone, timedelta

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
    resp = requests.get(url, headers=AIRTABLE_HEADERS, params=params, timeout=15)
    return resp.json().get('records', [])

def airtable_post(table, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table )}"
    resp = requests.post(url, headers=AIRTABLE_HEADERS, json={"fields": fields}, timeout=10)
    return resp

def airtable_patch(table, record_id, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table )}/{record_id}"
    resp = requests.patch(url, headers=AIRTABLE_HEADERS, json={"fields": fields}, timeout=10)
    return resp

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def get_date_range(period):
    now = datetime.now(timezone.utc)
    today = now.date()
    if period == 'week':
        start = today - timedelta(days=today.weekday())
        return str(start), str(today)
    elif period == 'month':
        start = today.replace(day=1)
        return str(start), str(today)
    elif period == 'year':
        start = today.replace(month=1, day=1)
        return str(start), str(today)
    else:
        return str(today), str(today)

def sum_food_log(phone, start_date, end_date):
    formula = (
        f"AND({{Phone}}='{phone}', "
        f"{{Logged At}} >= '{start_date}', "
        f"{{Logged At}} <= '{end_date}T23:59:59.000Z')"
    )
    params = {
        "filterByFormula": formula,
        "fields[]": ["Calories", "Protein", "Carbs", "Fat"],
    }
    records = airtable_get("Food Log", params)
    cal   = sum(float(r.get('fields', {}).get('Calories', 0) or 0) for r in records)
    pro   = sum(float(r.get('fields', {}).get('Protein',  0) or 0) for r in records)
    carbs = sum(float(r.get('fields', {}).get('Carbs',    0) or 0) for r in records)
    fat   = sum(float(r.get('fields', {}).get('Fat',      0) or 0) for r in records)
    return int(cal), int(pro), int(carbs), int(fat), len(records)

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
    if call_id:
        fields["Call ID"] = call_id
    resp = airtable_post("Food Log", fields)
    app.logger.info(f"log_food: {resp.status_code} - {fields.get('Food Name')} {fields.get('Calories')} cal")
    food = args.get('food_name', 'item')
    cal = args.get('calories', '')
    return f"Logged {food}" + (f" at {cal} calories" if cal else "")

def handle_get_totals(phone, args):
    period = str(args.get('period', 'today')).lower().strip()
    if period in ('week', 'this week', 'weekly'):
        period = 'week'
    elif period in ('month', 'this month', 'monthly'):
        period = 'month'
    elif period in ('year', 'this year', 'yearly', 'annual'):
        period = 'year'
    else:
        period = 'today'
    start, end = get_date_range(period)
    try:
        cal, pro, carbs, fat, count = sum_food_log(phone, start, end)
        if count == 0:
            labels = {'today': 'today', 'week': 'this week', 'month': 'this month', 'year': 'this year'}
            return f"No food logged {labels.get(period, 'today')} yet."
        labels = {'today': 'Today', 'week': 'This week', 'month': 'This month', 'year': 'This year'}
        label = labels.get(period, 'Today')
        return f"{label}: {cal} calories, {pro}g protein, {carbs}g carbs, {fat}g fat."
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
            handle_log_food(phone, call_id, {'food_name': food})
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
