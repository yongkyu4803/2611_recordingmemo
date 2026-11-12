# drive2notion

Google Drive 폴더에 올린 M4A·MP3 녹음을 Mac에서 전사(로컬 Whisper)하고, Claude(구독 로그인)로 정리해 Notion 페이지로 만듭니다.

```
Drive 폴더 → 다운로드 → 무음 제거 → Whisper(로컬) → Claude 정리 → Notion 페이지 → 처리완료 폴더로 이동
```

- 오디오는 Mac 밖으로 나가지 않습니다. Anthropic에는 전사 텍스트만 갑니다.
- 추가 비용이 없습니다. Claude는 `claude` CLI에 로그인된 구독 계정으로 호출합니다.
- 기획 문서: `~/Desktop/solar/drive-audio-to-notion-prd-v2.md`

## 1. 설치

```bash
uv sync
```

Whisper 모델(`mlx-community/whisper-large-v3-mlx`, 약 3GB)은 첫 실행 때 자동으로 내려받습니다.

## 2. Drive 없이 먼저 시험하기

```bash
uv run drive2notion -v run-file ~/녹음/회의.m4a --no-notion
uv run drive2notion -v run-file ~/녹음/회의.mp3 --no-notion
```

Drive 자동 감시와 로컬 파일 처리 모두 `.m4a`, `.mp3`를 지원합니다. 확장자의 대소문자는 구분하지 않으며, Drive에서는 확장자가 없어도 M4A·MP3 전용 MIME 유형이면 감지합니다.

`data/notion_preview/`에 Notion에 올라갈 내용이 JSON으로 저장됩니다. `--no-notion`을 빼면 실제 Notion 페이지를 만듭니다(3번의 Notion 설정 필요).

## 3. 연결 설정

```bash
cp config.example.toml config.toml
cp .env.example .env && chmod 600 .env
```

### Google Drive (본인 계정 로그인)

처음 한 번만 설정합니다.

