$ErrorActionPreference = "Stop"

$ProjectPath = "C:\Users\tiwar\Downloads\naukri_automation_starter"
$LogPath = Join-Path $ProjectPath "auto-git-push.log"

Set-Location $ProjectPath

function Write-Log($Message) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') - $Message" |
        Out-File -FilePath $LogPath -Append -Encoding utf8
}

try {
    # Check that this is a Git repository
    git rev-parse --is-inside-work-tree 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "This folder is not a Git repository."
    }

    # Stop if a merge or rebase is unfinished
    if ((Test-Path ".git\MERGE_HEAD") -or
        (Test-Path ".git\rebase-merge") -or
        (Test-Path ".git\rebase-apply")) {
        throw "An unfinished merge or rebase needs attention."
    }

    # Check for actual file changes
    git status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "Could not check Git status."
    }

    $Changes = git status --porcelain

    if (-not $Changes) {
        Write-Log "No changes detected. Nothing to commit."
        exit 0
    }

    # Stage and commit changes
    git add -A
    if ($LASTEXITCODE -ne 0) {
        throw "Git add failed."
    }

    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    git commit -m "Project maintenance: $Timestamp"

    if ($LASTEXITCODE -ne 0) {
        throw "Git commit failed."
    }

    # Push the current branch
    $Branch = git branch --show-current
    if ($LASTEXITCODE -ne 0 -or -not $Branch) {
        throw "Could not determine the current branch."
    }

    git push origin $Branch
    if ($LASTEXITCODE -ne 0) {
        throw "Git push failed. Check authentication or remote changes."
    }

    Write-Log "Commit and push successful on branch $Branch."
}
catch {
    Write-Log "ERROR: $_"
    exit 1
}