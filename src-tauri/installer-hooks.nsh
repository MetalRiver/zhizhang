; 智账 · PathOrbit AI Ledger — Tauri NSIS installer hooks
; 旧品牌（AI 用量账本 / 0.10.0-rc.1 及更早）升级迁移：
;   - 旧 productName 卸载注册表键删除（避免 Apps & Features 双身份）
;   - 旧 Start Menu / Desktop 快捷方式删除
;   - 旧安装目录的程序文件保留不删（用户可自行清理；账本数据
;     %LOCALAPPDATA%\UsageLedger 与安装目录无关，永不受影响）
; 全部操作幂等：键/快捷方式不存在时为空操作。

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "智账：迁移旧品牌（AI 用量账本）安装身份…"
  ; 旧卸载注册表键（Tauri NSIS 以 productName 作为键名）
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AI 用量账本"
  ; 旧快捷方式（Start Menu / Desktop）
  Delete "$SMPROGRAMS\AI 用量账本.lnk"
  Delete "$SMPROGRAMS\AI 用量账本\*.*"
  RMDir "$SMPROGRAMS\AI 用量账本"
  Delete "$DESKTOP\AI 用量账本.lnk"
  DetailPrint "智账：旧品牌安装身份迁移完成。"
!macroend

; RC.3：卸载必须清理开机启动注册（§76），但绝不触碰：
;   - %LOCALAPPDATA%\UsageLedger（默认账本数据）
;   - 自定义 data_root（bootstrap.json 指向的用户账本）
;   - %LOCALAPPDATA%\PathOrbit\ZhiZhang（bootstrap pointer / desktop-state，
;     按产品策略与用户账本一起保留，供重装后安全重连）
!macro NSIS_HOOK_POSTUNINSTALL
  DetailPrint "智账：清理开机启动注册…"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "智账"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "usage-ledger-desktop"
  DetailPrint "智账：账本数据保留在原位置，未删除。"
!macroend
