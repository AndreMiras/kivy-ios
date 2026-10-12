#!/usr/bin/env python3
"""Bounded Release startup gate. Importable policy helpers also run on Linux."""
import argparse
import ast
import json
import math
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
CASES = {"ordinary": [], "native-option": ["-NSDocumentRevisionsDebugMode", "YES"]}
FIXED_ENVIRONMENT = {
    "KIVY_BUILD": "ios", "KIVY_WINDOW": "sdl2", "KIVY_IMAGE": "imageio,tex,gif,sdl2",
    "KIVY_AUDIO": "sdl2", "KIVY_GL_BACKEND": "sdl2", "KIVY_NO_CONSOLELOG": "1",
    "KIVY_NO_ARGS": "1", "PYTHONOPTIMIZE": "2",
}


class SmokeError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise SmokeError(reason)


def version_tuple(value):
    require(isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)*", value),
            "Unsupported numeric version: {!r}".format(value))
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * max(0, 3 - len(parts))


def recipe_version(filename, class_name):
    tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
    values = [node.value for cls in tree.body
              if isinstance(cls, ast.ClassDef) and cls.name == class_name
              for node in cls.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "version"
                      for target in node.targets)]
    require(len(values) == 1 and isinstance(values[0], ast.Constant)
            and isinstance(values[0].value, str),
            "{} must have one literal string version in {}".format(class_name, filename))
    return values[0].value


def available_destinations(output):
    active = False
    identifiers = set()
    for line in output.splitlines():
        if "Available destinations for" in line:
            active = True
        elif "Unavailable destinations for" in line:
            active = False
        if active and "platform:iOS Simulator," in line:
            match = re.search(r"\bid:([0-9A-Fa-f-]{36})\s*[,}]", line)
            if match:
                identifiers.add(match.group(1))
    return identifiers


def select_simulator(devices, runtimes, destinations, sdk, minimum):
    allowed = available_destinations(destinations)
    candidates = []
    for runtime in runtimes["runtimes"]:
        if (not runtime.get("isAvailable") or
                not runtime.get("identifier", "").startswith("com.apple.CoreSimulator.SimRuntime.iOS-")):
            continue
        version = version_tuple(runtime["version"])
        if version < version_tuple(minimum):
            continue
        for device in devices["devices"].get(runtime["identifier"], []):
            device_type = device.get("deviceTypeIdentifier")
            iphone = (".iPhone-" in device_type if isinstance(device_type, str)
                      else device.get("name", "").startswith("iPhone"))
            if (device.get("isAvailable") and iphone
                    and device.get("udid") in allowed):
                candidates.append(dict(device, runtime_version=runtime["version"],
                                       runtime_identifier=runtime["identifier"]))
    require(candidates, "No compatible available iPhone in scheme destinations; see device/runtime inventories")
    return sorted(candidates, key=lambda item: (
        version_tuple(item["runtime_version"])[:2] != version_tuple(sdk)[:2],
        tuple(-part for part in version_tuple(item["runtime_version"])),
        item["name"], item["udid"],
    ))[0]


def normalized_path(value):
    require(isinstance(value, str) and value, "Expected nonempty path")
    resolved = str(Path(value).resolve())
    # Simulator paths can name the same volume through /private or its alias.
    return resolved[8:] if resolved.startswith("/private/") else resolved


