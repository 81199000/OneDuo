# 一键签 Windows 打包（在 Windows 上跑）
#   powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1
# 产出：py\dist\OneDuo-<版本>-windows-x64-setup.exe
# 需要：Python 3.12（F:\Python312 或 PATH 里）、Inno Setup 6（C:\onetap\inno 或默认路径）、
#       ..\target\release\plumesign.exe（Mac 上交叉编译：cargo build --release -p plumesign --target x86_64-pc-windows-gnu）
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location (Join-Path $PSScriptRoot '..')       # py\

$ver = (Select-String -Path app\core.py -Pattern '^VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "== 一键签 $ver"

# 1. Python 环境
$py = @('F:\Python312\python.exe', (Get-Command python -ErrorAction SilentlyContinue).Source) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not (Test-Path .venv)) { & $py -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-win.txt pyinstaller

# 2. 签名组件
$plume = '..\target\release\plumesign.exe'
if (-not (Test-Path $plume)) { throw "缺 $plume（先在 Mac 上交叉编译）" }

# 3. 苹果手机驱动（从苹果官方 iTunes 安装包里抽出来，只取驱动）
$drv = 'packaging\drivers\AppleMobileDeviceSupport64.msi'
if (-not (Test-Path $drv)) {
  New-Item -ItemType Directory -Force packaging\drivers, $env:TEMP\itx | Out-Null
  $setup = "$env:TEMP\iTunes64Setup.exe"
  if (-not (Test-Path $setup) -or (Get-Item $setup).Length -lt 150MB) {
    Write-Host '   下载苹果官方 iTunes 安装包（只为抽取驱动）…'
    Invoke-WebRequest 'https://www.apple.com/itunes/download/win64' -OutFile $setup -UseBasicParsing
  }
  Start-Process $setup -ArgumentList "/extract `"$env:TEMP\itx`"" -Wait -WindowStyle Hidden
  Copy-Item "$env:TEMP\itx\AppleMobileDeviceSupport64.msi" $drv
}

# 3b. Bonjour（Wi‑Fi 发现手机用）：苹果官方 Bonjour Print Services 包里的 Bonjour64.msi
$bj = 'packaging\drivers\Bonjour64.msi'
if (-not (Test-Path $bj)) {
  $bs = "$env:TEMP\BonjourPSSetup.exe"
  Invoke-WebRequest 'https://download.info.apple.com/Mac_OS_X/061-8098.20100603.gthyu/BonjourPSSetup.exe' -OutFile $bs -UseBasicParsing
  if ((Get-AuthenticodeSignature $bs).SignerCertificate.Subject -notmatch 'Apple Inc') { throw 'Bonjour 安装包签名不是苹果' }
  throw "请把 BonjourPSSetup.exe 里的 Bonjour64.msi 解出来放到 $bj（Mac 上：bsdtar -xf BonjourPSSetup.exe）"
}

# 4. PyInstaller（spec 里 Windows 分支 → dist\一键签\）
Remove-Item -Recurse -Force build, 'dist\一键签' -ErrorAction SilentlyContinue
& .\.venv\Scripts\python.exe -m PyInstaller packaging\OneTap.spec --noconfirm --log-level WARN
if (-not (Test-Path 'dist\一键签\一键签.exe')) { throw 'PyInstaller 没产出 一键签.exe' }

# 5. Inno Setup
$iscc = @('C:\onetap\inno\ISCC.exe', "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw '没找到 Inno Setup 6（ISCC.exe）' }
& $iscc /Q "/DAppVersion=$ver" packaging\OneTap.iss
if ($LASTEXITCODE -ne 0) { throw "ISCC 失败 $LASTEXITCODE" }
$out = Get-Item "dist\OneDuo-$ver-windows-x64-setup.exe"
Write-Host ("✅ {0}  {1:N1} MB" -f $out.FullName, ($out.Length / 1MB))
