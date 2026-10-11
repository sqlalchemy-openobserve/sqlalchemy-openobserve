"""Work out the time range of a query"""


def extract_time_range(sql: str) -> tuple[int, int]:
    """
    Return the (start, end) time range to send with a query, in microseconds
    since the epoch.

    OpenObserve needs these outside the SQL on every search.
    """
    raise NotImplementedError
