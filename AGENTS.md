# AGENTS.md

CLI tool (Python ≥3.14, managed by `uv`) that finds/downloads updates from the
MS Update Catalog, deep-merges them into extracted Acer OEM packages, and
downloads fresh UWP app bundles from the MS Store. `README.md` is empty and
`package.json` is an unrelated stub — ignore both.

## Commands (always run through `uv`)
- All tests: `uv run pytest`
- Single test: `uv run pytest tests/test_uwp.py::test_name`
- Lint: `uv run ruff check` (add `--fix`)
- Typecheck: `uv run ty check`
- CLI: `uv run python -m windriveinstall <cmd>` (console scripts: `windriveinstall`, `windrive`, `wdi`)

## Known baselines — do not "fix" unrelated code
- `ruff check` already fails with 33 errors (docstrings `D` + a few `E501`/`PL`) in
  `catalog.py`, `downloader.py`, `devices.py`, `models.py`, `ui.py`, `utils.py`.
  Keep your own additions clean; leave those files alone unless tasked.
- `ty check` has 1 pre-existing error: `win32com.client` in `devices.py`.
- No pytest config section; tests auto-discover in `tests/`.
  `test_builder.py`, `test_uwp.py`, `test_rolling.py`, `test_slim_template.py`
  exist, all fully offline (network is monkeypatched).

## Platform gotchas (dev env is Linux/WSL)
- Off-Windows allowed: `build --template-dir …`, `uwp-update`, and
  `run --template-dir … --no-drivers` (rebuild from existing cache).
  Everything else (incl. `build`/`run`/`rolling` without a template,
  `driver-update`, `run --template-dir` without `--no-drivers`) is aborted by
  `__main__.main` with the "только в ОС Windows" message. `build` on Linux is
  portable only when `--template-dir`/`--device` resolves to a template.
- Windows-only deps (`win32com`, `selenium`) are deliberately isolated: try/except +
  `_windows_only` fallback in `__main__.py` and `rolling.py`; `devices.py`/
  `downloader.py` are imported only inside those guarded blocks. Do **not** import
  them at module top level or Linux test collection breaks.
- Keep selenium-free network helpers in `utils.py` (`get_http_session`,
  `download_file`); `uwp.py` relies on them to stay importable off-Windows.

## Architecture (non-obvious wiring)
- `catalog.py` catalog search · `devices.py` local devices (win32com) ·
  `downloader.py` Selenium catalog downloads · `installer.py` · `ui.py` ·
  `utils.py` · `models.py` · `rolling.py` (rolling build + driver-update) ·
  `slim.py` (ужимка шаблона: два идемпотентных прогона).
- `builder.py` is the OEM merge core: component scan, `parse_package_family`
  (canonical UWP family parser), classic driver deep-merge (now also HWID-overlap
  scoring from `.inf`), UWP bundle replacement, `Install_Factory.bat` generation,
  `.device.json` read/write and snapshot-component handling (source: system).
- Pipeline: `OEM/*.zip` → `Template/<Device>/` (static base + `.components`
  manifest + `.device.json`) → `Cache/<Device>/{Drivers,Apps}` (fresh downloads)
  → `Output/<Device>` (clean result; `Output/current` for rolling, default).
  `build_oem_package(template_dir, out_dir, drivers_dir, apps_dir,
  snapshot_hwids, arch)` does a full reset of `out_dir` each run.
- `run` is the single update command (umbrella): `run --template-dir
  Template/<Device>` = `driver-update` → `uwp-update` → `build` → install offer
  (elevated `Install_Factory.bat`); `run` without a template = `rolling` +
  install offer (`install_drivers.bat`). Flags `--no-drivers`, `--no-apps`
  (rebuild from existing cache), `--no-install`, `--arch`. The granular
  commands (`build`, `rolling`, `driver-update`, `uwp-update`) still work.
