# vita-recomp-kit

A shared toolchain for the static recompilation of PS Vita games: it turns a
game's decrypted ARM (Thumb-2, ARM, VFP, NEON) code into C++, builds that into
DLLs a Vita3K fork loads in place of its JIT, and holds the tools used to study
the binaries and drive test runs. It is used, as a git submodule, by three
game ports: ss-delta (Soul Sacrifice Delta), utakata (Shinobido 2: Revenge
of Zen) and vekta-mercenary (Killzone Mercenary). The history comes from ss-delta, where it was written; comments that
cite `docs/...` (decisions D6-D11, plan phases) refer to ss-delta's documents.

**No game data is in this repository**, and none may be committed: no ELFs,
pkgs, licences (work.bin, .rif, zRIF), firmware, generated C++, screenshots,
recordings or logs. Each game project keeps those in its own ignored
directories and passes their paths in.

**Licence:** MIT (LICENSE).

## Contents

| Path | What |
| --- | --- |
| `recomp/recompile.py` | the recompiler: ELF -> `funcs.h`, `bucket_NNN.cpp`, `table.cpp`, `report.txt` |
| `recomp/armrecomp/` | its package: decoder front end (Capstone, exidx, jump tables), instruction semantics, NEON, emitter |
| `recomp/runtime/` | headers the generated code includes (`armrecomp.h`, `neon.h`), the DLL ABI shared with the fork (`aot_abi.h`) and the DLL entry (`aot_dll.cpp`) |
| `recomp/make_tests.py`, `recomp/tests/` | dynarmic comparison tests (per instruction and per function) and their harness |
| `recomp/forms.py` | histogram of instruction forms in a module |
| `recomp/CMakeLists.txt` | builds `gen_<module>.lib`, `<prefix><module>.dll` and `recomp_tests` |
| `recomp/run-tests.ps1` | regenerates, builds and runs the comparison tests for each module |
| `tools/vitaelf.py` | library: segments, module_info, NID-named imports/exports, exidx of a Vita SCE ELF |
| `tools/velf.py`, `tools/inventory.py`, `tools/imix.py` | ELF facts, the code inventory, an instruction mix |
| `tools/sfo.py` | prints a `param.sfo` |
| `tools/v3kdrive.py` | drives Vita3K or a single-game front end of the fork: launch, scripted input, screenshots, FPS |
| `tools/stackdump.py` | call stacks of every thread of a running process (hang diagnosis) |
| `tools/mkzrif.py`, `tools/fetch-update.py` | a zRIF from a work.bin; a title's update pkg from Sony's server |
| `tools/proccap/proccap.cpp`, `tools/wavstats.py` | records one process's audio to WAV; loudness and onsets over time |
| `tools/leak-scan.py` | pre-push hook (`install <repo>`) or `check <range>`: blocks game-data files, big files, licence strings, tokens, user folders and the owner's private patterns (`git config leakscan.patterns <file>`, kept outside the repo) from reaching a remote; `git config leakscan.allow <globs>` exempts a clone's deliberate files (e.g. `docs/assets/screenshots/*.jpg`) from the file-type check |
| `tools/build-vita3k.py` | builds a game project's `third_party/vita3k` with MSVC + Ninja into `build/vita3k` (`msvc.py` finds MSVC with vswhere); `--games-only`: the single-game executables without Vita3K's Qt interface (no Qt needed) |
| `tools/build-recomp.py` | builds a game project's `generated/<module>/` into `ssd_aot_<module>.dll`: MSVC + CMake + Ninja, or `--toolchain <dir>` without any of them |
| `tools/toolchain.py`, `tools/toolchain-windows.json`, `tools/toolchain-linux.json`, `tools/toolchain-macos.json` | a compiler for the modules that needs no Visual Studio: llvm-mingw's clang-cl and lld-link with Microsoft's C++ runtime and Windows SDK downloaded from Microsoft's Visual Studio manifest, every file pinned by SHA-256 in the lock file; also the VC++ runtime DLLs a playable folder carries. Linux and macOS: zig |
| `tools/install-game.py`, `tools/make-dist.py` | from the player's dump (firmware, game, patch, add-ons; pkgs with their licence) to a playable folder `dist/<name>/`, as the game's `game.json` describes it (`gameproject.py`); `--bin`, `--toolchain`, `--out` for a prebuilt front end, the downloaded toolchain and any folder |
| `tools/fetch-firmware.py` | the PS Vita firmware Vita3K needs (3.74 and the font package) from Sony's update servers, checked by SHA-256 |
| `tools/offline.py` | the setup's downloads taken from the player's own folders instead (`--offline`), matched by size and SHA-256 whatever their names; run alone, it lists which pinned downloads a folder holds |
| `tools/make-setup.py`, `tools/setup-windows.json`, `tools/setup-linux.json`, `tools/setup-macos.json`, `tools/setup.py`, `tools/setupgui.py`, `tools/dumpinfo.py` | a game's release for players: the game's own folder with its front end and, in `setup/`, the importer: Python (Windows: the embeddable distribution; Linux and macOS: python-build-standalone) with capstone and PyYAML (pinned) and the kit; `setup.py` runs on the player's computer, started by the game (see below) |

