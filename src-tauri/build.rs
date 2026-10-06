fn main() {
    // RC.3：注册应用命令 ACL 权限（allow-<cmd>），供远端 UI（127.0.0.1:8787）
    // capability 按最小集授权；本地窗口不受影响。
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
            "open_data_root",
            "resolve_close_choice",
            "get_desktop_state",
            "set_close_behavior",
            "set_autostart",
            "get_boot_info",
            "retry_recovery",
            "complete_first_run",
            "pick_folder",
            "set_data_root",
            "migrate_data_root_plan",
            "migrate_data_root_execute",
            "check_for_update",
            "download_and_install_update",
        ])),
    )
    .expect("failed to run tauri-build");
}
