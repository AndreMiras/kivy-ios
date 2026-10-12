"""Generated-app startup probe; success requires a running Kivy Clock callback."""
import sys

original_argv = list(sys.argv)

# Capture native argv before any other imports can inspect it.
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import traceback  # noqa: E402


result_dir = Path.home() / "Documents" / "ci-smoke"
payload = {
    "schema_version": 1, "nonce": None, "case": None,
    "argv": original_argv, "stage": "bootstrap", "status": "running",
}


def write_result():
    temporary = result_dir / "result.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    os.replace(str(temporary), str(result_dir / "result.json"))


def report_failure():
    payload.update(status="failed", error=traceback.format_exc())
    write_result()


try:
    with (result_dir / "request.json").open(encoding="utf-8") as stream:
        request = json.load(stream)
    payload.update(nonce=request.get("nonce"), case=request.get("case"))
    write_result()
    cases = {"ordinary": [], "native-option": ["-NSDocumentRevisionsDebugMode", "YES"]}
    if (type(request.get("schema_version")) is not int or
            request["schema_version"] != 1 or
            not isinstance(request.get("nonce"), str) or not request["nonce"] or
            request.get("case") not in cases or
            request.get("expected_args") != cases[request["case"]]):
        raise RuntimeError("Invalid startup request")
    if (not original_argv or not original_argv[0] or
            original_argv[1:] != request["expected_args"]):
        raise RuntimeError("Native argv was consumed or changed: {!r}".format(original_argv))

    # This disables only Kivy's parser, after native CPython initialization.
    os.environ["KIVY_NO_ARGS"] = "1"
    payload["stage"] = "bundled-imports"
    write_result()
    import sqlite3
    import ssl
    import zlib

    sample = {"startup": [1, "ios"]}
    if json.loads(json.dumps(sample)) != sample:
        raise RuntimeError("JSON round trip failed")
    if zlib.decompress(zlib.compress(b"startup")) != b"startup":
        raise RuntimeError("zlib round trip failed")
    connection = sqlite3.connect(":memory:")
    try:
        if connection.execute("select 1").fetchone() != (1,):
            raise RuntimeError("SQLite operation failed")
    finally:
        connection.close()
    if not ssl.OPENSSL_VERSION:
        raise RuntimeError("SSL version missing")
    payload.update(
        imports={"json": True, "zlib": True, "sqlite3": True, "ssl": True},
        python_version=list(sys.version_info[:3]),
        paths={"prefix": sys.prefix, "sys_path": list(sys.path),
               "cwd": os.getcwd(), "main_file": __file__},
        environment={key: os.environ.get(key) for key in (
            "PYTHONHOME", "PYTHONPATH", "KIVY_ORIENTATION", "KIVY_BUILD",
            "KIVY_WINDOW", "KIVY_IMAGE", "KIVY_AUDIO", "KIVY_GL_BACKEND",
            "KIVY_NO_CONSOLELOG", "KIVY_NO_ARGS", "PYTHONOPTIMIZE",
        )},
        stage="kivy-import",
    )
    write_result()
    import kivy
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.core.window import Window
    from kivy.uix.widget import Widget

    class StartupSmokeApp(App):
        def build(self):
            return Widget()

        def on_start(self):
            payload["stage"] = "kivy-on-start"
            write_result()
            Clock.schedule_once(self.report_event_loop, 0)

        def report_event_loop(self, dt):
            try:
                payload["stage"] = "kivy-event-loop"
                if (Window is None or type(Window).__name__ != "WindowSDL" or
                        not all(value > 0 for value in Window.size)):
                    raise RuntimeError("Expected a positive-sized SDL2 Window")
                payload.update(
                    kivy_version=kivy.__version__, status="passed",
                    window={"provider": type(Window).__name__, "size": list(Window.size)},
                )
                write_result()
            except BaseException:
                report_failure()
            finally:
                self.stop()

    payload["stage"] = "kivy-run"
    write_result()
    StartupSmokeApp().run()
    if payload["status"] != "passed":
        raise RuntimeError("App returned without event-loop completion")
except BaseException:
    report_failure()
    raise
