"""
Regression tests for the VoiceTrim paywall gate.
Tests the check_subscription function and the /incoming-call endpoint
to verify that non-subscribers are blocked and subscribers are allowed.

Run with: python -m pytest test_paywall.py -v
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """Create a test client for the Flask app."""
    os.environ.setdefault('AIRTABLE_TOKEN', 'test_token')
    os.environ.setdefault('AIRTABLE_BASE', 'test_base')
    os.environ['VOICE_GATEWAY_API_KEY'] = 'test-api-key-12345'
    os.environ['LANDING_PAGE_URL'] = 'https://voicetrim-landing.manus.space'
    os.environ['PAYWALL_BYPASS'] = 'false'

    from app import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def make_incoming_call_body(phone='+15551234567'):
    """Build a minimal Vapi incoming-call request body."""
    return {
        "message": {
            "call": {
                "customer": {"number": phone},
                "id": "call_test_123"
            }
        }
    }


# ── check_subscription unit tests ─────────────────────────────────────────────

class TestCheckSubscription:
    def test_bypass_mode_always_returns_true(self):
        """When PAYWALL_BYPASS=true, all callers are allowed regardless of subscription."""
        os.environ['AIRTABLE_TOKEN'] = 'test_token'
        os.environ['AIRTABLE_BASE'] = 'test_base'
        os.environ['PAYWALL_BYPASS'] = 'true'
        os.environ['VOICE_GATEWAY_API_KEY'] = 'test-key'
        from app import check_subscription
        # Reload to pick up env change
        import importlib
        import app as app_module
        importlib.reload(app_module)
        # With bypass, should always return True
        assert app_module.PAYWALL_BYPASS is True
        os.environ['PAYWALL_BYPASS'] = 'false'

    def test_no_api_key_allows_call(self):
        """When VOICE_GATEWAY_API_KEY is not set, calls are allowed (fail-open)."""
        os.environ['AIRTABLE_TOKEN'] = 'test_token'
        os.environ['AIRTABLE_BASE'] = 'test_base'
        original_key = os.environ.pop('VOICE_GATEWAY_API_KEY', None)
        try:
            import importlib
            import app as app_module
            importlib.reload(app_module)
            result = app_module.check_subscription('+15551234567')
            assert result is True
        finally:
            if original_key:
                os.environ['VOICE_GATEWAY_API_KEY'] = original_key

    def test_network_error_allows_call(self):
        """When the subscription check endpoint is unreachable, calls are allowed (fail-open)."""
        os.environ['AIRTABLE_TOKEN'] = 'test_token'
        os.environ['AIRTABLE_BASE'] = 'test_base'
        os.environ['VOICE_GATEWAY_API_KEY'] = 'test-key'
        os.environ['PAYWALL_BYPASS'] = 'false'
        import importlib
        import app as app_module
        importlib.reload(app_module)

        with patch('requests.get', side_effect=Exception("Connection refused")):
            result = app_module.check_subscription('+15551234567')
        assert result is True  # fail-open

    def test_active_subscriber_allowed(self):
        """Active subscribers should pass the paywall check."""
        os.environ['AIRTABLE_TOKEN'] = 'test_token'
        os.environ['AIRTABLE_BASE'] = 'test_base'
        os.environ['VOICE_GATEWAY_API_KEY'] = 'test-key'
        os.environ['PAYWALL_BYPASS'] = 'false'
        import importlib
        import app as app_module
        importlib.reload(app_module)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'result': {'data': {'json': {'active': True, 'status': 'active'}}}
        }
        with patch('requests.get', return_value=mock_response):
            result = app_module.check_subscription('+15551234567')
        assert result is True

    def test_trialing_subscriber_allowed(self):
        """Trialing subscribers should pass the paywall check."""
        os.environ['AIRTABLE_TOKEN'] = 'test_token'
        os.environ['AIRTABLE_BASE'] = 'test_base'
        os.environ['VOICE_GATEWAY_API_KEY'] = 'test-key'
        os.environ['PAYWALL_BYPASS'] = 'false'
        import importlib
        import app as app_module
        importlib.reload(app_module)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'result': {'data': {'json': {'active': True, 'status': 'trialing'}}}
        }
        with patch('requests.get', return_value=mock_response):
            result = app_module.check_subscription('+15559876543')
        assert result is True

    def test_non_subscriber_blocked(self):
        """Non-subscribers should be blocked by the paywall check."""
        os.environ['AIRTABLE_TOKEN'] = 'test_token'
        os.environ['AIRTABLE_BASE'] = 'test_base'
        os.environ['VOICE_GATEWAY_API_KEY'] = 'test-key'
        os.environ['PAYWALL_BYPASS'] = 'false'
        import importlib
        import app as app_module
        importlib.reload(app_module)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'result': {'data': {'json': {'active': False, 'status': None}}}
        }
        with patch('requests.get', return_value=mock_response):
            result = app_module.check_subscription('+19999999999')
        assert result is False

    def test_canceled_subscriber_blocked(self):
        """Canceled subscribers should be blocked by the paywall check."""
        os.environ['AIRTABLE_TOKEN'] = 'test_token'
        os.environ['AIRTABLE_BASE'] = 'test_base'
        os.environ['VOICE_GATEWAY_API_KEY'] = 'test-key'
        os.environ['PAYWALL_BYPASS'] = 'false'
        import importlib
        import app as app_module
        importlib.reload(app_module)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'result': {'data': {'json': {'active': False, 'status': 'canceled'}}}
        }
        with patch('requests.get', return_value=mock_response):
            result = app_module.check_subscription('+15550000000')
        assert result is False


# ── /incoming-call endpoint integration tests ─────────────────────────────────

class TestIncomingCallPaywall:
    def test_non_subscriber_receives_upsell_message(self, client):
        """Non-subscribers should receive a friendly upsell message and the call ends."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'result': {'data': {'json': {'active': False, 'status': None}}}
        }
        with patch('requests.get', return_value=mock_response):
            # Also mock the Airtable user lookup
            with patch('app.get_user_fast', return_value=None):
                resp = client.post(
                    '/incoming-call',
                    json=make_incoming_call_body('+19999999999'),
                    content_type='application/json'
                )
        assert resp.status_code == 200
        data = resp.get_json()
        overrides = data.get('assistantOverrides', {})
        assert 'subscription' in overrides.get('firstMessage', '').lower() or \
               'voicetrim' in overrides.get('firstMessage', '').lower()
        assert overrides.get('endCallAfterSpoken') is True

    def test_active_subscriber_receives_greeting(self, client):
        """Active subscribers should receive the normal personalized greeting."""
        mock_sub_response = MagicMock()
        mock_sub_response.status_code = 200
        mock_sub_response.json.return_value = {
            'result': {'data': {'json': {'active': True, 'status': 'active'}}}
        }
        with patch('requests.get', return_value=mock_sub_response):
            with patch('app.get_user_fast', return_value={
                'first_name': 'Alex',
                'email': 'alex@example.com',
                'timezone': 'EST',
                'is_new': False,
                'cached_at': 9999999999
            }):
                resp = client.post(
                    '/incoming-call',
                    json=make_incoming_call_body('+15551234567'),
                    content_type='application/json'
                )
        assert resp.status_code == 200
        data = resp.get_json()
        overrides = data.get('assistantOverrides', {})
        assert 'Alex' in overrides.get('firstMessage', '')
        assert overrides.get('endCallAfterSpoken') is not True
