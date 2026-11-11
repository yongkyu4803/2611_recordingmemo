"""로컬 Whisper 전사: 하위 프로세스 실행 + 환각·반복 구간 정리."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import NeedsReviewError, PipelineError

log = logging.getLogger(__name__)

# Whisper가 무음·잡음 구간에서 자주 지어내는 한국어 문구
HALLUCINATION_PATTERNS = [
    r"시청(해)?\s*주셔서\s*감사합니다",
    r"구독(과|하고)?\s*좋아요",
    r"좋아요(와|랑)?\s*구독",
    r"다음\s*영상에서\s*만나요",
    r"MBC\s*뉴스",
    r"KBS\s*뉴스",
    r"자막\s*(제작|제공)",
    r"한글\s*자막",
]
_HALLUCINATION_RE = re.compile("|".join(HALLUCINATION_PATTERNS))
_PUNCT_RE = re.compile(r"[\s.,!?~…·\-]+")

MIN_CHARS_PER_MIN = 50


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    segments: list[Segment]
    removed_hallucination: int
    removed_repeat: int
    elapsed_sec: float

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def to_json(self) -> dict:
        return {
            "segments": [s.__dict__ for s in self.segments],
            "removed_hallucination": self.removed_hallucination,
            "removed_repeat": self.removed_repeat,
            "elapsed_sec": self.elapsed_sec,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Transcript":
        return cls(
            segments=[Segment(**s) for s in data["segments"]],
            removed_hallucination=data.get("removed_hallucination", 0),
            removed_repeat=data.get("removed_repeat", 0),
            elapsed_sec=data.get("elapsed_sec", 0.0),
        )


def _is_hallucination(text: str) -> bool:
    """알려진 환각 문구를 빼고 나면 실질적인 내용이 남지 않는 구간인지."""
    if not _HALLUCINATION_RE.search(text):
        return False
    rest = _PUNCT_RE.sub("", _HALLUCINATION_RE.sub("", text))
    return len(rest) <= 3


def clean_segments(raw: list[dict]) -> tuple[list[Segment], int, int]:
    """빈 구간, 환각 구간, 같은 문장이 3번 이상 이어지는 반복을 제거한다.

    반복은 처음 1번만 남긴다.
    """
    removed_h = 0
    candidates: list[Segment] = []
    for s in raw:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if _is_hallucination(text):
            removed_h += 1
            continue
        candidates.append(Segment(float(s["start"]), float(s["end"]), text))

    # 같은 내용이 연속되는 구간을 묶는다
    kept: list[Segment] = []
    removed_r = 0
    i = 0
    while i < len(candidates):
        key = _PUNCT_RE.sub("", candidates[i].text)
        j = i + 1
        while j < len(candidates) and _PUNCT_RE.sub("", candidates[j].text) == key:
            j += 1
        run = candidates[i:j]
        if len(run) >= 3:
            kept.append(run[0])
            removed_r += len(run) - 1
        else:
            kept.extend(run)
        i = j
    return kept, removed_h, removed_r


def transcribe(
    wav: Path,
    out_json: Path,
    *,
    model: str,
    language: str,
    glossary: list[str],
    duration_sec: float,
    timeout_factor: float,
    timeout_min_sec: int,
) -> Transcript:
    prompt = ", ".join(glossary) if glossary else ""
    raw_out = out_json.with_suffix(".raw.json")
    timeout = max(timeout_min_sec, duration_sec * timeout_factor)
    cmd = [
        sys.executable,
        "-m",
        "drive2notion.whisper_worker",
        str(wav),
        str(raw_out),
        model,
        language,
        prompt,
    ]
    started = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise PipelineError(f"전사가 제한 시간({int(timeout)}초)을 넘었습니다.") from e
    elapsed = round(time.monotonic() - started, 1)
    if result.returncode != 0 or not raw_out.exists():
        raise PipelineError(f"Whisper 실행 실패: {result.stderr.strip()[-500:]}")

    raw = json.loads(raw_out.read_text(encoding="utf-8"))
    segments, removed_h, removed_r = clean_segments(raw.get("segments", []))
    transcript = Transcript(segments, removed_h, removed_r, elapsed)
    out_json.write_text(json.dumps(transcript.to_json(), ensure_ascii=False), encoding="utf-8")
    raw_out.unlink(missing_ok=True)
    log.info(
        "전사 완료: %d구간, 환각 제거 %d, 반복 제거 %d, %.1f초",
        len(segments),
        removed_h,
        removed_r,
        elapsed,
    )

    minutes = max(duration_sec / 60, 1)
    chars = len(transcript.text.replace(" ", ""))
    if chars < MIN_CHARS_PER_MIN * minutes and duration_sec >= 60:
        raise NeedsReviewError(
            f"전사 결과가 너무 짧습니다 ({chars}자 / {minutes:.0f}분). 녹음 상태를 확인하세요."
        )
    if chars == 0:
        raise NeedsReviewError("전사 결과가 비어 있습니다.")
    return transcript
