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

# ── Paywall configuration ─────────────────────────────────────────────────────
# URL of the landing page server's subscription check endpoint
LANDING_PAGE_URL = os.environ.get('LANDING_PAGE_URL', 'https://voicetrim-landing.manus.space')
# Shared secret key that authenticates this middleware to the landing page server
VOICE_GATEWAY_API_KEY = os.environ.get('VOICE_GATEWAY_API_KEY', '')
# Set to 'true' to bypass the paywall gate (useful for testing)
PAYWALL_BYPASS = os.environ.get('PAYWALL_BYPASS', 'false').lower() == 'true'

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
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table)}"
    resp = requests.get(url, headers=AIRTABLE_HEADERS, params=params, timeout=15)
    return resp.json().get('records', [])

def airtable_post(table, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table)}"
    resp = requests.post(url, headers=AIRTABLE_HEADERS, json={"fields": fields}, timeout=10)
    return resp

def airtable_patch(table, record_id, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table)}/{record_id}"
    resp = requests.patch(url, headers=AIRTABLE_HEADERS, json={"fields": fields}, timeout=10)
    return resp

def airtable_delete(table, record_id):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{requests.utils.quote(table)}/{record_id}"
    resp = requests.delete(url, headers=AIRTABLE_HEADERS, timeout=10)
    return resp

def now_utc():
    return datetime.now(timezone.utc).isoformat()

# ── Timezone helpers ──────────────────────────────────────────────────────────

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

def get_user_profile_fields(phone):
    """Get full user profile from Contacts table."""
    try:
        params = {"filterByFormula": f"{{Phone Number}}='{phone}'", "maxRecords": 1}
        records = airtable_get("Contacts", params)
        if records:
            return records[0].get('fields', {}), records[0]['id']
    except Exception:
        pass
    return {}, None

def call_openai(system_prompt, user_prompt, max_tokens=800):
    """Call OpenAI GPT-4.1-mini for AI-generated content."""
    try:
        import openai
        api_key = os.environ.get('OPENAI_API_KEY', '')
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        app.logger.error(f"OpenAI error: {e}")
        return None

# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_log_food(phone, call_id, args):
    """Log a food item to Airtable."""
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
    """Delete a food entry by name."""
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
        cal = record.get('fields', {}).get('Calories', 0) or 0

        del_resp = airtable_delete("Food Log", record_id)
        if del_resp.status_code == 200:
            return f"Done, removed {actual_name} from your log."
        else:
            return f"Something went wrong removing {food_name}. Please try again."
    except Exception as e:
        app.logger.error(f"handle_delete_food error: {e}", exc_info=True)
        return "I had trouble removing that. Please try again."

def handle_get_totals(phone, args):
    """Get nutrition totals for today, week, month, or year."""
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
    """Save user profile — timezone, name, email, calorie goal."""
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
    """Save a usual/regular meal for quick logging later."""
    meal_name = args.get('meal_name', '')
    foods = args.get('foods', '')
    try:
        params = {"filterByFormula": f"AND({{Phone}}='{phone}', {{Meal Name}}='{meal_name}')", "maxRecords": 1}
        records = airtable_get("Usuals", params)
        if records:
            airtable_patch("Usuals", records[0]['id'], {"Foods": str(foods)})
        else:
            airtable_post("Usuals", {"Phone": phone, "Meal Name": str(meal_name), "Foods": str(foods)})
        return f"Saved your usual {meal_name}."
    except Exception as e:
        app.logger.error(f"handle_save_usual error: {e}")
        return "Saved."

def handle_log_usual(phone, call_id, args):
    """Log a previously saved usual meal."""
    meal_name = args.get('meal_name', '')
    try:
        params = {"filterByFormula": f"AND({{Phone}}='{phone}', {{Meal Name}}='{meal_name}')", "maxRecords": 1}
        records = airtable_get("Usuals", params)
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
    """Add an item to the shopping list."""
    item = args.get('item', '')
    qty  = args.get('quantity', '')
    try:
        airtable_post("Shopping List", {
            "Phone": phone,
            "Item Name": str(item),
            "Quantity": str(qty),
            "Session Date": datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            "In Cart": False
        })
        return f"Added {item} to your shopping list."
    except Exception as e:
        app.logger.error(f"handle_log_shopping_item error: {e}")
        return "Added."

def handle_save_meal_plan(phone, args):
    """Save a meal plan entry."""
    day   = args.get('day', '')
    meal  = args.get('meal', '')
    foods = args.get('foods', '')
    try:
        airtable_post("Meal Plan", {
            "Phone": phone,
            "Day": str(day),
            "Meal Type": str(meal),
            "Foods": str(foods),
            "Week Of": datetime.now(timezone.utc).strftime('%Y-%m-%d')
        })
        return f"Saved {meal} for {day} — {foods}."
    except Exception as e:
        app.logger.error(f"handle_save_meal_plan error: {e}")
        return "Saved."

def handle_send_summary_email(phone, args):
    """Send weekly nutrition summary email to the user via SendGrid."""
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
                return
            user_fields = records[0].get('fields', {})
            email = user_fields.get('Email', '')
            name  = user_fields.get('Name', 'there')
            goal  = user_fields.get('Calorie Goal')
            if not email:
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
    """Returns the opening greeting for the caller."""
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

# ── NEW: Goal Setting ─────────────────────────────────────────────────────────

