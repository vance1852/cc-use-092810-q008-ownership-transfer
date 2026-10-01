"""所有权流转服务向 API 和 CLI 暴露的稳定错误。"""


class TitleError(RuntimeError):
    code = "title_error"
    status = 400


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
