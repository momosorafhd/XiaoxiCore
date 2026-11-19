"""Small, shared errors for the platform control API."""


class PlatformError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self):
        return {"code": self.code, "message": self.message}


def unavailable(message="模块当前不可用"):
    return PlatformError("service_unavailable", message, 503)
