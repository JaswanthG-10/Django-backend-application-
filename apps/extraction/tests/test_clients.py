"""Unit tests for ThirdPartyExtractionClient external communication."""
from unittest.mock import MagicMock, patch
import pytest
import requests
from apps.extraction.clients import ThirdPartyExtractionClient
from apps.common.exceptions import (
    ExternalServiceError,
    ExternalServiceTimeoutError,
    ExternalServiceAuthError,
    ResourceNotFoundError,
    RateLimitExceededError,
)


class TestThirdPartyExtractionClient:
    @pytest.fixture
    def client(self):
        return ThirdPartyExtractionClient(
            base_url="https://api.crm-provider.test/v1",
            timeout=5
        )

    def test_extract_data_success(self, client):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {"id": "c1", "email": "a@test.com", "first_name": "A", "last_name": "B"}
            ]
        }
        mock_session.get.return_value = mock_response
        client.session = mock_session

        records = client.extract_data(api_token="valid-token-123", source_type="crm")

        assert len(records) == 1
        assert records[0]["id"] == "c1"
        assert mock_session.get.call_count == 1
        call_args, call_kwargs = mock_session.get.call_args
        assert call_args[0] == "https://api.crm-provider.test/v1/crm/records"
        assert call_kwargs["headers"]["Authorization"] == "Bearer valid-token-123"

    def test_empty_token_raises_auth_error(self, client):
        with pytest.raises(ExternalServiceAuthError):
            client.extract_data(api_token="", source_type="crm")

    def test_401_raises_auth_error(self, client):
        mock_session = MagicMock()
        mock_response = MagicMock(status_code=401, text="Unauthorized token")
        mock_session.get.return_value = mock_response
        client.session = mock_session

        with pytest.raises(ExternalServiceAuthError):
            client.extract_data(api_token="bad-token", source_type="crm")

    def test_403_raises_auth_error(self, client):
        mock_session = MagicMock()
        mock_response = MagicMock(status_code=403, text="Forbidden scope")
        mock_session.get.return_value = mock_response
        client.session = mock_session

        with pytest.raises(ExternalServiceAuthError):
            client.extract_data(api_token="restricted-token", source_type="crm")

    def test_404_raises_not_found_error(self, client):
        mock_session = MagicMock()
        mock_response = MagicMock(status_code=404, text="Resource not found")
        mock_session.get.return_value = mock_response
        client.session = mock_session

        with pytest.raises(ResourceNotFoundError):
            client.extract_data(api_token="valid-token", source_type="nonexistent")

    def test_429_raises_rate_limit_error(self, client):
        mock_session = MagicMock()
        mock_response = MagicMock(status_code=429, text="Rate limit exceeded")
        mock_session.get.return_value = mock_response
        client.session = mock_session

        with pytest.raises(RateLimitExceededError):
            client.extract_data(api_token="valid-token", source_type="crm")

    def test_500_raises_service_error(self, client):
        mock_session = MagicMock()
        mock_response = MagicMock(status_code=500, text="Internal provider breakdown")
        mock_session.get.return_value = mock_response
        client.session = mock_session

        with pytest.raises(ExternalServiceError):
            client.extract_data(api_token="valid-token", source_type="crm")

    def test_timeout_raises_timeout_error(self, client):
        mock_session = MagicMock()
        mock_session.get.side_effect = requests.exceptions.Timeout("Connection timed out")
        client.session = mock_session

        with pytest.raises(ExternalServiceTimeoutError):
            client.extract_data(api_token="valid-token", source_type="crm")

    def test_malformed_json_raises_service_error(self, client):
        mock_session = MagicMock()
        mock_response = MagicMock(status_code=200, text="<html>502 Bad Gateway</html>")
        mock_response.json.side_effect = ValueError("No JSON object could be decoded")
        mock_session.get.return_value = mock_response
        client.session = mock_session

        with pytest.raises(ExternalServiceError) as exc_info:
            client.extract_data(api_token="valid-token", source_type="crm")
        assert "malformed non-JSON response" in exc_info.value.message
