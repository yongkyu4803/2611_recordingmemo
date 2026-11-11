"""ffprobe로 메타데이터를 읽고, ffmpeg로 Whisper 입력용 WAV를 만든다."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import NeedsReviewError, PermanentError, PipelineError

# 앞뒤 무음 제거: 앞쪽을 자르고, 뒤집어서 다시 앞쪽(=원래 끝)을 자른 뒤 되돌린다.
# 말소리 앞에 0.5초 여유를 남긴다.
_TRIM_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.5,"
    "areverse,"
    "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.5,"
    "areverse"
)


@dataclass
class AudioInfo:
    duration_sec: float
    recorded_at: str | None  # m4a 메타데이터의 creation_time (있을 때만)


def _tool(name: str) -> str:
    path = shutil.which(name) or (f"/opt/homebrew/bin/{name}" if Path(f"/opt/homebrew/bin/{name}").exists() else None)
    if not path:
        raise PipelineError(f"{name}을(를) 찾을 수 없습니다. `brew install ffmpeg`로 설치하세요.")
    return path


def probe(path: Path) -> AudioInfo:
    result = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PermanentError(f"ffprobe가 파일을 읽지 못했습니다: {result.stderr.strip()[:300]}")
    fmt = json.loads(result.stdout).get("format", {})
    try:
        duration = float(fmt.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise PermanentError("재생 시간이 0입니다. 손상된 파일일 수 있습니다.")
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    return AudioInfo(duration_sec=duration, recorded_at=tags.get("creation_time"))


def to_wav(src: Path, dst: Path, trim_silence: bool = True) -> Path:
    """16kHz 모노 WAV로 변환한다."""
    cmd = [_tool("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", "16000"]
    if trim_silence:
        cmd += ["-af", _TRIM_FILTER]
    cmd.append(str(dst))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dst.exists():
        raise PermanentError(f"ffmpeg 변환 실패: {result.stderr.strip()[:300]}")
    # WAV 헤더(44바이트)만 남았다면 전체가 무음이라 잘려 나간 것
    if dst.stat().st_size < 16000 * 2:  # 1초 미만
        raise NeedsReviewError("무음 제거 후 남은 소리가 1초 미만입니다. 녹음이 비어 있을 수 있습니다.")
    return dst
