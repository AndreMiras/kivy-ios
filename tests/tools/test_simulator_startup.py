"""Acceptance/failure policies for the real simulator harness, without Xcode."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("simulator_startup", ROOT / ".ci/test_simulator_startup.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def udid(number):
    return "00000000-0000-0000-0000-{:012d}".format(number)


def inventories():
    runtimes = {"runtimes": []}
    devices = {"devices": {}}
    for index, version in enumerate(("14.0", "17.9", "17.10", "18.5", "19.0")):
        identifier = "com.apple.CoreSimulator.SimRuntime.iOS-" + version.replace(".", "-")
        runtimes["runtimes"].append({"identifier": identifier, "version": version,
                                     "isAvailable": version != "19.0"})
        devices["devices"][identifier] = [{"name": "iPhone Z", "udid": udid(index),
                                          "isAvailable": True, "state": "Shutdown"}]
    destinations = "Available destinations for the startupsmoke scheme:\n" + "\n".join(
        "{ platform:iOS Simulator, arch:arm64, id:" + udid(index) + ", name:iPhone Z }"
        for index in range(5)
    )
    return devices, runtimes, destinations


def test_selector_prefers_sdk_then_numeric_fallback():
    devices, runtimes, destinations = inventories()
    runtimes["runtimes"][4]["isAvailable"] = True
    assert smoke.select_simulator(devices, runtimes, destinations, "18.5", "15.0")["udid"] == udid(3)
    assert smoke.select_simulator(devices, runtimes, destinations, "20.0", "15.0")["udid"] == udid(4)
    runtimes["runtimes"][4]["isAvailable"] = False
    assert smoke.select_simulator(devices, runtimes, destinations, "20.0", "15.0")["udid"] == udid(3)
    devices["devices"][runtimes["runtimes"][3]["identifier"]][0]["isAvailable"] = False
    assert smoke.select_simulator(devices, runtimes, destinations, "20.0", "15.0")["udid"] == udid(2)


def test_selector_ties_and_exclusions():
    devices, runtimes, destinations = inventories()
    group = devices["devices"][runtimes["runtimes"][3]["identifier"]]
    group.extend({"name": name, "udid": udid(index), "isAvailable": True}
                 for index, name in ((7, "iPhone A"), (6, "iPhone A"), (8, "iPad"), (9, "iPhone absent")))
    group[3].update(name="iPhone renamed iPad", deviceTypeIdentifier="com.apple.CoreSimulator.SimDeviceType.iPad-Pro")
    destinations += "\n" + "\n".join(
        "{ platform:iOS Simulator, id:" + udid(index) + ", name:device }" for index in (6, 7, 8))
    destinations += "\nUnavailable destinations for the startupsmoke scheme:\n"
    destinations += "{ platform:iOS Simulator, id:" + udid(9) + ", name:iPhone absent }"
    assert smoke.select_simulator(devices, runtimes, destinations, "18.5", "15.0")["udid"] == udid(6)
    # Unavailable runtime/device, old deployment target, iPad and unavailable
    # scheme section cannot satisfy a missing compatible candidate.
    for device in group:
        if device["udid"] in (udid(3), udid(6), udid(7)):
            device["isAvailable"] = False
    with pytest.raises(smoke.SmokeError, match="No compatible available iPhone"):
        smoke.select_simulator(devices, runtimes, destinations, "18.5", "18.0")
    with pytest.raises(smoke.SmokeError, match="No compatible"):
        smoke.select_simulator(devices, runtimes, "Available destinations for scheme:\n"
                               "{ platform:iOS Simulator, id:dvtdevice-placeholder, name:Any iOS Simulator Device }",
                               "18.5", "15.0")


@pytest.fixture
def valid_result(tmp_path):
    app = tmp_path / "startupsmoke.app"
    app.mkdir()
    python = smoke.recipe_version(ROOT / "kivy_ios/recipes/python3/__init__.py", "Python3Recipe")
    kivy = smoke.recipe_version(ROOT / "kivy_ios/recipes/kivy/__init__.py", "KivyRecipe")
    major, minor = smoke.version_tuple(python)[:2]
    library = app / "lib" / "python{}.{}".format(major, minor)
    request = {"schema_version": 1, "nonce": "fresh", "case": "ordinary", "expected_args": []}
    result = {
        "schema_version": 1, "nonce": "fresh", "case": "ordinary", "status": "passed",
        "stage": "kivy-event-loop", "argv": [str(app / "startupsmoke")],
        "python_version": list(smoke.version_tuple(python)), "kivy_version": kivy,
        "paths": {"prefix": str(app), "sys_path": [str(library), str(library / "site-packages")],
                  "cwd": str(app / "YourApp"), "main_file": str(app / "YourApp/main.pyc")},
        "environment": dict(smoke.FIXED_ENVIRONMENT, PYTHONHOME=str(app),
                            PYTHONPATH="{}:{}:{}:.".format(app, library, library / "site-packages"),
                            KIVY_ORIENTATION="Portrait LandscapeLeft"),
        "imports": {name: True for name in ("json", "zlib", "sqlite3", "ssl")},
        "window": {"provider": "WindowSDL", "size": [390, 844]},
    }
    arguments = (app, "startupsmoke", python, kivy, "Portrait LandscapeLeft")
    return result, request, arguments


@pytest.mark.parametrize("case", ["ordinary", "native-option"])
def test_correct_launches_validate(valid_result, case):
    result, request, arguments = valid_result
    request.update(case=case, expected_args=smoke.CASES[case])
    result.update(case=case, argv=result["argv"] + smoke.CASES[case])
    smoke.validate_result(result, request, *arguments)


def test_equivalent_private_and_symlink_paths_validate(valid_result, tmp_path):
    result, request, arguments = valid_result
    alias = tmp_path / "alias.app"
    alias.symlink_to(arguments[0], target_is_directory=True)
    result["argv"][0] = str(alias / "startupsmoke")
    smoke.validate_result(result, request, *arguments)
    # /private/var and /var are equivalent even on Linux without macOS aliases.
    assert smoke.normalized_path("/private/var/containers/app") == smoke.normalized_path("/var/containers/app")


@pytest.mark.parametrize("location,value,reason", [
    (("argv",), [""], "argv\\[0\\]"),
    (("argv",), [], "argv list"),
    (("argv",), ["/other/app"], "installed executable"),
    (("schema_version",), True, "schema_version"),
    (("nonce",), "old", "nonce"),
    (("case",), "native-option", "case"),
    (("stage",), "bootstrap", "kivy-event-loop"),
    (("status",), "failed", "status"),
    (("python_version",), [0, 0, 0], "Python version"),
    (("kivy_version",), "stale", "Kivy version"),
    (("paths", "prefix"), "/wrong", "sys.prefix"),
    (("paths", "cwd"), "/wrong", "cwd"),
    (("paths", "main_file"), "/wrong/main.py", "main_file"),
    (("paths", "sys_path"), [], "standard library"),
    (("environment", "PYTHONHOME"), "/wrong", "PYTHONHOME"),
    (("environment", "PYTHONPATH"), ".", "PYTHONPATH"),
    (("environment", "KIVY_ORIENTATION"), "LandscapeLeft Portrait", "KIVY_ORIENTATION"),
    (("environment", "KIVY_NO_CONSOLELOG"), "0", "KIVY_NO_CONSOLELOG"),
    (("environment", "KIVY_GL_BACKEND"), "mock", "KIVY_GL_BACKEND"),
    (("imports", "ssl"), 1, "ssl"),
    (("window", "provider"), "mock", "provider"),
    (("window", "size"), [0, 844], "dimensions"),
    (("window", "size"), [True, 844], "dimensions"),
    (("window", "size"), [float("nan"), 844], "dimensions"),
])
def test_inconsistent_results_are_rejected(valid_result, location, value, reason):
    result, request, arguments = valid_result
    target = result
    for key in location[:-1]:
        target = target[key]
    target[location[-1]] = value
    with pytest.raises(smoke.SmokeError, match=reason):
        smoke.validate_result(result, request, *arguments)


def test_consumed_native_options_and_missing_site_packages_fail(valid_result):
    result, request, arguments = valid_result
    request.update(case="native-option", expected_args=smoke.CASES["native-option"])
    result["case"] = "native-option"
    with pytest.raises(smoke.SmokeError, match="argv suffix"):
        smoke.validate_result(result, request, *arguments)
    result["argv"] += request["expected_args"]
    result["paths"]["sys_path"].pop()
    with pytest.raises(smoke.SmokeError, match="site-packages"):
        smoke.validate_result(result, request, *arguments)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


@pytest.mark.parametrize("kind", ["missing", "stale", "bootstrap"])
def test_incomplete_results_timeout_under_overall_budget(tmp_path, valid_result, kind):
    result, request, _ = valid_result
    filename = tmp_path / "result.json"
    if kind == "stale":
        result["nonce"] = "old"
    elif kind == "bootstrap":
        result.update(stage="bootstrap", status="running")
    if kind != "missing":
        smoke.atomic_json(filename, result)
    clock = Clock()
    with pytest.raises(smoke.SmokeError, match="timed out; last stage"):
        smoke.wait_for_result(filename, request, 1.2, 60, clock, clock.sleep)
    assert clock.now == pytest.approx(1.2)


@pytest.mark.parametrize("change,reason", [
    ({"status": "failed", "error": "controlled failure"}, "controlled failure"),
    ({"case": "wrong"}, "Wrong result case"),
    ({"schema_version": 2}, "schema_version"),
    ({"stage": "bootstrap"}, "kivy-event-loop"),
    ({"status": "unknown"}, "stage/status"),
])
def test_current_bad_result_fails_immediately(tmp_path, valid_result, change, reason):
    result, request, _ = valid_result
    result.update(change)
    filename = tmp_path / "result.json"
    smoke.atomic_json(filename, result)
    clock = Clock()
    with pytest.raises(smoke.SmokeError, match=reason):
        smoke.wait_for_result(filename, request, 60, 60, clock, clock.sleep)
    assert clock.now == 0


def test_malformed_json_fails_and_fresh_result_replaces_stale(tmp_path, valid_result):
    result, request, _ = valid_result
    filename = tmp_path / "result.json"
    filename.write_text("{bad json", encoding="utf-8")
    clock = Clock()
    with pytest.raises(smoke.SmokeError, match="Malformed result JSON"):
        smoke.wait_for_result(filename, request, 60, 60, clock, clock.sleep)
    stale = deepcopy(result)
    stale["nonce"] = "old"
    smoke.atomic_json(filename, stale)

    def publish(delay):
        clock.sleep(delay)
        smoke.atomic_json(filename, result)

    assert smoke.wait_for_result(filename, request, 60, 60, clock, publish) == result
    assert clock.now == 0.5


def test_observer_cannot_accept_result_after_deadline(tmp_path, valid_result):
    result, request, _ = valid_result
    filename = tmp_path / "result.json"
    smoke.atomic_json(filename, result)
    clock = Clock()
    with pytest.raises(smoke.SmokeError, match="after deadline"):
        smoke.wait_for_result(filename, request, 1, 60, clock, clock.sleep,
                              observe=lambda result: clock.sleep(2))


def test_subprocess_budget_and_group_kill_on_timeout(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(smoke.time, "monotonic", clock)
    # The pure helper's clock boundary also permits deterministic deadline tests.
    assert smoke.remaining_timeout(0.2, 600, clock) == 0.2
    with pytest.raises(smoke.SmokeError, match="deadline"):
        smoke.remaining_timeout(0, 30, clock)
    calls = []

    class Process:
        pid = 1234

        def wait(self, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("test", timeout)
            return 0

    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *args, **kwargs: Process())
    signals = []
    monkeypatch.setattr(smoke.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(smoke.time, "sleep", clock.sleep)
    # remaining_timeout's default is bound on import; inject its clock at boundary.
    original = smoke.remaining_timeout
    monkeypatch.setattr(smoke, "remaining_timeout", lambda deadline, maximum: original(deadline, maximum, clock))
    runner = smoke.Runner(tmp_path, 0.2)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(["test"], "test", maximum=600)
    assert calls[0] == 0.2
    assert signals == [(1234, smoke.signal.SIGTERM), (1234, smoke.signal.SIGKILL)]


def test_literal_recipe_versions_do_not_execute_source(tmp_path):
    source = tmp_path / "recipe.py"
    source.write_text('raise RuntimeError("must not execute")\nclass Recipe:\n    version = "3.11.6"\n')
    assert smoke.recipe_version(source, "Recipe") == "3.11.6"
    source.write_text('class Recipe:\n    version = get_version()\n')
    with pytest.raises(smoke.SmokeError, match="literal string version"):
        smoke.recipe_version(source, "Recipe")


def test_terminate_allows_only_expected_not_running_error():
    runner = SimpleNamespace(run=lambda *args, **kwargs: (3, "found nothing to terminate"))
    smoke.terminate_app(runner, udid(1), "bundle")
    runner.run = lambda *args, **kwargs: (1, "simulator unavailable")
    with pytest.raises(smoke.SmokeError, match="Unexpected simctl terminate"):
        smoke.terminate_app(runner, udid(1), "bundle")


def test_unsupported_host_fails_with_retained_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.platform, "system", lambda: "Linux")
    with pytest.raises(smoke.SmokeError, match="Apple Silicon"):
        smoke.execute(SimpleNamespace(artifacts=str(tmp_path), timeout=900, result_timeout=60))
    diagnostics = tmp_path / "diagnostics"
    assert "Apple Silicon" in (diagnostics / "error.txt").read_text()
    assert (diagnostics / "host.json").exists()
    assert (diagnostics / "cleanup.json").exists()