def handle_set_goal(phone, args):
    """Save user's fitness goal and calculate daily calorie/macro targets."""
    try:
        goal_type     = str(args.get('goal_type', '')).strip()       # lose_weight, gain_muscle, maintain
        current_weight = args.get('current_weight')                   # lbs
        target_weight  = args.get('target_weight')                    # lbs
        height_inches  = args.get('height_inches')                    # total inches
        age            = args.get('age')
        gender         = str(args.get('gender', 'unknown')).lower()
        activity_level = str(args.get('activity_level', 'moderate')).lower()  # sedentary, light, moderate, active, very_active
        timeline_weeks = args.get('timeline_weeks', 12)

        # Calculate BMR using Mifflin-St Jeor
        calorie_goal = 2000  # default
        protein_goal = 150
        carb_goal    = 200
        fat_goal     = 65

        if current_weight and height_inches and age:
            weight_kg = float(current_weight) * 0.453592
            height_cm = float(height_inches) * 2.54
            age_val   = float(age)

            if gender in ('male', 'm'):
                bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age_val + 5
            else:
                bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age_val - 161

            activity_multipliers = {
                'sedentary': 1.2, 'light': 1.375, 'moderate': 1.55,
                'active': 1.725, 'very_active': 1.9
            }
            multiplier = activity_multipliers.get(activity_level, 1.55)
            tdee = bmr * multiplier

            if goal_type == 'lose_weight':
                calorie_goal = int(tdee - 500)   # 1 lb/week deficit
                protein_goal = int(float(current_weight) * 0.8)  # 0.8g per lb
                fat_goal     = int(calorie_goal * 0.25 / 9)
                carb_goal    = int((calorie_goal - protein_goal * 4 - fat_goal * 9) / 4)
            elif goal_type == 'gain_muscle':
                calorie_goal = int(tdee + 300)   # lean bulk surplus
                protein_goal = int(float(current_weight) * 1.0)  # 1g per lb
                fat_goal     = int(calorie_goal * 0.25 / 9)
                carb_goal    = int((calorie_goal - protein_goal * 4 - fat_goal * 9) / 4)
            else:  # maintain
                calorie_goal = int(tdee)
                protein_goal = int(float(current_weight) * 0.7)
                fat_goal     = int(calorie_goal * 0.30 / 9)
                carb_goal    = int((calorie_goal - protein_goal * 4 - fat_goal * 9) / 4)

            # Clamp to reasonable ranges
            calorie_goal = max(1200, min(calorie_goal, 4000))
            protein_goal = max(50, min(protein_goal, 300))
            carb_goal    = max(50, min(carb_goal, 500))
            fat_goal     = max(30, min(fat_goal, 150))

        # Save to Contacts table
        contact_fields, contact_id = get_user_profile_fields(phone)
        update_fields = {
            "Goal": goal_type,
            "Calorie Goal": calorie_goal,
            "Protein Goal": protein_goal,
            "Carb Goal": carb_goal,
            "Fat Goal": fat_goal,
        }
        if current_weight:
            update_fields["Phone Number"] = phone  # ensure phone is set

        if contact_id:
            airtable_patch("Contacts", contact_id, update_fields)
        else:
            update_fields["Phone Number"] = phone
            airtable_post("Contacts", update_fields)

        # Also update Users table calorie goal for get_totals to use
        params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
        user_records = airtable_get("Users", params)
        if user_records:
            airtable_patch("Users", user_records[0]['id'], {"Calorie Goal": calorie_goal})
        else:
            airtable_post("Users", {"Phone": phone, "Calorie Goal": calorie_goal})

        goal_labels = {
            'lose_weight': 'lose weight',
            'gain_muscle': 'build muscle',
            'maintain': 'maintain your weight'
        }
        goal_label = goal_labels.get(goal_type, goal_type)

        app.logger.info(f"set_goal: {phone} goal={goal_type} cal={calorie_goal} pro={protein_goal}g")
        return (f"Got it! Your goal is to {goal_label}. "
                f"Your daily targets are {calorie_goal} calories, "
                f"{protein_goal}g protein, {carb_goal}g carbs, and {fat_goal}g fat. "
                f"Want me to create a 7-day meal plan based on these targets?")
    except Exception as e:
        app.logger.error(f"handle_set_goal error: {e}", exc_info=True)
        return "I saved your goal. Want me to create a meal plan?"

# ── NEW: Meal Plan Generator ──────────────────────────────────────────────────

