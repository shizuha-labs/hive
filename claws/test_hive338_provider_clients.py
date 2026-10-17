from unittest import mock

import requests
from django.test import SimpleTestCase

from claws.provider_clients import ProviderProbeError, discover_models, validate_credential


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"data": [{"id": "model-a"}]}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ProviderClientProbeTests(SimpleTestCase):
    providers = ["openai", "anthropic", "openai_compatible"]

    def _base_url(self, provider):
        return "https://compatible.example" if provider == "openai_compatible" else ""

    def test_validate_ok_for_all_provider_types(self):
        for provider in self.providers:
            with self.subTest(provider=provider), mock.patch("claws.provider_clients.requests.request", return_value=FakeResponse()) as req:
                result = validate_credential(provider, self._base_url(provider), "sk-secret-value", timeout=1)
                self.assertEqual(result, {"ok": True, "error_class": None, "detail": ""})
                headers = req.call_args.kwargs["headers"]
                self.assertNotIn("sk-secret-value", repr({k: v for k, v in headers.items() if k.lower() != "authorization" and k.lower() != "x-api-key"}))

    def test_discover_models_parses_models_for_all_provider_types(self):
        payload = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        for provider in self.providers:
            with self.subTest(provider=provider), mock.patch("claws.provider_clients.requests.request", return_value=FakeResponse(payload=payload)):
                self.assertEqual(discover_models(provider, self._base_url(provider), "sk-secret-value", timeout=1), ["model-a", "model-b"])

    def test_auth_failure_for_all_provider_types(self):
        payload = {"error": {"type": "authentication_error", "message": "bad key sk-secret-value"}}
        for provider in self.providers:
            with self.subTest(provider=provider), mock.patch("claws.provider_clients.requests.request", return_value=FakeResponse(401, payload)):
                result = validate_credential(provider, self._base_url(provider), "sk-secret-value", timeout=1)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_class"], "auth_failure")
                self.assertNotIn("sk-secret-value", result["detail"])

    def test_rate_limit_for_all_provider_types(self):
        payload = {"error": {"type": "rate_limit_error", "message": "too many requests"}}
        for provider in self.providers:
            with self.subTest(provider=provider), mock.patch("claws.provider_clients.requests.request", return_value=FakeResponse(429, payload)):
                result = validate_credential(provider, self._base_url(provider), "sk-secret-value", timeout=1)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_class"], "rate_limit")

    def test_quota_exhausted_for_all_provider_types(self):
        payload = {"error": {"code": "insufficient_quota", "message": "quota exhausted"}}
        for provider in self.providers:
            with self.subTest(provider=provider), mock.patch("claws.provider_clients.requests.request", return_value=FakeResponse(429, payload)):
                result = validate_credential(provider, self._base_url(provider), "sk-secret-value", timeout=1)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_class"], "quota_exhausted")

    def test_network_error_for_all_provider_types(self):
        for provider in self.providers:
            with self.subTest(provider=provider), mock.patch("claws.provider_clients.requests.request", side_effect=requests.Timeout("timeout sk-secret-value")):
                result = validate_credential(provider, self._base_url(provider), "sk-secret-value", timeout=1)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_class"], "network")
                self.assertNotIn("sk-secret-value", result["detail"])

    def test_anthropic_discover_models_static_fallback_when_models_endpoint_missing(self):
        with mock.patch("claws.provider_clients.requests.request", return_value=FakeResponse(404, {"error": {"message": "not found"}})):
            models = discover_models("anthropic", "", "sk-secret-value", timeout=1)
        self.assertIn("claude-3-5-haiku-latest", models)

    def test_invalid_provider_and_openai_compatible_missing_base_url(self):
        self.assertEqual(validate_credential("bogus", "", "sk-secret-value")["error_class"], "invalid_provider")
        self.assertEqual(validate_credential("openai_compatible", "", "sk-secret-value")["error_class"], "invalid_provider")

    def test_invalid_model_response_raises_for_discovery(self):
        with mock.patch("claws.provider_clients.requests.request", return_value=FakeResponse(payload={"data": []})):
            with self.assertRaises(ProviderProbeError) as ctx:
                discover_models("openai", "", "sk-secret-value", timeout=1)
        self.assertEqual(ctx.exception.result.error_class, "invalid_response")
