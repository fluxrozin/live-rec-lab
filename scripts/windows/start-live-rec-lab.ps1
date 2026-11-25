# Windowsサインイン時にWSL2のLiveRecorder（通常運用）を非対話で起動する。
# 登録はタスクスケジューラ（ログオン時）を利用者が手動で行う。手順は docs/manual/README.md を参照。
param(
  [string]$Distro = "Ubuntu",
  [string]$RepoPath = "~/projects/live-rec-lab",
  [string]$Command = "./scripts/start-live-rec-lab.sh"   # 動作確認用に差し替え可能
)
$ErrorActionPreference = "Stop"
# 空白を含む引数は1つの文字列として明示的に引用する（Start-Process の配列渡しは引用しない）
$bash = "cd $RepoPath && $Command"
$argString = "-d $Distro --exec bash -lc `"$bash`""
Start-Process -WindowStyle Hidden -FilePath "wsl.exe" -ArgumentList $argString