- Template preparation + 2-stage slim (both idempotent, in `slim.py`):
  1. `scripts/prepare_template.py` (one-time, intentionally outside the CLI)
     extracts `OEM/*.zip` → `Template/` (one folder per archive) and writes a
     `.components` manifest that marks explicit component boundaries (prevents
     multi-root apps like Nitro Sense from being split) plus `.device.json`
     (HWID per classic component, package families, versions, `"arch": "x64"`).
     `--copy-drivers` stages `Drivers/` into `Template/Drivers`. The script
     refuses a non-empty `--template-dir` (`FileExistsError`) — clear it before
     re-running. Then it calls `slim.apply_first_run` pass 1 (no cache needed):
     fixes `hwids` in `.device.json` from the template `.inf` (UTF-16-aware,
     e.g. Killer LAN/WiFi), deletes VGA classics (`VGA_AMD_*`/`VGA_NVIDIA_*`),
     moves VGA Utility UWP apps `Drivers/` → `Apps/`, and converts
     `Audio_Realtek` from a Setup.exe black box to a pnputil component
     (wrapper + `0x*.ini` stripped, `Setup_Driver.cmd` + `InfFiles.txt`
     generated, `Prepackage.xml` `Exec` rewritten).
  2. `driver-update` (→ `rolling.update_template_drivers`) downloads fresh
     catalog drivers into `Cache/<Device>/Drivers` and then calls
     `slim.apply_second_run` pass 2: pnputil classics (`Airplane Mode`,
     `LAN_Killer`, `Bluetooth`, `Wireless LAN`, `TouchPad`) with a fresh catalog
     leaf by HWID get their old payload cut (keep scaffolding); without a leaf
     the component is deleted (folder + manifest + meta entries).
     Both passes preserve extra top-level `.device.json` keys (e.g. `arch`).
  3. `build --template-dir Template/<Device>` reads components from the manifest
     and fresh drivers/apps from `Cache/<Device>`; no manifest → heuristic
     scan; a component `.zip` in the template → `ValueError` (run the script).
     `--snapshot` (Windows) appends WMI HWIDs to `.device.json` as
     `source: system` components that only merge fresh catalog drivers.
- Classic merge quirks (deep-merge, not just copy): HWIDs are matched from
  `.inf` but fall back to `.device.json` for slimmed components whose `.inf`
  was pruned; `.inf` reading is UTF-16 BOM-aware. After a fresh-leaf merge a
  pnputil component's `InfFiles.txt` is rewritten with the actual `.inf` paths
  in the output (so `Setup_Driver.cmd`/`Install.cmd` install the fresh leaves,
  not stale names).
