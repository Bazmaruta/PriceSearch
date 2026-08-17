# Runs 4 sequential batches (10,11,12,13) detached so the shell timeout can't kill them.
# Each invocation is a separate `python run_canonical_batch.py --verbose` (test mode = 1 batch each).
$ErrorActionPreference = "Continue"
$log = "C:\Users\vprad\Agents\PriceSearch\Search2.0\DataSeo\logs\batches10-13_console.log"
$wd = "C:\Users\vprad\Agents\PriceSearch\Search2.0\DataSeo"
foreach ($i in 10..13) {
    "===== BATCH $i START $(Get-Date -Format 'HH:mm:ss') =====" | Out-File $log -Append -Encoding utf8
    & python run_canonical_batch.py --verbose 2>&1 | Out-File $log -Append -Encoding utf8
    "===== BATCH $i END $(Get-Date -Format 'HH:mm:ss') exit=$LASTEXITCODE =====" | Out-File $log -Append -Encoding utf8
}
"ALL DONE $(Get-Date -Format 'HH:mm:ss')" | Out-File $log -Append -Encoding utf8
