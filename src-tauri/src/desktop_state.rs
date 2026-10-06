//! desktop_state.rs — 桌面生命周期状态（RC.3）
//!
//! 固定控制目录 `%LOCALAPPDATA%\PathOrbit\ZhiZhang\` 中的
//! `desktop-state.json`：与数据地址无关的桌面状态（first_run_complete /
//! close_behavior / last_seen_version）。不进账本 DB、不进 bootstrap。
//!
//! 契约：
//!   - 全部 atomic write（temp + replace），绝不半写。
//!   - 文件缺失/损坏 → 安全默认值（close_behavior 未设置 = ASK），
//!     不 panic、不阻塞启动。
//!   - launch_at_login 真实状态以 OS 注册结果为准，不在此文件表达。

use std::path::PathBuf;

pub const CONTROL_DIR_SCHEMA_VERSION: u32 = 1;

/// close_behavior 的三个合法值（§7-9）。
pub const CLOSE_ASK: &str = "ask";
pub const CLOSE_BACKGROUND: &str = "background";
pub const CLOSE_EXIT: &str = "exit";

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct DesktopState {
    pub schema_version: u32,
    /// 真正 First Run 完成后置 true（RC.2 升级用户自动视为 true，见 adoption 逻辑）。
    #[serde(default)]
    pub first_run_complete: bool,
    /// None = 未设置（RC.2 升级用户 → ASK_ON_FIRST_CLOSE）。
    #[serde(default)]
    pub close_behavior: Option<String>,
    /// 升级后 What's New 只显示一次的游标。
    #[serde(default)]
    pub last_seen_version: Option<String>,
}

impl Default for DesktopState {
    fn default() -> Self {
        Self {
            schema_version: CONTROL_DIR_SCHEMA_VERSION,
            first_run_complete: false,
            close_behavior: None,
            last_seen_version: None,
        }
    }
}

/// 固定控制目录（永远不随 data_root 迁移）。
pub fn control_dir() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("USERPROFILE")
                .map(|p| PathBuf::from(p).join("AppData").join("Local"))
                .unwrap_or_else(|| PathBuf::from("."))
        });
    base.join("PathOrbit").join("ZhiZhang")
}

fn state_file() -> PathBuf {
    control_dir().join("desktop-state.json")
}

/// 读取桌面状态。缺失/损坏 → 默认值（close_behavior=None → ASK）。
pub fn load() -> DesktopState {
    read_from(&state_file())
}

pub fn read_from(path: &PathBuf) -> DesktopState {
    match std::fs::read_to_string(path) {
        Ok(text) => match serde_json::from_str::<DesktopState>(&text) {
            Ok(mut s) => {
                // schema 向前兼容：只接受当前已知 schema，未知更高版本按默认处理
                if s.schema_version > CONTROL_DIR_SCHEMA_VERSION {
                    DesktopState::default()
                } else {
                    s.schema_version = CONTROL_DIR_SCHEMA_VERSION;
                    s
                }
            }
            Err(_) => DesktopState::default(),
        },
        Err(_) => DesktopState::default(),
    }
}

/// 原子写入（temp + replace）。失败返回错误字符串，由调用方决定是否致命。
pub fn save(state: &DesktopState) -> Result<(), String> {
    write_to(&state_file(), state)
}

pub fn write_to(path: &PathBuf, state: &DesktopState) -> Result<(), String> {
    let dir = path
        .parent()
        .ok_or_else(|| "desktop-state 路径异常".to_string())?;
    std::fs::create_dir_all(dir).map_err(|e| format!("创建控制目录失败：{e}"))?;
    let mut tmp = path.clone();
    tmp.set_extension("json.tmp");
    let text = serde_json::to_string_pretty(state).map_err(|e| e.to_string())?;
    {
        use std::io::Write;
        let mut f = std::fs::File::create(&tmp).map_err(|e| format!("写临时文件失败：{e}"))?;
        f.write_all(text.as_bytes()).map_err(|e| e.to_string())?;
        f.sync_all().ok();
    }
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("原子替换 desktop-state 失败：{e}")
    })
}

/// close_behavior 解析：未设置/非法值 → Ask（RC.2 升级用户首次 × 询问）。
pub fn resolve_close_behavior(state: &DesktopState) -> &str {
    match state.close_behavior.as_deref() {
        Some(CLOSE_BACKGROUND) => CLOSE_BACKGROUND,
        Some(CLOSE_EXIT) => CLOSE_EXIT,
        _ => CLOSE_ASK,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_path(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "zhizhang-ds-test-{}-{}",
            std::process::id(),
            tag
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("desktop-state.json")
    }

    #[test]
    fn missing_file_yields_default_ask() {
        let p = tmp_path("missing");
        let s = read_from(&p);
        assert_eq!(s, DesktopState::default());
        assert_eq!(resolve_close_behavior(&s), CLOSE_ASK);
    }

    #[test]
    fn roundtrip_preserves_fields() {
        let p = tmp_path("roundtrip");
        let mut s = DesktopState::default();
        s.first_run_complete = true;
        s.close_behavior = Some(CLOSE_BACKGROUND.to_string());
        s.last_seen_version = Some("0.11.0-rc.3".into());
        write_to(&p, &s).unwrap();
        // 无残留临时文件
        assert!(!p.with_extension("json.tmp").exists());
        let back = read_from(&p);
        assert_eq!(back, s);
        assert_eq!(resolve_close_behavior(&back), CLOSE_BACKGROUND);
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[test]
    fn corrupt_file_yields_default_not_panic() {
        let p = tmp_path("corrupt");
        std::fs::write(&p, "{\"schema_version\":1,\"close_beha").unwrap();
        let s = read_from(&p);
        assert_eq!(resolve_close_behavior(&s), CLOSE_ASK);
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[test]
    fn illegal_close_behavior_falls_back_to_ask() {
        let mut s = DesktopState::default();
        s.close_behavior = Some("minimize".into());
        assert_eq!(resolve_close_behavior(&s), CLOSE_ASK);
    }

    #[test]
    fn future_schema_version_is_ignored_safely() {
        let p = tmp_path("future");
        std::fs::write(
            &p,
            r#"{"schema_version":99,"close_behavior":"exit","first_run_complete":true}"#,
        )
        .unwrap();
        let s = read_from(&p);
        assert_eq!(resolve_close_behavior(&s), CLOSE_ASK);
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }

    #[test]
    fn write_is_atomic_replace_not_half_write() {
        let p = tmp_path("atomic");
        let mut s = DesktopState::default();
        write_to(&p, &s).unwrap();
        let before = std::fs::read_to_string(&p).unwrap();
        s.close_behavior = Some(CLOSE_EXIT.into());
        write_to(&p, &s).unwrap();
        let after = std::fs::read_to_string(&p).unwrap();
        assert!(before.contains("\"close_behavior\": null"));
        assert!(after.contains("\"close_behavior\": \"exit\""));
        let _ = std::fs::remove_dir_all(p.parent().unwrap());
    }
}
