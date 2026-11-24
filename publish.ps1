# 发布到 Comfy Registry（Windows PowerShell）
#
# 用法：
#   .\publish.ps1 -PublisherId 你的发布者ID
#   .\publish.ps1 -PublisherId 你的发布者ID -Token "pat-xxxxxxxx"
#
# -Token 不填时会读环境变量 COMFY_REGISTRY_TOKEN；再没有就由 comfy-cli 交互式询问。
# 发布前请确认：pyproject.toml 里 name 已定稿（创建后不可改）、version 已递增（语义化版本）。

param(
    [Parameter(Mandatory = $true)][string]$PublisherId,
    [string]$Token = $env:COMFY_REGISTRY_TOKEN,
    [string]$RepoUrl = "",
    [switch]$SkipPublish
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$toml = Join-Path $root "pyproject.toml"
if (-not (Test-Path -LiteralPath $toml)) { throw "找不到 pyproject.toml：$toml" }

# 1) 写回 PublisherId / Repository
$text = [System.IO.File]::ReadAllText($toml)
$text = [regex]::Replace($text, '(?m)^PublisherId = .*$', "PublisherId = `"$PublisherId`"")
if ($RepoUrl) { $text = [regex]::Replace($text, '(?m)^Repository = .*$', "Repository = `"$RepoUrl`"") }
[System.IO.File]::WriteAllText($toml, $text, [System.Text.UTF8Encoding]::new($false))
Write-Host "pyproject.toml 已更新：PublisherId = $PublisherId" -ForegroundColor Green

# 2) 版本与名称自检
$version = ([regex]::Match($text, '(?m)^version = "([^"]+)"')).Groups[1].Value
$name = ([regex]::Match($text, '(?m)^name = "([^"]+)"')).Groups[1].Value
Write-Host "name = $name / version = $version"
if ($name -match '[^a-z0-9\-_]') { throw "name 只能用小写字母、数字、- 和 _：$name" }

# 3) 本地节点自检（可选，失败不阻塞发布）
$py = Join-Path $root "selftest_local.py"
if (Test-Path -LiteralPath $py) {
    try { & python $py } catch { Write-Host "本地自检跳过：$($_.Exception.Message)" -ForegroundColor Yellow }
}

if ($SkipPublish) { Write-Host "只改了元数据，没发布（-SkipPublish）"; exit 0 }

# 4) 发布
if (-not (Get-Command comfy -ErrorAction SilentlyContinue)) {
    Write-Host "没装 comfy-cli，先装：pip install comfy-cli" -ForegroundColor Yellow
    exit 1
}
Push-Location $root
try {
    if ($Token) { comfy node publish --token $Token } else { comfy node publish }
} finally {
    Pop-Location
}
