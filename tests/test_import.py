import subprocess
import sys

import pytest
from sqlalchemy.engine import make_url

from sqlalchemy_openobserve.dialect import OpenObserveDialect

URLS = [
    "openobserve://user:pass@localhost:5080/default",
    "openobserve+rest://user:pass@localhost:5080/default",
]


@pytest.mark.parametrize("url", URLS)
def test_url_resolves_to_dialect(url: str) -> None:
    assert make_url(url).get_dialect() is OpenObserveDialect


@pytest.mark.parametrize("url", URLS)
def test_url_resolves_without_importing_the_package(url: str) -> None:
    # A fresh interpreter that never imports sqlalchemy_openobserve, so only
    # the entry point can resolve the URL
    code = (
        "import sys\n"
        "from sqlalchemy.engine import make_url\n"
        "assert 'sqlalchemy_openobserve' not in sys.modules\n"
        f"print(make_url({url!r}).get_dialect().__name__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "OpenObserveDialect"
