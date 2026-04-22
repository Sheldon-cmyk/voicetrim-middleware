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


# ── Onboarding routing tests ───────────────────────────────────────────────────

class TestOnboardingRouting:
    """Tests for non-subscriber routing to onboarding assistant."""

    def test_non_subscriber_routes_to_onboarding_assistant(self, client):
        """Non-subscribers with onboarding assistant ID configured get routed there."""
        os.environ['VAPI_ONBOARDING_ASSISTANT_ID'] = 'onboarding-asst-123'
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'result': {'data': {'json': {'active': False, 'status': None}}}
        }
        with patch('requests.get', return_value=mock_response):
            with patch('app.get_user_fast', return_value=None):
                resp = client.post(
                    '/incoming-call',
                    json=make_incoming_call_body('+15559990001'),
                    content_type='application/json'
                )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['assistantId'] == 'onboarding-asst-123'
        # No assistantOverrides — the onboarding assistant handles everything
        assert 'assistantOverrides' not in data

    def test_non_subscriber_fallback_when_no_onboarding_assistant(self, client):
        """Non-subscribers get upsell message when no onboarding assistant is configured."""
        os.environ['VAPI_ONBOARDING_ASSISTANT_ID'] = ''
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'result': {'data': {'json': {'active': False, 'status': None}}}
        }
        with patch('requests.get', return_value=mock_response):
            with patch('app.get_user_fast', return_value=None):
                resp = client.post(
                    '/incoming-call',
                    json=make_incoming_call_body('+15559990002'),
                    content_type='application/json'
                )
        assert resp.status_code == 200
        data = resp.get_json()
        overrides = data.get('assistantOverrides', {})
        assert overrides.get('endCallAfterSpoken') is True
        assert 'voicetrim-landing.manus.space' in overrides.get('firstMessage', '')


# ── /send-signup-link endpoint tests ─────────────────────────────────────────

def make_tool_call_body(phone, name, email):
    """Build a Vapi tool-call request body for send_signup_link."""
    return {
        'message': {
            'call': {'customer': {'number': phone}, 'id': 'call_test_456'},
            'toolCalls': [{
                'id': 'tc_test_001',
                'function': {
                    'name': 'send_signup_link',
                    'arguments': json.dumps({'caller_name': name, 'caller_email': email})
                }
            }]
        }
    }


class TestSendSignupLink:
    """Tests for the /send-signup-link endpoint."""

    def test_send_signup_link_success(self, client):
        """Valid name + email triggers landing page API call and returns success message."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'result': {'data': {'json': {'success': True, 'emailSent': True}}}
        }
        with patch('requests.post', return_value=mock_resp):
            resp = client.post(
                '/send-signup-link',
                json=make_tool_call_body('+15551234567', 'John Doe', 'john@example.com'),
                content_type='application/json'
            )
        assert resp.status_code == 200
        data = resp.get_json()
        results = data.get('results', [])
        assert len(results) == 1
        assert results[0]['toolCallId'] == 'tc_test_001'
        assert 'john@example.com' in results[0]['result']

    def test_send_signup_link_missing_email(self, client):
        """Missing email returns an error result."""
        resp = client.post(
            '/send-signup-link',
            json=make_tool_call_body('+15551234567', 'John Doe', ''),
            content_type='application/json'
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'error' in data['results'][0]['result'].lower()

    def test_send_signup_link_invalid_email(self, client):
        """Invalid email (no @) returns an error result."""
        resp = client.post(
            '/send-signup-link',
            json=make_tool_call_body('+15551234567', 'John Doe', 'notanemail'),
            content_type='application/json'
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'error' in data['results'][0]['result'].lower()

    def test_send_signup_link_landing_page_down(self, client):
        """When landing page is unreachable, returns a graceful fallback message."""
        with patch('requests.post', side_effect=Exception('Connection refused')):
            resp = client.post(
                '/send-signup-link',
                json=make_tool_call_body('+15551234567', 'Jane Smith', 'jane@example.com'),
                content_type='application/json'
            )
        assert resp.status_code == 200
        data = resp.get_json()
        # Should not crash — returns a graceful fallback
        assert len(data.get('results', [])) == 1

    def test_send_signup_link_no_tool_calls(self, client):
        """Empty tool calls list returns an error result."""
        resp = client.post(
            '/send-signup-link',
            json={'message': {'call': {'customer': {'number': '+15551234567'}}, 'toolCalls': []}},
            content_type='application/json'
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'error' in data['results'][0]['result'].lower()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
