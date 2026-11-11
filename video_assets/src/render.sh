#!/bin/bash
# HTML 소스를 PNG로 렌더링하고, 구조도 단계 애니메이션 MP4를 만든다.
#   video_assets/src/render.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="$SRC/../out"
FRAMES="$OUT/frames"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p "$OUT" "$FRAMES"

shot() { # shot <html파일> <쿼리> <가로> <세로> <출력>
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --virtual-time-budget=4000 --window-size="$3,$4" --screenshot="$5" \
    "file://$SRC/$1?$2" >/dev/null 2>&1
  echo "  $(basename "$5")"
}

echo "구조도 PNG"
shot diagram.html "" 1920 1080 "$OUT/diagram_16x9.png"
shot diagram.html "ratio=v" 1080 1920 "$OUT/diagram_9x16.png"

echo "수치 카드 PNG"
for c in 1 2 3 4; do
  shot cards.html "card=$c" 1920 1080 "$OUT/card${c}_16x9.png"
  shot cards.html "card=$c&ratio=v" 1080 1920 "$OUT/card${c}_9x16.png"
done

echo "비교·한계 카드 PNG"
for c in 1 2; do
  shot compare.html "card=$c" 1920 1080 "$OUT/compare${c}_16x9.png"
  shot compare.html "card=$c&ratio=v" 1080 1920 "$OUT/compare${c}_9x16.png"
done
shot limits.html "" 1920 1080 "$OUT/limits_16x9.png"
shot limits.html "ratio=v" 1080 1920 "$OUT/limits_9x16.png"

echo "구조도 단계 프레임"
for ratio in h v; do
  if [[ $ratio == h ]]; then W=1920; H=1080; else W=1080; H=1920; fi
  for s in 0 1 2 3 4 5 6 7; do
    shot diagram.html "step=$s&ratio=$ratio" $W $H "$FRAMES/${ratio}_$s.png"
  done
  cp "$OUT/diagram_$([[ $ratio == h ]] && echo 16x9 || echo 9x16).png" "$FRAMES/${ratio}_8.png"
done

echo "구조도 MP4 (단계마다 1.2초, 0.3초 전환, 마지막 3초 유지)"
for ratio in h v; do
  name=$([[ $ratio == h ]] && echo 16x9 || echo 9x16)
  inputs=(); for s in 0 1 2 3 4 5 6 7 8; do
    dur=1.5; [[ $s == 8 ]] && dur=3.5
    inputs+=(-loop 1 -t "$dur" -i "$FRAMES/${ratio}_$s.png")
  done
  # 각 장면 1.5초 중 0.3초 겹쳐 전환 → 장면당 1.2초
  filter=""; prev="[0:v]"; offset=1.2
  for s in 1 2 3 4 5 6 7 8; do
    filter+="${prev}[$s:v]xfade=transition=fade:duration=0.3:offset=${offset}[x$s];"
    prev="[x$s]"; offset=$(echo "$offset + 1.2" | bc)
  done
  ffmpeg -v error -y "${inputs[@]}" -filter_complex "${filter%;}" -map "[x8]" \
    -r 30 -pix_fmt yuv420p -c:v libx264 -crf 18 "$OUT/diagram_steps_$name.mp4"
  echo "  diagram_steps_$name.mp4"
done
