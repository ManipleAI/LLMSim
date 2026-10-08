"""LLMSim for pytest.

    # conftest.py
    pytest_plugins = ["testkit.pytest_plugin"]

    # test_refunds.py
    def test_over_limit_refund_is_handed_on(llmsim):
        llmsim.assert_passes("refund-over-limit")                 # a saved scenario
        llmsim.assert_passes(my_scenario_dict, tier="simulated")  # or one written in the test

    # a file named  anything.scenario.json  is collected as a test of its own

The server is LLMSIM_URL (default http://localhost:8099). A failing scenario fails the test with the
checks that did not hold, in the words LLMSim uses. Repeat a case with `repeat=(n, required)`.
"""

import json
import os

from .client import Client, LLMSimError, failure_text

try:                                           # the plugin needs pytest; the rest of the kit does not
    import pytest
except ImportError:                            # pragma: no cover
    pytest = None


class Session:
    def __init__(self, server=None):
        self.client = Client(server)

    def run(self, scenario, **options):
        return self.client.run(scenario, **options)

    def assert_passes(self, scenario, repeat=None, **options):
        """Run a scenario and fail with the reason if it did not pass. repeat=(n, required) runs it n times."""
        n, required = repeat or (1, 1)
        if not 1 <= required <= n:
            raise ValueError("repeat=(n, required): required must be from 1 to n")
        results = [self.run(scenario, **options) for _ in range(n)]
        passed = sum(1 for r in results if r["verdict"] == "pass")
        if passed < required:
            worst = next(r for r in results if r["verdict"] != "pass")
            raise AssertionError("%d of %d run(s) passed, %d required\n%s" % (passed, n, required, failure_text(worst)))
        return results[0] if n == 1 else results


if pytest is not None:
    @pytest.fixture
    def llmsim():
        return Session()

    def pytest_collect_file(parent, file_path):
        if file_path.name.endswith(".scenario.json"):
            return ScenarioFile.from_parent(parent, path=file_path)

    class ScenarioFile(pytest.File):
        def collect(self):
            yield ScenarioItem.from_parent(self, name=self.path.stem.replace(".scenario", ""))

    class ScenarioItem(pytest.Item):
        def runtest(self):
            with open(self.path) as fh:
                scenario = json.load(fh)
            try:
                Session().assert_passes(scenario)
            except LLMSimError as exc:
                raise AssertionError("LLMSim could not run it: %s" % exc)

        def repr_failure(self, excinfo):
            return str(excinfo.value)

        def reportinfo(self):
            return self.path, 0, "scenario %s" % self.name
