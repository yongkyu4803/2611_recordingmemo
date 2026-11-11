"""설정 읽기: config.toml(일반 설정) + .env(비밀 값)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    root: Path
    # Drive
    drive_folder_id: str = ""
    done_folder_name: str = "처리완료"
    min_age_minutes: int = 2
    # 본인 계정 OAuth: 클라이언트 파일(Google Cloud에서 받은 것)과 로그인 후 저장되는 토큰
    google_oauth_client_file: str = "~/.config/drive2notion/google-oauth-client.json"
    google_token_file: str = "~/.config/drive2notion/google-token.json"
    # 파일 대신 콘솔에 보이는 값을 .env에 직접 넣어도 된다 (값이 있으면 파일보다 우선)
    google_client_id: str = ""
    google_client_secret: str = ""
    # 로그인 결과를 받을 로컬 포트. '웹 애플리케이션' 클라이언트라면
    # 승인된 리디렉션 URI에 http://localhost:<포트>/ 를 등록해야 한다.
    google_oauth_port: int = 8765
    # Whisper
    whisper_model: str = "mlx-community/whisper-large-v3-mlx"
    whisper_language: str = "ko"
    whisper_timeout_factor: float = 0.5  # 녹음 길이 × 이 값 (초)
    whisper_timeout_min_sec: int = 600
    # Claude
    claude_model: str = "opus"
    claude_max_turns: int = 3
    # Notion
    notion_token: str = ""
    notion_parent_page_id: str = ""
    notion_version: str = "2025-09-03"
    # 운영
    max_attempts: int = 5
    keep_days: int = 30
    glossary: list[str] = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def summaries_dir(self) -> Path:
        return self.data_dir / "summaries"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "notion_preview"

    @property
    def claude_workdir(self) -> Path:
        return self.data_dir / "claude_workdir"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.tmp_dir,
            self.transcripts_dir,
            self.summaries_dir,
            self.logs_dir,
            self.preview_dir,
            self.claude_workdir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 형식의 .env를 읽는다. 따옴표와 # 주석을 처리한다."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def notion_page_id(value: str) -> str:
    """Notion 페이지 링크나 ID에서 32자리 페이지 ID를 뽑는다. 못 찾으면 원래 값을 돌려준다."""
    import re

    value = value.strip()
    path = value.split("?")[0].split("#")[0]
    found = re.findall(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", path)
    return found[-1].replace("-", "").lower() if found else value


def load_config(root: Path | None = None) -> Config:
    root = root or Path(os.environ.get("DRIVE2NOTION_ROOT", PROJECT_ROOT))
    cfg = Config(root=root)

    toml_path = root / "config.toml"
    if toml_path.exists():
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        drive = data.get("drive", {})
        whisper = data.get("whisper", {})
        claude = data.get("claude", {})
        notion = data.get("notion", {})
        ops = data.get("ops", {})
        cfg.drive_folder_id = drive.get("folder_id", cfg.drive_folder_id)
        cfg.done_folder_name = drive.get("done_folder_name", cfg.done_folder_name)
        cfg.min_age_minutes = drive.get("min_age_minutes", cfg.min_age_minutes)
        cfg.whisper_model = whisper.get("model", cfg.whisper_model)
        cfg.whisper_language = whisper.get("language", cfg.whisper_language)
        cfg.claude_model = claude.get("model", cfg.claude_model)
        cfg.claude_max_turns = claude.get("max_turns", cfg.claude_max_turns)
        cfg.notion_parent_page_id = notion.get("parent_page_id", cfg.notion_parent_page_id)
        cfg.notion_version = notion.get("version", cfg.notion_version)
        cfg.max_attempts = ops.get("max_attempts", cfg.max_attempts)
        cfg.keep_days = ops.get("keep_days", cfg.keep_days)
        cfg.glossary = list(data.get("glossary", {}).get("terms", []))

    env = {**_load_env_file(root / ".env"), **os.environ}
    cfg.google_oauth_client_file = env.get("GOOGLE_OAUTH_CLIENT_FILE", cfg.google_oauth_client_file)
    cfg.google_token_file = env.get("GOOGLE_TOKEN_FILE", cfg.google_token_file)
    cfg.google_client_id = env.get("GOOGLE_CLIENT_ID", "").strip()
    cfg.google_client_secret = env.get("GOOGLE_CLIENT_SECRET", "").strip()
    if env.get("GOOGLE_OAUTH_PORT", "").strip().isdigit():
        cfg.google_oauth_port = int(env["GOOGLE_OAUTH_PORT"].strip())
    cfg.notion_token = env.get("NOTION_TOKEN", cfg.notion_token)
    cfg.notion_parent_page_id = notion_page_id(env.get("NOTION_PARENT_PAGE_ID", cfg.notion_parent_page_id))
    cfg.drive_folder_id = env.get("DRIVE_FOLDER_ID", cfg.drive_folder_id)
    return cfg
