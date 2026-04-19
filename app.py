from flask import Flask, request, jsonify
import requests
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
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

def airtable_delete(table, record_id):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table )}/{record_id}"
    resp = requests.delete(url, headers=AIRTABLE_HEADERS, timeout=10)
    return resp

def now_utc():
    return datetime.now(timezone.utc).isoformat()

TIMEZONE_OFFSETS = {
    'est': -5, 'edt': -4, 'eastern': -5, 'new york': -5, 'miami': -5, 'boston': -5, 'atlanta': -5,
    'cst': -6, 'cdt': -5, 'central': -6, 'chicago': -6, 'dallas': -6, 'houston': -6,
    'mst': -7, 'mdt': -6, 'mountain': -7, 'denver': -7, 'phoenix': -7,
    'pst': -8, 'pdt': -7, 'pacific': -8, 'los angeles': -8, 'seattle': -8, 'san francisco': -8,
    'akst': -9, 'akdt': -8, 'alaska': -9, 'anchorage': -9,
    'hst': -10, 'hawaii': -10, 'honolulu': -10,
    'gmt': 0, 'utc': 0, 'london': 0,
    'bst': 1, 'ireland': 1, 'dublin': 1,
    'cet': 1, 'cest': 2, 'paris': 1, 'berlin': 1, 'rome': 1, 'madrid': 1, 'amsterdam': 1,
    'eet': 2, 'eest': 3, 'athens': 2, 'cairo': 2, 'johannesburg': 2,
    'msk': 3, 'moscow': 3, 'riyadh': 3, 'dubai': 4, 'abu dhabi': 4,
    'karachi': 5, 'islamabad': 5,
    'india': 5.5, 'ist': 5.5, 'mumbai': 5.5, 'delhi': 5.5, 'bangalore': 5.5,
    'dhaka': 6, 'colombo': 5.5,
    'bangkok': 7, 'jakarta': 7, 'hanoi': 7,
    'china': 8, 'beijing': 8, 'shanghai': 8, 'singapore': 8, 'sgt': 8,
    'hong kong': 8, 'taipei': 8, 'perth': 8,
    'japan': 9, 'jst': 9, 'tokyo': 9, 'osaka': 9, 'korea': 9, 'kst': 9, 'seoul': 9,
    'aest': 10, 'sydney': 10, 'melbourne': 10, 'brisbane': 10,
    'aedt': 11, 'adelaide': 9.5,
    'nzst': 12, 'auckland': 12, 'nzdt': 13,
    'brazil': -3, 'brt': -3, 'sao paulo': -3, 'rio': -3,
    'argentina': -3, 'buenos aires': -3,
    'chile': -4, 'colombia': -5, 'bogota': -5, 'lima': -5, 'peru': -5,
    'mexico': -6, 'mexico city': -6,
    'nigeria': 1, 'lagos': 1, 'nairobi': 3, 'eat': 3,
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
    start_utc = datetime(start_local.year, start_local.month, start_local.day,
                         0, 0, 0, tzinfo=timezone.utc) - offset
    end_utc = datetime(end_local.year, end_local.month, end_local.day,
                       23, 59, 59, tzinfo=timezone.utc) - offset
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

def handle_delete_food(phone, args):
    food_name = str(args.get('food_name', '')).strip()
    if not food_name:
        return "Which item would you like me to remove?"
    start_utc, end_utc = get_local_date_range(phone, 'today')
    formula = (
        f"AND({{Phone}}='{phone}', "
        f"{{Logged At}} >= '{start_utc}', "
        f"{{Logged At}} <= '{end_utc}', "
        f"FIND(LOWER('{food_name.lower()}'), LOWER({{Food Name}})) > 0)"
    )
    params = {
        "filterByFormula": formula,
        "sort[0][field]": "Logged At",
        "sort[0][direction]": "desc",
        "maxRecords": 1
    }
    try:
        records = airtable_get("Food Log", params)
        if not records:
            start_utc2, end_utc2 = get_local_date_range(phone, 'week')
            formula2 = (
                f"AND({{Phone}}='{phone}', "
                f"{{Logged At}} >= '{start_utc2}', "
                f"FIND(LOWER('{food_name.lower()}'), LOWER({{Food Name}})) > 0)"
            )
            params2 = {
                "filterByFormula": formula2,
                "sort[0][field]": "Logged At",
                "sort[0][direction]": "desc",
                "maxRecords": 1
            }
            records = airtable_get("Food Log", params2)
        if not records:
            return f"I don't see {food_name} in your recent logs. Nothing was removed."
        record = records[0]
        record_id = record['id']
        actual_name = record.get('fields', {}).get('Food Name', food_name)
        del_resp = airtable_delete("Food Log", record_id)
        if del_resp.status_code == 200:
            app.logger.info(f"delete_food: removed '{actual_name}' for {phone}")
            return f"Done, removed {actual_name} from your log."
        else:
            return f"Something went wrong removing {food_name}. Please try again."
    except Exception as e:
        app.logger.error(f"handle_delete_food error: {e}", exc_info=True)
        return "I had trouble removing that. Please try again."

def handle_get_totals(phone, args):
    period = str(args.get('period', 'today')).lower().strip()
    if period in ('week', 'this week', 'weekly', 'week so far'):
        period = 'week'
    elif period in ('month', 'this month', 'monthly', 'month so far'):
        period = 'month'
    elif period in ('year', 'this year', 'yearly', 'annual', 'year so far'):
        period = 'year'
    else:
        period = 'today'
    try:
        start_utc, end_utc = get_local_date_range(phone, period)
        cal, pro, carbs, fat, count = sum_food_log(phone, start_utc, end_utc)
        if count == 0:
            labels = {'today': 'today', 'week': 'this week', 'month': 'this month', 'year': 'this year'}
            return f"Nothing logged {labels.get(period, 'today')} yet."
        labels = {'today': 'Today', 'week': 'This week', 'month': 'This month', 'year': 'This year'}
        label = labels.get(period, 'Today')
        goal_text = ""
        try:
            params = {"filterByFormula": f"{{Phone}}='{phone}'", "fields[]": ["Calorie Goal"], "maxRecords": 1}
            user_records = airtable_get("Users", params)
            if user_records:
                goal = user_records[0].get('fields', {}).get('Calorie Goal')
                if goal and period == 'today':
                    goal = int(goal)
                    remaining = goal - cal
                    if remaining > 0:
                        goal_text = f" You have {remaining} calories left of your {goal} goal."
                    else:
                        over = abs(remaining)
                        goal_text = f" You're {over} calories over your {goal} goal."
        except Exception:
            pass
        return f"{label}: {cal} calories, {pro}g protein, {carbs}g carbs, {fat}g fat.{goal_text}"
    except Exception as e:
        app.logger.error(f"handle_get_totals error: {e}", exc_info=True)
        return "I couldn't get your totals right now. Try again in a moment."

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
        if args.get('calorie_goal'):
            try:
                fields["Calorie Goal"] = float(args['calorie_goal'])
            except (ValueError, TypeError):
                pass
        if records:
            airtable_patch("Users", records[0]['id'], fields)
        else:
            airtable_post("Users", fields)
        parts = []
        if args.get('timezone'):
            parts.append(f"timezone set to {args['timezone']}")
        if args.get('name'):
            parts.append(f"name saved")
        if args.get('email'):
            parts.append(f"email saved")
        if args.get('calorie_goal'):
            parts.append(f"calorie goal set to {args['calorie_goal']}")
        return "Got it, " + ", ".join(parts) + "." if parts else "Profile saved."
    except Exception as e:
        app.logger.error(f"handle_save_profile error: {e}")
        return "Saved."

def handle_save_usual(phone, args):
    meal_name = args.get('meal_name', '')
    foods = args.get('foods', '')
    try:
        params = {"filterByFormula": f"AND({{Phone}}='{phone}', {{Meal Name}}='{meal_name}')", "maxRecords": 1}
        records = airtable_get("Usual Meals", params)
        if records:
            airtable_patch("Usual Meals", records[0]['id'], {"Foods": str(foods), "Saved At": now_utc()})
        else:
            airtable_post("Usual Meals", {"Phone": phone, "Meal Name": str(meal_name), "Foods": str(foods), "Saved At": now_utc()})
        return f"Saved your usual {meal_name}."
    except Exception as e:
        app.logger.error(f"handle_save_usual error: {e}")
        return "Saved."

def handle_log_usual(phone, call_id, args):
    meal_name = args.get('meal_name', '')
    try:
        params = {"filterByFormula": f"AND({{Phone}}='{phone}', {{Meal Name}}='{meal_name}')", "maxRecords": 1}
        records = airtable_get("Usual Meals", params)
        if not records:
            return f"I don't have a usual meal saved called '{meal_name}'. Want me to save one?"
        foods_str = records[0].get('fields', {}).get('Foods', '')
        logged = []
        for food in [x.strip() for x in foods_str.split(',') if x.strip()]:
            handle_log_food(phone, call_id, {'food_name': food})
            logged.append(food)
        return f"Logged your usual {meal_name} — {', '.join(logged)}."
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
    day   = args.get('day', '')
    meal  = args.get('meal', '')
    foods = args.get('foods', '')
    try:
        airtable_post("Meal Plans", {"Phone": phone, "Day": str(day), "Meal": str(meal), "Foods": str(foods), "Saved At": now_utc()})
        return f"Saved {meal} for {day} — {foods}."
    except Exception as e:
        app.logger.error(f"handle_save_meal_plan error: {e}")
        return "Saved."

def handle_send_summary_email(phone, args):
    import threading

    def send_async():
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            SENDGRID_KEY = os.environ.get('SENDGRID_API_KEY', '')
            FROM_EMAIL   = os.environ.get('FROM_EMAIL', 'Sheldon@matcedi.com')
            if not SENDGRID_KEY:
                app.logger.error('SENDGRID_API_KEY not set')
                return

            params = {'filterByFormula': f"{{Phone}}='{phone}'", 'maxRecords': 1}
            records = airtable_get('Users', params)
            if not records:
                app.logger.warning(f'No user record for {phone}')
                return
            user_fields = records[0].get('fields', {})
            email = user_fields.get('Email', '')
            name  = user_fields.get('Name', 'there')
            goal  = user_fields.get('Calorie Goal')
            if not email:
                app.logger.warning(f'No email for {phone}')
                return

            start_utc, end_utc = get_local_date_range(phone, 'week')
            cal, pro, carbs, fat, count = sum_food_log(phone, start_utc, end_utc)

            first_name = name.split()[0] if name and name != 'there' else 'there'
            goal_line = ''
            if goal and count > 0:
                avg = cal / max(count, 1)
                if avg <= float(goal):
                    goal_line = f'<p style="color:#2e7d32;">On track — averaging {int(avg)} cal/day vs your {int(float(goal))} goal.</p>'
                else:
                    goal_line = f'<p style="color:#e65100;">Slightly over — averaging {int(avg)} cal/day vs your {int(float(goal))} goal.</p>'

            html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
<div style="background:#1a1a2e;padding:24px;border-radius:8px 8px 0 0;text-align:center;">
  <h1 style="color:#fff;margin:0;font-size:24px;">VoiceTrim</h1>
  <p style="color:#a0aec0;margin:4px 0 0;font-size:13px;">Your Weekly Nutrition Summary</p>
</div>
<div style="background:#fff;padding:32px;border:1px solid #e0e0e0;border-top:none;">
  <h2 style="color:#1a1a1a;margin:0 0 8px;">Hey {first_name}!</h2>
  <p style="color:#555;">Here's your nutrition recap for this week.</p>
  {goal_line}
  <table style="width:100%;border-collapse:collapse;margin:20px 0;">
    <tr style="background:#f8f9fa;">
      <td style="padding:16px;text-align:center;border-radius:6px;"><div style="font-size:28px;font-weight:700;color:#1a1a1a;">{cal:,}</div><div style="font-size:11px;color:#888;text-transform:uppercase;margin-top:4px;">Calories</div></td>
      <td style="width:8px;"></td>
      <td style="padding:16px;text-align:center;background:#f8f9fa;border-radius:6px;"><div style="font-size:28px;font-weight:700;color:#4caf50;">{pro}g</div><div style="font-size:11px;color:#888;text-transform:uppercase;margin-top:4px;">Protein</div></td>
      <td style="width:8px;"></td>
      <td style="padding:16px;text-align:center;background:#f8f9fa;border-radius:6px;"><div style="font-size:28px;font-weight:700;color:#2196f3;">{carbs}g</div><div style="font-size:11px;color:#888;text-transform:uppercase;margin-top:4px;">Carbs</div></td>
      <td style="width:8px;"></td>
      <td style="padding:16px;text-align:center;background:#f8f9fa;border-radius:6px;"><div style="font-size:28px;font-weight:700;color:#ff9800;">{fat}g</div><div style="font-size:11px;color:#888;text-transform:uppercase;margin-top:4px;">Fat</div></td>
    </tr>
  </table>
  <div style="text-align:center;margin-top:24px;">
    <a href="tel:+19714321012" style="background:#1a1a2e;color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:600;">Call VoiceTrim</a>
    <p style="color:#aaa;font-size:12px;margin-top:8px;">+1 (971) 432-1012</p>
  </div>
</div>
</body></html>"""

            message = Mail(
                from_email=(FROM_EMAIL, 'VoiceTrim'),
                to_emails=email,
                subject='Your VoiceTrim Weekly Summary',
                html_content=html
            )
            sg = SendGridAPIClient(SENDGRID_KEY)
            sg.send(message)
            app.logger.info(f'Weekly summary sent to {email}')
        except Exception as e:
            app.logger.error(f'send_summary_email error: {e}', exc_info=True)

    threading.Thread(target=send_async, daemon=True).start()
    return "I'll send your nutrition summary to your email shortly."

def handle_get_user_profile(phone, args):
    try:
        params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
        records = airtable_get("Users", params)
        if not records:
            return "Hey, welcome to VoiceTrim! I'm your personal nutrition assistant. What's your name?"
        fields = records[0].get('fields', {})
        name  = fields.get('Name') or ''
        email = fields.get('Email') or ''
        is_new = not name and not email
        if is_new:
            return "Hey, welcome to VoiceTrim! I'm your personal nutrition assistant. What's your name?"
        first_name = name.split()[0] if name else 'there'
        return f"Hey {first_name}, what are we logging today?"
    except Exception as e:
        app.logger.error(f"handle_get_user_profile error: {e}")
        return "Hey, welcome to VoiceTrim! What's your name?"

TOOL_HANDLERS = {
    'log_food':           lambda phone, call_id, args: handle_log_food(phone, call_id, args),
    'delete_food':        lambda phone, call_id, args: handle_delete_food(phone, args),
    'get_totals':         lambda phone, call_id, args: handle_get_totals(phone, args),
    'save_profile':       lambda phone, call_id, args: handle_save_profile(phone, args),
    'save_usual':         lambda phone, call_id, args: handle_save_usual(phone, args),
    'log_usual':          lambda phone, call_id, args: handle_log_usual(phone, call_id, args),
    'log_shopping_item':  lambda phone, call_id, args: handle_log_shopping_item(phone, args),
    'save_meal_plan':     lambda phone, call_id, args: handle_save_meal_plan(phone, args),
    'send_summary_email': lambda phone, call_id, args: handle_send_summary_email(phone, args),
    'get_user_profile':   lambda phone, call_id, args: handle_get_user_profile(phone, args),
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

@app.route('/call-start', methods=['POST'])
def call_start():
    try:
        body = request.json or {}
        call = body.get('message', {}).get('call', body.get('call', {}))
        phone = call.get('customer', {}).get('number', '')
        app.logger.info(f"call-start: phone={phone}")

        first_name = 'there'
        is_new = True
        if phone:
            try:
                params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
                records = airtable_get("Users", params)
                if records:
                    fields = records[0].get('fields', {})
                    name = fields.get('Name', '')
                    email = fields.get('Email', '')
                    if name or email:
                        is_new = False
                        first_name = name.split()[0] if name else 'there'
            except Exception as e:
                app.logger.warning(f"call-start lookup error: {e}")

        if is_new:
            greeting = "Hey there, welcome to VoiceTrim! I'm your personal nutrition assistant. What's your name?"
        else:
            greeting = f"Hey {first_name}, what are we logging today?"

        app.logger.info(f"call-start: greeting='{greeting}'")
        return jsonify({
            "variableValues": {
                "greeting": greeting,
                "first_name": first_name,
                "is_new_user": str(is_new).lower()
            }
        })
    except Exception as e:
        app.logger.error(f"call-start error: {e}", exc_info=True)
        return jsonify({"variableValues": {"greeting": "Hey there, what are we logging today?", "first_name": "there", "is_new_user": "false"}})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "VoiceTrim Middleware", "version": "2.0"})

@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": "VoiceTrim Middleware", "status": "running", "version": "2.0"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5556))
    app.run(host='0.0.0.0', port=port)
