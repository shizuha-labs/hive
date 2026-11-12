"""Pure condition classifiers for HIVE-2804. No Django import."""

FETCH_ERROR_CODES = frozenset({
    'fetch_error',
    'timeout',
    'request_failed',
    'quota_refresh_failed',
    'provider_error',
})


def is_fetch_error_detail(detail: str) -> bool:
    """True for projected fetch-failure codes.

    The bounded snapshot has no attempt counter, so this matches the code the
    projection actually stores. It does not invent an N-attempt field.
    """
    code = str(detail or '').strip().lower()
    if code in FETCH_ERROR_CODES:
        return True
    return code.startswith('http_5')
