# vita-recomp

A shared toolchain for the static recompilation of PS Vita games: it turns a
game's decrypted ARM (Thumb-2, ARM, VFP, NEON) code into C++, builds that into
DLLs a Vita3K fork loads in place of its JIT, and holds the tools used to study
the binaries and drive test runs. It is used, as a git submodule, by two game
ports: ss-delta (Soul Sacrifice Delta) and shinobido-2 (Shinobido 2: Revenge
of Zen). The history comes from ss-delta, where it was written; comments that
cite `docs/...` (decisions D6-D11, plan phases) refer to ss-delta's documents.

**No game data is in this repository**, and none may be committed: no ELFs,
pkgs, licences (work.bin, .rif, zRIF), firmware, generated C++, screenshots,
recordings or logs. Each game project keeps those in its own ignored
directories and passes their paths in.

**Licence:** not chosen yet (the owner's decision); there is no LICENSE file.

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
- `python -I tools/v3kdrive.py <dir> launch|start|run|bootseq|title|close ... [--exe NAME] [--title PREFIX] [--player N]`: without `--exe`, the one known front end (`ss-delta.exe`, `shinobido2.exe`) in `<dir>`, else `ss-delta.exe`
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
