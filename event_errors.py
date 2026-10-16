"""Fixed, non-sensitive error codes for MCP event operations."""


class EventError(ValueError):
    def __init__(self, code, *, http_status=None):
        self.code = code
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        super().__init__(code)