## Tools

Python 3.12 with `capstone` and `pyyaml` (and `numpy` for `wavstats.py`).
Run Python that reads game data with `python -I` and pass paths as arguments.
Each script's docstring has the details.

- `python recomp/recompile.py <elf> <out dir> [--load-base HEX] [--bucket-bytes N] [--only ADDR,...]` (`--help` for the output files)
- `python -I recomp/make_tests.py <elf> <out dir>/tests/<module> [--per-form N] [--functions M]`
- `python -I recomp/forms.py <elf> [top N]`
- `.\recomp\run-tests.ps1 -Root <game project> [-Modules eboot,libc]` (or `-ElfDir`, `-GeneratedDir`, `-BuildDir`, `-Vita3kSrc`, `-Vita3kBuild`, `-DevShell` each); run it in an MSVC developer shell or pass `-DevShell <script>`
- `.\recomp\tests\guarded-run.ps1 <exe> <log> [args...]`: runs a test binary under a memory and time ceiling
- `python -I tools/velf.py <elf> <vita-headers db dir> [--json out.json]`
- `python -I tools/inventory.py <elf> <vita-headers db dir> <out dir>`
- `python -I tools/imix.py <elf> [velf json] [--seg-off HEX] [--seg-va HEX] [--exidx HEX]`: the code segment and the exidx start come from the ELF unless overridden
- `python -I tools/sfo.py <param.sfo>`
- `python -I tools/v3kdrive.py <dir> launch|start|run|bootseq|title|close ... [--exe NAME] [--title PREFIX] [--player N]`: without `--exe`, the one known front end (`ss-delta.exe`, `shinobido2.exe`, `vekta-mercenary.exe`) in `<dir>`, else `ss-delta.exe`
- `python -I tools/stackdump.py <pid or image name> [max frames]`
- `python -I tools/mkzrif.py <psvpfstools keyflate.c> <work.bin>`
- `python -I tools/fetch-update.py <title id> [out dir]`
- `proccap <pid> <out.wav> [--quiet]`; build: `cl /nologo /O2 /EHsc /std:c++20 proccap.cpp ole32.lib mmdevapi.lib`
- `python -I tools/wavstats.py <run.wav> [window seconds] [csv out]`

## Building the recompiled modules