def validate_result(result, request, installed_app, executable, python_version,
                    kivy_version, orientations):
    require(isinstance(result, dict), "Result must be an object")
    require(type(result.get("schema_version")) is int and result["schema_version"] == 1,
            "Invalid result schema_version")
    require(result.get("nonce") == request["nonce"], "Wrong result nonce")
    require(result.get("case") == request["case"], "Wrong result case")
    require(result.get("status") == "passed", "Result status is not passed")
    require(result.get("stage") == "kivy-event-loop", "Result has not reached kivy-event-loop")
    argv = result.get("argv")
    require(isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv),
            "Invalid argv list")
    app = normalized_path(str(installed_app))
    require(argv[0] and normalized_path(argv[0]) == normalized_path(str(installed_app / executable)),
            "argv[0] does not identify installed executable")
    require(argv[1:] == request["expected_args"], "Native argv suffix was consumed or changed")
    expected_python = list(version_tuple(python_version))
    actual_python = result.get("python_version")
    require(isinstance(actual_python, list) and all(type(part) is int for part in actual_python)
            and actual_python == expected_python, "Python version differs from checkout recipe")
    require(result.get("kivy_version") == kivy_version, "Kivy version differs from checkout recipe")
    paths = result.get("paths")
    environment = result.get("environment")
    require(isinstance(paths, dict) and isinstance(environment, dict), "Missing paths/environment")
    require(normalized_path(paths.get("prefix")) == app, "sys.prefix is not bundled resource root")
    require(normalized_path(environment.get("PYTHONHOME")) == app, "PYTHONHOME is not bundled resource root")
    your_app = normalized_path(str(installed_app / "YourApp"))
    require(normalized_path(paths.get("cwd")) == your_app, "cwd is not bundled YourApp")
    require(normalized_path(paths.get("main_file")) == your_app + "/main.pyc", "main_file is not bundled main.pyc")
    sys_path = paths.get("sys_path")
    require(isinstance(sys_path, list) and all(isinstance(path, str) for path in sys_path),
            "Invalid sys.path")
    normalized = {normalized_path(path if path else paths["cwd"]) for path in sys_path}
    major, minor = expected_python[:2]
    stdlib = app + "/lib/python{}.{}".format(major, minor)
    archive = app + "/lib/python{}{}.zip".format(major, minor)
    require(stdlib in normalized or archive in normalized, "sys.path missing bundled standard library")
    require(stdlib + "/site-packages" in normalized, "sys.path missing bundled site-packages")
    pythonpath = environment.get("PYTHONPATH")
    require(isinstance(pythonpath, str), "Invalid PYTHONPATH")
    env_paths = {normalized_path(str(Path(paths["cwd"]) / path))
                 for path in pythonpath.split(":")}
    require({app, stdlib, stdlib + "/site-packages", your_app} <= env_paths,
            "PYTHONPATH missing bundled resource/application paths")
    require(environment.get("KIVY_ORIENTATION") == orientations, "KIVY_ORIENTATION differs from built plist")
    for key, expected in FIXED_ENVIRONMENT.items():
        require(environment.get(key) == expected, "Unexpected environment " + key)
    imports = result.get("imports")
    require(isinstance(imports, dict), "Missing import operations")
    for name in ("json", "zlib", "sqlite3", "ssl"):
        require(imports.get(name) is True, "Import operation failed: " + name)
    window = result.get("window")
    require(isinstance(window, dict) and window.get("provider") == "WindowSDL", "Window provider is not SDL2")
    size = window.get("size")
    require(isinstance(size, list) and len(size) == 2 and
            all(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in size),
            "Window dimensions must be positive finite numbers")


def remaining_timeout(deadline, maximum, clock=time.monotonic):
    remaining = deadline - clock()
    require(remaining > 0, "Overall deadline exhausted")
    return min(maximum, remaining)