def handle_generate_meal_plan(phone, args):
    """Generate a personalized 7-day meal plan using AI and save to Airtable (async)."""
    preferences   = str(args.get('preferences', '')).strip()
    dietary_notes = str(args.get('dietary_notes', '')).strip()

    def _generate():
        try:
            contact_fields, _ = get_user_profile_fields(phone)
            calorie_goal  = contact_fields.get('Calorie Goal', 2000)
            protein_goal  = contact_fields.get('Protein Goal', 150)
            carb_goal     = contact_fields.get('Carb Goal', 200)
            fat_goal      = contact_fields.get('Fat Goal', 65)
            goal_type     = contact_fields.get('Goal', 'maintain')

            system_prompt = """You are a professional nutritionist creating a practical, realistic 7-day meal plan.
Output ONLY a JSON array. Each entry has: day (Monday-Sunday), meal_type (Breakfast/Lunch/Dinner/Snack),
meal_name (short), foods (comma-separated ingredients/items), calories (integer), protein (integer grams),
carbs (integer grams), fat (integer grams).
Keep meals simple, affordable, and easy to prepare. Use common grocery store items."""

            user_prompt = f"""Create a 7-day meal plan (3 meals + 1 snack per day = 28 entries) for someone with:
- Goal: {goal_type}
- Daily targets: {calorie_goal} calories, {protein_goal}g protein, {carb_goal}g carbs, {fat_goal}g fat
- Preferences: {preferences or 'none specified'}
- Dietary notes: {dietary_notes or 'none'}

Each day should hit close to the daily targets when meals are summed.
Output ONLY valid JSON array, no markdown, no explanation."""

            ai_response = call_openai(system_prompt, user_prompt, max_tokens=2000)
            if not ai_response:
                app.logger.error(f"generate_meal_plan: no AI response for {phone}")
                return

            clean = re.sub(r'```(?:json)?', '', ai_response).strip()
            meal_entries = json.loads(clean)

            # Clear existing meal plan
            week_of = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            existing = airtable_get("Meal Plan", {"filterByFormula": f"{{Phone}}='{phone}'"})
            for r in existing:
                airtable_delete("Meal Plan", r['id'])

            saved = 0
            for entry in meal_entries:
                try:
                    airtable_post("Meal Plan", {
                        "Phone": phone,
                        "Week Of": week_of,
                        "Day": str(entry.get('day', '')),
                        "Meal Type": str(entry.get('meal_type', '')),
                        "Meal Name": str(entry.get('meal_name', '')),
                        "Foods": str(entry.get('foods', '')),
                        "Estimated Calories": int(entry.get('calories', 0)),
                        "Estimated Protein": int(entry.get('protein', 0)),
                        "Estimated Carbs": int(entry.get('carbs', 0)),
                        "Estimated Fat": int(entry.get('fat', 0)),
                    })
                    saved += 1
                except Exception as e:
                    app.logger.warning(f"Meal plan entry save error: {e}")

            app.logger.info(f"generate_meal_plan: saved {saved} entries for {phone}")
        except Exception as e:
            app.logger.error(f"generate_meal_plan async error: {e}", exc_info=True)

    threading.Thread(target=_generate, daemon=True).start()
    return ("I'm generating your personalized meal plan right now — this takes about 15 seconds. "
            "Say 'read my meal plan' in a moment to hear it, or 'create my shopping list' once it's ready.")

# ── NEW: Get Meal Plan ────────────────────────────────────────────────────────

def handle_get_meal_plan(phone, args):
    """Read back the user's meal plan by day."""
    try:
        day_filter = str(args.get('day', '')).strip()  # e.g., "Monday" or "today" or "" for all

        # Resolve "today" to actual day name
        if day_filter.lower() in ('today', 'now'):
            day_filter = datetime.now(timezone.utc).strftime('%A')

        if day_filter:
            formula = f"AND({{Phone}}='{phone}', {{Day}}='{day_filter}')"
        else:
            formula = f"{{Phone}}='{phone}'"

        records = airtable_get("Meal Plan", {"filterByFormula": formula, "maxRecords": 28})

        if not records:
            return "You don't have a meal plan yet. Say 'create my meal plan' and I'll build one for you."

        # Group by day
        day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        meal_order = ['Breakfast', 'Lunch', 'Dinner', 'Snack']
        by_day = {}
        for r in records:
            f = r.get('fields', {})
            d = f.get('Day', 'Unknown')
            if d not in by_day:
                by_day[d] = []
            by_day[d].append(f)

        if day_filter and day_filter in by_day:
            # Single day
            meals = sorted(by_day[day_filter], key=lambda x: meal_order.index(x.get('Meal Type', 'Snack')) if x.get('Meal Type') in meal_order else 99)
            lines = [f"{day_filter}:"]
            for m in meals:
                lines.append(f"{m.get('Meal Type', '')}: {m.get('Meal Name', '')} — {m.get('Estimated Calories', 0)} cal")
            return " ".join(lines)
        else:
            # Summary of all days
            lines = []
            for day in day_order:
                if day in by_day:
                    total_cal = sum(int(m.get('Estimated Calories', 0) or 0) for m in by_day[day])
                    lines.append(f"{day}: {total_cal} cal")
            return "Your 7-day meal plan: " + ", ".join(lines) + ". Say a specific day to hear the full menu."
    except Exception as e:
        app.logger.error(f"handle_get_meal_plan error: {e}", exc_info=True)
        return "I couldn't retrieve your meal plan right now."

# ── NEW: Shopping List Generator ─────────────────────────────────────────────

