"""Notion 페이지 생성과 본문 블록 구성.

Notion API 한도:
- 텍스트 객체 하나에 2,000자
- 요청 한 번에 블록 100개
- 평균 초당 3건
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .errors import AuthError, PipelineError
from .summarize import Summary
from .transcribe import Segment

log = logging.getLogger(__name__)

TEXT_LIMIT = 2000
PARAGRAPH_TARGET = 1800  # 타임스탬프 접두어 여유
BLOCKS_PER_REQUEST = 100
MIN_INTERVAL_SEC = 0.35

_SENTENCE_END = re.compile(r"(?<=[.!?。…])\s+|(?<=[다요죠까네])\.?\s+")


# ---------- 블록 만들기 (순수 함수, 테스트 대상) ----------

def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """limit 이하 조각으로 나눈다. 가능하면 문장 경계, 안 되면 공백, 그래도 안 되면 글자 수로 자른다."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for m in _SENTENCE_END.finditer(window):
            cut = m.end()
        if cut <= limit // 2:
            space = window.rfind(" ")
            cut = space + 1 if space > limit // 2 else limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": chunk}} for chunk in split_text(text)] or [
        {"type": "text", "text": {"content": ""}}
    ]


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich(text), **extra}}


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def transcript_paragraphs(segments: list[Segment], target: int = PARAGRAPH_TARGET) -> list[str]:
    """구간을 이어 붙여 target 자 이하 문단으로 만든다. 문단 앞에 시작 시각을 붙인다."""
    paragraphs: list[str] = []
    buf: list[str] = []
    buf_len = 0
    start = 0.0
    for seg in segments:
        if not buf:
            start = seg.start
        if buf and buf_len + len(seg.text) + 1 > target:
            paragraphs.append(f"[{fmt_ts(start)}] " + " ".join(buf))
            buf, buf_len, start = [], 0, seg.start
        if len(seg.text) > target:
            for piece in split_text(seg.text, target):
                paragraphs.append(f"[{fmt_ts(seg.start)}] {piece}")
            continue
        buf.append(seg.text)
        buf_len += len(seg.text) + 1
    if buf:
        paragraphs.append(f"[{fmt_ts(start)}] " + " ".join(buf))
    return paragraphs


@dataclass
class PageMeta:
    file_name: str
    recorded_at: str | None
    duration_sec: float
    drive_link: str | None
    file_id: str
    whisper_model: str
    processed_at: str


