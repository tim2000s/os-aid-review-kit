"""With KIT_REQUIRE_TOOLS set (as CI sets it), a test skipped for a missing tool fails instead, so a
job cannot pass by skipping what it was meant to test."""

import os

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("KIT_REQUIRE_TOOLS") and report.skipped and "not installed" in str(report.longrepr):
        report.outcome = "failed"
