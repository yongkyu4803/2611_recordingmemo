"""Google Drive: 새 m4a 조회, 다운로드, 처리완료 폴더로 이동.

인증은 본인 Google 계정 OAuth. `drive2notion auth`로 브라우저에서 한 번 로그인하면
토큰 파일이 저장되고, 이후에는 그 토큰으로 무인 실행한다.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .errors import AuthError, PipelineError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
FIELDS = "id, name, mimeType, size, md5Checksum, createdTime, modifiedTime, webViewLink"


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int
    md5: str | None
    created_time: str
    web_link: str | None


def is_m4a(name: str, mime_type: str) -> bool:
    """확장자가 .m4a이거나, 확장자가 없어도 mimeType이 m4a 전용 값이면 m4a로 본다."""
    return name.lower().endswith(".m4a") or mime_type in {"audio/x-m4a", "audio/m4a"}


def old_enough(created_time: str, min_age_minutes: int, now: datetime | None = None) -> bool:
    created = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    return now - created >= timedelta(minutes=min_age_minutes)


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _retryable(exc: BaseException) -> bool:
    from googleapiclient.errors import HttpError

    if isinstance(exc, HttpError):
        return exc.resp.status == 429 or exc.resp.status >= 500
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


_retry = retry(
    retry=retry_if_exception(_retryable),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)


RELOGIN_HINT = "터미널에서 `uv run drive2notion auth`를 실행해 다시 로그인하세요."


def _save_token(creds, token_path: Path) -> None:  # noqa: ANN001
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)


def client_config(client_id: str, client_secret: str) -> dict:
    """콘솔에서 복사한 ID·비밀번호로 '데스크톱 앱' 클라이언트 설정을 만든다."""
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def login(
    client_file: str, token_file: str, client_id: str = "", client_secret: str = "", port: int = 8765
) -> Path:
    """브라우저를 열어 본인 계정으로 로그인하고 토큰을 저장한다. 사람이 터미널에서 한 번 실행한다.

    .env의 GOOGLE_CLIENT_ID·GOOGLE_CLIENT_SECRET이 있으면 그것을, 없으면 클라이언트 JSON 파일을 쓴다.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    if client_id and client_secret:
        if not client_id.endswith(".apps.googleusercontent.com"):
            raise AuthError("GOOGLE_CLIENT_ID 형식이 이상합니다. '…apps.googleusercontent.com'으로 끝나야 합니다.")
        flow = InstalledAppFlow.from_client_config(client_config(client_id, client_secret), SCOPES)
    else:
        client_path = Path(client_file).expanduser()
        if not client_path.exists():
            raise AuthError(
                "Google OAuth 클라이언트 정보가 없습니다. .env에 GOOGLE_CLIENT_ID와 GOOGLE_CLIENT_SECRET을 넣거나, "
                f"'데스크톱 앱' 클라이언트 JSON을 {client_path}에 두세요."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    # offline + consent: 갱신 토큰(refresh token)을 확실히 받기 위해
    creds = flow.run_local_server(
        port=port,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="브라우저에서 Google 로그인을 진행하세요. 열리지 않으면 이 주소를 여세요:\n{url}",
        success_message="로그인이 끝났습니다. 이 창을 닫아도 됩니다.",
    )
    if not creds.refresh_token:
        raise AuthError("갱신 토큰을 받지 못했습니다. 다시 시도하세요.")
    token_path = Path(token_file).expanduser()
    _save_token(creds, token_path)
    return token_path


def load_credentials(token_file: str):  # noqa: ANN201
    """저장된 토큰을 읽고, 만료됐으면 갱신한다. 실패하면 AuthError."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = Path(token_file).expanduser()
    if not token_path.exists():
        raise AuthError(f"Google 로그인 토큰이 없습니다: {token_path}. {RELOGIN_HINT}")
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (ValueError, KeyError) as e:
        raise AuthError(f"토큰 파일을 읽지 못했습니다: {e}. {RELOGIN_HINT}") from e
    if not creds.valid:
        if not creds.refresh_token:
            raise AuthError(f"갱신 토큰이 없습니다. {RELOGIN_HINT}")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # 앱이 '테스트' 상태면 7일 뒤 갱신 토큰이 만료돼 여기로 온다
            raise AuthError(f"Google 로그인이 만료됐습니다 ({e}). {RELOGIN_HINT}") from e
        _save_token(creds, token_path)
    return creds


class DriveClient:
    def __init__(self, token_file: str):
        from googleapiclient.discovery import build

        creds = load_credentials(token_file)
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _execute(self, request):  # noqa: ANN001
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        @_retry
        def go():
            return request.execute()

        try:
            return go()
        except RefreshError as e:
            raise AuthError(f"Google 로그인이 만료됐습니다 ({e}). {RELOGIN_HINT}") from e
        except HttpError as e:
            if e.resp.status in (401, 403, 404):
                raise AuthError(
                    f"Drive 접근 오류({e.resp.status}): 폴더 ID와 로그인한 계정을 확인하세요. {e}"
                ) from e
            raise PipelineError(f"Drive 오류({e.resp.status}): {e}") from e

    def list_new_m4a(self, folder_id: str, min_age_minutes: int) -> list[DriveFile]:
        files: list[DriveFile] = []
        token = None
        q = f"'{folder_id}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'"
        while True:
            res = self._execute(
                self.service.files().list(
                    q=q,
                    fields=f"nextPageToken, files({FIELDS})",
                    pageSize=100,
                    pageToken=token,
                    orderBy="createdTime",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
            )
            for f in res.get("files", []):
                if not is_m4a(f["name"], f.get("mimeType", "")):
                    continue
                if not old_enough(f["createdTime"], min_age_minutes):
                    log.info("업로드 직후라 다음 폴링으로 미룸: %s", f["name"])
                    continue
                files.append(
                    DriveFile(
                        id=f["id"],
                        name=f["name"],
                        mime_type=f.get("mimeType", ""),
                        size=int(f.get("size", 0)),
                        md5=f.get("md5Checksum"),
                        created_time=f["createdTime"],
                        web_link=f.get("webViewLink"),
                    )
                )
            token = res.get("nextPageToken")
            if not token:
                return files

    def download(self, file_id: str, dst: Path, expected_md5: str | None, attempts: int = 3) -> Path:
        from google.auth.exceptions import RefreshError
        from googleapiclient.http import MediaIoBaseDownload

        for i in range(attempts):
            request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
            with dst.open("wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=16 * 1024 * 1024)
                done = False
                while not done:
                    try:
                        _, done = _retry(downloader.next_chunk)()
                    except RefreshError as e:
                        raise AuthError(f"Google 로그인이 만료됐습니다 ({e}). {RELOGIN_HINT}") from e
            if not expected_md5 or md5_of(dst) == expected_md5:
                return dst
            log.warning("MD5 불일치, 다시 받음 (%d/%d)", i + 1, attempts)
        raise PipelineError("다운로드한 파일의 MD5가 Drive 값과 계속 다릅니다.")

    def ensure_subfolder(self, parent_id: str, name: str) -> str:
        safe = name.replace("'", "\\'")
        res = self._execute(
            self.service.files().list(
                q=f"'{parent_id}' in parents and name = '{safe}' and mimeType = '{FOLDER_MIME}' and trashed = false",
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        if res.get("files"):
            return res["files"][0]["id"]
        folder = self._execute(
            self.service.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            )
        )
        log.info("Drive에 '%s' 폴더를 만들었습니다.", name)
        return folder["id"]

    def move(self, file_id: str, from_folder: str, to_folder: str) -> None:
        self._execute(
            self.service.files().update(
                fileId=file_id,
                addParents=to_folder,
                removeParents=from_folder,
                fields="id, parents",
                supportsAllDrives=True,
            )
        )