- UWP merge matches by package family (from the component's `AUMIDs.txt`) first,
  then by strict name prefix (`builder._find_fresh_uwp_app`): the component name
  must START with the fresh-app folder name (e.g. `Quick Access_Acer_...` →
  `Quick Access`). A fresh app folder may hold several main packages
  (`VGA Utility` bundling both AMD Radeon SW + NVIDIA Control Panel; `XPERI DTS
  Utility` two bundles) — each main package is matched separately, so no package
  is lost and no component grabs a neighbour's package (the old one-token
  `_name_overlap` wrongly gave VGA Utility to XPERI and Acer Care Center to
  Quick Access). OEM app components live in `Template/<Device>/Apps`, fresh
  packages in `Cache/<Device>/Apps/<App>/{bundle,Dependencies}` (kept separate
  so the family/name matcher can't pick the component itself).
- UWP "Utility" apps often declare `<uap5:DriverDependency>` in their manifest
  (AMD Radeon→`UWPPair.inf`, Realtek Audio Control→`RealtekHSA.inf`, DTS X
  Ultra→`dtsapo4xultrahsa.inf`). On a clean Windows with the paired driver not
  yet installed, OEM `Install_UWP.cmd` DISM provisioning fails with
  `0x80073CFD`/`Error 15613` ("A Prerequisite for an install could not be
  satisfied") and the script falls into its `:Reboot` branch (UWPFAIL tag +
  missing `acerReboot.exe` → `ErrorMsg.vbs` popup + visible `timeout /t 10`).
  NVIDIA Control Panel, DTS Sound Unbound, Acer Care Center, Quick Access have
  no driver dependency and provision fine. The paired drivers (AMD GPU, Realtek
  audio via `Audio_Realtek` pnputil) are absent from the template by design, so
  driver-dependent apps only install after the driver exists on the system.
   The build mitigates this: `Install_Factory.bat` (`generate_factory_install_bat`)
   stable-partitions entries so classic driver installers
   (`Setup_Driver.cmd`/`Install.cmd`/*.exe, incl. `Audio_Realtek` and Chipset)
   run before any UWP provisioning (`Install_UWP.cmd`/`Setup_APP.cmd`, classified
   by `_script_is_uwp_installer`), and `_soften_dism_failure` rewrites the OEM
   `call :Reboot` inside the `if %DISMErrCode% neq 0 (...)` block of every DISM
   script that goes to the shared deps pool into a readable console/log warning
   + `goto :END` (soft skip — no MsgBox/timeout/pause). Remaining prereq-failing
   apps (AMD Radeon, Realtek Audio Control, DTS X Ultra) install on a second
   `Install_Factory.bat` run after their paired driver exists.
- Output deps layout (dedup): fresh UWP dependencies land in a single shared
  `Output/<Device>/Apps/Dependencies` pool (filename = family+version+arch).
  Per-app copies stay only where a script owns them: DISM-style `Install_UWP.cmd`/
  `Setup_APP.cmd` components get their dep-discovery lines rewritten to
  `%~dp0..\Dependencies\...` (original saved as `.cmd.orig`) and then use the
  shared pool; `Setup.exe` components and any component whose DISM script doesn't
  match the known pattern keep per-app `Dependencies/` (fallback). Unmatched fresh
  apps have no installer, so their deps always fold into the shared pool
  (`copy_fresh_apps` also strips a legacy per-app `Dependencies` folder when
  folding). `uwp.py` already downloads each dep once into the cache pool.
- x64-only deps: `uwp.py` (`pick_uwp_packages`/`download_uwp_updates`, param
  `arch`), the component merge (`builder._merge_uwp`, dep copy filtered by
  `MergeContext.target_arch`) and the pool materialization in
  `builder.copy_fresh_apps` keep only neutral + target-arch packages
  (`package_arch` token helper); for x64 devices x86/arm/arm64 are dropped
  from Output. Arch comes from `--arch`, else the `"arch"` key in
  `Template/<Device>/.device.json` (default `"x64"`); rolling uses the local
  machine arch (`rolling._local_arch`).
- `uwp-update --template-dir Template/<Device>` resolves apps via
  `store.rg-adguard.net` (Retail ring), writing `Cache/<Device>/Apps`;
  `--force` re-downloads even when the cached version is current.
- `driver-update --template-dir …` downloads catalog drivers for the template's
  HWIDs (not the system's installed versions) into `Cache/<Device>/Drivers`,
  skipping leaves that already contain an `.inf`, then runs the pass-2 slim
  (`apply_second_run`).

## Data directories (multi-GB real inputs/outputs)
`OEM/` 2.2G · `Drivers/` 776M · `Template/` (now `Template/Nitro5/`, ~1.6G after
pass-1 slim) · `Cache/Nitro5/` (real downloads exist) · `Output/` appears after
builds. `OEM/` and `Drivers/` are gitignored; `Template/`, `Cache/` and
`Output/` are not. `Template/Nitro5` is ~1.6G — never rglob or copy it wholesale
in tests or ad-hoc scripts (a full smoke needs a destination with >5G free).

## Conventions
- Docstrings and user-facing strings are Russian; docstring lint (`D`) is on, so add
  module/class/function docstrings to new code.
- ruff: line-length 88, double quotes, isort first-party `windriveinstall`.
- `git` CLI is not available in this environment.
