# Windows launcher, started by transcribe-windows.cmd at the root of the repository:
#   transcribe-windows C:\Users\me\Videos\2026-09-24 14-05-02.mkv
#
# First run: downloads into %LOCALAPPDATA%\obs-transcribe\ a portable Python, ffmpeg (unless
# ffmpeg and ffprobe are already on the PATH) and a prebuilt whisper.cpp: the CUDA build when an
# NVIDIA GPU is present, the CPU build otherwise. Nothing is installed system-wide and no
# administrator rights are needed; delete that directory to start over.
#
# Environment variables:
#   TRANSCRIBE_GPU=cuda|cpu   whisper.cpp build to use [cuda if an NVIDIA GPU is detected]
#
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with its progress bar

$PythonVersion = '3.14.10'
$FfmpegVersion = '9.0.2'
$WhisperRelease = 'b5454'                  # whisper.cpp v1.9.5

$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$FfmpegUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$FfmpegVersion/ffmpeg-$FfmpegVersion-essentials_build.zip"
$WhisperUrls = @{
    # CUDA 11.8 build: works with any NVIDIA driver from 2022 on, CUDA runtime included in the zip
    cuda = "https://github.com/ggml-org/whisper.cpp/releases/download/$WhisperRelease/whisper-bin-win-cuda-11.8.0-x64.zip"
    cpu  = "https://github.com/ggml-org/whisper.cpp/releases/download/$WhisperRelease/whisper-bin-x64.zip"
}

$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $env:LOCALAPPDATA 'obs-transcribe'

function Write-Log([string]$Message) {
    # stderr, so that "transcribe-windows --stdout video.mkv | claude -p ..." only pipes the Markdown
    [Console]::Error.WriteLine($Message)
}

function Get-Download([string]$Url, [string]$Dest) {
    Write-Log "Downloading $Url"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -fL --retry 3 -o $Dest $Url
        if ($LASTEXITCODE -ne 0) { throw "download failed: $Url" }
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Dest
    }
}

# Download a zip and extract it into $Dest, once. Return $Dest.
function Install-Zip([string]$Url, [string]$Dest) {
    if (Test-Path -LiteralPath $Dest) { return $Dest }
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    $zip = "$Dest.zip"
    $tmp = "$Dest.tmp"
    if (Test-Path -LiteralPath $tmp) { Remove-Item -Recurse -Force -LiteralPath $tmp }
    Get-Download $Url $zip
    New-Item -ItemType Directory -Path $tmp | Out-Null
    Write-Log "Extracting $(Split-Path -Leaf $zip)"
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($tar) {
        & $tar.Source -xf $zip -C $tmp
        if ($LASTEXITCODE -ne 0) { throw "extraction failed: $zip" }
    } else {
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp
    }
    Move-Item -LiteralPath $tmp -Destination $Dest
    Remove-Item -Force -LiteralPath $zip
    return $Dest
}

# Directory holding $Exe somewhere under $Dir.
function Find-ExeDir([string]$Dir, [string]$Exe) {
    $found = Get-ChildItem -LiteralPath $Dir -Recurse -Filter $Exe | Select-Object -First 1
    if (-not $found) { throw "$Exe not found in $Dir (delete that directory and try again)" }
    return $found.DirectoryName
}

try {
    # whisper.cpp
    $gpu = $env:TRANSCRIBE_GPU
    if (-not $gpu) {
        $adapters = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
        $gpu = if ($adapters -match 'NVIDIA') { 'cuda' } else { 'cpu' }
    }
    if (-not $WhisperUrls.ContainsKey($gpu)) { throw "TRANSCRIBE_GPU must be cuda or cpu (got `"$gpu`")" }
    $whisper = Install-Zip $WhisperUrls[$gpu] (Join-Path $Data "whisper.cpp-$WhisperRelease-$gpu")
    $whisperDir = Find-ExeDir $whisper 'whisper-cli.exe'

    # ffmpeg / ffprobe: the ones on the PATH if any (winget install Gyan.FFmpeg, ...), else a portable copy
    $ffmpegDir = $null
    if (-not ((Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) -and (Get-Command ffprobe.exe -ErrorAction SilentlyContinue))) {
        $ffmpeg = Install-Zip $FfmpegUrl (Join-Path $Data "ffmpeg-$FfmpegVersion")
        $ffmpegDir = Find-ExeDir $ffmpeg 'ffprobe.exe'
    }

    # Python: an embeddable copy, so that the Microsoft Store "python" alias or an old
    # system Python never gets in the way
    $pythonDir = Install-Zip $PythonUrl (Join-Path $Data "python-$PythonVersion")
    $python = Join-Path $pythonDir 'python.exe'
} catch {
    Write-Log "Error: $($_.Exception.Message)"
    exit 2
}

$env:PATH = (@(@($whisperDir, $ffmpegDir) | Where-Object { $_ }) + $env:PATH) -join ';'
& $python (Join-Path $Root 'transcribe.py') @args
exit $LASTEXITCODE
