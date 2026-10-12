"""Simulator startup acceptance policies, usable without platform tools."""
import ast
import json
import math
from pathlib import Path
import re
import time


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


def atomic_json(filename, value):
    temporary = filename.with_suffix(filename.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(filename)
