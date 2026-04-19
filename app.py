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

TIMEZONE_OFFSETS = {
    'est': -5, 'edt': -4, 'eastern': -5,
    'cst': -6, 'cdt': -5, 'central': -6,
    'mst': -7, 'mdt': -6, 'mountain': -7,
    'pst': -8, 'pdt': -7, 'pacific': -8,
    'akst': -9, 'akdt': -8, 'alaska': -9,
    'hst': -10, 'hawaii': -10,
    'gmt': 0, 'utc': 0,
    'bst': 1, 'ist': 1,
    'cet': 1, 'cest': 2,
    'eet': 2, 'eest': 3,
    'msk': 3,
    'ist_india': 5.5, 'india': 5.5,
    'cst_china': 8, 'china': 8, 'sgt': 8, 'singapore': 8,
    'jst': 9, 'japan': 9, 'kst': 9, 'korea': 9,
    'aest': 10, 'aedt': 11, 'australia_east': 10,
    'nzst': 12, 'nzdt': 13,
    'eat': 3, 'cat': 2, 'wat': 1,
    'gst': 4,
    'art': -3, 'brt': -3, 'brazil': -3,
    'clst': -3, 'clt': -4, 'chile': -4,
    'pert': -5, 'colombia': -5,
    'mxt': -6, 'mexico': -6,
}

def get_user_timezone(phone):
    try:
        params = {"filterByFormula": f"{{Phone}}='{phone}'", "fields[]": ["Timezone"], "maxRecords": 1}
        records = airtable_get("Users", params)
        if records:
            tz = records[0].get('fields', {}).get('Timezone', '')
            if tz:
                return tz.strip()
    except Exception as e:
        app.logger.warning(f"Could not get timezone for {phone}: {e}")
    return 'UTC'

def parse_timezone_to_offset(tz_str):
    if not tz_str:
        return 0
    tz_lower = tz_str.lower().strip()
    import re
    match = re.search(r'([+-])(\d{1,2})(?::(\d{2}))?', tz_str)
    if match:
        sign = 1 if match.group(1) == '+' else -1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        return sign * (hours + minutes / 60)
    for key, offset in TIMEZONE_OFFSETS.items():
        if key in tz_lower:
            return offset
    return 0

def get_local_date_range(phone, period):
    tz_str = get_user_timezone(phone)
    offset_hours = parse_timezone_to_offset(tz_str)
    offset = timedelta(hours=offset_hours)
    now_local = datetime.now(timezone.utc) + offset
    today_local = now_local.date()
    if period == 'week':
        start_local = today_local - timedelta(days=today_local.weekday())
        end_local = today_local
    elif period == 'month':
        start_local = today_local.replace(day=1)
        end_local = today_local
    elif period == 'year':
        start_local = today_local.replace(month=1, day=1)
        end_local = today_local
    else:
        start_local = today_local
        end_local = today_local
    start_utc = datetime(start_local.year, start_local.month, start_local.day, 0, 0, 0, tzinfo=timezone.utc) - offset
    end_utc = datetime(end_local.year, end_local.month, end_local.day, 23, 59, 59, tzinfo=timezone.utc) - offset
    return start_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'), end_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')

def sum_food_log(phone, start_utc, end_utc):
    formula = f"AND({{Phone}}='{phone}', {{Logged At}} >= '{start_utc}', {{Logged At}} <= '{end_utc}')"
    params = {"filterByFormula": formula, "fields[]": ["Calories", "Protein", "Carbs", "Fat"]}
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
    try:
        start_utc, end_utc = get_local_date_range(phone, period)
        cal, pro, carbs, fat, count = sum_food_log(phone, start_utc, end_utc)
        if count == 0:
            labels = {'today': 'today', 'week': 'this week', 'month': 'this month', 'year': 'this year'}
            return f"No food logged {labels.get(period, 'today')} yet."
        labels = {'today': 'Today', 'week': 'This week', 'month': 'This month', 'year': 'This year'}
        label = labels.get(period, 'Today')
        return f"{label}: {cal} calories, {pro}g protein, {carbs}g carbs, {fat}g fat."
    except Exception as e:
        app.logger.error(f"handle_get_totals error: {e}", exc_info=True)
        return "I couldn't retrieve your totals right now. Please try again."

def handle_save_profile(phone, args):
    try:
        params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
        records = airtable_get("Users", params)
        fields = {"Phone": phone}
        if args.get('timezone'):
            fields["Timezone"] = str(args['timezone'])
        if args.get('name'):
            fields["Name"] = str(args['name'])
        if args.get('email'):
            fields["Email"] = str(args['email'])
        if records:
            airtable_patch("Users", records[0]['id'], fields)
        else:
            airtable_post("Users", fields)
        tz = args.get('timezone', '')
        return f"Profile saved." + (f" Timezone set to {tz}." if tz else "")
    except Exception as e:
        app.logger.error(f"handle_save_profile error: {e}")
        return "Profile saved."

def handle_save_usual(phone, args):
    meal_name = args.get('meal_name', '')
    foods = args.get('foods', '')
    try:
        airtable_post("Usual Meals", {"Phone": phone, "Meal Name": str(meal_name), "Foods": str(foods), "Saved At": now_utc()})
        return f"Saved '{meal_name}' as a usual meal."
    except Exception as e:
        app.logger.error(f"handle_save_usual error: {e}")
        return "Saved."

def handle_log_usual(phone, call_id, args):
    meal_name = args.get('meal_name', '')
    try:
        params = {"filterByFormula": f"AND({{Phone}}='{phone}', {{Meal Name}}='{meal_name}')", "maxRecords": 1}
        records = airtable_get("Usual Meals", params)
        if not records:
            return f"I couldn't find a usual meal called '{meal_name}'."
        foods_str = records[0].get('fields', {}).get('Foods', '')
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
        airtable_post("Shopping List", {"Phone": phone, "Item": str(item), "Quantity": str(qty), "Added At": now_utc()})
        return f"Added {item} to your shopping list."
    except Exception as e:
        app.logger.error(f"handle_log_shopping_item error: {e}")
        return "Added."

def handle_save_meal_plan(phone, args):
    day = args.get('day', '')
    meal = args.get('meal', '')
    foods = args.get('foods', '')
    try:
        airtable_post("Meal Plans", {"Phone": phone, "Day": str(day), "Meal": str(meal), "Foods": str(foods), "Saved At": now_utc()})
        return f"Saved meal plan for {day} {meal}."
    except Exception as e:
        app.logger.error(f"handle_save_meal_plan error: {e}")
        return "Saved."

def handle_send_summary_email(phone, args):
    app.logger.info(f"send_summary_email requested for {phone}")
    return "I'll send your summary shortly."

TOOL_HANDLERS = {
    'log_food':           lambda phone, call_id, args: handle_log_food(phone, call_id, args),
    'get_totals':         lambda phone, call_id, args: handle_get_totals(phone, args),
    'save_profile':       lambda phone, call_id, args: handle_save_profile(phone, args),
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