```
cmake -S <this repo>/recomp -B <build dir> -G Ninja -DCMAKE_BUILD_TYPE=Release ^
      -DGENERATED_DIR=<game>/generated -DVITA3K_SRC=<fork source> -DVITA3K_BUILD=<fork build>
cmake --build <build dir>
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `GENERATED_DIR` | none (required) | where `recompile.py` wrote `<module>/` and `make_tests.py` `tests/<module>/`; configuration stops if it is unset or not a directory |
| `RECOMP_MODULES` | `eboot;libc;libfios2;libult` | modules to build (a module without generated code is skipped with a warning) |
| `RECOMP_DLL_PREFIX` | `ssd_aot_` | DLL name prefix; the fork loads `ssd_aot_<module>.dll` (enabled by its `SSD_AOT*` environment variables) |
| `RECOMP_JOBS` | `4` | parallel compiles of generated code (RAM bound) |
| `RECOMP_COUNT_INSNS` | `ON` | count executed guest instructions |
| `RECOMP_BISECT` | `OFF` | route direct calls through the host (bisecting with `SSD_AOT_RANGE`) |
| `RECOMP_TESTS` | `ON` | build `recomp_tests` when `<GENERATED_DIR>/tests/<RECOMP_TEST_MODULE>/tests.cpp` exists |
| `RECOMP_TEST_MODULE` | `eboot` | the module whose tests to build |
| `VITA3K_SRC`, `VITA3K_BUILD` | none | the fork's source and build trees (dynarmic for the tests); required when the tests are built |
| `VITA3K_CONFIG` | `RelWithDebInfo` | configuration of the fork build whose libraries the tests link |

### Without Visual Studio

`tools/toolchain.py` downloads a toolchain for the modules (447 MB, ~930 MB
unpacked: llvm-mingw's clang 23 as `clang-cl`/`lld-link`/`llvm-lib`, the MSVC
14.44 runtime libraries and headers, the Windows SDK 10.0.26100 for x64),
checks every file against `tools/toolchain-windows.json` and unpacks it.
Microsoft's files come from Microsoft's servers under Microsoft's licence
(the Visual Studio Build Tools licence, linked in the lock file), which whoever
runs the tool accepts; nothing of Microsoft's is in this repository.

```
python -I tools/toolchain.py fetch tools/toolchain-windows.json <dir> --accept-microsoft-license
python -I tools/build-recomp.py --project <game> --toolchain <dir> [--jobs N]   # -> <game>/build/recomp-llvm
python -I tools/toolchain.py resolve tools/toolchain-windows.json                # maintainers: pin newer releases
```

`build-recomp.py --toolchain` compiles with `recomp/CMakeLists.txt`'s Release
flags, archives `gen_<module>.lib` and links the DLL, incrementally, with no
CMake or Ninja. Soul Sacrifice Delta's ten modules build in 3 min with 6 jobs
(MSVC: ~12 min); they load in the MSVC-built fork, and the differential check
found no mismatch.

## A release for players

A player who has a dump of the game, but no Git, Visual Studio or Python,
gets the game's own folder: one program to start, which builds the game
the first time. The release holds the game's front end, its files, and in
`setup/` the importer: Python (with Tk), this kit and the game's
`game.json`. It holds no game or firmware data.

```
python -I tools/build-vita3k.py --project <game> --games-only     # -> <game>/build/vita3k-games
python -I tools/make-setup.py build tools/setup-windows.json --project <game> ^
       --bin <game>/build/vita3k-games/bin/RelWithDebInfo --out <dir>/<id> --zip [--archive <path>]
```

On Windows the folder carries the VC++ runtime DLLs beside `<id>.exe`, from
the build machine's Visual Studio: the executable has to start before
anything is downloaded.

The front end (the fork's `game-frontend/importer.cpp`) starts the
importer, `setup/kit/tools/setup.py`, when the game isn't built yet,
or when asked (`--install`), and waits for it. With no parts given, the
importer opens a window (`setupgui.py`). It asks for the dump's parts (the
game, its update and add-ons, with their licences; `dumpinfo.py` finds
what a dump already holds) and, on Windows, Microsoft's licence. The
front end's `--dump <file>` instead passes the parts on for the console
flow. Either way, the importer then:

1. downloads the firmware from Sony (`fetch-firmware.py`) and the toolchain
   (`toolchain.py`) into `setup/downloads` (`--cache <dir>`: elsewhere, kept);
   a file found in an `--offline <folder|file>` or the game folder's
   `offline/` (same size and SHA-256, any name: `offline.py`) is taken
   from there instead, so with a folder `--download-only` made the setup
   needs no network;
2. installs the game, runs it once (hidden and silent: the front end's
   `--dump-elfs`) to write the modules as loaded,
   recompiles them and compiles them with the toolchain, in the game's own
   folder (`install-game.py --bin <folder> --out <folder> --toolchain`;
   `make-dist.py` leaves the running front end's files alone);
3. deletes its downloads (unless `--cache`), its work files
   (`setup/work`) and the modules as loaded; if asked, it adds a desktop
   shortcut, a menu entry, a Steam entry or a copy in `~/Applications`.

Offline: `setup.py --download-only <dir> [--for windows,linux,macos,android|all]`
(the front end's `--download-only` passes it on) fetches everything the
setup would download, for this platform or the ones named, into `<dir>`
with `offline.json` (file, size, SHA-256, URL), and builds nothing. The
licences are accepted as for a build (`--accept-microsoft-license`,
`--accept-android-sdk-license`): the player downloads Sony's, Microsoft's
and Google's files themselves, and nothing is redistributed. Run again,
it fetches only what is missing.

The window's Play (or exit code 0) hands back to the front end, which boots
the game in the same process. Everything stays in the game's folder: the
front end takes every path from it (`--game <folder>`, else the
executable's).

`make-setup.py` refuses to write a release that holds anything that looks
like game data (ELFs, pkgs, PUPs, licences, recompiled modules, saves,
logs).

### Linux (and the Steam Deck)

The fork builds the games' front ends on Linux as `<id>.x86_64`
(`build-vita3k.py` on Linux: the games only, GCC, libstdc++ linked in; run
it with Python 3.12 or newer). Built in the Steam Runtime 3 (sniper) SDK,
they need glibc 2.30, zlib and Vulkan, nothing else. The modules are
`ssd_aot_<module>.so`.

The toolchain is zig alone (`tools/toolchain-linux.json`, 57 MB): clang with
libc++ and glibc's link stubs, MIT and Apache licences, nothing to accept.
`toolchain.py fetch tools/toolchain-linux.json <dir>` unpacks it, and
`build-recomp.py --toolchain <dir>` and the setup compile with it (target
`x86_64-linux-gnu.2.30`, baseline CPU; each module exports only
`armrecomp_aot_init`).

```
python3.12 -I tools/build-vita3k.py --project <game> --target <id>   # -> <game>/build/vita3k-games
python3.12 -I tools/make-setup.py build tools/setup-linux.json --project <game> \
       --bin <game>/build/vita3k-games/bin/RelWithDebInfo --out <dir>/<id> --zip [--archive <path>]