def handle_generate_shopping_list(phone, args):
    """Generate a grocery shopping list from the meal plan using AI (async)."""
    def _generate():
        try:
            records = airtable_get("Meal Plan", {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 28})
            if not records:
                app.logger.warning(f"generate_shopping_list: no meal plan for {phone}")
                return

            meals_text = []
            for r in records:
                f = r.get('fields', {})
                meals_text.append(f"{f.get('Day','')} {f.get('Meal Type','')}: {f.get('Foods','')}")
            meal_plan_str = "\n".join(meals_text)

            system_prompt = """You are a professional nutritionist creating a grocery shopping list.
Output ONLY a JSON array. Each item has: item_name (string), category (Produce/Protein/Dairy/Grains/Frozen/Pantry/Beverages/Other),
quantity (string like "2 lbs" or "1 dozen"), calories_per_serving (integer), protein_per_serving (integer grams),
carbs_per_serving (integer grams), fat_per_serving (integer grams).
Consolidate duplicate ingredients. Use common grocery store names."""

            user_prompt = f"""Create a consolidated grocery shopping list for this 7-day meal plan:

{meal_plan_str}

Consolidate all ingredients, remove duplicates, and organize by grocery store category.
Output ONLY valid JSON array, no markdown, no explanation."""

            ai_response = call_openai(system_prompt, user_prompt, max_tokens=2000)
            if not ai_response:
                app.logger.error(f"generate_shopping_list: no AI response for {phone}")
                return

            clean = re.sub(r'```(?:json)?', '', ai_response).strip()
            items = json.loads(clean)

            # Clear existing shopping list
            existing = airtable_get("Shopping List", {"filterByFormula": f"{{Phone}}='{phone}'"})
            for r in existing:
                airtable_delete("Shopping List", r['id'])

            session_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            saved = 0
            for item in items:
                try:
                    airtable_post("Shopping List", {
                        "Phone": phone,
                        "Session Date": session_date,
                        "Item Name": str(item.get('item_name', '')),
                        "Category": str(item.get('category', 'Other')),
                        "Quantity": str(item.get('quantity', '')),
                        "Calories Per Serving": int(item.get('calories_per_serving', 0)),
                        "Protein Per Serving": int(item.get('protein_per_serving', 0)),
                        "Carbs Per Serving": int(item.get('carbs_per_serving', 0)),
                        "Fat Per Serving": int(item.get('fat_per_serving', 0)),
                        "In Cart": False
                    })
                    saved += 1
                except Exception as e:
                    app.logger.warning(f"Shopping list item save error: {e}")

            app.logger.info(f"generate_shopping_list: saved {saved} items for {phone}")
        except Exception as e:
            app.logger.error(f"generate_shopping_list async error: {e}", exc_info=True)

    threading.Thread(target=_generate, daemon=True).start()
    return ("I'm building your shopping list right now — give me about 15 seconds. "
            "Say 'read my shopping list' in a moment and I'll guide you through it.")

# ── NEW: Get Shopping List ────────────────────────────────────────────────────

def handle_get_shopping_list(phone, args):
    """Read the shopping list, optionally filtered by category."""
    try:
        category_filter = str(args.get('category', '')).strip()
        show_unchecked_only = args.get('unchecked_only', True)

        if category_filter:
            formula = f"AND({{Phone}}='{phone}', {{Category}}='{category_filter}')"
        elif show_unchecked_only:
            formula = f"AND({{Phone}}='{phone}', NOT({{In Cart}}))"
        else:
            formula = f"{{Phone}}='{phone}'"

        records = airtable_get("Shopping List", {"filterByFormula": formula, "maxRecords": 100})

        if not records:
            if show_unchecked_only:
                return "Everything on your shopping list is already in your cart! You're all set."
            return "Your shopping list is empty. Say 'create my shopping list' to generate one from your meal plan."

        # Group by category
        by_cat = {}
        for r in records:
            f = r.get('fields', {})
            cat = f.get('Category', 'Other')
            if cat not in by_cat:
                by_cat[cat] = []
            by_cat[cat].append(f.get('Item Name', '') + (f" — {f.get('Quantity','')}" if f.get('Quantity') else ''))

        cat_order = ['Produce', 'Protein', 'Dairy', 'Grains', 'Frozen', 'Pantry', 'Beverages', 'Other']
        lines = []
        for cat in cat_order:
            if cat in by_cat:
                lines.append(f"{cat}: {', '.join(by_cat[cat])}")

        remaining = len(records)
        return f"You have {remaining} items left. " + ". ".join(lines) + "."
    except Exception as e:
        app.logger.error(f"handle_get_shopping_list error: {e}", exc_info=True)
        return "I couldn't retrieve your shopping list right now."

# ── NEW: Check Off Shopping Item ─────────────────────────────────────────────

def handle_check_off_item(phone, args):
    """Mark a shopping list item as in-cart."""
    try:
        item_name = str(args.get('item_name', '')).strip()
        if not item_name:
            return "Which item did you grab?"

        formula = (
            f"AND({{Phone}}='{phone}', "
            f"FIND(LOWER('{item_name.lower()}'), LOWER({{Item Name}})) > 0, "
            f"NOT({{In Cart}}))"
        )
        records = airtable_get("Shopping List", {"filterByFormula": formula, "maxRecords": 1})

        if not records:
            return f"I don't see {item_name} on your list, or it's already checked off."

        record_id = records[0]['id']
        actual_name = records[0].get('fields', {}).get('Item Name', item_name)
        airtable_patch("Shopping List", record_id, {"In Cart": True})

        # Count remaining
        remaining_records = airtable_get("Shopping List", {
            "filterByFormula": f"AND({{Phone}}='{phone}', NOT({{In Cart}}))",
            "maxRecords": 100
        })
        remaining = len(remaining_records)

        if remaining == 0:
            return f"Got it, {actual_name} is in your cart. That's everything on your list — you're done shopping!"
        return f"Got it, {actual_name} is in your cart. {remaining} items left."
    except Exception as e:
        app.logger.error(f"handle_check_off_item error: {e}", exc_info=True)
        return "Checked off."

