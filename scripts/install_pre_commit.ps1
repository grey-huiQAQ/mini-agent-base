# 安装 pre-commit 钩子（Windows）—— 由本脚本生成 .git/hooks/pre-commit
# 用法：powershell -ExecutionPolicy Bypass -File .\scripts\install_pre_commit.ps1
# 说明：
#   - .git/hooks/ 内的钩子不被 Git 跟踪，克隆新环境后需重新执行本脚本；
#   - 钩子采用纯 Python 启动器，不依赖 msys sh（Git for Windows 的 msys
#     解释器在受限环境/沙箱下可能无法创建信号管道而崩溃）；
#   - shebang 指向安装时解析出的“无空格绝对路径”原生 python（Windows 的
#     Git 按 shebang 直接 spawn 解释器，路径含空格或裸名 python 会失败）；
#   - 检查逻辑本体是跟踪文件 scripts/pre_commit_hook.py（进程内执行四项检查）；
#   - 本脚本只写入 .git/hooks/pre-commit，不修改任何项目文件。
$ErrorActionPreference = "Stop"

# 项目根目录 = scripts/ 的上一级
$root = Split-Path -Parent $PSScriptRoot
$hooksDir = Join-Path $root ".git\hooks"
$hookFile = Join-Path $hooksDir "pre-commit"

if (-not (Test-Path $hooksDir)) {
    throw "未找到 .git/hooks 目录（$hooksDir），请确认在 Git 仓库内运行。"
}

# ---- 解析可用的原生 python（shebang 不允许含空格的路径）----
$candidates = @()
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) { $candidates += $venvPy }
$cmdPy = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cmdPy) { $candidates += $cmdPy.Source }
$cmdPyLauncher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cmdPyLauncher) { $candidates += $cmdPyLauncher.Source }

$shebang = $null
foreach ($p in $candidates) {
    if ($p -and ($p -notmatch "[\s]")) {
        $shebang = "#!" + ($p.Replace("\", "/"))
        break
    }
}
if (-not $shebang) {
    # 兜底：找不到无空格路径时退回 PATH 查找（部分环境可用）
    $shebang = "#!python"
    Write-Warning "未找到无空格的 python 绝对路径，shebang 退回 '#!python'（依赖 PATH，可能无法被 Git 执行）"
}

$content = @"
$shebang
# -*- coding: utf-8 -*-
# 由 scripts/install_pre_commit.ps1 自动生成；请勿手工编辑。
# 提交前运行四项机械检查（scripts/pre_commit_hook.py），任一失败即阻止提交。
# 禁止使用 git commit --no-verify 绕过本钩子（见项目根 AGENTS.md）。
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.exit(runpy.run_path(str(ROOT / "scripts" / "pre_commit_hook.py"), run_name="__main__"))
"@

# 统一换行为 LF 并写入（shebang 首行必须是纯 LF，且不能带 BOM），编码 UTF-8 无 BOM
$content = $content -replace "`r`n", "`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($hookFile, $content, $utf8NoBom)

Write-Output "已生成：$hookFile"
Write-Output "shebang：$shebang"
Write-Output "---- 文件内容 ----"
Get-Content $hookFile -Encoding utf8
Write-Output "------------------"
Write-Output "完成：请在项目根目录执行 git commit 验证钩子生效。"
