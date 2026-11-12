from datetime import datetime, timezone

import pytest

from drive2notion.audio import supported_audio_suffix
from drive2notion.drive import old_enough
from drive2notion.notion import (
    BLOCKS_PER_REQUEST,
    TEXT_LIMIT,
    PageMeta,
    batches,
    header_blocks,
    page_title,
    split_text,
    transcript_paragraphs,
)
from drive2notion.summarize import ActionItem, Summary, _classify, build_prompt
from drive2notion.errors import AuthError, PipelineError, QuotaError
from drive2notion.transcribe import Segment, clean_segments


# ---------- 텍스트 분할 ----------

def test_split_text_respects_limit_and_keeps_content():
    text = "회의를 시작하겠습니다. " * 400  # 약 5,200자
    parts = split_text(text)
    assert all(len(p) <= TEXT_LIMIT for p in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_split_text_without_spaces_hard_cuts():
    text = "가" * 4500
    parts = split_text(text)
    assert [len(p) for p in parts] == [2000, 2000, 500]


def test_transcript_paragraphs_under_limit_with_timestamps():
    segs = [Segment(i * 5.0, i * 5.0 + 5, "오늘 안건은 예산 집행 현황입니다.") for i in range(2000)]
    paras = transcript_paragraphs(segs)
    assert all(len(p) <= TEXT_LIMIT for p in paras)
    assert paras[0].startswith("[00:00:00] ")
    total = sum(p.count("예산 집행") for p in paras)
    assert total == 2000  # 빠진 구간 없음


def test_batches_of_100():
    assert [len(b) for b in batches(list(range(250)))] == [100, 100, 50]


# ---------- 환각·반복 제거 ----------

def test_clean_removes_hallucination_and_long_repeats():
    raw = [
        {"start": 0, "end": 2, "text": "안녕하세요 회의 시작합니다."},
        {"start": 2, "end": 4, "text": "시청해 주셔서 감사합니다."},
        {"start": 4, "end": 6, "text": "네."},
        {"start": 6, "end": 8, "text": "네."},
        {"start": 8, "end": 9, "text": "좋습니다"},
        {"start": 9, "end": 10, "text": "좋습니다"},
        {"start": 10, "end": 11, "text": "좋습니다."},
        {"start": 11, "end": 12, "text": ""},
        {"start": 12, "end": 14, "text": "유튜브 구독과 좋아요 얘기는 다음 안건에서 하죠."},
    ]
    kept, removed_h, removed_r = clean_segments(raw)
    texts = [s.text for s in kept]
    assert "시청해 주셔서 감사합니다." not in texts
    assert texts.count("네.") == 2  # 두 번 반복은 그대로 둔다
    assert texts.count("좋습니다") == 1  # 세 번 이상 반복은 하나만
    assert "유튜브 구독과 좋아요 얘기는 다음 안건에서 하죠." in texts  # 내용이 있는 문장은 유지
    assert removed_h == 1 and removed_r == 2


# ---------- Drive 필터 ----------

@pytest.mark.parametrize(("name", "mime_type", "expected"), [
    ("회의.M4A", "audio/mp4", ".m4a"),
    ("녹음", "audio/x-m4a", ".m4a"),
    ("녹음", "audio/m4a", ".m4a"),
    ("회의.mp3", "audio/mpeg", ".mp3"),
    ("회의.MP3", "application/octet-stream", ".mp3"),
    ("녹음", "audio/mpeg", ".mp3"),
    ("녹음", "audio/mp3", ".mp3"),
    ("녹음", "audio/x-mp3", ".mp3"),
    ("영상.mp4", "video/mp4", None),
    ("메모.txt", "text/plain", None),
    ("녹음", "application/octet-stream", None),
])
def test_supported_audio_suffix(name, mime_type, expected):
    assert supported_audio_suffix(name, mime_type) == expected


def test_old_enough():
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    assert old_enough("2026-11-09T11:57:00.000Z", 2, now)
    assert not old_enough("2026-11-09T11:59:30.000Z", 2, now)


# ---------- Notion 본문 ----------

def _summary(**kw):
    base = dict(title="예산 회의", summary="요약입니다.", key_points=["포인트"], decisions=[], action_items=[], uncertain_terms=[])
    base.update(kw)
    return Summary(**base)


def _meta():
    return PageMeta("a.m4a", "2026-11-09T10:00:00Z", 3725, "https://drive", "fid", "mlx-community/whisper-large-v3-mlx", "2026-11-09T21:00+09:00")


def test_header_blocks_skip_empty_sections_and_end_with_toggle():
    blocks = header_blocks(_summary(), _meta())
    headings = [b["heading_2"]["rich_text"][0]["text"]["content"] for b in blocks if b["type"] == "heading_2"]
    assert headings == ["요약", "주요 포인트", "전사 원문"]
    assert blocks[-1]["heading_2"]["is_toggleable"] is True
    assert "길이: 62분 5초" in blocks[0]["callout"]["rich_text"][0]["text"]["content"]


def test_header_blocks_action_items_format():
    s = _summary(action_items=[ActionItem(task="보고서 작성", owner="김주무관", due=None), ActionItem(task="자료 공유")])
    todos = [b for b in header_blocks(s, _meta()) if b["type"] == "to_do"]
    assert todos[0]["to_do"]["rich_text"][0]["text"]["content"] == "보고서 작성 (담당: 김주무관)"
    assert todos[1]["to_do"]["rich_text"][0]["text"]["content"] == "자료 공유"


def test_header_blocks_capped_at_100():
    s = _summary(key_points=[f"포인트 {i}" for i in range(150)])
    blocks = header_blocks(s, _meta())
    assert len(blocks) == BLOCKS_PER_REQUEST
    assert blocks[-1]["type"] == "heading_2"


def test_title_date_uses_local_time(monkeypatch):
    import os
    import time

    monkeypatch.setenv("TZ", "Asia/Seoul")
    time.tzset()
    try:
        meta = _meta()
        meta.recorded_at = "2026-10-17T20:30:00.000000Z"  # 한국 시각 10월 18일 05:30
        assert page_title(_summary(), meta).startswith("[2026-10-18]")
        callout = header_blocks(_summary(), meta)[0]["callout"]["rich_text"][0]["text"]["content"]
        assert "녹음 시각: 2026-10-18 05:30" in callout
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_page_title_fallback_to_filename():
    assert page_title(_summary(title="  "), _meta()) == "[2026-11-09] a.m4a"
    assert page_title(_summary(), _meta()) == "[2026-11-09] 예산 회의"


# ---------- Notion 페이지 ID ----------

def test_notion_page_id_from_link_or_id():
    from drive2notion.config import notion_page_id

    pid = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
    assert notion_page_id(f"https://www.notion.so/myspace/회의록-{pid}?source=copy_link") == pid
    assert notion_page_id(f"https://www.notion.so/{pid}#abc") == pid
    assert notion_page_id("1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d") == pid
    assert notion_page_id("") == ""


# ---------- Claude ----------

def test_build_prompt_wraps_transcript():
    p = build_prompt("본문", file_name="a.m4a", recorded_at=None, duration_sec=600, glossary=["국토부"])
    assert "<transcript>\n본문\n</transcript>" in p
    assert "약 10분" in p and "국토부" in p


def test_classify_errors():
    assert isinstance(_classify("Claude AI usage limit reached|1731200000"), QuotaError)
    assert isinstance(_classify("Invalid API key · Please run /login"), AuthError)
    assert type(_classify("something else")) is PipelineError
