# VoiceTrim Middleware

Receives Vapi tool calls and writes directly to Airtable.

## Endpoints
- `GET /health` - Health check
- `POST /tool/log_food` - Log food entry
- `POST /tool/get_totals` - Get daily totals
- `POST /tool/save_usual` - Save usual meal
- `POST /tool/log_usual` - Log usual meal
- `POST /tool/log_shopping_item` - Add shopping item
- `POST /tool/save_meal_plan` - Save meal plan
- `POST /tool/send_summary_email` - Send summary email
