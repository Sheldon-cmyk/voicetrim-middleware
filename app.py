from flask import Flask, request, jsonify
import requests
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
AIRTABLE_BASE = os.environ["AIRTABLE_BASE"]

AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

# ── In-memory user cache for instant call-start greetings ─────────────────────
_user_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes

def _cache_user(phone, first_name, email, tz):
    with _cache_lock:
        _user_cache[phone] = {
            'first_name': first_name,
            'email': email,
            'timezone': tz,
            'is_new': not first_name and not email,
            'cached_at': time.time()
        }

def _get_cached_user(phone):
    with _cache_lock:
        entry = _user_cache.get(phone)
        if entry and (time.time() - entry['cached_at']) < CACHE_TTL:
            return entry
    return None

def _refresh_cache_bg(phone):
    def _do():
        try:
            params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
            records = airtable_get("Users", params)
            if records:
                f = records[0].get('fields', {})
                name = f.get('Name', '')
                email = f.get('Email', '')
                tz = f.get('Timezone', 'UTC')
                first_name = name.split()[0] if name else ''
                _cache_user(phone, first_name, email, tz)
        except Exception as e:
            app.logger.warning(f"cache refresh error for {phone}: {e}")
    threading.Thread(target=_do, daemon=True).start()

def get_user_fast(phone):
    cached = _get_cached_user(phone)
    if cached:
        if (time.time() - cached['cached_at']) > 240:
            _refresh_cache_bg(phone)
        return cached
    try:
        params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
        records = airtable_get("Users", params)
        if records:
            f = records[0].get('fields', {})
            name = f.get('Name', '')
            email = f.get('Email', '')
            tz = f.get('Timezone', 'UTC')
            first_name = name.split()[0] if name else ''
            _cache_user(phone, first_name, email, tz)
            return _get_cached_user(phone)
    except Exception as e:
        app.logger.warning(f"get_user_fast error: {e}")
    return None

# ── Core helpers ──────────────────────────────────────────────────────────────

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
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

def get_user_timezone(phone):
    user = get_user_fast(phone)
    if user:
        return user.get('timezone') or 'UTC'
    return 'UTC'

