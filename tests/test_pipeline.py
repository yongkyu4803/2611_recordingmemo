"""파이프라인 재개·중복 방지 테스트. Whisper·Claude·Notion은 가짜로 바꾼다."""

import json
import subprocess
from pathlib import Path

import pytest

from drive2notion import pipeline as pl
from drive2notion.config import Config
from drive2notion.errors import QuotaError
from drive2notion.state import StateDB
from drive2notion.summarize import Summary, SummaryResult
from drive2notion.transcribe import Segment, Transcript


class FakeNotion:
    def __init__(self, fail_on_append_call: int | None = None):
        self.pages = []
        self.appended = []
        self.calls = 0
        self.fail_on = fail_on_append_call

    def create_page(self, title, blocks):
        self.pages.append(title)
        return f"page-{len(self.pages)}", "https://notion.so/x", "toggle-1"

    def find_toggle(self, page_id):
        return "toggle-1"

    def append(self, block_id, blocks):
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            raise pl.PipelineError("일시적 Notion 오류")
        self.appended.extend(blocks)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config(root=tmp_path)
    cfg.ensure_dirs()
    audio = tmp_path / "meeting.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "aac", str(audio)],
        check=True,
    )
    calls = {"transcribe": 0, "summarize": 0}

    def fake_transcribe(wav, out_json, **kw):
        calls["transcribe"] += 1
        segs = [Segment(i * 2.0, i * 2.0 + 2, f"문장 {i}번입니다. " * 20) for i in range(2000)]  # 약 200문단
        t = Transcript(segs, 0, 0, 1.0)
        out_json.write_text(json.dumps(t.to_json(), ensure_ascii=False), encoding="utf-8")
        return t

    def fake_summarize(text, **kw):
        calls["summarize"] += 1
        return SummaryResult(Summary(title="테스트", summary="요약", key_points=["a"]), None, 0.01, 10)

    monkeypatch.setattr(pl, "transcribe", fake_transcribe)
    monkeypatch.setattr(pl, "summarize", fake_summarize)
    monkeypatch.setattr(pl, "notify", lambda *a: None)
    db = StateDB(cfg.db_path)
    rec = db.add("local-1", "meeting.m4a", source="local", local_path=str(audio))
    yield cfg, db, rec, calls
    db.close()


def test_full_run_creates_one_page_with_all_paragraphs(env):
    cfg, db, rec, calls = env
    notion = FakeNotion()
    rec = pl.Pipeline(cfg, db, notion=notion).run(rec)
    assert rec.stage == "done" and rec.flag is None
    assert len(notion.pages) == 1
    assert notion.calls > 1  # 100블록씩 나눠 보냄
    assert all(len(b["paragraph"]["rich_text"][0]["text"]["content"]) <= 2000 for b in notion.appended)
    assert rec.blocks_appended == len(notion.appended)


def test_resume_after_notion_failure_does_not_duplicate(env):
    cfg, db, rec, calls = env
    notion = FakeNotion(fail_on_append_call=2)
    rec = pl.Pipeline(cfg, db, notion=notion).run(rec)
    assert rec.stage == "publishing" and rec.flag == "failed" and rec.attempts == 1
    sent_before = len(notion.appended)

    rec = pl.Pipeline(cfg, db, notion=notion).run(db.get("local-1"))
    assert rec.stage == "done"
    assert len(notion.pages) == 1  # 페이지를 새로 만들지 않음
    assert calls == {"transcribe": 1, "summarize": 1}  # 전사·정리 재실행 없음
    texts = [b["paragraph"]["rich_text"][0]["text"]["content"] for b in notion.appended]
    assert len(texts) == len(set(texts))  # 중복 문단 없음
    assert len(texts) > sent_before


def test_quota_error_does_not_count_attempts(env, monkeypatch):
    cfg, db, rec, calls = env

    def quota(*a, **kw):
        raise QuotaError("usage limit reached")

    monkeypatch.setattr(pl, "summarize", quota)
    p = pl.Pipeline(cfg, db, notion=FakeNotion())
    rec = p.run(rec)
    assert rec.stage == "transcribed" and rec.flag == "waiting_quota" and rec.attempts == 0
    assert p.quota_blocked
    assert [r.file_id for r in db.pending()] == ["local-1"]  # 다음 폴링에서 다시 시도


def test_attempts_reach_permanent(env, monkeypatch):
    cfg, db, rec, calls = env
    cfg.max_attempts = 2

    def boom(*a, **kw):
        raise pl.PipelineError("일시 오류")

    monkeypatch.setattr(pl, "summarize", boom)
    p = pl.Pipeline(cfg, db, notion=FakeNotion())
    rec = p.run(rec)
    assert rec.flag == "failed"
    rec = p.run(rec)
    assert rec.flag == "failed_permanent"
    assert db.pending() == []


class FakeDrive:
    def __init__(self, src: Path):
        self.src = src
        self.moved = []

    def download(self, file_id, dst, md5):
        dst.write_bytes(self.src.read_bytes())
        return dst

    def ensure_subfolder(self, parent, name):
        return "done-folder"

    def move(self, file_id, from_folder, to_folder):
        self.moved.append(file_id)


def test_drive_preview_does_not_move_file_and_resumes_later(env, tmp_path):
    cfg, db, local_rec, calls = env
    cfg.drive_folder_id = "folder"
    fake_drive = FakeDrive(Path(local_rec.local_path))
    rec = db.add("drive-1", "음성.m4a", source="drive", created_time="2026-11-09T11:00:00Z")

    rec = pl.Pipeline(cfg, db, drive=fake_drive, dry_run_notion=True).run(rec)
    assert rec.stage == "summarized" and rec.flag is None
    assert fake_drive.moved == []  # 미리보기에서는 Drive 파일을 옮기지 않음
    assert (cfg.preview_dir / "drive-1.json").exists()

    # Notion 설정 후 실제 실행: 전사·정리 없이 업로드부터
    notion = FakeNotion()
    rec = pl.Pipeline(cfg, db, drive=fake_drive, notion=notion).run(db.get("drive-1"))
    assert rec.stage == "done"
    assert fake_drive.moved == ["drive-1"]
    assert len(notion.pages) == 1
    assert calls == {"transcribe": 1, "summarize": 1}


def test_corrupt_file_is_permanent(env, tmp_path):
    cfg, db, _, calls = env
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio")
    rec = db.add("local-bad", "bad.m4a", source="local", local_path=str(bad))
    rec = pl.Pipeline(cfg, db, notion=FakeNotion()).run(rec)
    assert rec.flag == "failed_permanent"
    assert calls["transcribe"] == 0