# ── NEW: Log Cart Item to Food Diary ─────────────────────────────────────────

def handle_log_cart_item(phone, call_id, args):
    """Log a grocery item selected at the store directly to the food diary."""
    try:
        item_name = str(args.get('item_name', '')).strip()
        quantity  = str(args.get('quantity', '1 serving')).strip()
        meal_type = str(args.get('meal_type', '')).strip()

        # Look up nutrition from shopping list
        formula = (
            f"AND({{Phone}}='{phone}', "
            f"FIND(LOWER('{item_name.lower()}'), LOWER({{Item Name}})) > 0)"
        )
        records = airtable_get("Shopping List", {"filterByFormula": formula, "maxRecords": 1})

        calories = 0
        protein  = 0
        carbs    = 0
        fat      = 0

        if records:
            f = records[0].get('fields', {})
            calories = int(f.get('Calories Per Serving', 0) or 0)
            protein  = int(f.get('Protein Per Serving', 0) or 0)
            carbs    = int(f.get('Carbs Per Serving', 0) or 0)
            fat      = int(f.get('Fat Per Serving', 0) or 0)

        # Log to food diary
        log_args = {
            'food_name': item_name,
            'calories': calories,
            'protein': protein,
            'carbs': carbs,
            'fat': fat
        }
        handle_log_food(phone, call_id, log_args)

        # Also mark as in-cart on shopping list
        if records:
            airtable_patch("Shopping List", records[0]['id'], {"In Cart": True})

        cal_text = f" at {calories} calories" if calories else ""
        return f"Logged {item_name}{cal_text} to your food diary."
    except Exception as e:
        app.logger.error(f"handle_log_cart_item error: {e}", exc_info=True)
        return "Logged."

# ── NEW: Get Goals ────────────────────────────────────────────────────────────

def handle_get_goals(phone, args):
    """Read back the user's current nutrition goals."""
    try:
        contact_fields, _ = get_user_profile_fields(phone)
        calorie_goal = contact_fields.get('Calorie Goal')
        protein_goal = contact_fields.get('Protein Goal')
        carb_goal    = contact_fields.get('Carb Goal')
        fat_goal     = contact_fields.get('Fat Goal')
        goal_type    = contact_fields.get('Goal', '')

        if not calorie_goal:
            return "You haven't set a goal yet. Say 'set my goal' and I'll walk you through it."

        goal_labels = {
            'lose_weight': 'lose weight',
            'gain_muscle': 'build muscle',
            'maintain': 'maintain your weight'
        }
        goal_label = goal_labels.get(goal_type, goal_type or 'reach your goal')

        return (f"Your goal is to {goal_label}. "
                f"Daily targets: {int(calorie_goal)} calories, "
                f"{int(protein_goal or 0)}g protein, "
                f"{int(carb_goal or 0)}g carbs, "
                f"{int(fat_goal or 0)}g fat.")
    except Exception as e:
        app.logger.error(f"handle_get_goals error: {e}", exc_info=True)
        return "I couldn't retrieve your goals right now."

# ── Tool dispatch ─────────────────────────────────────────────────────────────

def handle_send_shopping_list_email(phone, args):
    """Email the user their grocery shopping list."""
    def send_async():
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            SENDGRID_KEY = os.environ.get('SENDGRID_API_KEY', '')
            FROM_EMAIL   = os.environ.get('FROM_EMAIL', 'hello@voicetrim.com')
            if not SENDGRID_KEY:
                app.logger.error('SENDGRID_API_KEY not set')
                return

            # Get user email
            params = {'filterByFormula': f"{{Phone}}='{phone}'", 'maxRecords': 1}
            records = airtable_get('Users', params)
            if not records:
                return
            user = records[0].get('fields', {})
            email = user.get('Email', '')
            name  = user.get('First Name', 'there')
            if not email:
                return

            # Get shopping list
            sl_records = airtable_get('Shopping List', {
                'filterByFormula': f"{{Phone}}='{phone}'",
                'maxRecords': 200
            })

            if not sl_records:
                return

            # Group by category
            cat_order = ['Produce', 'Protein', 'Dairy', 'Grains', 'Frozen', 'Pantry', 'Beverages', 'Other']
            by_cat = {}
            for r in sl_records:
                f = r.get('fields', {})
                cat = f.get('Category', 'Other')
                if cat not in by_cat:
                    by_cat[cat] = []
                item = f.get('Item Name', '')
                qty  = f.get('Quantity', '')
                checked = f.get('In Cart', False)
                status = '✓' if checked else '○'
                by_cat[cat].append(f"{status} {item}" + (f" ({qty})" if qty else ''))

            # Build HTML email
            html_rows = ''
            for cat in cat_order:
                if cat in by_cat:
                    items_html = ''.join(f'<li style="margin:4px 0;">{i}</li>' for i in by_cat[cat])
                    html_rows += f'''
                    <tr>
                      <td style="padding:12px 16px; vertical-align:top;">
                        <strong style="color:#2d6a4f;font-size:15px;">{cat}</strong>
                        <ul style="margin:6px 0 0 0;padding-left:18px;color:#333;font-size:14px;">{items_html}</ul>
                      </td>
                    </tr>'''

            total = len(sl_records)
            html_body = f'''
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
              <div style="background:#2d6a4f;padding:24px;border-radius:8px 8px 0 0;">
                <h1 style="color:#fff;margin:0;font-size:24px;">VoiceTrim Shopping List</h1>
                <p style="color:#b7e4c7;margin:4px 0 0 0;">Hey {name}, here are your {total} items!</p>
              </div>
              <table style="width:100%;border-collapse:collapse;background:#fff;">
                {html_rows}
              </table>
              <div style="background:#f0f4f0;padding:16px;border-radius:0 0 8px 8px;text-align:center;">
                <p style="color:#666;font-size:12px;margin:0;">Generated by VoiceTrim &bull; Call +1 (971) 432-1012 to update your list</p>
              </div>
            </div>'''

            message = Mail(
                from_email=FROM_EMAIL,
                to_emails=email,
                subject=f'Your VoiceTrim Shopping List ({total} items)',
                html_content=html_body
            )
            sg = SendGridAPIClient(SENDGRID_KEY)
            sg.send(message)
            app.logger.info(f'Shopping list email sent to {email}')
        except Exception as e:
            app.logger.error(f'send_shopping_list_email error: {e}', exc_info=True)

    threading.Thread(target=send_async, daemon=True).start()
    return "I'm sending your shopping list to your email right now. Check your inbox in a moment!"


