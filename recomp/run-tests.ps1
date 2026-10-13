# Regenerate, build and run the dynarmic comparison tests (per instruction and per function) for
# each module. Needs the decrypted ELFs (<ElfDir>/<module>.elf) and a build of the Vita3K fork
# (for its dynarmic), in an MSVC developer shell (or pass -DevShell <script> to dot-source one).
#   .\recomp\run-tests.ps1 -Root <game project> [-Modules eboot,libc] [-PerForm 20] [-Iterations 256]
#                          [-Functions 300] [-FunctionIterations 64]
# -Root is the game project's directory; the other paths default to its layout:
#   -ElfDir <Root>\extracted\elf          -GeneratedDir <Root>\generated     -BuildDir <Root>\build
#   -Vita3kSrc <Root>\third_party\vita3k  -Vita3kBuild <Root>\build\vita3k
#   -DevShell <Root>\tools\devshell.ps1 (when it exists)
# Without -Root, this repository's directory (the layout when recomp\ sat inside the game project)
# or its parent (a submodule) is used when it has extracted\elf\; otherwise -Root, or every path,
# must be given.
param(
    [string[]]$Modules = @('eboot', 'libc', 'libfios2', 'libult'),
    [int]$PerForm = 20,
    [int]$Iterations = 256,
    [int]$Functions = 300,
    [int]$FunctionIterations = 64,
    [string]$Root,
    [string]$ElfDir,
    [string]$GeneratedDir,
    [string]$BuildDir,
    [string]$Vita3kSrc,
    [string]$Vita3kBuild,
    [string]$DevShell
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Root) {
    # the old layout (recomp\ inside the game project), or this repository as a submodule of it
    foreach ($c in @($repo, (Split-Path -Parent $repo))) {
        if (Test-Path (Join-Path $c 'extracted\elf')) { $Root = $c; break }
    }
}
function Get-FullPath([string]$p) { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p) }
function Resolve-Arg([string]$value, [string]$name, [string]$sub) {
    if ($value) { return Get-FullPath $value }
    if (-not $Root) { throw "pass -$name (or -Root <game project>, whose $sub it then is)" }
    return Get-FullPath (Join-Path $Root $sub)
}
$ElfDir = Resolve-Arg $ElfDir 'ElfDir' 'extracted\elf'
$GeneratedDir = Resolve-Arg $GeneratedDir 'GeneratedDir' 'generated'
$BuildDir = Resolve-Arg $BuildDir 'BuildDir' 'build'
$Vita3kSrc = Resolve-Arg $Vita3kSrc 'Vita3kSrc' 'third_party\vita3k'
$Vita3kBuild = Resolve-Arg $Vita3kBuild 'Vita3kBuild' 'build\vita3k'
if (-not $DevShell -and $Root -and (Test-Path (Join-Path $Root 'tools\devshell.ps1'))) {
    $DevShell = Join-Path $Root 'tools\devshell.ps1'
}
foreach ($m in $Modules) {
    if (-not (Test-Path "$ElfDir\$m.elf")) { throw "no $ElfDir\$m.elf: pass -ElfDir (or -Root)" }
}
foreach ($p in @(@('Vita3kSrc', $Vita3kSrc), @('Vita3kBuild', $Vita3kBuild))) {
    if (-not (Test-Path $p[1] -PathType Container)) { throw "$($p[0]) $($p[1]) does not exist: pass -$($p[0]) (or -Root)" }
}
Push-Location $repo
try {
    if ($DevShell) { . $DevShell | Out-Null }
    elseif (-not $env:VCToolsVersion) { throw 'no MSVC environment: run in a developer shell, or pass -DevShell <script>' }
    $failed = @()
    foreach ($m in $Modules) {
        python -I recomp/make_tests.py "$ElfDir\$m.elf" "$GeneratedDir\tests\$m" --per-form $PerForm --functions $Functions
        if ($LASTEXITCODE) { throw "make_tests failed for $m" }
        $build = "$BuildDir\recomp-$m"
        $ErrorActionPreference = 'Continue'
        cmake -S recomp -B $build -G Ninja -DCMAKE_BUILD_TYPE=Release "-DRECOMP_MODULES=" "-DRECOMP_TEST_MODULE=$m" `
            "-DGENERATED_DIR=$GeneratedDir" "-DVITA3K_SRC=$Vita3kSrc" "-DVITA3K_BUILD=$Vita3kBuild" | Out-Null
        cmake --build $build --target recomp_tests | Select-String -Pattern ' error ' | Select-Object -First 5
        $ErrorActionPreference = 'Stop'
        # under a memory and time ceiling: a runaway native test once hung the machine
        & "$PSScriptRoot\tests\guarded-run.ps1" "$build\recomp_tests.exe" "$build\tests.txt" -Rest $Iterations, '*', $FunctionIterations
        $code = $LASTEXITCODE
        Get-Content "$build\tests.txt" | Select-Object -Last 12
        if ($code) { $failed += $m }
    }
    if ($failed) { Write-Host "failing: $($failed -join ', ')"; exit 1 }
    Write-Host 'all modules pass'
} finally {
    Pop-Location
}
