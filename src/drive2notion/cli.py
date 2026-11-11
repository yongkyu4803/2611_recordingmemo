"""명령줄 진입점.

  drive2notion auth                      Google 로그인 (처음 한 번)
  drive2notion poll                     Drive 폴링 1회 (launchd가 3분마다 실행)
  drive2notion run-file 녹음.m4a          로컬 파일 1개 처리 (Drive 없이 시험할 때)
  drive2notion run-file 녹음.m4a --no-notion   Notion에 올리지 않고 미리보기 JSON만 저장
  drive2notion status                    파일별 상태 보기
  drive2notion reset <파일ID> [--from 단계]    실패한 파일을 다시 시도하게 만들기
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, load_config
from .drive import md5_of
from .errors import AuthError
from .pipeline import Pipeline
from .state import STAGES, StateDB

log = logging.getLogger("drive2notion")


def setup_logging(cfg: Config, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(cfg.logs_dir / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO if verbose else logging.WARNING)
    root.addHandler(sh)
    # 외부 라이브러리 로그는 줄인다
    for noisy in ("httpx", "googleapiclient", "urllib3", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RunLock:
    """동시에 두 프로세스가 돌지 않게 한다. 이미 잡혀 있으면 False."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.fh = self.path.open("w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.fh.close()
            return False

    def release(self) -> None:
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def _make_notion(cfg: Config, no_notion: bool):  # noqa: ANN202
    if no_notion:
        return None
    from .notion import NotionWriter

    return NotionWriter(cfg.notion_token, cfg.notion_parent_page_id, cfg.notion_version)


def cmd_poll(cfg: Config, args: argparse.Namespace) -> int:
    from .drive import DriveClient

    lock = RunLock(cfg.data_dir / "run.lock")
    if not lock.acquire():
        log.info("이전 실행이 아직 진행 중이라 이번 폴링은 건너뜁니다.")
        return 0
    db = StateDB(cfg.db_path)
    try:
        try:
            drive = DriveClient(cfg.google_token_file)
            notion = _make_notion(cfg, args.no_notion)
        except AuthError as e:
            log.error("%s", e)
            from .pipeline import notify

            notify("녹음 정리: 설정 필요", str(e))
            return 2
        pipe = Pipeline(cfg, db, drive=drive, notion=notion, dry_run_notion=args.no_notion)
        pipe.cleanup()
        try:
            added = pipe.discover()
            log.info("새 파일 %d개 등록", added)
        except Exception as e:  # noqa: BLE001 - 조회 실패해도 밀린 파일은 처리한다
            log.error("Drive 조회 실패, 이번에는 기존 대기 파일만 처리: %s", e)
        pending = db.pending()
        if args.limit:
            pending = pending[: args.limit]
        for rec in pending:
            rec = pipe.run(rec)
            if args.verbose:
                print(f"{rec.name}: 단계={rec.stage} 플래그={rec.flag or '-'} {rec.last_error or ''}")
        return 0
    finally:
        db.close()
        lock.release()


def cmd_auth(cfg: Config, args: argparse.Namespace) -> int:
    """브라우저로 Google 로그인 후 토큰 저장, 감시 폴더 접근까지 확인한다."""
    from .drive import DriveClient, login

    try:
        token = login(
            cfg.google_oauth_client_file,
            cfg.google_token_file,
            cfg.google_client_id,
            cfg.google_client_secret,
            cfg.google_oauth_port,
        )
        print(f"로그인 토큰을 저장했습니다: {token}")
        drive = DriveClient(cfg.google_token_file)
        about = drive.service.about().get(fields="user(emailAddress)").execute()
        print(f"로그인 계정: {about['user']['emailAddress']}")
        if cfg.drive_folder_id:
            folder = drive._execute(
                drive.service.files().get(fileId=cfg.drive_folder_id, fields="name", supportsAllDrives=True)
            )
            files = drive.list_new_m4a(cfg.drive_folder_id, 0)
            print(f"감시 폴더 확인: '{folder['name']}' (m4a {len(files)}개)")
        else:
            print("DRIVE_FOLDER_ID가 비어 있습니다. .env에 감시할 폴더 ID를 넣으세요.")
        return 0
    except AuthError as e:
        print(str(e), file=sys.stderr)
        return 2