def handle_send_meal_plan_email(phone, args):
    """Email the user their 7-day meal plan."""
    def send_async():
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            SENDGRID_KEY = os.environ.get('SENDGRID_API_KEY', '')
            FROM_EMAIL   = os.environ.get('FROM_EMAIL', 'hello@voicetrim.com')
            if not SENDGRID_KEY:
                app.logger.error('SENDGRID_API_KEY not set')
                return

            # Get user email
            params = {'filterByFormula': f"{{Phone}}='{phone}'", 'maxRecords': 1}
            records = airtable_get('Users', params)
            if not records:
                return
            user = records[0].get('fields', {})
            email = user.get('Email', '')
            name  = user.get('First Name', 'there')
            if not email:
                return

            # Get meal plan
            mp_records = airtable_get('Meal Plan', {
                'filterByFormula': f"{{Phone}}='{phone}'",
                'maxRecords': 28
            })

            if not mp_records:
                return

            # Group by day
            day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            meal_order = ['Breakfast', 'Lunch', 'Dinner', 'Snack']
            by_day = {}
            for r in mp_records:
                f = r.get('fields', {})
                d = f.get('Day', 'Unknown')
                if d not in by_day:
                    by_day[d] = []
                by_day[d].append(f)

            # Build HTML rows
            html_rows = ''
            for day in day_order:
                if day not in by_day:
                    continue
                meals = sorted(by_day[day], key=lambda x: meal_order.index(x.get('Meal Type','Snack')) if x.get('Meal Type') in meal_order else 99)
                meals_html = ''.join(
                    f'<tr><td style="padding:4px 8px;color:#555;font-size:13px;">{m.get("Meal Type","")}</td>'
                    f'<td style="padding:4px 8px;font-size:13px;">{m.get("Meal Name","")}</td>'
                    f'<td style="padding:4px 8px;color:#888;font-size:13px;text-align:right;">{m.get("Estimated Calories",0)} cal</td></tr>'
                    for m in meals
                )
                day_total = sum(m.get('Estimated Calories', 0) for m in meals)
                html_rows += f'''
                <tr style="background:#f0f4f0;">
                  <td colspan="3" style="padding:10px 16px;font-weight:bold;color:#2d6a4f;font-size:15px;">{day} &mdash; {day_total} cal total</td>
                </tr>
                {meals_html}'''

            html_body = f'''
            <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
              <div style="background:#2d6a4f;padding:24px;border-radius:8px 8px 0 0;">
                <h1 style="color:#fff;margin:0;font-size:24px;">VoiceTrim 7-Day Meal Plan</h1>
                <p style="color:#b7e4c7;margin:4px 0 0 0;">Hey {name}, here is your personalized meal plan!</p>
              </div>
              <table style="width:100%;border-collapse:collapse;background:#fff;">
                {html_rows}
              </table>
              <div style="background:#f0f4f0;padding:16px;border-radius:0 0 8px 8px;text-align:center;">
                <p style="color:#666;font-size:12px;margin:0;">Generated by VoiceTrim &bull; Call +1 (971) 432-1012 to update your plan</p>
              </div>
            </div>'''

            message = Mail(
                from_email=FROM_EMAIL,
                to_emails=email,
                subject='Your VoiceTrim 7-Day Meal Plan',
                html_content=html_body
            )
            sg = SendGridAPIClient(SENDGRID_KEY)
            sg.send(message)
            app.logger.info(f'Meal plan email sent to {email}')
        except Exception as e:
            app.logger.error(f'send_meal_plan_email error: {e}', exc_info=True)

    threading.Thread(target=send_async, daemon=True).start()
    return "I'm sending your meal plan to your email right now. Check your inbox in a moment!"


