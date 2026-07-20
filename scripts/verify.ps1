$ErrorActionPreference = "Stop"

Write-Host "[1/3] Backend tests"
Push-Location "$PSScriptRoot\..\backend"
try {
    $baseTemp = ".pytest_tmp_$((Get-Date).ToString('yyyyMMddHHmmssfff'))"
    $projectPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    if (Test-Path $projectPython) {
        & $projectPython -m pytest -q -p no:cacheprovider --basetemp $baseTemp
    }
    else {
        python -m pytest -q -p no:cacheprovider --basetemp $baseTemp
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Backend tests failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "[2/3] Frontend tests"
Push-Location "$PSScriptRoot\..\frontend"
try {
    npm.cmd run test
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend tests failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "[3/3] Frontend build"
Push-Location "$PSScriptRoot\..\frontend"
try {
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
