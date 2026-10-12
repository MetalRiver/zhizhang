param(
  [switch]$Precheck,
  [string]$KeyPath    = $env:ZHIZHANG_SIGNING_PRIVATE_KEY_PATH,
  [string]$PubKeyPath = $env:ZHIZHANG_SIGNING_PUBLIC_KEY_PATH
)
# ZhiZhang RC.7 签名构建脚本（<repo>\scripts\sign-build.ps1）
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sign-build.ps1 -Precheck `
#     -KeyPath <private.key> -PubKeyPath <public.key>
#       无密钥预检：不提示口令、不构建。校验路径、node、本地 Tauri CLI、
#       私钥格式（内存内，仅报布尔），并以 PATH 变量跑一次错误口令探针
#       确认签名阶段变量设置正确。
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sign-build.ps1 `
#     -KeyPath <private.key> -PubKeyPath <public.key>
#       正式签名构建：隐藏输入口令 → signer sign 探针 → tauri build → 校验 .sig。
#
# 私钥/公钥路径来源（二选一，禁止把个人绝对路径硬编码进仓库）：
#   -KeyPath / -PubKeyPath 参数，或环境变量
#   ZHIZHANG_SIGNING_PRIVATE_KEY_PATH / ZHIZHANG_SIGNING_PUBLIC_KEY_PATH。
#
# 变量契约（两阶段互斥，避免 signer sign 的参数冲突）：
#   探针阶段（signer sign）：仅 TAURI_SIGNING_PRIVATE_KEY_PATH + PASSWORD，
#                             清除 TAURI_SIGNING_PRIVATE_KEY。
#   构建阶段（tauri build）：仅 TAURI_SIGNING_PRIVATE_KEY(=原文件绝对路径)
#                             + PASSWORD，清除 TAURI_SIGNING_PRIVATE_KEY_PATH。
#   private.key 全程只读、不解码、不复制明文副本。
#
# .sig 格式：Tauri CLI 2.12.0 产出的是 Base64 字符串，需先解码再核对
#   minisign 结构（untrusted/trusted comment）与版本绑定，不能凭原始
#   文本含不含 untrusted comment 判失败。
#
# 口令：Read-Host -AsSecureString 隐藏输入，不落盘、不日志。
# 清理：try/finally 清除三个签名环境变量与探针产物。
# PS 5.1 兼容：全部 Join-Path 只用两个参数。
$ErrorActionPreference = 'Stop'

# ============ 路径计算（唯一事实源） ============
$repo      = Split-Path -Parent $PSScriptRoot
$keyPath   = $KeyPath
$pubPath   = $PubKeyPath
# 未提供时占位：让 Test-Path 安全判为 MISSING，而不是因空参数抛异常
if ([string]::IsNullOrWhiteSpace($keyPath)) { $keyPath = '(未提供 -KeyPath)' }
if ([string]::IsNullOrWhiteSpace($pubPath)) { $pubPath = '(未提供 -PubKeyPath)' }
$confPath  = Join-Path $repo 'src-tauri\tauri.conf.json'
$pkgPath   = Join-Path $repo 'package.json'
$tauriCli  = Join-Path $repo 'node_modules\@tauri-apps\cli\tauri.js'
$nsisDir   = Join-Path $repo 'src-tauri\target\release\bundle\nsis'
$appVer    = '1.0.0'

function Clear-SigningEnv {
  Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY -ErrorAction SilentlyContinue
  Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY_PATH -ErrorAction SilentlyContinue
  Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY_PASSWORD -ErrorAction SilentlyContinue
}

# 解码 .sig（Base64 → minisign 文本）并校验结构；返回布尔与版本绑定情况
function Test-SigFile([string]$sigPath) {
  if (-not (Test-Path -LiteralPath $sigPath)) { return $null }
  $raw = [IO.File]::ReadAllText($sigPath).Trim()
  try {
    $dec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($raw))
  } catch {
    return @{ ok = $false; version_bound = $false; note = 'Base64 解码失败' }
  }
  $ok = ($dec -match 'untrusted comment') -and ($dec -match 'trusted comment')
  $bound = $dec.Contains($appVer)
  return @{ ok = $ok; version_bound = $bound; note = $(if ($ok) { 'minisign 结构有效' } else { '缺少 minisign 结构' }) }
}

# ============ 无密钥预检（全部在口令输入之前） ============
Write-Host '--- 启动前路径解析 ---'
Write-Host ("  脚本目录 = {0}" -f $PSScriptRoot)
Write-Host ("  repo     = {0}" -f $repo)

$checks = @()
$checks += , @('minisign 私钥', $keyPath,
               (Test-Path -LiteralPath $keyPath -PathType Leaf))
$checks += , @('minisign 公钥', $pubPath,
               (Test-Path -LiteralPath $pubPath -PathType Leaf))