1. [Google Cloud 콘솔](https://console.cloud.google.com/)에서 프로젝트를 만들고 **API 및 서비스 → 라이브러리**에서 **Google Drive API**를 사용 설정합니다.
2. **Google 인증 플랫폼**(OAuth 동의 화면)을 설정합니다.
   - 앱 이름과 지원 이메일은 아무거나 넣어도 됩니다(예: `drive2notion`).
   - **대상**: 개인 Gmail 계정이면 **외부**를 고릅니다. 회사 Google Workspace 계정이면 **내부**를 고르세요. 내부를 고르면 아래 7일 문제와 경고 화면이 없습니다.
   - 외부를 골랐다면 **대상 → 앱 게시**를 눌러 상태를 **프로덕션**으로 바꿉니다. **테스트** 상태로 두면 로그인이 7일마다 만료됩니다.
3. **클라이언트 → 클라이언트 만들기**에서 유형을 **데스크톱 앱**으로 만듭니다. 클라이언트 상세 화면의 **클라이언트 ID**와 **클라이언트 보안 비밀번호**를 `.env`의 `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`에 붙여 넣습니다. 비밀번호가 가려져 있으면 **보안 비밀번호 추가**로 새로 만듭니다. (JSON 파일로 받았다면 `~/.config/drive2notion/google-oauth-client.json`에 두어도 됩니다.)
4. Drive에서 녹음을 올릴 폴더를 만들고, 폴더 주소 `https://drive.google.com/drive/folders/<폴더ID>`의 폴더 ID를 `.env`의 `DRIVE_FOLDER_ID`에 적습니다.
5. 로그인합니다. 브라우저가 열리면 본인 계정을 고르세요.
   ```bash
   uv run drive2notion auth
   ```
   "Google에서 확인하지 않은 앱" 화면이 나오면 **고급 → (앱 이름)(으)로 이동**을 누릅니다. 직접 만든 앱이라 나오는 경고입니다. 끝나면 로그인 계정과 감시 폴더의 M4A·MP3 개수가 출력됩니다.

토큰은 `~/.config/drive2notion/google-token.json`(권한 600)에 저장됩니다. 로그인이 만료되거나 취소되면 macOS 알림이 뜨니, 그때 `auth`를 다시 실행하면 됩니다.

처리가 끝난 파일은 감시 폴더 안의 `처리완료` 하위 폴더로 옮겨집니다. 이 하위 폴더는 자동으로 만들어집니다.

### Notion

1. [Notion 통합 관리](https://www.notion.so/profile/integrations)에서 내부 통합을 만들고 토큰을 `.env`의 `NOTION_TOKEN`에 적습니다.
2. 녹음 페이지가 쌓일 부모 페이지를 열고 **⋯ → 연결 → 방금 만든 통합**을 추가합니다.
3. 부모 페이지 주소 끝의 32자리 ID를 `.env`의 `NOTION_PARENT_PAGE_ID`에 적습니다.

### Claude

터미널에서 `claude`가 구독 계정으로 로그인돼 있으면 됩니다. `.env`나 셸 설정에 `ANTHROPIC_API_KEY`를 넣지 마세요. 넣으면 구독 대신 API 요금이 청구됩니다(프로그램이 실행 중에 이 값을 지우긴 합니다).

## 4. 한 번 돌려 보고 자동 실행 등록

```bash
uv run drive2notion -v poll
```

```bash
scripts/install_launchd.sh
```

3분마다 실행됩니다. 해제는 `scripts/install_launchd.sh uninstall`입니다. Mac이 잠들어 있는 동안에는 실행되지 않고, 깨어나면 밀린 파일을 처리합니다.

## 5. 명령어

| 명령 | 하는 일 |
|---|---|
| `drive2notion auth` | Google 로그인 (처음 한 번, 또는 만료 시) |
| `drive2notion poll` | Drive 폴링 1회 (launchd가 실행) |
| `drive2notion run-file <파일> [--no-notion] [--force]` | 로컬 파일 1개 처리 |
| `drive2notion status` | 파일별 상태와 마지막 오류 |
| `drive2notion reset <파일ID> [--from 단계]` | 실패·확인 필요 파일을 다시 시도하게 만듦 |

앞에 `uv run`을 붙여 실행합니다. `-v`를 붙이면 진행 로그가 화면에도 나옵니다. 로그 파일은 `data/logs/app.log`입니다.

## 6. 상태와 플래그

단계: `discovered → downloaded → transcribed → summarized → publishing → published → done`

| 플래그 | 뜻 | 다음 동작 |
|---|---|---|
| `failed` | 일시적 실패 | 다음 폴링에서 다시 시도, 5회 실패하면 `failed_permanent` |
| `waiting_quota` | Claude 구독 사용량 한도 | 다음 폴링에서 다시 시도 (횟수에 안 셈) |
| `failed_permanent` | 손상된 파일 등 | 알림, `reset`으로 수동 재시도 |
| `needs_review` | 전사가 비정상적으로 짧음 | 알림, 녹음 확인 후 `reset` |
| `duplicate` | 같은 내용(MD5) 파일이 이미 처리됨 | 처리완료 폴더로만 이동 |

중간에 멈추면 마지막으로 성공한 단계 다음부터 이어 갑니다. 전사와 정리 결과는 `data/transcripts/`, `data/summaries/`에 30일간 보관되므로 재시도할 때 다시 돌리지 않습니다.

## 7. 설정 (`config.toml`)

- `whisper.model`: 속도가 부족하면 `mlx-community/whisper-large-v3-turbo`
- `claude.model`: `opus`(기본) 또는 `sonnet`(사용량 절약)
- `glossary.terms`: 자주 나오는 사람·기관 이름. 전사 표기와 정리에 참고로 쓰입니다.

## 8. 테스트

```bash
uv run pytest -q
```
