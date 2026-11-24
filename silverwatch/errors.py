class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details

    def payload(self):
        value = {"code": self.code, "message": self.message}
        if self.details is not None:
            value["details"] = self.details
        return value


def missing(kind: str, key: str) -> Problem:
    return Problem(404, f"{kind}_missing", f"找不到{kind}: {key}")


def conflict(code: str, message: str, details=None) -> Problem:
    return Problem(409, code, message, details)


def invalid(code: str, message: str, details=None) -> Problem:
    return Problem(422, code, message, details)
