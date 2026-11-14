"""Explicit acceptance mode: an unavailable prerequisite is not a passing suite."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--fail-on-skip", action="store_true", help="Fail required checks on any skip")


def pytest_configure(config):
    if config.getoption("--fail-on-skip"):
        config.pluginmanager.register(_RequiredChecks(), "required-checks-recorder")


class _RequiredChecks:
    def __init__(self):
        self.skipped = 0

    def pytest_runtest_logreport(self, report):
        self.skipped += bool(report.skipped)

    def pytest_collectreport(self, report):
        self.skipped += bool(report.skipped)

    def pytest_sessionfinish(self, session, exitstatus):
        if self.skipped:
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_sep("=", "Required acceptance suite contained skipped checks")
            if exitstatus == 0:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
