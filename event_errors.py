"""Fixed, non-sensitive error codes for MCP event operations."""


class EventError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

