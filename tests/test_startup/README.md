# Release simulator startup smoke

Run on an **Apple Silicon Mac**, using active Xcode and an already available
compatible iPhone simulator. Build recipes from this checkout first:

```bash
toolchain build python3 kivy
python3 .ci/test_simulator_startup.py --timeout 900 --result-timeout 60 --artifacts build/ci-startup
```

The harness reuses the distribution and active Xcode, generates a local app
from production templates, selects an available scheme-compatible iPhone,
and explicitly builds **Release / arm64**. It does not download runtimes;
missing compatible devices fail the gate. Local distributions must match the
checkout's Python/Kivy recipe versions; stale builds fail version validation.

Two fresh processes are launched:

- `ordinary`: no trailing arguments; argv contains only the app executable.
- `native-option`: exactly `-NSDocumentRevisionsDebugMode YES` after the
  executable, preserved in Python argv before the first Kivy import.

The host writes a per-launch nonce and expectations to
`Documents/ci-smoke/request.json` in the installed app's **data** container.
The app atomically writes `result.json` there. Bootstrap or a launch PID alone
cannot pass: success is written by a scheduled Clock callback in the running
Kivy event loop, with bundled path/environment/import checks and a real SDL2
Window. The host validates reported values against installed paths, built plist
orientations and recipe versions. Checks use explicit conditionals so they
remain active under `PYTHONOPTIMIZE=2`; Kivy's downstream CLI parser is disabled
only after CPython has initialized and original argv has been checked.

Inspect `build/ci-startup/diagnostics/` for Xcode/SDK and device inventories,
`selected.json`, full build output, host `error.txt`, cleanup errors/timing,
and each case's request/result/last-observed JSON plus stdout/stderr. No JSON
usually indicates native startup failure; the last recorded stage distinguishes
bootstrap, bundled imports and Kivy startup/event-loop failures. A failure
also attempts a bounded recent simulator log and copies recent startup crash
reports. `build/ci-startup/build.xcresult/` contains Xcode diagnostics when
produced; `DerivedData/` is kept locally but is not uploaded by CI.

The overall work budget defaults to 900 seconds, individual builds to 600,
boot readiness to 180, platform commands to 30, and each result wait to at most
60. Commands and waits are capped by the remaining overall budget. Cleanup and
failure diagnostics have a separate bounded 30-second allowance. There are no
case retries. Cleanup removes the dedicated app and shuts down the selected
simulator only when this harness initiated its boot; it never erases devices.

Before rerunning, move/remove only the disposable `startupsmoke-ios` project
you generated, and choose a fresh artifacts directory (or remove your previous
smoke artifacts). Unexpected existing generated projects fail rather than being
overwritten. CI runs this smoke only in non-venv `build_python3_kivy` on
`macos-latest`, and uploads diagnostics on success and failure.
