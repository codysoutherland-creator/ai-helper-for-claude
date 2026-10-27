# AI Helper (for Claude) - one-line installer for Windows. Unofficial; not affiliated with Anthropic.
#
#   irm https://raw.githubusercontent.com/codysoutherland-creator/ai-helper-for-claude/main/install.ps1 | iex
#
# Running it again updates the app. Needs no admin rights, no git and no system Python: it downloads the source,
# gets Python through uv (installed to %USERPROFILE%\.local\bin if missing), makes a private venv and hands over to
# install.py (mod junction, mod-list, config, Claude Code skill + MCP server, the desktop shortcut).
#
# Environment overrides:
#   AIHELPER_HOME     install folder (default %LOCALAPPDATA%\ai-helper-for-claude)
#   AIHELPER_SOURCE   install from a local folder, .zip or .tar.gz instead of downloading (CI, testing)
#   AIHELPER_ZIP      download this .zip URL instead (e.g. while the repository is private)
#   AIHELPER_REF      branch or tag to download (default: the latest release, else main)
#   AIHELPER_PYTHON   Python version for the venv (default 3.14)
#   AIHELPER_UV       an existing uv.exe to use
# Run as a file, arguments go to install.py:  powershell -ExecutionPolicy Bypass -File install.ps1 --check

