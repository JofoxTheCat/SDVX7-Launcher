# SDVX ∇ Launcher | Discord Rich Presence

A launcher for **SOUND VOLTEX** (running on **spice2x** with an **Asphyxia CORE**
EA service) that shows a live, detailed **Discord Rich Presence**:

- Current state — **Menu / Song Select / Playing / Result**
- **Song title** and **jacket** artwork
- **Difficulty** and **level** (e.g. `EXH 16`), read live from game memory
- The **carded-in player's name**, read live from memory
- The player's **VolForce**, computed from the Asphyxia savedata for whichever
  account is currently logged in

It launches Asphyxia and the game for you, then keeps Discord in sync via a
poll-based heartbeat so the presence never drifts out of date.

---

## How it works

| Data | Source |
|------|--------|
| State / song / jacket | Parsed from the game's stdout log |
| Difficulty + level | Read live from `soundvoltex.dll` memory (spice2x never logs it) |
| Player name | Read live from memory; the per-account offset is discovered automatically from the Asphyxia profile names and cached |
| VolForce | Computed from the Asphyxia savedata for the active account |

The name and difficulty live at module-relative offsets inside
`soundvoltex.dll`. They are located once (the difficulty via a short guided
scan, the name automatically) and then cached in the config for instant reuse.

---

## Prerequisites

