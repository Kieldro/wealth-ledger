"""pytest, dashboard tests: when a browser test (test_ui.py) fails, keep what the browser showed.

The test's page fixture leaves a `ui_dump` function on the test item; it is called here while the pages are still
open, and where it saved the screenshots and logs is added to the failure report."""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    dump = getattr(item, "ui_dump", None)
    if dump and report.failed and report.when in ("setup", "call"):
        report.sections.append(("browser artifacts", dump()))
