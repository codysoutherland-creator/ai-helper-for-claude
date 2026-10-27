# Restart an isolated headless TEST server with the current mod code. Never touches the player's real game.
# The Factorio binary comes from $env:FACTORIO_BIN / $env:FACTORIO_EXE, else Steam/standalone discovery (platforms.py).
#   powershell -NoProfile -ExecutionPolicy Bypass -File testenv/restart.ps1                 # default instance
#   powershell -NoProfile -ExecutionPolicy Bypass -File testenv/restart.ps1 -Name foreman -Slot 3
# Slot N => RCON 27016+N, UDP 34500+N, game port 34199+N. Default slot 0 = RCON 27016, UDP 34500.
# -Stop just stops that instance. State resets on every restart (fresh copy of the save).
# -Save <file> starts from another save in testenv\write (default test.zip), e.g. -Save seed_123.zip.
# Test instances get the mod's startup setting claude-debug-ops=true (test ops: chat_simulate, gui_click, ...) via a
# generated mod-settings.dat; -NoDebugOps starts one like a player's game. RCON is bound to 127.0.0.1 only.
param([string]$Name = "default", [int]$Slot = 0, [switch]$Stop, [string]$Save = "test.zip", [switch]$NoDebugOps)
$root = Split-Path -Parent $PSScriptRoot
$rootFwd = $root -replace '\\', '/'
$inst = "$root\testenv\inst\$Name"
$instFwd = $inst -replace '\\', '/'
$rcon = 27016 + $Slot; $udp = 34500 + $Slot; $gport = 34199 + $Slot
if (-not $Stop -and -not (Test-Path "$root\testenv\write\$Save")) { "save not found: testenv\write\$Save"; exit 1 }

Get-CimInstance Win32_Process -Filter "name='factorio.exe'" |
  Where-Object { ($_.CommandLine -like "*testenv/inst/$Name/*") -or (($Name -eq "default") -and ($_.CommandLine -like "*$rootFwd/testenv/write*")) } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
if ($Stop) { "stopped $Name"; exit 0 }
Start-Sleep -Seconds 2

New-Item -ItemType Directory -Force "$inst\write\saves", "$inst\mods" | Out-Null
Copy-Item -Force "$root\testenv\write\$Save" "$inst\write\$Save"
Copy-Item -Force "$root\testenv\mods\mod-list.json" "$inst\mods\mod-list.json"
Remove-Item -Recurse -Force "$inst\mods\claude-bridge" -ErrorAction SilentlyContinue
Copy-Item -Recurse "$root\mod\claude-bridge" "$inst\mods\claude-bridge"
$debugOps = if ($NoDebugOps) { "claude-debug-ops=false" } else { "claude-debug-ops=true" }
& "$root\.venv\Scripts\python.exe" "$root\testenv\mod_settings_dat.py" "$inst\mods\mod-settings.dat" $debugOps
@"
[path]
read-data=__PATH__executable__/../../data
write-data=$instFwd/write
"@ | Out-File -Encoding ascii "$inst\config.ini"

$exe = & "$root\.venv\Scripts\python.exe" -c "import sys; sys.path.insert(0, sys.argv[1]); from factorio_claude import platforms; print(platforms.find_factorio()[0] or '')" "$root"
if (-not $exe) { "factorio.exe not found: set FACTORIO_EXE"; exit 1 }
$argList = @("--config", "$instFwd/config.ini", "--mod-directory", "$instFwd/mods",
  "--start-server", "$instFwd/write/$Save",
  "--rcon-bind", "127.0.0.1:$rcon", "--rcon-password", "testpw", "--port", "$gport", "--enable-lua-udp", "$udp",
  "--server-settings", "$rootFwd/testenv/server-settings.json")
Remove-Item "$inst\server.log" -ErrorAction SilentlyContinue
Start-Process -FilePath $exe -ArgumentList $argList -RedirectStandardOutput "$inst\server.log" -WindowStyle Hidden
for ($i = 0; $i -lt 80; $i++) {
  Start-Sleep -Milliseconds 500
  if ((Test-Path "$inst\server.log") -and (Select-String -Path "$inst\server.log" -Pattern "Starting RCON|Error" -Quiet)) { break }
}
Start-Sleep -Milliseconds 500
Select-String -Path "$inst\server.log" -Pattern "Error|error" | Select-Object -First 8
"restarted $Name (rcon $rcon, udp $udp) log: $inst\server.log"
