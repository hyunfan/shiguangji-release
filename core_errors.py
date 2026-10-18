"""Shared failure types and stable source-result classification."""


class Cancelled(Exception):
    pass


class ArchiveError(Exception):
    def __init__(self, message, *, reason='', status=0):
        super().__init__(message)
        self.reason = reason
        self.status = status


def failure_kind(error):
    """Use typed metadata/cause chains; never infer HTTP codes from prose."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, 'reason', '') in {'invalid_media', 'too_large', 'unsupported'}:
            return error.reason
        code = getattr(error, 'status', 0) or getattr(error, 'code', 0)
        if code in {404, 410}:
            return 'not_found'
        if code in {401, 403, 451}:
            return 'access_denied'
        error = error.__cause__
    return 'network'
