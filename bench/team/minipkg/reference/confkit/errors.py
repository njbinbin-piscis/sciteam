"""Exception hierarchy of confkit."""


class ConfkitError(Exception):
    """Base class of all confkit errors."""


class ParseError(ConfkitError):
    def __init__(self, detail: str, line: int, source: str):
        self.detail = detail
        self.line = line
        self.source = source
        super().__init__(f"{source}:{line}: {detail}")


class InterpolationError(ConfkitError):
    pass


class MergeError(ConfkitError):
    pass


class ValidationError(ConfkitError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))