def get_local_date_range(phone, period='today'):
    import zoneinfo
    tz_name = get_user_timezone(phone)
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo('UTC')
    now_local = datetime.now(tz)
    if period == 'today':
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
    elif period == 'week':
        start = (now_local - timedelta(days=now_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
    elif period == 'month':
        start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end   = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
    elif period == 'year':
        start = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end   = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
    start_utc = start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    end_utc   = end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    return start_utc, end_utc

def sum_food_log(phone, start_utc, end_utc):
    formula = f"AND({{Phone}}='{phone}',IS_AFTER({{Logged At}},'{start_utc}'),IS_BEFORE({{Logged At}},'{end_utc}'))"
    records = airtable_get("Food Log", {"filterByFormula": formula, "maxRecords": 500})
    cal = pro = carbs = fat = 0.0
    for r in records:
        f = r.get('fields', {})
        cal   += float(f.get('Calories', 0) or 0)
        pro   += float(f.get('Protein', 0) or 0)
        carbs += float(f.get('Carbs', 0) or 0)
        fat   += float(f.get('Fat', 0) or 0)
    return int(cal), round(pro,1), round(carbs,1), round(fat,1), len(records)

# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_log_food(phone, call_id, args):
    food_name = args.get('food_name', 'unknown food')
    try:
        calories = float(args.get('calories', 0) or 0)
        protein  = float(args.get('protein', 0) or 0)
        carbs    = float(args.get('carbs', 0) or 0)
        fat      = float(args.get('fat', 0) or 0)
    except (ValueError, TypeError):
        calories = protein = carbs = fat = 0.0
    fields = {
        "Phone":     phone,
        "Food Name": str(food_name),
        "Calories":  calories,
        "Protein":   protein,
        "Carbs":     carbs,
        "Fat":       fat,
        "Logged At": now_utc(),
        "Call ID":   str(call_id)
    }
    resp = airtable_post("Food Log", fields)
    app.logger.info(f"log_food: {resp.status_code} - {food_name} {calories} cal")
    if resp.status_code in (200, 201):
        return f"Logged {food_name}, {int(calories)} calories."
    else:
        app.logger.error(f"log_food error: {resp.text[:200]}")
        return f"Logged {food_name}."

def handle_delete_food(phone, args):
    food_name = args.get('food_name', '')
    try:
        formula = f"AND({{Phone}}='{phone}',FIND(LOWER('{food_name.lower()}'),LOWER({{Food Name}}))>0)"
        params  = {"filterByFormula": formula, "sort[0][field]": "Logged At", "sort[0][direction]": "desc", "maxRecords": 1}
        records = airtable_get("Food Log", params)
        if not records:
            return f"I couldn't find {food_name} in your recent logs."
        record_id = records[0]['id']
        found_name = records[0].get('fields', {}).get('Food Name', food_name)
        airtable_delete("Food Log", record_id)
        return f"Removed {found_name}."
    except Exception as e:
        app.logger.error(f"handle_delete_food error: {e}")
        return f"Removed {food_name}."

def handle_get_totals(phone, args):
    period = args.get('period', 'today').lower()
    try:
        start_utc, end_utc = get_local_date_range(phone, period)
        cal, pro, carbs, fat, count = sum_food_log(phone, start_utc, end_utc)
        if count == 0:
            label = {'today': 'today', 'week': 'this week', 'month': 'this month', 'year': 'this year'}.get(period, period)
            return f"No food logged {label} yet."
        label = {'today': 'today', 'week': 'this week', 'month': 'this month', 'year': 'this year'}.get(period, period)
        user = get_user_fast(phone)
        goal = None
        if user:
            try:
                params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
                records = airtable_get("Users", params)
                if records:
                    goal = records[0].get('fields', {}).get('Calorie Goal')
            except Exception:
                pass
        summary = f"{label.capitalize()}: {cal:,} calories, {pro}g protein, {carbs}g carbs, {fat}g fat"
        if goal and period == 'today':
            remaining = int(float(goal)) - cal
            if remaining > 0:
                summary += f". You have {remaining:,} calories left for the day."
            else:
                summary += f". You're {abs(remaining):,} calories over your goal."
        return summary
    except Exception as e:
        app.logger.error(f"handle_get_totals error: {e}")
        return "Could not retrieve totals right now."

def handle_save_profile(phone, args):
    try:
        params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
        records = airtable_get("Users", params)
        fields = {"Phone": phone}
        if args.get('name'):
            fields['Name'] = str(args['name'])
        if args.get('email'):
            fields['Email'] = str(args['email'])
        if args.get('timezone'):
            fields['Timezone'] = str(args['timezone'])
        if args.get('calorie_goal'):
            try:
                fields["Calorie Goal"] = float(args['calorie_goal'])
            except (ValueError, TypeError):
                pass
        if records:
            airtable_patch("Users", records[0]['id'], fields)
        else:
            airtable_post("Users", fields)
        # Invalidate cache so next call gets fresh data
        with _cache_lock:
            _user_cache.pop(phone, None)
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
        params = {"filterByFormula": f"AND({{Phone}}='{phone}',{{Day}}='{day}',{{Meal}}='{meal}')", "maxRecords": 1}
        records = airtable_get("Meal Plan", params)
        if records:
            airtable_patch("Meal Plan", records[0]['id'], {"Foods": str(foods)})
        else:
            airtable_post("Meal Plan", {"Phone": phone, "Day": str(day), "Meal": str(meal), "Foods": str(foods)})
        return f"Saved {meal} for {day}."
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
            tz    = user_fields.get('Timezone', 'UTC')
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
            message = Mail(from_email=(FROM_EMAIL, 'VoiceTrim'), to_emails=email,
                           subject='Your VoiceTrim Weekly Summary', html_content=html)
            sg = SendGridAPIClient(SENDGRID_KEY)
            sg.send(message)
            app.logger.info(f'Weekly summary sent to {email}')
        except Exception as e:
            app.logger.error(f'send_summary_email error: {e}', exc_info=True)
    threading.Thread(target=send_async, daemon=True).start()
    return "I'll send your nutrition summary to your email shortly."

def handle_get_user_profile(phone, args):
    try:
        user = get_user_fast(phone)
        if not user or user['is_new']:
            return "Hey, welcome to VoiceTrim! I'm your personal nutrition assistant. What's your name?"
        first_name = user['first_name'] or 'there'
        return f"Hey {first_name}, what are we logging today?"
    except Exception as e:
        app.logger.error(f"handle_get_user_profile error: {e}")
        return "Hey, welcome to VoiceTrim! What's your name?"

# ── Tool dispatch ─────────────────────────────────────────────────────────────

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
            user = get_user_fast(phone)
            if user and not user['is_new']:
                first_name = user['first_name'] or 'there'
                is_new = False

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
        return jsonify({"variableValues": {"greeting": "Hey, what are we logging today?", "first_name": "there", "is_new_user": "false"}})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "VoiceTrim Middleware", "version": "2.0"})

@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": "VoiceTrim Middleware", "status": "running", "version": "2.0"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5556))
    app.run(host='0.0.0.0', port=port)
