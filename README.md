# ADI Series Volume Controller

Controls the output volume(s) of an RME ADI-2 DAC / ADI-2 Pro / ADI-2/4 Pro SE
from any device that can send keystrokes — a USB volume wheel, a Microsoft
Surface Dial, an IR remote through a receiver, or a plain keyboard shortcut.
Runs in the tray, talks to the device over MIDI SysEx, and (optionally)
keeps the front-panel LCD in sync over IR when the active output is switched.

Originally implemented in AutoHotkey (still included as a lightweight bonus
alternative, see "Dev / support tools" below), then rewritten in Python for
a single dependency-free executable, a guided Set Hotkeys wizard, and a
second, independent controller (split mode).

## Features

- **Highly customizable.** Hotkeys, step sizes, the danger threshold,
  default volume levels and mode settings are stored in config.ini and can
  be edited directly; hotkeys can also be set through the guided wizard
  instead of hand-editing.
- **Hotkey-driven control.** Any input device that can send a keystroke
  works: left/right for volume down/up, click to switch outputs or mute.
  Devices with more than three states (a wheel with a click-and-turn
  gesture, a remote with extra buttons) can also use click+left / click+right
  (coarse ±1.5 dB steps by default) and a long click.
- **Fast-spin detection.** Ticks arriving faster than a configurable
  threshold use a larger step (0.5 dB by default) than slower, deliberate
  turns (0.1 dB by default).
- **Danger threshold.** Any move at or above a configurable volume level
  (−6 dB by default) resets to the configured default instead of applying,
  so an accidental move at high volume cannot raise it further.
- **Split mode.** Two independent controllers, one per output (Out 1/2 and
  Out 3/4). In split mode, click toggles mute on that controller's output
  instead of switching outputs, and long click swaps which controller is
  linked to which output.
- **Set Hotkeys wizard.** Guided key capture for every action, with
  duplicate-key detection across both controllers. If Controller 1 is
  already configured, a shortcut screen offers "Set up second set of
  controls only", which keeps Controller 1 unchanged and only asks for
  Controller 2, instead of requiring the whole wizard again.
- **Background resync.** Periodically, and on demand from the tray, re-reads
  the device's actual state, so changes made from the front panel or RME's
  own ADI-2 Remote app do not leave the app's internal state out of date.
