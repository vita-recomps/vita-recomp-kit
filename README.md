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
| `tools/leak-scan.py` | pre-push hook (`install <repo>`) or `check <range>`: blocks game-data files, big files, licence strings, tokens, user folders and the owner's private patterns (`git config leakscan.patterns <file>`, kept outside the repo) from reaching a remote |
| `tools/build-vita3k.py` | builds a game project's `third_party/vita3k` with MSVC + Ninja into `build/vita3k` (`msvc.py` finds MSVC with vswhere); `--games-only`: the single-game executables without Vita3K's Qt interface (no Qt needed) |
| `tools/build-recomp.py` | builds a game project's `generated/<module>/` into `ssd_aot_<module>.dll`: MSVC + CMake + Ninja, or `--toolchain <dir>` without any of them |
| `tools/toolchain.py`, `tools/toolchain-windows.json` | a compiler for the modules that needs no Visual Studio: llvm-mingw's clang-cl and lld-link with Microsoft's C++ runtime and Windows SDK downloaded from Microsoft's Visual Studio manifest, every file pinned by SHA-256 in the lock file; also the VC++ runtime DLLs a playable folder carries |
| `tools/install-game.py`, `tools/make-dist.py` | from the player's dump (firmware, game, patch, add-ons; pkgs with their licence) to a playable folder `dist/<name>/`, as the game's `game.json` describes it (`gameproject.py`); `--bin`, `--toolchain`, `--out` for a prebuilt front end, the downloaded toolchain and any folder |
| `tools/fetch-firmware.py` | the PS Vita firmware Vita3K needs (3.74 and the font package) from Sony's update servers, checked by SHA-256 |
| `tools/make-setup.py`, `tools/setup-windows.json`, `tools/setup.py` | a game's setup for players: Python's embeddable distribution (with capstone and PyYAML, pinned), the kit, the game's front end; `setup.py` runs on the player's PC (see below) |

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

## A setup for players

A player who has a dump of the game, but no Git, Visual Studio or Python,
gets a playable folder from a setup. The setup holds Python's embeddable
distribution, this kit and the game's front end, and no game or firmware
data.

```
python -I tools/build-vita3k.py --project <game> --games-only     # -> <game>/build/vita3k-games
python -I tools/make-setup.py build tools/setup-windows.json --project <game> ^
       --bin <game>/build/vita3k-games/bin/RelWithDebInfo --out <dir>/<game>-setup --zip
```

On the player's PC, `setup.bat` opens a window (`setupgui.py`, Tk from
python.org's `tcltk.msi`). The window asks for:
* the dump's parts: the game, its update and add-ons, with their licences;
* the playable folder;
* acceptance of Microsoft's licence.

With arguments (taken as `install-game.py` takes them), or with
`--console`, `setup.bat` runs in the console instead. Either way, it then:

1. downloads the firmware from Sony (`fetch-firmware.py`) and the toolchain
   (`toolchain.py`) into `%LOCALAPPDATA%\vita-recomps`, shared by every
   game's setup;
2. installs the game, runs it once to write the modules as loaded,
   recompiles them and compiles them with the toolchain (`install-game.py
   --bin --toolchain --out`);
3. lays out the playable folder with the VC++ runtime DLLs beside the
   executable (by default `<game id>` next to the setup's folder) and, if
   asked, puts a shortcut to it on the desktop.

The game's front end sits in the setup's `kit\game\` as the template the
playable folder is made from, out of sight. On its own it has no game
installed, so it isn't meant to be started.

`make-setup.py` refuses to write a setup that holds anything that looks
like game data (ELFs, pkgs, PUPs, licences, recompiled modules, saves,
logs).