def cmd_run_file(cfg: Config, args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        print(f"파일이 없습니다: {path}", file=sys.stderr)
        return 1
    db = StateDB(cfg.db_path)
    try:
        md5 = md5_of(path)
        file_id = f"local-{md5[:16]}"
        if args.force and db.get(file_id):
            db.update(file_id, stage="discovered", flag=None, attempts=0, last_error=None,
                      page_id=None, page_url=None, toggle_block_id=None, blocks_appended=0)
        rec = db.add(file_id, path.name, source="local", md5=md5, size=path.stat().st_size, local_path=str(path))
        if rec.stage == "done":
            print(f"이미 처리한 파일입니다: {rec.page_url}  (다시 하려면 --force)")
            return 0
        if rec.flag:
            # 직접 다시 실행한 것이므로 이전 실패 표시를 지우고 멈춘 단계부터 이어 간다
            rec = db.update(file_id, flag=None)
        try:
            notion = _make_notion(cfg, args.no_notion)
        except AuthError as e:
            print(f"{e}\nNotion 없이 시험하려면 --no-notion을 붙이세요.", file=sys.stderr)
            return 2
        pipe = Pipeline(cfg, db, notion=notion, dry_run_notion=args.no_notion)
        rec = pipe.run(rec)
        print(f"상태: {rec.stage}  플래그: {rec.flag or '-'}")
        if rec.last_error:
            print(f"오류: {rec.last_error}")
        if rec.page_url:
            print(f"결과: {rec.page_url}")
        return 0 if rec.stage == "done" else 1
    finally:
        db.close()


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    db = StateDB(cfg.db_path)
    try:
        rows = db.all()
        if not rows:
            print("기록된 파일이 없습니다.")
            return 0
        for r in rows[: args.limit]:
            dur = f"{int((r.duration_sec or 0) // 60)}분" if r.duration_sec else "-"
            print(f"{r.stage:<12} {r.flag or '':<16} {dur:>5}  {r.name}  [{r.file_id}]")
            if r.last_error:
                print(f"{'':<12} └ {r.last_error[:160]}")
        return 0
    finally:
        db.close()


def cmd_reset(cfg: Config, args: argparse.Namespace) -> int:
    db = StateDB(cfg.db_path)
    try:
        rec = db.get(args.file_id)
        if not rec:
            print("해당 파일 ID가 없습니다.", file=sys.stderr)
            return 1
        fields = {"flag": None, "attempts": 0, "last_error": None}
        if args.from_stage:
            fields["stage"] = args.from_stage
        db.update(args.file_id, **fields)
        print(f"초기화했습니다: {rec.name} → 단계 {args.from_stage or rec.stage}")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drive2notion", description="Drive 녹음 → Notion 정리")
    parser.add_argument("-v", "--verbose", action="store_true", help="진행 로그를 화면에도 출력")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="Google 계정 로그인 (처음 한 번, 또는 로그인 만료 시)")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("poll", help="Drive 폴링 1회")
    p.add_argument("--no-notion", action="store_true", help="Notion에 올리지 않고 미리보기만 저장 (Drive 파일은 옮기지 않음)")
    p.add_argument("--limit", type=int, default=0, help="이번 실행에서 처리할 최대 파일 수 (0=전부)")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("run-file", help="로컬 m4a 파일 1개 처리")
    p.add_argument("path")
    p.add_argument("--no-notion", action="store_true", help="Notion에 올리지 않고 미리보기만 저장")
    p.add_argument("--force", action="store_true", help="이미 처리한 파일도 처음부터 다시")
    p.set_defaults(func=cmd_run_file)

    p = sub.add_parser("status", help="파일별 상태")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset", help="실패한 파일 다시 시도")
    p.add_argument("file_id")
    p.add_argument("--from", dest="from_stage", choices=STAGES, help="이 단계부터 다시")
    p.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg, args.verbose)
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