def wait_for_result(filename, request, deadline, timeout, clock=time.monotonic,
                    sleep=time.sleep, observe=lambda result: None):
    end = clock() + remaining_timeout(deadline, timeout, clock)
    stage = "no-result"
    while clock() < end:
        if filename.exists():
            try:
                result = json.loads(filename.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as error:
                raise SmokeError("Malformed result JSON: {}".format(error)) from error
            observe(result)
            require(isinstance(result, dict) and type(result.get("schema_version")) is int
                    and result["schema_version"] == 1, "Invalid result schema_version")
            require(isinstance(result.get("nonce"), str) and isinstance(result.get("case"), str),
                    "Invalid result nonce/case schema")
            if result["nonce"] == request["nonce"]:
                require(result["case"] == request["case"], "Wrong result case")
                stage = result.get("stage")
                require(isinstance(stage, str) and stage and
                        result.get("status") in ("running", "passed", "failed"), "Invalid result stage/status")
                if result["status"] == "failed":
                    raise SmokeError("App failed at {}: {}".format(stage, result.get("error", "no traceback")))
                if result["status"] == "passed":
                    require(stage == "kivy-event-loop", "Passed result has not reached kivy-event-loop")
                    require(clock() < end, "Result arrived after deadline")
                    return result
            else:
                stage = "stale-result"
        sleep(min(0.5, max(0, end - clock())))
    raise SmokeError("{} timed out; last stage: {}".format(request["case"], stage))


class Runner:
    def __init__(self, diagnostics, deadline):
        self.diagnostics = diagnostics
        self.deadline = deadline
        self.counter = 0

    def run(self, args, label, maximum=30, allow_failure=False):
        timeout = remaining_timeout(self.deadline, maximum)
        self.counter += 1
        logfile = self.diagnostics / "{:02d}-{}.log".format(self.counter, label)
        with logfile.open("w", encoding="utf-8") as stream:
            stream.write("Command: {!r}\nTimeout: {:.2f}s\n".format(args, timeout))
            stream.flush()
            process = subprocess.Popen(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except BaseException:
                # Kill the entire group, including children after the leader exits.
                self.stop_group(process)
                raise
        output = logfile.read_text(encoding="utf-8", errors="replace")
        # Strip our header from machine-readable command output.
        output = output.split("\n", 2)[2]
        require(allow_failure or process.returncode == 0,
                "{} failed ({}); see {}".format(label, process.returncode, logfile))
        return process.returncode, output

    @staticmethod
    def stop_group(process):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            if sig == signal.SIGTERM:
                time.sleep(0.25)
        process.wait(timeout=0.5)


def atomic_json(filename, value):
    temporary = filename.with_suffix(filename.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(filename)


def terminate_app(runner, udid, bundle):
    code, output = runner.run(["xcrun", "simctl", "terminate", udid, bundle],
                              "terminate", allow_failure=True)
    require(code == 0 or (code == 3 and any(message in output.lower() for message in (
        "found nothing to terminate", "not running", "no such process",
    ))), "Unexpected simctl terminate failure: " + output)


def project_minimum(filename):
    # Parse the generated project rather than assuming the current template target.
    from pbxproj import XcodeProject
    project = XcodeProject.load(str(filename))
    values = [configuration.buildSettings["IPHONEOS_DEPLOYMENT_TARGET"]
              for configuration in project.objects.get_objects_in_section("XCBuildConfiguration")
              if "IPHONEOS_DEPLOYMENT_TARGET" in configuration.buildSettings]
    require(values, "Generated project has no deployment target")
    return max(values, key=version_tuple)


def execute(args):
    artifacts = Path(args.artifacts).resolve()
    diagnostics = artifacts / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    runner = Runner(diagnostics, time.monotonic() + args.timeout)
    selected = None
    bundle = None
    installed = False
    owns_boot = False
    case = "setup"
    last_stage = "host-setup"
    failed = True
    started = time.monotonic()
    try:
        require(not (artifacts / "build.xcresult").exists(), "Preexisting build.xcresult; use fresh smoke artifacts")
        architecture = platform.machine()
        atomic_json(diagnostics / "host.json", {"architecture": architecture, "configuration": "Release"})
        require(platform.system() == "Darwin" and architecture == "arm64",
                "Startup smoke requires an Apple Silicon macOS host")
        runner.run(["xcode-select", "-p"], "xcode-select")
        runner.run(["xcodebuild", "-version"], "xcode-version")
        _, sdk = runner.run(["xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"], "sdk")
        python_version = recipe_version(ROOT / "kivy_ios/recipes/python3/__init__.py", "Python3Recipe")
        kivy_version = recipe_version(ROOT / "kivy_ios/recipes/kivy/__init__.py", "KivyRecipe")
        version_tuple(python_version)
        generated = ROOT / "startupsmoke-ios"
        require(not generated.exists(), "Preexisting startupsmoke-ios; move/remove your disposable generated project before rerunning")
        runner.run(["toolchain", "create", "StartupSmoke", str(ROOT / "tests/test_startup")], "create")
        project = generated / "startupsmoke.xcodeproj"
        minimum = project_minimum(project / "project.pbxproj")
        _, devices = runner.run(["xcrun", "simctl", "list", "devices", "available", "--json"], "devices")
        _, runtimes = runner.run(["xcrun", "simctl", "list", "runtimes", "--json"], "runtimes")
        _, destinations = runner.run(["xcodebuild", "-project", str(project), "-scheme", "startupsmoke",
                                      "-showdestinations"], "destinations")
        selected = select_simulator(json.loads(devices), json.loads(runtimes), destinations, sdk.strip(), minimum)
        atomic_json(diagnostics / "selected.json", selected)
        udid = selected["udid"]
        derived = artifacts / "DerivedData"
        runner.run(["xcodebuild", "-project", str(project), "-scheme", "startupsmoke",
                    "-configuration", "Release", "-destination", "platform=iOS Simulator,id=" + udid,
                    "-derivedDataPath", str(derived), "-resultBundlePath", str(artifacts / "build.xcresult"),
                    "build", "CODE_SIGNING_ALLOWED=NO", "ARCHS=arm64"], "build", maximum=600)
        product = derived / "Build/Products/Release-iphonesimulator/startupsmoke.app"
        with (product / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        bundle = info["CFBundleIdentifier"]
        executable = info["CFBundleExecutable"]
        require(isinstance(bundle, str) and bundle and isinstance(executable, str) and executable,
                "Built product has invalid bundle identifier/executable")
        require(version_tuple(selected["runtime_version"]) >= version_tuple(info["MinimumOSVersion"]),
                "Selected runtime is below built MinimumOSVersion")
        names = info["UISupportedInterfaceOrientations"]
        require(names and all(name.startswith("UIInterfaceOrientation") for name in names), "Invalid built orientations")
        orientations = " ".join(name[len("UIInterfaceOrientation"):] for name in names)
        atomic_json(diagnostics / "product.json", {
            "bundle": bundle, "executable": executable, "minimum": info["MinimumOSVersion"],
            "orientations": orientations, "python_version": python_version, "kivy_version": kivy_version,
        })
        _, current_devices = runner.run(["xcrun", "simctl", "list", "devices", "available", "--json"], "pre-boot-devices")
        current = [device for group in json.loads(current_devices)["devices"].values()
                   for device in group if device.get("udid") == udid]
        require(len(current) == 1, "Selected simulator disappeared before boot")
        if current[0].get("state") != "Booted":
            owns_boot = True
            runner.run(["xcrun", "simctl", "boot", udid], "boot")
        runner.run(["xcrun", "simctl", "bootstatus", udid, "-b"], "bootstatus", maximum=180)
        # Mark ownership before submission so a partial install is cleaned up too.
        installed = True
        runner.run(["xcrun", "simctl", "install", udid, str(product)], "install")
        _, app_path = runner.run(["xcrun", "simctl", "get_app_container", udid, bundle, "app"], "app-container")
        _, data_path = runner.run(["xcrun", "simctl", "get_app_container", udid, bundle, "data"], "data-container")
        installed_app = Path(app_path.strip())
        result_dir = Path(data_path.strip()) / "Documents/ci-smoke"
        result_dir.mkdir(parents=True, exist_ok=True)
        _, help_output = runner.run(["xcrun", "simctl", "launch", "--help"], "launch-help", allow_failure=True)
        require("--stdout" in help_output and "--stderr" in help_output, "simctl launch lacks stdout/stderr capture")
        for case, trailing_args in CASES.items():
            last_stage = "no-result"
            case_dir = diagnostics / case
            case_dir.mkdir()
            terminate_app(runner, udid, bundle)
            for name in ("result.json", "result.json.tmp"):
                path = result_dir / name
                if path.exists():
                    path.unlink()
            request = {"schema_version": 1, "nonce": uuid.uuid4().hex,
                       "case": case, "expected_args": trailing_args}
            atomic_json(result_dir / "request.json", request)
            atomic_json(case_dir / "request.json", request)

            def observe(result):
                nonlocal last_stage
                last_stage = result.get("stage", "invalid-result") if isinstance(result, dict) else "invalid-result"
                atomic_json(case_dir / "last-observed.json", result)

            try:
                runner.run(["xcrun", "simctl", "launch", "--stdout=" + str(case_dir / "stdout.log"),
                            "--stderr=" + str(case_dir / "stderr.log"), udid, bundle] + trailing_args, case + "-launch")
                result = wait_for_result(
                    result_dir / "result.json", request, runner.deadline, min(60, args.result_timeout),
                    observe=observe,
                )
                validate_result(result, request, installed_app, executable, python_version, kivy_version, orientations)
            finally:
                if (result_dir / "result.json").exists():
                    shutil.copyfile(result_dir / "result.json", case_dir / "result.json")
            print("{} passed on {} ({})".format(case, selected["name"], udid), flush=True)
        failed = False
    except BaseException as error:
        message = "{} on {} (last stage: {}): {}; diagnostics: {}".format(case, selected, last_stage, error, diagnostics)
        (diagnostics / "error.txt").write_text(message + "\n", encoding="utf-8")
        print(message, file=sys.stderr, flush=True)
        raise
    finally:
        runner.deadline = time.monotonic() + 28  # Leave room for group termination.
        cleanup_errors = []

        def best_effort(operation):
            try:
                operation()
            except BaseException as error:
                cleanup_errors.append(str(error))

        if selected:
            udid = selected["udid"]
            if installed:
                best_effort(lambda: terminate_app(runner, udid, bundle))
                best_effort(lambda: runner.run(["xcrun", "simctl", "uninstall", udid, bundle], "uninstall", maximum=5))
            if failed:
                best_effort(lambda: runner.run(
                    ["xcrun", "simctl", "spawn", udid, "log", "show", "--last", "2m", "--style", "compact",
                     "--predicate", 'process == "startupsmoke"'], "simulator-log", maximum=5))

                def copy_crashes():
                    crash_dir = diagnostics / "crashes"
                    crash_dir.mkdir(exist_ok=True)
                    directories = [Path.home() / "Library/Logs/DiagnosticReports"]
                    simulator_data = Path.home() / "Library/Developer/CoreSimulator/Devices" / udid / "data"
                    directories.extend(simulator_data / "Library/Logs" / name
                                       for name in ("CrashReporter", "DiagnosticReports"))
                    for index, directory in enumerate(directories):
                        remaining_timeout(runner.deadline, 1)
                        reports = list(directory.glob("startupsmoke*"))
                        for report in sorted(reports, key=lambda path: path.stat().st_mtime, reverse=True)[:10]:
                            remaining_timeout(runner.deadline, 1)
                            if (report.is_file() and report.stat().st_size <= 5 * 1024 * 1024 and
                                    report.stat().st_mtime >= time.time() - (time.monotonic() - started) - 60):
                                shutil.copyfile(report, crash_dir / (str(index) + "-" + report.name))
                best_effort(copy_crashes)
            if owns_boot:
                best_effort(lambda: runner.run(["xcrun", "simctl", "shutdown", udid], "shutdown", maximum=5))
        best_effort(lambda: atomic_json(diagnostics / "cleanup.json", {
            "errors": cleanup_errors, "elapsed_seconds": time.monotonic() - started,
        }))
        if cleanup_errors:
            print("Cleanup/diagnostics errors: " + repr(cleanup_errors), file=sys.stderr)
            if not failed:
                raise SmokeError("Smoke passed but cleanup failed; see cleanup.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--result-timeout", type=float, default=60)
    parser.add_argument("--artifacts", default="build/ci-startup")
    args = parser.parse_args()
    if not all(math.isfinite(value) and value > 0 for value in (args.timeout, args.result_timeout)):
        parser.error("timeouts must be positive finite seconds")

    def interrupted(signum, frame):
        # A second termination must not interrupt the bounded cleanup allowance.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise SmokeError("CI termination signal {}".format(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        execute(args)
    except (Exception, KeyboardInterrupt) as error:
        print("Startup smoke failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