python3.12 -I tools/make-setup.py resolve-linux tools/setup-linux.json <tag>   # maintainers
```

The Linux release is a `.tar.gz`: `<id>.x86_64` (its debug information
stripped) and `<id>.png`, and in `setup/` python-build-standalone's
CPython 3.12 (Tk included, for the same window) with the wheels and the
kit. It works as on Windows. The game's folder gets
`<id>.desktop`; `--shortcut` puts it in the applications menu and on the
desktop too, and `--steam` (ticked by default in the window) adds the game
to the running Steam client as a non-Steam game, the way SteamOS's "Add to
Steam" does (`steam://addnonsteamgame/<the .desktop file>`, accepted while
`/tmp/addnonsteamgamefile` exists): Steam takes its name, executable, start
folder and icon, and on a Steam Deck the game starts from Game Mode.
Without Steam running it says so; "Add a Non-Steam Game" then does the
same. Soul Sacrifice Delta on a Steam Deck: 20 min from the dump to the
playable folder, 14 of them compiling (6 jobs).

### macOS (Apple Silicon)

The fork builds the games' front ends on macOS as `<id>.arm64`
(`build-vita3k.py` on macOS: the games only, Apple's clang from Xcode or
the Command Line Tools, OpenSSL and Boost built from source, minimum macOS
13.3), next to `libMoltenVK.dylib`. The modules are
`ssd_aot_<module>.dylib`.

The toolchain is zig, as on Linux (`tools/toolchain-macos.json`, 51 MB):
target `aarch64-macos.13.3`, CPU `apple_m1`. Its linker signs each module
ad hoc, which is all Apple Silicon needs to load it. It takes no export
list, so hidden visibility leaves `armrecomp_aot_init` and libc++'s
`operator new`/`delete` exported (two-level namespaces keep them inside).

```
python3.12 -I tools/build-vita3k.py --project <game> --target <id>   # -> <game>/build/vita3k-games
python3.12 -I tools/make-setup.py build tools/setup-macos.json --project <game> \
       --bin <game>/build/vita3k-games/bin/RelWithDebInfo --out <dir>/<id> --zip [--archive <path>]
python3.12 -I tools/make-setup.py resolve-macos tools/setup-macos.json <tag>   # maintainers
```

The macOS release is a `.tar.gz`: `<name>.app`, which runs `<id>.arm64`
itself (debug map stripped; `libMoltenVK.dylib` beside it in
`Contents/MacOS`; the game's icon in a macOS icon's shape; the bundle
signed ad hoc as a whole; `SDL_FILESYSTEM_BASE_DIR_TYPE = parent`, so the
game's folder is the one holding the app; Game Mode in fullscreen), and in
`setup/` python-build-standalone's CPython 3.12 for `aarch64-apple-darwin`
(Tk included) with the wheels and the kit. Nothing is signed with a
Developer ID: the player lets the app through once (System Settings,
Privacy & Security, Open Anyway). Gatekeeper then runs it from a
translocated copy; the front end clears the download's quarantine from its
folder and starts again from there. `--shortcut` copies the app to
`~/Applications`, pointed at the game's folder (`LSEnvironment`
`VITA_RECOMPS_GAME_DIR`). Soul Sacrifice Delta on an M5: 5.6 min from the
dump to the game, downloads included.