### Common
- **Python 3.10+**
- **[pypresence](https://github.com/qwertyquerty/pypresence)** — `pip install pypresence`
- **Discord** desktop app running and logged in
- A working **spice2x** build of SOUND VOLTEX
- **Asphyxia CORE** with the SDVX plugin (used as the EA service and as the
  source of profile names + savedata for VolForce)

### Windows
- Memory reading (difficulty + name) requires **Administrator rights**. The
  recommended EXE build auto-elevates via UAC; running from source means
  starting it from an elevated terminal (see *Installation & running*).

### Linux
- **Wine** or **Proton** to run the game (a Windows binary)
- Asphyxia CORE — a **native Linux build is recommended**; a Windows build is
  run through Wine automatically
- **ptrace** permission to read the game's memory via `/proc/<pid>/mem`. Because
  the launcher starts the game itself, the default `kernel.yama.ptrace_scope=1`
  usually suffices. If reads fail, either run the launcher with elevated
  privileges or set `sudo sysctl -w kernel.yama.ptrace_scope=0`.

---

## Installation & running

On **Windows** the launcher needs Administrator rights to read the game's memory
(difficulty + player name). The recommended, normal way is to build a standalone
**EXE** with the `--uac-admin` flag — it then **auto-prompts for elevation (UAC)**
on every launch, so memory reads just work.

### Recommended (Windows): build & run the EXE
1. Install Python 3.10+ and the build tools:
   ```
   pip install pypresence pyinstaller
   ```
2. Build it (from the folder containing `sdvx_rpc.py` and the icon):
   ```
   python -m PyInstaller --onefile --uac-admin --name "SDVX7_LauncherV1-00" --icon=sdvxrpc.ico sdvx_rpc.py
   ```
3. The EXE is created in `dist/`. Put `SDVX7_LauncherV1-00.exe` in your spice2x
   game folder (next to `spice64.exe`) and run it — Windows shows a UAC prompt
   (thanks to `--uac-admin`), and memory reads work without any extra steps.

### Alternative: run from source (Windows)
Install the dependency (`pip install pypresence`), then start the script from a
terminal you opened **as Administrator** (right-click the terminal → *Run as
administrator*), with the spice2x game folder as the working directory:
```
python sdvx_rpc.py
```
> Double-clicking the `.py` does **not** elevate. Without admin the memory reads
> (difficulty + name) fail — that's exactly what the `--uac-admin` EXE avoids.

### Linux
No EXE/admin step — run from source with the game folder as the working
directory:
```
python3 sdvx_rpc.py
```
Memory is read via `/proc` and needs ptrace permission (see *Prerequisites*).

---

## First run

On the first start a short **Quick Setup** wizard asks for:
- a fallback player name (optional — the name is auto-detected anyway),
- the jacket image source,
- the EA service / Asphyxia path and URL,
- the SpiceAPI port and heartbeat timing,
- what to show on Discord (VolForce, difficulty) and whether to enable debug.

After the game launches you are guided **once** to locate the difficulty offset
(`--find-diff`-style): play a couple of charts and confirm the difficulty each
round; the launcher learns and saves the offset.

The **player-name offset is found automatically** the first time each account
reaches *My Room* — it scans for that account's Asphyxia profile name, saves the
offset, and from then on simply reads it (no more scanning). Switching accounts
is handled transparently: each account's name lives at its own offset, all kept
in `name_offsets`.

> The name and VolForce only appear once you are **carded-in (My Room)** — before
> login there is no valid player, so nothing is shown.

---

## Configuration (`sdvx_rpc_config.json`)

| Key | Meaning |
|-----|---------|
| `asphyxia_exe` | Path to the Asphyxia CORE executable to auto-launch |
| `asphyxia_url` | EA service URL passed to the game via `-url` |
| `player_name` | Optional fallback name |
| `jacket_mode` | `S` standard CDN · `O` custom online URL · `L` local server · `N` none |
| `jacket_url` | Base URL for jackets (modes `O` / `L`) |
| `spice_api_port` | SpiceAPI TCP port (default `1337`) |
| `spice_diff_offset` | Difficulty byte offset in `soundvoltex.dll` (from `--find-diff`) |
| `spice_diff_map` | Learned byte→difficulty-slot encoding |
| `name_offsets` | `{ "ACCOUNT": offset, … }` — per-account name offsets (auto-filled) |
| `player_name_offset` | Legacy single name offset (auto-migrated into `name_offsets`) |
| `heartbeat_min` | Fastest update spacing in seconds (default `2`) |
| `heartbeat_max` | Re-assert interval in seconds; heals dropped updates (default `15`) |
| `show_vf` | Show VolForce (`true`/`false`) |
| `show_diff` | Show difficulty + level (`true`/`false`) |
| `game_exe_path` | Full path to `spice64.exe` (else relative / run from the game folder); also anchors the music_db search |
| `music_db_path` | Full path to `music_db*.xml` (else auto-located; the **largest** match wins, so the full db beats partial/omnimix dbs) |
| `asphyxia_savedata` | Explicit path to the SDVX savedata `.db` (else auto-located) |
| `vf_clear_coeff` | Optional override of the clear-type VolForce coefficients |
| `launch_prefix` | **Linux only** — launch wrapper, e.g. `["wine"]` or a Proton command |
| `wine_prefix` | **Linux only** — `WINEPREFIX` path used for the game + Asphyxia |
| `ryunet_cid` | NFC card id for custom Ryunet servers — VolForce is then fetched from the Ryunet API instead of local savedata |
| `pcbid_by_url` | `{ "<custom_ea_url>": "<pcbid>" }` — PCBID per custom server (asked once, passed to spice2x via `-p`) |

---

## Command-line flags

| Flag | Purpose |
|------|---------|
| *(none)* | Normal launch |
| `--debug` | Verbose console output (state changes, detected difficulty, name resolution, Discord payloads) |
| `--find-diff` | Guided differential scan to locate the difficulty offset (run while the game is up; Windows: as admin) |
| `--find-name` | Re-locate the player-name offset for the **currently carded-in** account; lists every match |
| `--vf-test` | Print the active account and the VolForce of each account |
| `--dump-db` | Print the first few `<music>` entries from the song database (format inspection) |
| `--spice-dump` | Connect via SpiceAPI and hex-dump a memory window (manual reverse-engineering helper) |
| `--discover` | Force a fresh difficulty-offset discovery even if one is saved |

---

## Updates

On startup the launcher checks GitHub for a newer release (compares the local
version to the latest tag) and, if one exists, prints the download link and
offers to open it. The check has a short timeout and is skipped silently if you
are offline, so it never blocks the launch.

## Known difficulty offsets

The difficulty byte offset in `soundvoltex.dll` is game-version-specific. If your
version is listed here you can set `spice_diff_offset` directly instead of
running `--find-diff`:

| Game version | `spice_diff_offset` |
|--------------|---------------------|
| KFC-2026012700 | `0x11DDE18` |
| KFC-2026032402 | `0x11FFBF8` |

(Contributions welcome — if you locate the offset for another version with
`--find-diff`, please open an issue/PR so others can reuse it.)

## Troubleshooting

- **Name shows `GUEST` or VolForce is missing** — you are not carded-in yet;
  both appear once you reach *My Room*.
- **Wrong name after switching accounts** — run `python sdvx_rpc.py --find-name`
  while carded-in as that account; if several matches are listed and the name
  doesn't update, set `name_offsets` for that account to a different listed
  value.
- **Difficulty not shown while playing** — run `--find-diff` to (re)learn the
  offset, then restart the launcher.
- **Memory read failed** — Windows: run as Administrator. Linux: ptrace is
  restricted (see *Prerequisites*).
- **Discord not updating** — make sure the Discord desktop app is running and
  logged in before launching. The Script also stops working if you're are selecting something in the command window.

---

## Sources & references

This project was built with reference to, and credit to, the following:

- **spice2x** — the SDVX runtime and SpiceAPI this launcher targets —
  <https://github.com/spice2x/spice2x.github.io>
- **Ryu7w7 — SDVX-Discord-Rich-Presence** — prior art / inspiration and the
  jacket CDN (`jackets.ryu7w7.xyz`) — <https://github.com/Ryu7w7/SDVX-Discord-Rich-Presence>
- **Oscript07 — SDVX-Rich-Presence** — prior art / inspiration —
  <https://github.com/Oscript07/SDVX-Rich-Presence>
- **brenbread — SDVX VOLFORCE Calculator** — VolForce calculation reference —
  <https://github.com/brenbread/SDVX-VOLFORCE-Calculator>
- **SDVX.org Compendium — VolForce** — the VolForce formula and grade/clear
  coefficients — <https://www.sdvx.org/en/compendium/volforce>
- **Asphyxia CORE** — the EA service, profile data and savedata used for names
  and VolForce
- **pypresence** — the Discord IPC / Rich Presence library —
  <https://github.com/qwertyquerty/pypresence>
- **e-amusement CDN** (`eacache.s.konaminet.jp`) — referenced for jacket art

All trademarks (SOUND VOLTEX, KONAMI, e-amusement) belong to their respective
owners. This is an unofficial, non-commercial fan tool.

---

## License

Released under the **MIT License**.