$checks += , @('tauri.conf.json', $confPath, (Test-Path -LiteralPath $confPath))
$checks += , @('package.json', $pkgPath, (Test-Path -LiteralPath $pkgPath))
$checks += , @('本地 Tauri CLI', $tauriCli, (Test-Path -LiteralPath $tauriCli))
foreach ($c in $checks) {
  Write-Host ("  [{0}] {1} -> {2}" -f
    $(if ($c[2]) { 'OK' } else { 'MISSING' }), $c[0], $c[1])
}

# 私钥格式（仅内存，只报布尔，绝不输出内容）
$keyB64Ok = $false
$keyHeadOk = $false
try {
  $keyB64 = [IO.File]::ReadAllText($keyPath).Trim()
  $keyBytes = [Convert]::FromBase64String($keyB64)
  $keyB64Ok = $true
  $keyHead = [Text.Encoding]::UTF8.GetString($keyBytes)
  $keyHeadOk = ($keyHead -match 'untrusted comment')
  $keyB64 = $null; $keyHead = $null; $keyBytes = $null
} catch { $keyB64Ok = $false }
Write-Host ("  [{0}] private.key 是合法 Base64" -f $(if ($keyB64Ok) { 'OK' } else { 'FAIL' }))
Write-Host ("  [{0}] 解码后含 minisign 头（不输出内容）" -f $(if ($keyHeadOk) { 'OK' } else { 'FAIL' }))

# node / 本地 CLI
$cliOk = $false; $nodeOk = $false; $cliOut = ''
$savedEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$nodeOut = & node --version 2>&1 | Out-String
if ($LASTEXITCODE -eq 0) { $nodeOk = $true }
if ($nodeOk) {
  $cliOut = & node $tauriCli --version 2>&1 | Out-String
  $cliOk = ($LASTEXITCODE -eq 0 -and $cliOut -match 'tauri-cli')
}
$ErrorActionPreference = $savedEap
Write-Host ("  [{0}] node --version => {1}" -f
  $(if ($nodeOk) { 'OK' } else { 'FAIL' }), $nodeOut.Trim())
Write-Host ("  [{0}] tauri --version => {1}" -f
  $(if ($cliOk) { 'OK' } else { 'FAIL' }), $cliOut.Trim())

# 端口 8787
$portCheck = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
Write-Host ("  [{0}] 端口 8787 空闲" -f $(if ($portCheck) { 'BUSY' } else { 'OK' }))

