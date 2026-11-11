"""파일별 처리 상태를 SQLite에 기록한다.

stage: 마지막으로 성공한 단계. 다음 실행은 이 단계 다음부터 이어 간다.
flag:  예외 상태. None이면 정상 진행 대상.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STAGES = [
    "discovered",
    "downloaded",
    "transcribed",
    "summarized",
    "publishing",
    "published",
    "done",
]

FLAG_FAILED = "failed"
FLAG_WAITING_QUOTA = "waiting_quota"
FLAG_PERMANENT = "failed_permanent"
FLAG_NEEDS_REVIEW = "needs_review"
FLAG_DUPLICATE = "duplicate"

# 다음 폴링에서 다시 시도하는 플래그
RETRYABLE_FLAGS = {None, FLAG_FAILED, FLAG_WAITING_QUOTA}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id         TEXT PRIMARY KEY,
    source          TEXT NOT NULL DEFAULT 'drive',
    name            TEXT NOT NULL,
    md5             TEXT,
    size            INTEGER,
    mime_type       TEXT,
    created_time    TEXT,
    web_link        TEXT,
    local_path      TEXT,
    recorded_at     TEXT,
    duration_sec    REAL,
    stage           TEXT NOT NULL,
    flag            TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    page_id         TEXT,
    page_url        TEXT,
    toggle_block_id TEXT,
    blocks_appended INTEGER NOT NULL DEFAULT 0,
    transcribe_sec  REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_md5 ON files(md5);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class FileRecord:
    file_id: str
    source: str
    name: str
    md5: str | None
    size: int | None
    mime_type: str | None
    created_time: str | None
    web_link: str | None
    local_path: str | None
    recorded_at: str | None
    duration_sec: float | None
    stage: str
    flag: str | None
    attempts: int
    last_error: str | None
    page_id: str | None
    page_url: str | None
    toggle_block_id: str | None
    blocks_appended: int
    transcribe_sec: float | None
    created_at: str
    updated_at: str


class StateDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get(self, file_id: str) -> FileRecord | None:
        row = self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        return FileRecord(**dict(row)) if row else None

    def add(
        self,
        file_id: str,
        name: str,
        *,
        source: str = "drive",
        md5: str | None = None,
        size: int | None = None,
        mime_type: str | None = None,
        created_time: str | None = None,
        web_link: str | None = None,
        local_path: str | None = None,
    ) -> FileRecord:
        """처음 본 파일을 discovered로 등록한다. 이미 있으면 기존 기록을 돌려준다."""
        existing = self.get(file_id)
        if existing:
            return existing
        now = _now()
        self.conn.execute(
            """INSERT INTO files (file_id, source, name, md5, size, mime_type, created_time,
                                  web_link, local_path, stage, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)""",
            (file_id, source, name, md5, size, mime_type, created_time, web_link, local_path, now, now),
        )
        self.conn.commit()
        return self.get(file_id)  # type: ignore[return-value]

    def update(self, file_id: str, **fields) -> FileRecord:
        if not fields:
            return self.get(file_id)  # type: ignore[return-value]
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE files SET {cols} WHERE file_id = ?", (*fields.values(), file_id))
        self.conn.commit()
        return self.get(file_id)  # type: ignore[return-value]

    def advance(self, file_id: str, stage: str, **fields) -> FileRecord:
        """단계 성공: stage를 올리고 플래그와 오류를 지운다."""
        assert stage in STAGES, stage
        return self.update(file_id, stage=stage, flag=None, last_error=None, **fields)

    def find_done_by_md5(self, md5: str, exclude_id: str) -> FileRecord | None:
        row = self.conn.execute(
            """SELECT * FROM files WHERE md5 = ? AND file_id != ? AND page_id IS NOT NULL
               ORDER BY created_at LIMIT 1""",
            (md5, exclude_id),
        ).fetchone()
        return FileRecord(**dict(row)) if row else None

    def pending(self) -> list[FileRecord]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE stage != 'done' ORDER BY created_time, created_at"
        ).fetchall()
        records = [FileRecord(**dict(r)) for r in rows]
        return [r for r in records if r.flag in RETRYABLE_FLAGS]

    def all(self) -> list[FileRecord]:
        rows = self.conn.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
        return [FileRecord(**dict(r)) for r in rows]
