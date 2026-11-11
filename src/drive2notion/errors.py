"""단계별 실패를 상태 DB의 어떤 플래그로 남길지 구분하는 예외들."""


class PipelineError(Exception):
    """재시도하면 나아질 수 있는 일반 실패. 시도 횟수를 센다."""


class PermanentError(PipelineError):
    """다시 해도 같은 결과가 나오는 실패 (예: 손상된 파일)."""


class NeedsReviewError(PipelineError):
    """사람이 확인해야 하는 결과 (예: 전사가 비정상적으로 짧음)."""


class QuotaError(PipelineError):
    """Claude 구독 사용량 한도. 시도 횟수에 넣지 않고 다음 폴링에서 다시 시도."""


class AuthError(PipelineError):
    """로그인·토큰 문제. 사람이 고쳐야 하므로 알림을 보내고 시도 횟수에는 넣지 않는다."""
