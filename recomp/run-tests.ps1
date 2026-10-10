# Regenerate, build and run the dynarmic comparison tests (per instruction and per function) for
# each module (plan Phase 3). Needs the ELFs in extracted/elf/ and a Vita3K build
# (tools/build-vita3k.ps1).
#   .\recomp\run-tests.ps1 [-Modules eboot,libc] [-PerForm 20] [-Iterations 256] [-Functions 300]
#                          [-FunctionIterations 64]
param(
    [string[]]$Modules = @('eboot', 'libc', 'libfios2', 'libult'),
    [int]$PerForm = 20,
    [int]$Iterations = 256,
    [int]$Functions = 300,
    [int]$FunctionIterations = 64
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    . .\tools\devshell.ps1 | Out-Null
    $failed = @()
    foreach ($m in $Modules) {
        python -I recomp/make_tests.py "extracted/elf/$m.elf" "generated/tests/$m" --per-form $PerForm --functions $Functions
        if ($LASTEXITCODE) { throw "make_tests failed for $m" }
        $build = "build\recomp-$m"
        $ErrorActionPreference = 'Continue'
        cmake -S recomp -B $build -G Ninja -DCMAKE_BUILD_TYPE=Release "-DRECOMP_MODULES=" "-DRECOMP_TEST_MODULE=$m" | Out-Null
        cmake --build $build --target recomp_tests | Select-String -Pattern ' error ' | Select-Object -First 5
        $ErrorActionPreference = 'Stop'
        & "$build\recomp_tests.exe" $Iterations '*' $FunctionIterations | Tee-Object -FilePath "$build\tests.txt" | Select-Object -Last 12
        if ($LASTEXITCODE) { $failed += $m }
    }
    if ($failed) { Write-Host "failing: $($failed -join ', ')"; exit 1 }
    Write-Host 'all modules pass'
} finally {
    Pop-Location
}
