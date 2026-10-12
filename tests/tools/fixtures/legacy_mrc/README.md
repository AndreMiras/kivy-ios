# Frozen legacy MRC project

These files are frozen from the production templates at master revision
`892c4b72ee4eb3537310b0694d5fb73cb6a18d53`, before the ARC/PyConfig migration:
`kivy_ios/tools/templates/{{ cookiecutter.project_name }}-ios/`.
They must not be regenerated from current templates during tests.

The one-time rendering substitutions were:

- `cookiecutter.project_name`: `legacy`
- `cookiecutter.project_dir`: `/ci/legacy-app`
- `cookiecutter.dist_dir`: `/ci/legacy-dist` (including the template's
  nonbreaking-space variant in the hostpython compile command)

The only project customization is the additional `custom_helper` native target,
with independent Debug/Release configurations and fixed `C100…` object IDs.
The original app target, configuration lists, build phases, references and
settings are retained. `main.m` and `bridge.m` retain the historical manual
memory management, including the autorelease pool and explicit releases.

This is a parser/updater fixture, not a complete native-build fixture: resources,
headers, application code and distribution artifacts are intentionally absent.
The helper target need not build. Tests supplying explicit ARC/modules `YES`
check only preservation and scalar serialization; they do not claim these MRC
sources compile with ARC enabled.