# 残留清理
foreach ($v in 'TAURI_SIGNING_PRIVATE_KEY', 'TAURI_SIGNING_PRIVATE_KEY_PATH',
               'TAURI_SIGNING_PRIVATE_KEY_PASSWORD') {
  if (Test-Path ("Env:\" + $v)) {
    Remove-Item ("Env:\" + $v) -ErrorAction SilentlyContinue
    Write-Host ("  已清理残留 " + $v)
  }
}
$strayProbe = @(Get-ChildItem $env:TEMP -Filter 'zz-*probe-*' -ErrorAction SilentlyContinue)
foreach ($s in $strayProbe) { Remove-Item -LiteralPath $s.FullName -Force }
if ($strayProbe.Count -gt 0) {
  Write-Host ("  已清理 {0} 个残留探针产物" -f $strayProbe.Count)
}

# 探针阶段变量机制验证（无密钥）：仅 PATH + 假口令，期望"口令错误"
$envProbeOk = $false
$envProbeDetail = '(skipped)'
if ($nodeOk -and $cliOk -and $keyB64Ok) {
  $probeFile = Join-Path $env:TEMP ('zz-envprobe-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.bin')
  [IO.File]::WriteAllBytes($probeFile, [Text.Encoding]::UTF8.GetBytes('zhizhang-env-probe'))
  $savedEap3 = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  Clear-SigningEnv
  $env:TAURI_SIGNING_PRIVATE_KEY_PATH = $keyPath
  $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = 'env-probe-not-a-real-password'
  $probeOut = & node $tauriCli signer sign $probeFile --app-version $appVer 2>&1 | Out-String
  $ErrorActionPreference = $savedEap3
  Clear-SigningEnv
  Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath ($probeFile + '.sig') -Force -ErrorAction SilentlyContinue
  $envProbeOk = ($probeOut -match 'password') -and ($probeOut -notmatch 'base64') `
    -and ($probeOut -notmatch 'not found') -and ($probeOut -notmatch 'cannot be used with')
  $envProbeDetail = $(if ($probeOut -match 'cannot be used with') { '参数互斥冲突——变量设置错误' }
    elseif ($probeOut -match 'password') { '达到口令校验层（PATH 变量被接受）' }
    elseif ($probeOut -match 'base64') { '密钥加载报 Base64 错误' }
    else { '未确认' })
}
Write-Host ("  [{0}] 探针阶段变量机制（仅 PATH+PASSWORD）：{1}" -f
  $(if ($envProbeOk) { 'OK' } else { 'FAIL' }), $envProbeDetail)

$failed = @($checks | Where-Object { -not $_[2] })
if ($failed.Count -gt 0 -or $portCheck -or -not $nodeOk -or -not $cliOk `
    -or -not $keyB64Ok -or -not $keyHeadOk -or -not $envProbeOk) {
  Write-Host 'PRECHECK-FAIL'
  exit 1
}

if ($Precheck) {
  Write-Host 'PRECHECK-OK（路径、node、CLI、端口、私钥格式、探针变量机制、无残留）'
  exit 0
}

# ============ 正式签名构建（try/finally 无条件清理） ============
$testFile = $null
$testSig = $null
try {
  Write-Host '请输入 minisign 私钥口令：'
  $secPass = Read-Host -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secPass)
  $passPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  if ([string]::IsNullOrEmpty($passPlain)) {
    Write-Host 'STOP: 口令不能为空'; exit 1
  }
  $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $passPlain

  # ---- 阶段 1：签名探针（仅 PATH 变量；清除 KEY 变量避免参数冲突）----
  Clear-SigningEnv
  $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $passPlain
  $env:TAURI_SIGNING_PRIVATE_KEY_PATH = $keyPath

  $testFile = Join-Path $env:TEMP ('zz-sign-probe-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.bin')
  [IO.File]::WriteAllBytes($testFile, [Text.Encoding]::UTF8.GetBytes('zhizhang-sign-probe'))
  $testSig = $testFile + '.sig'

  Set-Location $repo
  $savedEap2 = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  & node $tauriCli signer sign $testFile --app-version $appVer
  $probeExit = $LASTEXITCODE
  $ErrorActionPreference = $savedEap2

  if ($probeExit -ne 0 -or -not (Test-Path -LiteralPath $testSig)) {
    Write-Host ("STOP: 签名探针失败（exit={0}，.sig 未生成）——未启动正式构建" -f $probeExit)
    exit 1
  }
  $probeSig = Test-SigFile $testSig
  if (-not $probeSig -or -not $probeSig.ok) {
    Write-Host ("STOP: 探针 .sig 结构校验失败（{0}）——未启动正式构建" -f
      $(if ($probeSig) { $probeSig.note } else { '无法读取' }))
    exit 1
  }
  Write-Host ("签名探针通过（exit=0；.sig 结构有效；版本绑定={0}）" -f $probeSig.version_bound)

  # ---- 阶段 2：正式构建（仅 KEY 变量=原文件路径；清除 PATH 变量）----
  Clear-SigningEnv
  $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $passPlain
  $env:TAURI_SIGNING_PRIVATE_KEY = $keyPath

  Write-Host '正在构建签名安装包……'
  & node $tauriCli build --bundles nsis
  $buildExit = $LASTEXITCODE
  if ($buildExit -ne 0) {
    Write-Host ("BUILD-FAILED: tauri build 退出码 {0}" -f $buildExit)
    exit 1
  }

  # ---- 产物与签名校验 ----
  $setupExe = Get-ChildItem $nsisDir -Filter '*.exe' -ErrorAction SilentlyContinue |
    Select-Object -First 1
  $setupSig = Get-ChildItem $nsisDir -Filter '*.sig' -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if (-not $setupExe) { Write-Host 'BUILD-FAILED: NSIS 安装包未生成'; exit 1 }
  if (-not $setupSig) { Write-Host 'BUILD-FAILED: 安装包 .sig 未生成'; exit 1 }
  $sigInfo = Test-SigFile $setupSig.FullName
  if (-not $sigInfo -or -not $sigInfo.ok) {
    Write-Host ("BUILD-FAILED: 安装包 .sig 校验失败（{0}）" -f
      $(if ($sigInfo) { $sigInfo.note } else { '无法读取' }))
    exit 1
  }
  $sha = (Get-FileHash $setupExe.FullName -Algorithm SHA256).Hash.ToLower()
  Write-Host '--- 签名产物 ---'
  Write-Host ("  安装包:     {0} ({1} bytes)" -f $setupExe.Name, $setupExe.Length)
  Write-Host ("  签名文件:   {0} ({1} bytes)" -f $setupSig.Name, $setupSig.Length)
  Write-Host ("  .sig 结构:  {0}；版本绑定={1}" -f $sigInfo.note, $sigInfo.version_bound)
  Write-Host ("  SHA256:     {0}" -f $sha)
  Write-Host '--- SIGN-BUILD-OK ---'
} finally {
  Clear-SigningEnv
  if ($testSig -and (Test-Path -LiteralPath $testSig)) {
    Remove-Item -LiteralPath $testSig -Force -ErrorAction SilentlyContinue
  }
  if ($testFile -and (Test-Path -LiteralPath $testFile)) {
    Remove-Item -LiteralPath $testFile -Force -ErrorAction SilentlyContinue
  }
  $passPlain = $null
}
