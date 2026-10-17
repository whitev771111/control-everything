"""Shared structured input failures and explicit absolute-path validation."""

from pathlib import Path


class ProfileError(ValueError):
    def __init__(self, reason, field=None, *, status=422, message=None):
        super().__init__(reason)
        self.reason, self.field, self.status, self.message = reason, field, status, message


def absolute_path(value):
    return isinstance(value, str) and 1 <= len(value) <= 2048 and "\x00" not in value and Path(value).is_absolute()
