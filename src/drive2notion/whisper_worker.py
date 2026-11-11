"""mlx-whisper 전사를 별도 프로세스에서 실행한다.

사용: python -m drive2notion.whisper_worker <wav> <out.json> <model> <language> [initial_prompt]
끝나면 프로세스가 종료되므로 모델 메모리(약 3~4GB)가 바로 반환된다.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    wav, out, model, language = sys.argv[1:5]
    prompt = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None

    import mlx_whisper

    result = mlx_whisper.transcribe(
        wav,
        path_or_hf_repo=model,
        language=language,
        initial_prompt=prompt,
        condition_on_previous_text=False,  # 같은 문장 반복 현상 방지
        word_timestamps=True,  # hallucination_silence_threshold에 필요
        hallucination_silence_threshold=2.0,  # 무음 구간의 지어낸 문장 억제
        verbose=None,
    )
    segments = [
        {"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()}
        for s in result.get("segments", [])
    ]
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"language": result.get("language"), "segments": segments}, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
