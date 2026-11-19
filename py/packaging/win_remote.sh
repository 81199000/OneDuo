#!/bin/zsh
# 【开发用】把当前 py/ 和 catalog/ 同步到 Windows 测试机（C:\onetap\src），然后在那边跑一段 PowerShell。
#   ./packaging/win_remote.sh sync                  只同步
#   ./packaging/win_remote.sh run '<powershell>'    同步后在 C:\onetap\src\py 里跑
#   ./packaging/win_remote.sh exec '<powershell>'   不同步直接跑
# 依赖：Mac 能免密 ssh 到那台 Windows（export ONETAP_WIN=用户@IP）
set -e
W=${ONETAP_WIN:?先 export ONETAP_WIN=用户@Windows机器IP}
SSHO=(-4 -o BatchMode=yes -o ConnectTimeout=8)
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
S=${TMPDIR:-/tmp}

ps_run() {
  # UTF-8 输出，去掉 PowerShell 的 CLIXML 进度噪音
  local body="\$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; \$OutputEncoding=[Text.Encoding]::UTF8; $1"
  local b64=$(printf '%s' "$body" | iconv -f utf-8 -t utf-16le | base64)
  ssh "${SSHO[@]}" "$W" "powershell -NoProfile -NonInteractive -EncodedCommand $b64" 2>&1 \
    | LC_ALL=C tr -d '\r' | LC_ALL=C grep -av -e '#< CLIXML' -e '<Objs ' -e '</Objs>'
}

sync() {
  (cd "$ROOT" && rm -f "$S/onetap-src.zip" && zip -qr "$S/onetap-src.zip" py catalog -x 'py/.venv/*' 'py/build/*' 'py/dist/*' '*/__pycache__/*' 'py/packaging/OneTap.iconset/*')
  [ -f "$ROOT/target/x86_64-pc-windows-gnu/release/plumesign.exe" ] && (cd "$ROOT" && zip -qj "$S/onetap-plume.zip" target/x86_64-pc-windows-gnu/release/plumesign.exe)
  scp "${SSHO[@]}" -q "$S/onetap-src.zip" "$W:C:/onetap/src.zip"
  [ -f "$S/onetap-plume.zip" ] && scp "${SSHO[@]}" -q "$S/onetap-plume.zip" "$W:C:/onetap/plume.zip"
  ps_run "New-Item -ItemType Directory -Force C:\\onetap\\src\\target\\release | Out-Null; Get-ChildItem C:\\onetap\\src -Exclude target | Where-Object { \$_.Name -ne 'py' } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; Get-ChildItem C:\\onetap\\src\\py -Exclude .venv,build,dist -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force; Expand-Archive -Force C:\\onetap\\src.zip C:\\onetap\\src; if (Test-Path C:\\onetap\\plume.zip) { Expand-Archive -Force C:\\onetap\\plume.zip C:\\onetap\\src\\target\\release }; 'synced'"
}

case "$1" in
  sync) sync ;;
  run) sync >/dev/null; ps_run "Set-Location C:\\onetap\\src\\py; $2" ;;
  exec) ps_run "$2" ;;
  *) echo "用法: $0 sync | run '<ps>' | exec '<ps>'"; exit 1 ;;
esac
