[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [int]$PollSeconds = 5,
    [int]$MaxHours = 8
)

$ErrorActionPreference = "Continue"
$deadline = (Get-Date).AddHours($MaxHours)
$terminalStates = @("complete", "selection_complete", "error")

while ((Get-Date) -lt $deadline) {
    $project = $null
    $voice = $null
    try {
        $project = Invoke-RestMethod `
            -Uri "$BaseUrl/api/projects/$ProjectId" `
            -Method Get `
            -TimeoutSec 3
    }
    catch {
        # A dashboard restart is an expected part of readiness testing. Record
        # the gap and continue instead of terminating the sampler.
    }
    try {
        $voice = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8100/health" `
            -Method Get `
            -TimeoutSec 2
    }
    catch {
        # Voice is intentionally absent while parked and after cleanup.
    }

    $record = [ordered]@{
        timestamp = (Get-Date).ToUniversalTime().ToString("o")
        project_id = $ProjectId
        status = $project.status
        stage = $project.active_stage
        running = $project.running
        current_chapter = $project.current_gen_chapter
        generated_chapters = $project.generated_chapters
        mastered_chapters = $project.mastered_chapters
        voice_online = [bool]$voice
        model = $voice.model_loaded
        vram_used_gb = $voice.vram_used_gb
        vram_total_gb = $voice.vram_total_gb
    }
    $record | ConvertTo-Json -Compress | Add-Content `
        -LiteralPath $OutputPath `
        -Encoding utf8

    if (
        $project `
        -and -not $project.running `
        -and $terminalStates -contains [string]$project.status
    ) {
        break
    }
    Start-Sleep -Seconds ([math]::Max(1, $PollSeconds))
}
