# Run a test binary with a memory and time ceiling (a runaway test must not take the machine down).
#   .\recomp\tests\guarded-run.ps1 <exe> <log> [args...]
param([string]$Exe, [string]$Log, [int]$MaxMB = 3072, [int]$MaxMinutes = 20, [Parameter(ValueFromRemainingArguments)] [string[]]$Rest)
$p = Start-Process -FilePath $Exe -ArgumentList $Rest -RedirectStandardOutput $Log -NoNewWindow -PassThru
$sw = [Diagnostics.Stopwatch]::StartNew()
while (-not $p.HasExited) {
    Start-Sleep -Milliseconds 500
    try { $p.Refresh(); $mb = $p.WorkingSet64 / 1MB } catch { break }
    if ($mb -gt $MaxMB) { Stop-Process -Id $p.Id -Force; "KILLED: $([int]$mb) MB" | Add-Content $Log; exit 2 }
    if ($sw.Elapsed.TotalMinutes -gt $MaxMinutes) { Stop-Process -Id $p.Id -Force; 'KILLED: time limit' | Add-Content $Log; exit 3 }
}
$p.WaitForExit()
exit $p.ExitCode
