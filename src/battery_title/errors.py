"""权属流转服务向 API 和 CLI 暴露的稳定错误。"""


class TitleError(RuntimeError):
    code = "title_error"
    status = 400

    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class NotFound(TitleError):
    code = "not_found"
    status = 404


class Conflict(TitleError):
    code = "conflict"
    status = 409


class Forbidden(TitleError):
    code = "forbidden"
    status = 403


class InvalidState(TitleError):
    code = "invalid_state"
    status = 409


class ValidationFailed(TitleError):
    code = "validation_failed"
    status = 422


class SettlementBlocked(Conflict):
    """交割条件未满足：已完成的同意保留，权属不发生任何变化。"""

    code = "settlement_blocked"