def local_time(iso: str | None) -> str | None:
    """ISO 시각(UTC 'Z' 포함)을 이 Mac의 현지 시각 'YYYY-MM-DD HH:MM'으로 바꾼다."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def page_title(summary: Summary | None, meta: PageMeta) -> str:
    date = (local_time(meta.recorded_at) or local_time(meta.processed_at) or "")[:10]
    title = (summary.title.strip() if summary and summary.title.strip() else meta.file_name)
    return f"[{date}] {title}"


def header_blocks(summary: Summary, meta: PageMeta) -> list[dict]:
    """페이지 생성 때 함께 넣는 블록들. 마지막은 비어 있는 '전사 원문' 토글 제목."""
    minutes = int(meta.duration_sec // 60)
    seconds = int(meta.duration_sec % 60)
    info = [
        f"원본 파일: {meta.file_name}",
        f"녹음 시각: {local_time(meta.recorded_at) or '알 수 없음'}",
        f"길이: {minutes}분 {seconds}초",
        f"Drive: {meta.drive_link or '-'}",
        f"처리 일시: {local_time(meta.processed_at)}",
        f"Drive 파일 ID: {meta.file_id}",
        f"전사 모델: {meta.whisper_model.split('/')[-1]}",
    ]
    blocks: list[dict] = [
        _block("callout", "\n".join(info), icon={"type": "emoji", "emoji": "🎙️"}, color="gray_background"),
        _block("heading_2", "요약"),
        _block("paragraph", summary.summary),
        _block("heading_2", "주요 포인트"),
        *[_block("bulleted_list_item", p) for p in summary.key_points],
    ]
    if summary.decisions:
        blocks.append(_block("heading_2", "결정 사항"))
        blocks += [_block("bulleted_list_item", d) for d in summary.decisions]
    if summary.action_items:
        blocks.append(_block("heading_2", "할 일"))
        for item in summary.action_items:
            extra = " · ".join(x for x in (item.owner and f"담당: {item.owner}", item.due and f"기한: {item.due}") if x)
            text = f"{item.task} ({extra})" if extra else item.task
            blocks.append(_block("to_do", text, checked=False))
    if summary.uncertain_terms:
        blocks.append(_block("heading_2", "확인 필요한 표현"))
        blocks += [_block("bulleted_list_item", t) for t in summary.uncertain_terms]
    blocks.append(_block("heading_2", "전사 원문", is_toggleable=True))
    # 생성 요청 한 번에 100블록까지라서 넘치면 뒤쪽을 잘라 낸다(실제로는 거의 없음)
    if len(blocks) > BLOCKS_PER_REQUEST:
        blocks = blocks[: BLOCKS_PER_REQUEST - 1] + [blocks[-1]]
    return blocks


def batches(items: list, size: int = BLOCKS_PER_REQUEST) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------- API 호출 ----------

def _retryable(exc: BaseException) -> bool:
    from notion_client import APIResponseError
    from notion_client.errors import HTTPResponseError, RequestTimeoutError

    if isinstance(exc, RequestTimeoutError):
        return True
    if isinstance(exc, APIResponseError):
        return exc.status == 429 or exc.status >= 500
    if isinstance(exc, HTTPResponseError):
        return exc.status >= 500
    return isinstance(exc, (ConnectionError, TimeoutError))


_retry = retry(
    retry=retry_if_exception(_retryable),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)


class NotionWriter:
    def __init__(self, token: str, parent_page_id: str, version: str):
        if not token or not parent_page_id:
            raise AuthError("NOTION_TOKEN 또는 NOTION_PARENT_PAGE_ID가 비어 있습니다.")
        from notion_client import Client

        self.client = Client(auth=token, notion_version=version)
        self.parent_page_id = parent_page_id
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _call(self, fn, **kwargs):  # noqa: ANN001
        from notion_client import APIResponseError

        @_retry
        def go():
            self._throttle()
            return fn(**kwargs)

        try:
            return go()
        except APIResponseError as e:
            if e.status in (401, 403, 404):
                raise AuthError(
                    f"Notion 접근 오류({e.status}): {e}. 통합을 부모 페이지에 연결했는지 확인하세요."
                ) from e
            raise PipelineError(f"Notion 오류({e.status}): {e}") from e

    def create_page(self, title: str, blocks: list[dict]) -> tuple[str, str, str]:
        """페이지를 만들고 (page_id, url, 전사 원문 토글 블록 id)를 돌려준다."""
        page = self._call(
            self.client.pages.create,
            parent={"type": "page_id", "page_id": self.parent_page_id},
            properties={"title": {"title": [{"type": "text", "text": {"content": title[:TEXT_LIMIT]}}]}},
            children=blocks,
        )
        toggle_id = self.find_toggle(page["id"])
        return page["id"], page.get("url", ""), toggle_id

    def find_toggle(self, page_id: str) -> str:
        cursor = None
        toggle_id = None
        while True:
            kwargs = {"block_id": page_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            res = self._call(self.client.blocks.children.list, **kwargs)
            for b in res["results"]:
                if b["type"] == "heading_2" and b["heading_2"].get("is_toggleable"):
                    toggle_id = b["id"]
            if not res.get("has_more"):
                break
            cursor = res["next_cursor"]
        if not toggle_id:
            raise PipelineError("페이지에서 '전사 원문' 토글 블록을 찾지 못했습니다.")
        return toggle_id

    def append(self, block_id: str, blocks: list[dict]) -> None:
        self._call(self.client.blocks.children.append, block_id=block_id, children=blocks)


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="minutes")