- **Broadlink IR (optional).** The front-panel LCD's output-select indicator
  is not exposed over MIDI at all — not readable or settable via SysEx, only
  toggled by the physical remote or front panel. Sending its "VOL Push" IR
  command through a Broadlink hub keeps it in sync with output switches made
  from the app. The codes are built in (calculated from RME's own IR
  command documentation via
  [generate_rme_broadlink_ir](https://github.com/Thdub/generate_rme_broadlink_ir)),
  nothing to learn from a physical remote.
- **Config.ini validation.** Settings live in one plain-text file with
  inline explanations. A typo or an invalid value is reported with its
  exact line and reason instead of causing a crash or being silently
  ignored, and a badly broken file can be restored from a `.bak` copy
  automatically.
- **Multiple setups.** Named config files can be saved and loaded from the
  tray menu, for use across more than one room or system.
- **Console hidden by default.** Runs in the tray with no visible window.
  Setting `show_console` to `true` in config.ini displays a log window for
  troubleshooting.
- **No administrator rights required.** The keyboard hook works without
  elevation; it only cannot see keystrokes while an elevated window has
  focus, which is standard behavior for a non-elevated hook.

## Two ways to use this

- **Compiled release** (no Python involved). Download the zipped
  `ADI Series Volume Controller` folder from the GitHub Releases page and run the
  .exe directly. Broadlink support is already included in the executable.
- **From source.** Clone the repository and run `rme_app.py` with Python
  directly, or build an executable (see "Building from source" below). This
  path requires `pip install broadlink` only if Broadlink support is
  wanted, since the import is skipped when `use_broadlink = false`.

## Requirements

- Windows.
- [sendmidi / receivemidi](https://github.com/gbevin/SendMIDI) (same author,
  two separate releases) — `sendmidi.exe` and `receivemidi.exe`, placed next
  to the app (or pointed to by full path in `config.ini`). Needed either
  way, compiled release or from source.
- An RME ADI-2 DAC, ADI-2 Pro, or ADI-2/4 Pro SE, connected and visible as a
  MIDI port (`sendmidi.exe list` to find its exact name).
- Optional: a Broadlink IR hub, only if the LCD "VOL Push" realignment is
  wanted. Just the physical hub — no software prerequisite when running the
  compiled release (see "Two ways to use this" above).

## Quick start

1. Download the compiled release (or build it, see below) and place
   `sendmidi.exe` / `receivemidi.exe` next to `ADI Series Volume Controller.exe`
   (`tray.ico` too, for the custom tray icon — optional, falls back to the
   default Windows icon otherwise).
2. Run it. `config.ini` is created automatically on first launch.
3. With no hotkeys set, the **Set Hotkeys** wizard opens automatically —
   press the key/wheel action/remote button for each prompt. Left, right
   and click are required; the extended actions can be skipped if the
   device does not have them.
4. The tray icon is now live. Right-click it for Set Hotkeys, Split mode,
   Swap controllers (split mode only), Reload/Save as/Load config, Realign
   LCD (if Broadlink is enabled), and Quit.

To reconfigure later: tray → **Set Hotkeys...**. If Controller 1 already has
hotkeys, the wizard asks whether to redo everything or just add/replace
Controller 2 (this also turns split mode on).

## config.ini reference

The file is generated with full inline comments — this is a summary, not a
replacement for reading it:

| Section | What it controls |
|---|---|
| `[Device]` | `device_id` — which RME model: `71` ADI-2 DAC, `72` ADI-2 Pro, `73` ADI-2/4 Pro SE. |
| `[Mode]` | `default_output` (1/2 or 3/4) and `split_mode` (on/off). |
| `[Controls]` | Default volumes per output, the danger threshold, and the three step sizes (normal / fast-spin / coarse click+turn). |
| `[Hotkeys]` | Controller 1's key bindings. Set via the wizard, or by hand (`[modifiers+]key`, e.g. `alt+8`, `ctrl+alt+K`, `F20`, `alt+VK38`). |
| `[Split Mode Hotkeys]` | Controller 2's key bindings (split mode only). |
| `[Background Resync]` | `resync_interval_minutes` — 0 disables it. |
| `[Midi]` | Paths to `sendmidi.exe` / `receivemidi.exe`, and the exact MIDI port name. |
| `[Broadlink IR]` | `use_broadlink`, plus the hub's type/host/MAC if enabled. |
| `[Debug]` | `show_console` — a visible log window for troubleshooting. |

The app rewrites the file back into this exact reference layout whenever it
saves (from the wizard, split mode toggle, etc.) — values are kept, comments
and spacing are not disturbed.

## Building from source

`rme_app.py` needs only the Python standard library, plus `broadlink`
(optional, imported only if `use_broadlink = true`). To compile a
distributable folder with PyInstaller:

```
pip install pyinstaller broadlink
build.bat
```

`build.spec` builds in **folder mode** (`--onedir`, not a single exe) with
`dist_icons/app.ico` as the executable's icon, and pulls in `broadlink` +
`cryptography` (broadlink's crypto dependency, and the one PyInstaller most
often fails to auto-detect) with `collect_all` rather than relying on static
import analysis alone. `build.bat` then copies `sendmidi.exe`,
`receivemidi.exe` and `dist_icons/tray.ico` into the result. Output:
`dist\ADI Series Volume Controller\`, ready to copy anywhere as-is.

## Dev / support tools

A few small tools were built along the way and ship in this repo's `tools/`
folder as a bonus, even though the app itself does not need them at
runtime:

- **`ADI_Series_Volume_Controller.ahk`** (AutoHotkey v2) — the original
  implementation this app is based on, kept as a lightweight alternative.
  Same feature set (hotkey-driven volume control via MIDI SysEx, split
  mode, optional Broadlink IR support), but hotkeys are edited directly in
  the script instead of captured through a wizard, and there is no tray
  icon or GUI. Requires AutoHotkey v2 to run.
- **`RME_Sniff_SysEx.ps1`** — listens to the device's MIDI port and decodes
  every SysEx parameter it sends live (channel vs. device "containers",
  address/index/value), based on RME's own MIDI protocol document
  (`adi2remote_midi_protocol.ods`, RME GmbH / Ralf Männel). This is how the
  channel/parameter mapping used by `rme_app.py` (output address, volume and
  mute indices) was derived and cross-checked.
- **`generate_broadlink_ir.py`** — turns an RME NEC IR command byte into the
  raw Broadlink hex payload, from RME's published IR command documentation.
  The standalone, maintained version of this lives at
  [github.com/Thdub/generate_rme_broadlink_ir](https://github.com/Thdub/generate_rme_broadlink_ir).

## Protocol credit

MIDI SysEx control is based on RME's own published protocol
(`adi2remote_midi_protocol.ods`, © Ralf Männel for RME GmbH), covering the
ADI-2 DAC, ADI-2 Pro and ADI-2/4 Pro SE via a shared command set that differs
only by a one-byte device ID. It is not advertised as a public API — RME has
said on their forum that mapping MIDI to it is "a task for someone else" —
so this should be treated as best-effort, community-level support rather
than an official integration.

## Known limitations

- Windows only (Win32 APIs throughout: keyboard hook, tray icon, message
  loop).
- Tested against the ADI-2 Pro FS only. The DAC / Pro SE device IDs are
  wired in from RME's documentation but not independently verified against
  real hardware, and the Broadlink IR codes (generated by
  [generate_rme_broadlink_ir](https://github.com/Thdub/generate_rme_broadlink_ir))
  have only been confirmed on one hub/remote pairing — reports from other
  hardware, working or not, are welcome.
- The newer **ADI-2 Pro EX** (successor to the ADI-2 Pro FS) has no
  published device ID as of this writing — RME's protocol document still
  lists only DAC/Pro/Pro SE. Support will follow once RME publishes it (or
  someone sniffs it), likely as a fourth `device_id` value with the rest of
  the protocol unchanged.
