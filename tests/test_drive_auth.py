"""OAuth 토큰 읽기·갱신 실패가 모두 AuthError(재로그인 안내)로 바뀌는지 확인한다."""

import json

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from drive2notion import drive
from drive2notion.errors import AuthError


def _token(tmp_path, **extra):
    data = {
        "token": "old-access",
        "refresh_token": "refresh-1",
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": drive.SCOPES,
        "expiry": "2020-01-01T00:00:00Z",  # 만료된 상태
        **extra,
    }
    path = tmp_path / "token.json"
    path.write_text(json.dumps(data))
    return path


def test_missing_token_asks_login(tmp_path):
    with pytest.raises(AuthError, match="drive2notion auth"):
        drive.load_credentials(str(tmp_path / "none.json"))


def test_expired_token_is_refreshed_and_saved(tmp_path, monkeypatch):
    path = _token(tmp_path)

    def fake_refresh(self, request):
        self.token = "new-access"
        self.expiry = None

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = drive.load_credentials(str(path))
    assert creds.token == "new-access"
    assert json.loads(path.read_text())["token"] == "new-access"
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_revoked_refresh_token_asks_login(tmp_path, monkeypatch):
    path = _token(tmp_path)

    def fail(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", fail)
    with pytest.raises(AuthError, match="만료"):
        drive.load_credentials(str(path))


def test_token_without_refresh_token_asks_login(tmp_path):
    path = _token(tmp_path, refresh_token=None)
    with pytest.raises(AuthError):
        drive.load_credentials(str(path))


def test_login_requires_client_info(tmp_path):
    with pytest.raises(AuthError, match="GOOGLE_CLIENT_ID"):
        drive.login(str(tmp_path / "client.json"), str(tmp_path / "token.json"))


def test_login_rejects_malformed_client_id(tmp_path):
    with pytest.raises(AuthError, match="형식"):
        drive.login("", str(tmp_path / "token.json"), "abc", "secret")


def test_client_config_from_env_values():
    from google_auth_oauthlib.flow import InstalledAppFlow

    cfg = drive.client_config("123-abc.apps.googleusercontent.com", "s3cret")
    flow = InstalledAppFlow.from_client_config(cfg, drive.SCOPES)
    assert flow.client_type == "installed"
    assert flow.client_config["client_id"] == "123-abc.apps.googleusercontent.com"