TOOL_HANDLERS = {
    'log_food':                lambda phone, call_id, args: handle_log_food(phone, call_id, args),
    'delete_food':             lambda phone, call_id, args: handle_delete_food(phone, args),
    'get_totals':              lambda phone, call_id, args: handle_get_totals(phone, args),
    'save_profile':            lambda phone, call_id, args: handle_save_profile(phone, args),
    'save_usual':              lambda phone, call_id, args: handle_save_usual(phone, args),
    'log_usual':               lambda phone, call_id, args: handle_log_usual(phone, call_id, args),
    'log_shopping_item':       lambda phone, call_id, args: handle_log_shopping_item(phone, args),
    'save_meal_plan':          lambda phone, call_id, args: handle_save_meal_plan(phone, args),
    'send_summary_email':      lambda phone, call_id, args: handle_send_summary_email(phone, args),
    'get_user_profile':        lambda phone, call_id, args: handle_get_user_profile(phone, args),
    # NEW tools
    'set_goal':                lambda phone, call_id, args: handle_set_goal(phone, args),
    'generate_meal_plan':      lambda phone, call_id, args: handle_generate_meal_plan(phone, args),
    'get_meal_plan':           lambda phone, call_id, args: handle_get_meal_plan(phone, args),
    'generate_shopping_list':  lambda phone, call_id, args: handle_generate_shopping_list(phone, args),
    'get_shopping_list':       lambda phone, call_id, args: handle_get_shopping_list(phone, args),
    'check_off_item':          lambda phone, call_id, args: handle_check_off_item(phone, args),
    'log_cart_item':           lambda phone, call_id, args: handle_log_cart_item(phone, call_id, args),
    'get_goals':               lambda phone, call_id, args: handle_get_goals(phone, args),
    'send_shopping_list_email': lambda phone, call_id, args: handle_send_shopping_list_email(phone, args),
    'send_meal_plan_email':     lambda phone, call_id, args: handle_send_meal_plan_email(phone, args),
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

def check_subscription(phone):
    """Check if a phone number has an active VoiceTrim subscription.
    Returns True if active or trialing, False otherwise.
    Falls back to True (allow call) if the check endpoint is unreachable,
    so a server outage never blocks existing subscribers.
    """
    if PAYWALL_BYPASS:
        app.logger.info(f"Paywall bypassed for {phone} (PAYWALL_BYPASS=true)")
        return True
    if not VOICE_GATEWAY_API_KEY:
        app.logger.warning("VOICE_GATEWAY_API_KEY not set — paywall disabled")
        return True
    try:
        import urllib.parse
        params = urllib.parse.urlencode({
            'input': json.dumps({'json': {'phone': phone, 'apiKey': VOICE_GATEWAY_API_KEY}})
        })
        url = f"{LANDING_PAGE_URL}/api/trpc/subscription.checkByPhone?{params}"
        resp = requests.get(url, timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            active = data.get('result', {}).get('data', {}).get('json', {}).get('active', False)
            app.logger.info(f"Subscription check for {phone}: active={active}")
            return bool(active)
        else:
            app.logger.warning(f"Subscription check returned {resp.status_code} — allowing call (fail-open)")
            return True
    except Exception as e:
        app.logger.warning(f"Subscription check error for {phone}: {e} — allowing call (fail-open)")
        return True


@app.route('/incoming-call', methods=['POST'])
def incoming_call():
    """Vapi assistant-request handler — personalized greeting per caller."""
    ASSISTANT_ID = os.environ.get('VAPI_ASSISTANT_ID', '8cc2b2a2-5bb5-4f20-814b-d4a34db8d71d')
    ONBOARDING_ASSISTANT_ID = os.environ.get('VAPI_ONBOARDING_ASSISTANT_ID', '')
    try:
        body = request.json or {}
        msg = body.get('message', {})
        call = msg.get('call', {})
        phone = call.get('customer', {}).get('number', '')
        app.logger.info(f"incoming-call: phone={phone}")

        # ── Paywall gate ────────────────────────────────────────────────────────────────
        if phone and not check_subscription(phone):
            app.logger.info(f"incoming-call: {phone} is not subscribed — routing to onboarding")

            # If an onboarding assistant is configured, route there for a proper
            # sign-up conversation. Otherwise fall back to a simple upsell message.
            if ONBOARDING_ASSISTANT_ID:
                return jsonify({"assistantId": ONBOARDING_ASSISTANT_ID})
            else:
                return jsonify({
                    "assistantId": ASSISTANT_ID,
                    "assistantOverrides": {
                        "firstMessage": (
                            "Hi there! Welcome to VoiceTrim. It looks like you don't have an active "
                            "subscription yet. To get started, visit voicetrim-landing.manus.space "
                            "and sign up for just $9.99 a month. We'd love to have you! Goodbye."
                        ),
                        "endCallAfterSpoken": True
                    }
                })
        # ───────────────────────────────────────────────────────────────────────

        first_name = 'there'
        is_new = True

        if phone:
            user = get_user_fast(phone)
            if user and not user['is_new']:
                first_name = user['first_name'] or 'there'
                is_new = False

        if is_new:
            first_message = "Hey there, welcome to VoiceTrim! I'm your personal nutrition assistant. What's your name?"
        else:
            first_message = f"Hey {first_name}, what are we logging today?"

        app.logger.info(f"incoming-call: firstMessage='{first_message}'")

        return jsonify({
            "assistantId": ASSISTANT_ID,
            "assistantOverrides": {
                "firstMessage": first_message
            }
        })
    except Exception as e:
        app.logger.error(f"incoming-call error: {e}", exc_info=True)
        return jsonify({
            "assistantId": os.environ.get('VAPI_ASSISTANT_ID', '8cc2b2a2-5bb5-4f20-814b-d4a34db8d71d'),
            "assistantOverrides": {
                "firstMessage": "Hey there, what are we logging today?"
            }
        })


@app.route('/send-signup-link', methods=['POST'])
def send_signup_link():
    """Called by the Vapi onboarding assistant tool after collecting name + email.
    Forwards the data to the landing page server which saves the pending signup
    and sends the email.
    """
    try:
        body = request.json or {}
        msg = body.get('message', {})
        call = msg.get('call', {})
        phone = call.get('customer', {}).get('number', '')

        # Extract tool call arguments
        tool_calls = msg.get('toolCalls', msg.get('toolCallList', []))
        if not tool_calls:
            return jsonify({"results": [{"toolCallId": "unknown", "result": "error: no tool calls"}]})

        tc = tool_calls[0]
        tc_id = tc.get('id', 'unknown')
        func = tc.get('function', {})
        args = parse_args(func)

        caller_name = str(args.get('caller_name', '')).strip()
        caller_email = str(args.get('caller_email', '')).strip().lower()

        if not caller_name or not caller_email or '@' not in caller_email:
            app.logger.warning(f"send-signup-link: invalid args name='{caller_name}' email='{caller_email}'")
            return jsonify({"results": [{"toolCallId": tc_id, "result": "error: missing name or email"}]})

        app.logger.info(f"send-signup-link: phone={phone} name={caller_name} email={caller_email}")

        # Call the landing page server's sendSignupLink tRPC mutation
        import urllib.parse
        trpc_url = f"{LANDING_PAGE_URL}/api/trpc/subscription.sendSignupLink"
        payload = {
            'json': {
                'phone': phone,
                'name': caller_name,
                'email': caller_email,
                'apiKey': VOICE_GATEWAY_API_KEY,
                'origin': LANDING_PAGE_URL,
            }
        }
        resp = requests.post(
            trpc_url,
            json={'0': payload},
            headers={'Content-Type': 'application/json'},
            timeout=8
        )

        if resp.status_code == 200:
            data = resp.json()
            email_sent = data.get('result', {}).get('data', {}).get('json', {}).get('emailSent', False)
            if email_sent:
                result_msg = f"Done! I've sent the signup link to {caller_email}. Check your inbox in about a minute."
            else:
                result_msg = f"I've saved your details. You can sign up at voicetrim-landing.manus.space when you're ready."
        else:
            app.logger.warning(f"send-signup-link: landing page returned {resp.status_code}")
            result_msg = f"I've noted your details. Visit voicetrim-landing.manus.space to complete your signup."

        return jsonify({"results": [{"toolCallId": tc_id, "result": result_msg}]})

    except Exception as e:
        app.logger.error(f"send-signup-link error: {e}", exc_info=True)
        return jsonify({"results": [{"toolCallId": "error", "result": "done"}]}), 200

@app.route('/call-start', methods=['POST'])
def call_start():
    """Legacy endpoint — kept for backwards compatibility."""
    return incoming_call()

@app.route('/call-completed', methods=['POST'])
def call_completed():
    """
    Called by Vapi's end-of-call webhook when a call ends.
    Records the call in the landing page DB, updates the subscriber's streak,
    and returns streak milestone data so the next call can celebrate it.
    
    Expected Vapi payload:
    {
      "message": {
        "type": "end-of-call-report",
        "call": { "id": "...", "customer": { "number": "+1..." } },
        "durationSeconds": 120,
        "summary": "User logged chicken salad..."
      }
    }
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        message = body.get('message', body)  # Vapi wraps in message key
        call = message.get('call', {})
        phone = call.get('customer', {}).get('number', '')
        vapi_call_id = call.get('id', '')
        duration_seconds = int(message.get('durationSeconds', 0))
        summary = message.get('summary', '')

        if not phone:
            app.logger.warning('[call-completed] No phone number in payload')
            return jsonify({'success': False, 'error': 'no phone number'}), 400

        if not VOICE_GATEWAY_API_KEY:
            app.logger.warning('[call-completed] VOICE_GATEWAY_API_KEY not set, skipping record')
            return jsonify({'success': True, 'skipped': True})

        # Call the landing page server to record the call and update streak
        payload = {
            'json': {
                'phone': phone,
                'apiKey': VOICE_GATEWAY_API_KEY,
                'vapiCallId': vapi_call_id,
                'durationSeconds': duration_seconds,
                'summary': summary or None,
            }
        }
        resp = requests.post(
            f'{LANDING_PAGE_URL}/api/trpc/subscription.recordCall',
            json=payload,
            timeout=10
        )
        result = resp.json().get('result', {}).get('data', {}).get('json', {})
        is_first_call = result.get('isFirstCall', False)
        current_streak = result.get('currentStreak', 0)

        app.logger.info(
            f'[call-completed] phone={phone} duration={duration_seconds}s '
            f'isFirstCall={is_first_call} streak={current_streak}'
        )

        # Log streak milestones for visibility
        if current_streak in (3, 7, 14, 30, 60, 90, 100):
            app.logger.info(f'[streak-milestone] {phone} reached {current_streak}-day streak!')

        return jsonify({
            'success': True,
            'isFirstCall': is_first_call,
            'currentStreak': current_streak,
        })

    except Exception as e:
        app.logger.error(f'[call-completed] Error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "VoiceTrim Middleware", "version": "3.1"})

@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": "VoiceTrim Middleware", "status": "running", "version": "3.0"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5556))
    app.run(host='0.0.0.0', port=port)
