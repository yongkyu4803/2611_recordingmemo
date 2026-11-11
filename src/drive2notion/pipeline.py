"""단계 실행기. 파일마다 마지막으로 성공한 단계 다음부터 이어 간다."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from .audio import probe, to_wav
from .config import Config
from .errors import AuthError, NeedsReviewError, PermanentError, PipelineError, QuotaError
from .notion import (
    NotionWriter,
    PageMeta,
    _block,
    batches,
    header_blocks,
    now_local,
    page_title,
    transcript_paragraphs,
)
from .state import (
    FLAG_DUPLICATE,
    FLAG_FAILED,
    FLAG_NEEDS_REVIEW,
    FLAG_PERMANENT,
    FLAG_WAITING_QUOTA,
    FileRecord,
    StateDB,
)
from .summarize import Summary, summarize
from .transcribe import Transcript, transcribe

log = logging.getLogger(__name__)


def notify(title: str, message: str) -> None:
    """macOS 알림. 실패해도 무시한다."""
    safe = message.replace('"', "'")[:200]
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
            capture_output=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        db: StateDB,
        *,
        drive=None,  # noqa: ANN001 - DriveClient 또는 None
        notion: NotionWriter | None = None,
        dry_run_notion: bool = False,
    ):
        self.cfg = cfg
        self.db = db
        self.drive = drive
        self.notion = notion
        self.dry_run_notion = dry_run_notion
        self.quota_blocked = False
        self._done_folder_id: str | None = None
        self._stop = False

    # ---------- 경로 ----------
    def _tmp_audio(self, rec: FileRecord) -> Path:
        return self.cfg.tmp_dir / f"{rec.file_id}.m4a"

    def _source_audio(self, rec: FileRecord) -> Path:
        return Path(rec.local_path) if rec.source == "local" else self._tmp_audio(rec)

    def _transcript_path(self, rec: FileRecord) -> Path:
        return self.cfg.transcripts_dir / f"{rec.file_id}.json"

    def _summary_path(self, rec: FileRecord) -> Path:
        return self.cfg.summaries_dir / f"{rec.file_id}.json"

    # ---------- 실행 ----------
    def run(self, rec: FileRecord) -> FileRecord:
        """한 파일을 가능한 데까지 진행시키고, 실패하면 종류에 맞는 플래그를 남긴다."""
        log.info("처리 시작: %s (%s, 단계=%s)", rec.name, rec.file_id, rec.stage)
        try:
            rec = self._advance_all(rec)
        except QuotaError as e:
            self.quota_blocked = True
            log.warning("구독 사용량 한도: %s", e)
            rec = self.db.update(rec.file_id, flag=FLAG_WAITING_QUOTA, last_error=str(e))
        except AuthError as e:
            log.error("인증 문제: %s", e)
            notify("녹음 정리: 로그인 필요", str(e))
            rec = self.db.update(rec.file_id, flag=FLAG_FAILED, last_error=str(e))
        except NeedsReviewError as e:
            log.warning("확인 필요: %s — %s", rec.name, e)
            notify("녹음 정리: 확인 필요", f"{rec.name}: {e}")
            rec = self.db.update(rec.file_id, flag=FLAG_NEEDS_REVIEW, last_error=str(e))
        except PermanentError as e:
            log.error("영구 실패: %s — %s", rec.name, e)
            notify("녹음 정리: 처리 불가", f"{rec.name}: {e}")
            rec = self.db.update(rec.file_id, flag=FLAG_PERMANENT, last_error=str(e))
        except Exception as e:  # noqa: BLE001 - PipelineError와 예상 못 한 오류 모두
            if not isinstance(e, PipelineError):
                log.exception("예상하지 못한 오류: %s", rec.name)
            attempts = rec.attempts + 1
            flag = FLAG_PERMANENT if attempts >= self.cfg.max_attempts else FLAG_FAILED
            log.warning("실패 (%d/%d): %s — %s", attempts, self.cfg.max_attempts, rec.name, e)
            if flag == FLAG_PERMANENT:
                notify("녹음 정리: 반복 실패", f"{rec.name}: {e}")
            rec = self.db.update(rec.file_id, flag=flag, attempts=attempts, last_error=f"{type(e).__name__}: {e}")
        return rec

    def _advance_all(self, rec: FileRecord) -> FileRecord:
        steps = {
            "discovered": self._download,
            "downloaded": self._transcribe,
            "transcribed": self._summarize,
            "summarized": self._publish,
            "publishing": self._publish,
            "published": self._finish,
        }
        self._stop = False
        while rec.stage != "done" and not self._stop:
            started = time.monotonic()
            stage = rec.stage
            rec = steps[stage](rec)
            log.info("  %s → %s (%.1f초)", stage, rec.stage, time.monotonic() - started)
        return rec

    # ---------- 단계 ----------
    def _download(self, rec: FileRecord) -> FileRecord:
        if rec.source == "drive":
            if self.drive is None:
                raise AuthError("Drive 클라이언트가 설정되지 않았습니다.")
            self.drive.download(rec.file_id, self._tmp_audio(rec), rec.md5)
        src = self._source_audio(rec)
        if not src.exists():
            raise PermanentError(f"오디오 파일이 없습니다: {src}")
        info = probe(src)
        recorded_at = info.recorded_at or rec.created_time
        return self.db.advance(rec.file_id, "downloaded", duration_sec=info.duration_sec, recorded_at=recorded_at)

    def _transcribe(self, rec: FileRecord) -> FileRecord:
        src = self._source_audio(rec)
        if not src.exists():
            if rec.source == "drive":
                # 다운로드 후 전사 전에 멈췄고 임시 파일이 지워진 경우: 다시 받는다
                return self.db.advance(rec.file_id, "discovered")
            raise PermanentError(f"오디오 파일이 없습니다: {src}")
        wav = self.cfg.tmp_dir / f"{rec.file_id}.wav"
        try:
            to_wav(src, wav)
            t = transcribe(
                wav,
                self._transcript_path(rec),
                model=self.cfg.whisper_model,
                language=self.cfg.whisper_language,
                glossary=self.cfg.glossary,
                duration_sec=rec.duration_sec or 0,
                timeout_factor=self.cfg.whisper_timeout_factor,
                timeout_min_sec=self.cfg.whisper_timeout_min_sec,
            )
        finally:
            wav.unlink(missing_ok=True)
        if rec.source == "drive":
            self._tmp_audio(rec).unlink(missing_ok=True)
        return self.db.advance(rec.file_id, "transcribed", transcribe_sec=t.elapsed_sec)

    def _load_transcript(self, rec: FileRecord) -> Transcript:
        path = self._transcript_path(rec)
        if not path.exists():
            raise PipelineError("저장된 전사 파일이 없습니다. 전사를 다시 해야 합니다.")
        return Transcript.from_json(json.loads(path.read_text(encoding="utf-8")))

    def _summarize(self, rec: FileRecord) -> FileRecord:
        if self.quota_blocked:
            raise QuotaError("이번 실행에서 이미 사용량 한도에 걸려 정리를 건너뜁니다.")
        t = self._load_transcript(rec)
        result = summarize(
            t.text,
            file_name=rec.name,
            recorded_at=rec.recorded_at,
            duration_sec=rec.duration_sec or 0,
            glossary=self.cfg.glossary,
            model=self.cfg.claude_model,
            max_turns=self.cfg.claude_max_turns,
            workdir=self.cfg.claude_workdir,
        )
        payload = {
            "summary": result.summary.model_dump(),
            "notional_cost_usd": result.notional_cost_usd,
            "duration_ms": result.duration_ms,
            "model": self.cfg.claude_model,
        }
        self._summary_path(rec).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("정리 완료: '%s' (참고 비용 %s달러, 청구 안 됨)", result.summary.title, result.notional_cost_usd)
        return self.db.advance(rec.file_id, "summarized")

    def _load_summary(self, rec: FileRecord) -> Summary:
        path = self._summary_path(rec)
        if not path.exists():
            raise PipelineError("저장된 정리 파일이 없습니다.")
        return Summary.model_validate(json.loads(path.read_text(encoding="utf-8"))["summary"])

    def _page_meta(self, rec: FileRecord) -> PageMeta:
        return PageMeta(
            file_name=rec.name,
            recorded_at=rec.recorded_at,
            duration_sec=rec.duration_sec or 0,
            drive_link=rec.web_link,
            file_id=rec.file_id,
            whisper_model=self.cfg.whisper_model,
            processed_at=now_local(),
        )

    def _publish(self, rec: FileRecord) -> FileRecord:
        summary = self._load_summary(rec)
        transcript = self._load_transcript(rec)
        meta = self._page_meta(rec)
        title = page_title(summary, meta)
        head = header_blocks(summary, meta)
        paragraphs = [_block("paragraph", p) for p in transcript_paragraphs(transcript.segments)]

        if self.dry_run_notion or self.notion is None:
            preview = self.cfg.preview_dir / f"{rec.file_id}.json"
            preview.write_text(
                json.dumps({"title": title, "header_blocks": head, "transcript_blocks": paragraphs}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log.info("Notion 미리보기 저장(실제 업로드 안 함): %s", preview)
            if rec.source == "drive":
                # Drive 파일은 옮기지 않고 summarized에 둔다. Notion 설정 후 다음 실행에서 실제로 올린다.
                self._stop = True
                return rec
            return self.db.advance(rec.file_id, "published", page_url=f"file://{preview}")

        if not rec.page_id:
            page_id, url, toggle_id = self.notion.create_page(title, head)
            # 만든 즉시 기록해 두어야 재시도 때 페이지를 또 만들지 않는다
            rec = self.db.advance(
                rec.file_id, "publishing", page_id=page_id, page_url=url, toggle_block_id=toggle_id, blocks_appended=0
            )
            log.info("Notion 페이지 생성: %s", url)
        toggle_id = rec.toggle_block_id or self.notion.find_toggle(rec.page_id)

        done = rec.blocks_appended
        for batch in batches(paragraphs[done:]):
            self.notion.append(toggle_id, batch)
            done += len(batch)
            rec = self.db.update(rec.file_id, blocks_appended=done)
        return self.db.advance(rec.file_id, "published")

    def _finish(self, rec: FileRecord) -> FileRecord:
        if rec.source == "drive" and self.drive is not None:
            folder = self.cfg.drive_folder_id
            if self._done_folder_id is None:
                self._done_folder_id = self.drive.ensure_subfolder(folder, self.cfg.done_folder_name)
            self.drive.move(rec.file_id, folder, self._done_folder_id)
        log.info("완료: %s → %s", rec.name, rec.page_url)
        return self.db.advance(rec.file_id, "done")

    # ---------- 폴링 ----------
    def discover(self) -> int:
        """Drive에서 새 파일을 찾아 상태 DB에 등록한다. 등록한 수를 돌려준다."""
        if self.drive is None or not self.cfg.drive_folder_id:
            raise AuthError("Drive 설정이 없습니다 (.env의 DRIVE_FOLDER_ID 확인, `drive2notion auth`로 로그인).")
        added = 0
        for f in self.drive.list_new_m4a(self.cfg.drive_folder_id, self.cfg.min_age_minutes):
            if self.db.get(f.id):
                continue
            rec = self.db.add(
                f.id,
                f.name,
                md5=f.md5,
                size=f.size,
                mime_type=f.mime_type,
                created_time=f.created_time,
                web_link=f.web_link,
            )
            added += 1
            dup = self.db.find_done_by_md5(f.md5, f.id) if f.md5 else None
            if dup:
                log.info("중복 업로드로 건너뜀: %s (기존 페이지 %s)", f.name, dup.page_url)
                # published로 두면, 이동이 실패해도 다음 폴링에서 이동만 다시 시도한다
                self.db.update(rec.file_id, stage="published", flag=None, page_url=dup.page_url)
                try:
                    self._finish(self.db.get(rec.file_id))  # 처리완료 폴더로 이동
                    self.db.update(rec.file_id, flag=FLAG_DUPLICATE, last_error=f"중복: {dup.file_id}")
                except Exception as e:  # noqa: BLE001
                    log.warning("중복 파일 이동 실패, 다음 폴링에서 다시 시도: %s", e)
        return added

    def cleanup(self) -> None:
        """끝난 지 keep_days가 지난 전사·정리 파일을 지운다."""
        cutoff = time.time() - self.cfg.keep_days * 86400
        for d in (self.cfg.transcripts_dir, self.cfg.summaries_dir, self.cfg.preview_dir):
            for p in d.glob("*.json"):
                rec = self.db.get(p.stem)
                if rec and rec.stage == "done" and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        # 오래된 임시 파일
        for p in self.cfg.tmp_dir.glob("*"):
            if p.stat().st_mtime < time.time() - 86400:
                p.unlink(missing_ok=True)