function Install-AIHelper {
    param([string[]]$InstallerArgs = @())

    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Windows PowerShell 5.1 downloads very slowly with a progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
        [Net.SecurityProtocolType]::Tls12

    $repo = 'codysoutherland-creator/ai-helper-for-claude'
    $uvVersion = '0.12.24'
    $pyVersion = if ($env:AIHELPER_PYTHON) { $env:AIHELPER_PYTHON } else { '3.14' }
    # never part of an update: the user's own files in the install folder
    $keep = @('config.json', '.venv', 'logs', 'chat-session', 'plans', 'blueprints', 'foreman-notes.md')
    $devOnly = @('.git', '.venv', 'config.json', 'logs', 'chat-session', 'plans', 'blueprints', 'testenv',
        'foreman-notes.md', 'dist', 'build', '__pycache__', '.pytest_cache', 'MANIFEST')

    function Say([string]$msg) { Write-Host $msg }

    function Copy-Tree([string]$from, [string]$to, [string[]]$exclude) {
        New-Item -ItemType Directory -Force -Path $to | Out-Null
        foreach ($item in Get-ChildItem -LiteralPath $from -Force) {
            if ($exclude -contains $item.Name) { continue }
            $target = Join-Path $to $item.Name
            if ($item.PSIsContainer) { Copy-Tree $item.FullName $target $exclude }
            else { Copy-Item -LiteralPath $item.FullName -Destination $target -Force }
        }
    }

    function Get-LatestRef {
        # /releases/latest redirects to /releases/tag/<tag> (no API call, no rate limit); no release yet -> main
        try {
            $req = [Net.HttpWebRequest]::Create("https://github.com/$repo/releases/latest")
            $req.Method = 'HEAD'
            $req.AllowAutoRedirect = $false
            $resp = $req.GetResponse()
            $loc = $resp.Headers['Location']
            $resp.Close()
            if ($loc -match '/releases/tag/([^/?#]+)$') { return $Matches[1] }
        } catch { }
        return 'main'
    }

    function Get-Source([string]$stage) {
        # -> the folder that holds the app's files (the repository root)
        $src = $env:AIHELPER_SOURCE
        if ($src) {
            if (Test-Path -LiteralPath $src -PathType Container) {
                Say "Using the local folder $src"
                $out = Join-Path $stage 'src'
                Copy-Tree (Resolve-Path -LiteralPath $src).Path $out $devOnly
                return $out
            }
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { throw "AIHELPER_SOURCE=$src not found" }
            Say "Using the archive $src"
            $archive = (Resolve-Path -LiteralPath $src).Path
        } else {
            if ($env:AIHELPER_ZIP) { $url = $env:AIHELPER_ZIP }
            else {
                $ref = if ($env:AIHELPER_REF) { $env:AIHELPER_REF } else { Get-LatestRef }
                $kind = if ($ref -eq 'main' -or $ref -eq 'master') { 'heads' } else { 'tags' }
                $url = "https://codeload.github.com/$repo/zip/refs/$kind/$ref"
            }
            if (-not $url.StartsWith('https://')) { throw "refusing a non-HTTPS download: $url" }
            Say "Downloading $url"
            $archive = Join-Path $stage 'source.zip'
            try { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive }
            catch { throw "download failed ($($_.Exception.Message)). Is the repository public? AIHELPER_SOURCE can point at a local copy." }
        }
        $x = Join-Path $stage 'x'
        New-Item -ItemType Directory -Force -Path $x | Out-Null
        if ($archive -like '*.zip') { Expand-Archive -LiteralPath $archive -DestinationPath $x -Force }
        else {
            & tar.exe -xzf $archive -C $x
            if ($LASTEXITCODE) { throw "couldn't unpack $archive" }
        }
        $top = @(Get-ChildItem -LiteralPath $x -Force)
        if ($top.Count -eq 1 -and $top[0].PSIsContainer) { return $top[0].FullName }
        return $x
    }

    function Update-Files([string]$src, [string]$dest) {
        if (-not (Test-Path -LiteralPath (Join-Path $src 'install.py'))) {
            throw "the download doesn't look like AI Helper (no install.py)"
        }
        $base = (Resolve-Path -LiteralPath $src).Path.TrimEnd('\') + '\'
        $files = @(Get-ChildItem -LiteralPath $src -Recurse -File -Force |
            ForEach-Object { $_.FullName.Substring($base.Length).Replace('\', '/') } |
            Where-Object { $_ -ne 'MANIFEST' } | Sort-Object)
        $utf8 = New-Object Text.UTF8Encoding $false
        [IO.File]::WriteAllLines((Join-Path $src 'MANIFEST'), [string[]]$files, $utf8)
        $old = Join-Path $dest 'MANIFEST'
        if (Test-Path -LiteralPath $old) {
            $now = @{}
            foreach ($f in $files) { $now[$f] = $true }
            foreach ($f in [IO.File]::ReadAllLines($old)) {
                if (-not $f -or $f.StartsWith('/') -or $f.Contains('..') -or $f.Contains(':')) { continue }
                if ($keep -contains ($f.Split('/')[0])) { continue }
                if (-not $now.ContainsKey($f)) {
                    Remove-Item -LiteralPath (Join-Path $dest $f.Replace('/', '\')) -Force -ErrorAction SilentlyContinue
                }
            }
        }
        Copy-Tree $src $dest @()
    }

    function Find-Uv {
        $cands = @($env:AIHELPER_UV, (Join-Path $env:USERPROFILE '.local\bin\uv.exe'))
        $cmd = Get-Command uv.exe -ErrorAction SilentlyContinue
        if ($cmd) { $cands += $cmd.Source }
        foreach ($c in $cands) { if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return $c } }
        return $null
    }

    function Install-Uv {
        $uv = Find-Uv
        if ($uv) { return $uv }
        $bin = Join-Path $env:USERPROFILE '.local\bin'
        Say "Installing uv $uvVersion (Python manager) to $bin"
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        $env:UV_UNMANAGED_INSTALL = $bin
        $env:UV_NO_MODIFY_PATH = '1'
        # a child PowerShell: the uv installer must not be able to end this session
        $ps = (Get-Process -Id $PID).Path
        & $ps -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/$uvVersion/install.ps1 | iex" |
            Out-Null
        Remove-Item Env:UV_UNMANAGED_INSTALL, Env:UV_NO_MODIFY_PATH -ErrorAction SilentlyContinue
        $uv = Join-Path $bin 'uv.exe'
        if (-not (Test-Path -LiteralPath $uv)) { throw "couldn't install uv" }
        return $uv
    }

    $script:AIHelperExit = 0
    # `irm | iex` runs in the user's own session: put back every environment variable this function sets
    $savedEnv = @{}
    foreach ($n in 'AIHELPER_UV', 'AIHELPER_BOOTSTRAP', 'UV_PYTHON_PREFERENCE') {
        $savedEnv[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
    }
    try {
        $dest = if ($env:AIHELPER_HOME) { $env:AIHELPER_HOME } else { Join-Path $env:LOCALAPPDATA 'ai-helper-for-claude' }
        Say 'AI Helper (for Claude) installer (unofficial; not affiliated with or endorsed by Anthropic)'
        Say "Install folder: $dest"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $dest = (Resolve-Path -LiteralPath $dest).Path

        # Windows locks files that are in use: the venv can't be updated while the companion or a launcher runs
        $venvDir = (Join-Path $dest '.venv') + '\'
        $busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
                $_.Path -and $_.Path.StartsWith($venvDir, [StringComparison]::OrdinalIgnoreCase) })
        if ($busy.Count) {
            throw 'AI Helper is running (the hidden companion stops about a minute after Factorio quits). Quit Factorio, wait a minute, then run this again.'
        }

        $stage = Join-Path ([IO.Path]::GetTempPath()) ('aihelper-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        try {
            $src = Get-Source $stage
            Update-Files $src $dest
        } finally {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }

        $uv = Install-Uv
        $vpy = Join-Path $dest '.venv\Scripts\python.exe'
        $env:UV_PYTHON_PREFERENCE = 'only-managed'
        $ok = $false
        if (Test-Path -LiteralPath $vpy) {
            & $vpy -c "import sys; sys.exit(0 if '%d.%d' % sys.version_info[:2] == '$pyVersion' else 1)"
            $ok = ($LASTEXITCODE -eq 0)
        }
        if (-not $ok) {
            Say "Setting up Python $pyVersion (private to this app)"
            & $uv python install --no-bin $pyVersion | Out-Null
            if ($LASTEXITCODE) { throw 'uv python install failed' }
            & $uv venv --clear --python $pyVersion (Join-Path $dest '.venv') | Out-Null
            if ($LASTEXITCODE) { throw 'uv venv failed' }
        }
        # install.py installs the pinned, hash-checked packages (requirements.lock) with this uv
        $env:AIHELPER_UV = $uv
        $env:AIHELPER_BOOTSTRAP = '1'
        & $vpy (Join-Path $dest 'install.py') @InstallerArgs
        $script:AIHelperExit = $LASTEXITCODE
        if ($script:AIHelperExit) {
            Say ''
            Say "Some steps failed (see above). Fix them and run the same command again; it's safe to repeat."
        } else {
            Say ''
            Say 'Update or repair any time by running the same one-line command again.'
        }
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
        $script:AIHelperExit = 1
    } finally {
        foreach ($n in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($n, $savedEnv[$n], 'Process') }
    }
}

Install-AIHelper -InstallerArgs $args
# run as a file (CI, -File): report the result as the exit code. Pasted through `irm | iex`: never close the window.
if ($PSCommandPath -and (Split-Path -Leaf $PSCommandPath) -eq 'install.ps1' -and $script:AIHelperExit) {
    exit $script:AIHelperExit
}
