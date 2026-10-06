//! bootstrap.rs — 固定控制目录与 Data Root 解析（RC.3 §22-25、§37-39）
//!
//! 控制目录 `%LOCALAPPDATA%\PathOrbit\ZhiZhang\bootstrap.json` 只承担
//! 「真正账本在哪里」的引导职责。任何 Token/Project/Session/模型记录
//! 一律不得写入。
//!
//! 解析顺序（§24-25）：
//!   1. bootstrap.json 有效 → 采用其 data_root。
//!   2. bootstrap 缺失，但旧默认 `%LOCALAPPDATA%\UsageLedger\usage.db`
//!      存在 → RC.2 老用户：自动采用旧默认并补写 pointer（不重跑 First Run）。
//!   3. bootstrap 缺失、旧默认也没有有效账本 → 真正 First Run。
//!   4. bootstrap 存在但 JSON 损坏（§39）→ 先试旧默认；可确定唯一有效
//!      账本则恢复 pointer；否则进入重新连接账本界面（绝不新建空账）。
//!
//! §37 P0：bootstrap 指向的 data_root 失联时，绝不静默 fallback 到旧默认
//! 位置创建空账 —— 失联由 [`root_available`] 判定并上报恢复界面。

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const BOOTSTRAP_SCHEMA_VERSION: u32 = 1;

/// RC.2 默认数据根（升级兼容锚点，永不因 RC.3 改名，§19）。
pub fn legacy_default_root() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("USERPROFILE")
                .map(|p| PathBuf::from(p).join("AppData").join("Local"))
                .unwrap_or_else(|| PathBuf::from("."))
        });
    base.join("UsageLedger")
}

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

fn bootstrap_file() -> PathBuf {
    control_dir().join("bootstrap.json")
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Bootstrap {
    pub schema_version: u32,
    pub data_root: String,
}

impl Bootstrap {
    pub fn new(data_root: &Path) -> Self {
        Self {
            schema_version: BOOTSTRAP_SCHEMA_VERSION,
            data_root: data_root.to_string_lossy().to_string(),
        }
    }
}

/// bootstrap.json 读取结果（区分「缺失」与「损坏」，§39 恢复顺序依赖此区分）。
#[derive(Debug, Clone, PartialEq)]
pub enum BootstrapRead {
    Missing,
    Ok(Bootstrap),
    /// 存在但 schema 更高 / JSON 损坏 / 字段缺失。
    Corrupt(String),
}

pub fn read_from(path: &Path) -> BootstrapRead {
    match std::fs::read_to_string(path) {
        Err(_) => BootstrapRead::Missing,
        Ok(text) => match serde_json::from_str::<Bootstrap>(&text) {
            Ok(b) if b.schema_version == BOOTSTRAP_SCHEMA_VERSION && !b.data_root.is_empty() => {
                BootstrapRead::Ok(b)
            }
            Ok(_) => BootstrapRead::Corrupt("schema_version 或 data_root 非法".into()),
            Err(e) => BootstrapRead::Corrupt(format!("JSON 损坏：{e}")),
        },
    }
}

pub fn read() -> BootstrapRead {
    read_from(&bootstrap_file())
}

/// 原子写入 bootstrap pointer（temp + replace）。
pub fn write(root: &Path) -> Result<(), String> {
    write_to(&bootstrap_file(), &Bootstrap::new(root))
}

pub fn write_to(path: &Path, b: &Bootstrap) -> Result<(), String> {
    let dir = path.parent().ok_or_else(|| "bootstrap 路径异常".to_string())?;
    std::fs::create_dir_all(dir).map_err(|e| format!("创建控制目录失败：{e}"))?;
    let mut tmp = path.to_path_buf();
    tmp.set_extension("json.tmp");
    let text = serde_json::to_string_pretty(b).map_err(|e| e.to_string())?;
    {
        use std::io::Write;
        let mut f = std::fs::File::create(&tmp).map_err(|e| format!("写临时文件失败：{e}"))?;
        f.write_all(text.as_bytes()).map_err(|e| e.to_string())?;
        f.sync_all().ok();
    }
    std::fs::rename(&tmp, path)
        .map_err(|e| { let _ = std::fs::remove_file(&tmp); format!("原子替换 bootstrap 失败：{e}") })
}

/// 「有效智账账本」判定：usage.db 存在且非空（0 字节视为无效）。
pub fn has_valid_ledger(root: &Path) -> bool {
    let db = root.join("usage.db");
    match std::fs::metadata(&db) {
        Ok(m) => m.is_file() && m.len() > 0,
        Err(_) => false,
    }
}

/// §37 失联判定：data_root 必须存在且可访问（目录可读即可；
/// usage.db 由首次扫描创建，不能要求新位置一开始就有）。
pub fn root_available(root: &Path) -> bool {
    root.is_dir()
}

/// Data Root 解析结论。
#[derive(Debug, Clone, PartialEq)]
pub enum DataResolution {
    /// 来自 bootstrap pointer（或恢复后重写）。
    FromBootstrap(PathBuf),
    /// RC.2 老用户自动收编（已补写 pointer）。
    LegacyAdopted(PathBuf),
    /// 真正 First Run（无任何既有账本）。
    Fresh,
    /// §39：bootstrap 损坏且无法唯一确定账本 → 重新连接账本界面。
    ReconnectNeeded { reason: String },
}

/// 完整解析（不访问 UI，不做网络/全盘扫描 —— §39 禁止猜路径）。
/// RC.2 收编路径会原子补写 pointer；pointer 写失败不阻塞启动
///（下次启动重试收编，账本本身不受影响）。
pub fn resolve() -> DataResolution {
    resolve_in(read, legacy_default_root(), write, |p| {
        (p.exists(), has_valid_ledger(p))
    })
}

/// 生产入口（可注入测试版）：解析 + 收编时补写 pointer。
pub fn resolve_in(
    read_file: impl Fn() -> BootstrapRead,
    legacy_root: PathBuf,
    write_ptr: impl Fn(&Path) -> Result<(), String>,
    probe: impl Fn(&PathBuf) -> (bool, bool),
) -> DataResolution {
    let r = resolve_with(read_file, legacy_root, probe);
    if let DataResolution::LegacyAdopted(root) = &r {
        // pointer 写失败不阻塞启动：下次启动重试收编。
        let _ = write_ptr(root);
    }
    r
}

/// 可注入版本（测试用）。`legacy_root` 模拟旧默认位置；`probe` 返回 (路径存在, 有有效账本)。
pub fn resolve_with(
    read_file: impl Fn() -> BootstrapRead,
    legacy_root: PathBuf,
    probe: impl Fn(&PathBuf) -> (bool, bool),
) -> DataResolution {
    match read_file() {
        BootstrapRead::Ok(b) => DataResolution::FromBootstrap(PathBuf::from(&b.data_root)),
        BootstrapRead::Missing => {
            if probe(&legacy_root).1 {
                // RC.2 老用户：自动采用旧默认（pointer 由 resolve() 补写，§24）。
                DataResolution::LegacyAdopted(legacy_root)
            } else {
                DataResolution::Fresh
            }
        }
        BootstrapRead::Corrupt(reason) => {
            if probe(&legacy_root).1 {
                // 唯一可确定的有效账本 → 恢复（pointer 由 resolve() 补写，§39.2）。
                DataResolution::LegacyAdopted(legacy_root)
            } else {
                DataResolution::ReconnectNeeded { reason }
            }
        }
    }
}

/// data_root_kind（identity 契约）：旧默认 = production；其余 = custom。
pub fn data_root_kind(root: &Path) -> &'static str {
    if root == legacy_default_root() {
        "production"
    } else {
        "custom"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("zhizhang-boot-{}-{}", std::process::id(), tag));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// 把 legacy_default_root 指向沙盒：通过 LOCALAPPDATA env 注入。
    /// 注意：测试串行运行（cargo 默认同进程多线程 —— 这里用独立子进程不现实，
    /// 改为直接用 resolve_with 注入 probe，env 不动）。
    fn file_reader(path: PathBuf) -> impl Fn() -> BootstrapRead {
        move || read_from(&path)
    }

    #[test]
    fn missing_bootstrap_and_no_legacy_is_fresh() {
        let sandbox = tmpdir("fresh");
        // legacy 指向沙盒下不存在的子路径
        let legacy = sandbox.join("no-such-legacy");
        let r = resolve_with(file_reader(sandbox.join("none").join("bootstrap.json")), legacy.clone(), |_| (false, false));
        assert_eq!(r, DataResolution::Fresh);
        assert!(!has_valid_ledger(&legacy));
    }

    #[test]
    fn valid_bootstrap_wins_without_touching_legacy() {
        let sandbox = tmpdir("bootstrap-wins");
        let custom = sandbox.join("D盘数据");
        std::fs::create_dir_all(&custom).unwrap();
        let bf = sandbox.join("bootstrap.json");
        write_to(&bf, &Bootstrap::new(&custom)).unwrap();
        let r = resolve_with(file_reader(bf.clone()), legacy_default_root(), |_| (true, true));
        assert_eq!(r, DataResolution::FromBootstrap(custom));
        assert!(read_from(&bf) != BootstrapRead::Missing);
    }

    #[test]
    fn legacy_ledger_without_bootstrap_is_adopted() {
        let sandbox = tmpdir("adopt");
        let legacy = sandbox.join("legacy-root");
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::write(legacy.join("usage.db"), b"not-a-real-db").unwrap();
        let bf = sandbox.join("bootstrap.json");
        let r = resolve_in(
            file_reader(bf.clone()),
            legacy.clone(),
            |root| write_to(&bf.clone(), &Bootstrap::new(root)),
            |_| (true, true),
        );
        assert_eq!(r, DataResolution::LegacyAdopted(legacy.clone()));
        // pointer 已补写
        assert_eq!(read_from(&bf), BootstrapRead::Ok(Bootstrap::new(&legacy)));
    }

    #[test]
    fn corrupt_bootstrap_falls_back_to_unique_legacy_ledger() {
        let sandbox = tmpdir("corrupt-adopt");
        let legacy = sandbox.join("legacy-root");
        std::fs::create_dir_all(&legacy).unwrap();
        std::fs::write(legacy.join("usage.db"), b"x").unwrap();
        let bf = sandbox.join("bootstrap.json");
        std::fs::write(&bf, "{\"schema_version\":1,\"data_ro").unwrap();
        let r = resolve_in(
            file_reader(bf.clone()),
            legacy.clone(),
            |root| write_to(&bf.clone(), &Bootstrap::new(root)),
            |_| (true, true),
        );
        assert_eq!(r, DataResolution::LegacyAdopted(legacy.clone()));
        assert_eq!(read_from(&bf), BootstrapRead::Ok(Bootstrap::new(&legacy)));
    }

    #[test]
    fn corrupt_bootstrap_without_ledger_needs_reconnect() {
        let sandbox = tmpdir("corrupt-reconnect");
        let bf = sandbox.join("bootstrap.json");
        std::fs::write(&bf, "not json at all {{{").unwrap();
        let r = resolve_with(file_reader(bf), legacy_default_root(), |_| (false, false));
        match r {
            DataResolution::ReconnectNeeded { reason } => assert!(!reason.is_empty()),
            other => panic!("expected ReconnectNeeded, got {other:?}"),
        }
    }

    #[test]
    fn bootstrap_pointing_to_missing_root_is_not_silently_replaced() {
        // §37 P0：bootstrap 有效但 root 失联 → 解析仍返回 FromBootstrap，
        // 由启动流程进入恢复界面；绝不改写 bootstrap / 不 fallback。
        let sandbox = tmpdir("missing-root");
        let gone = sandbox.join("gone-drive");
        let bf = sandbox.join("bootstrap.json");
        write_to(&bf, &Bootstrap::new(&gone)).unwrap();
        let r = resolve_with(file_reader(bf), legacy_default_root(), |_| (false, false));
        assert_eq!(r, DataResolution::FromBootstrap(gone.clone()));
        assert!(!root_available(&gone));
    }

    #[test]
    fn zero_byte_usage_db_is_not_a_valid_ledger() {
        let sandbox = tmpdir("zero-db");
        std::fs::write(sandbox.join("usage.db"), b"").unwrap();
        assert!(!has_valid_ledger(&sandbox));
        std::fs::write(sandbox.join("usage.db"), b"SQLite format 3\0").unwrap();
        assert!(has_valid_ledger(&sandbox));
    }

    #[test]
    fn data_root_kind_contract() {
        assert_eq!(data_root_kind(&legacy_default_root()), "production");
        assert_eq!(data_root_kind(Path::new("D:\\智账数据")), "custom");
    }

    #[test]
    fn bootstrap_write_is_atomic_and_roundtrips() {
        let sandbox = tmpdir("atomic");
        let bf = sandbox.join("bootstrap.json");
        write_to(&bf, &Bootstrap::new(Path::new("E:\\我的数据\\智账"))).unwrap();
        assert!(!bf.with_extension("json.tmp").exists());
        assert_eq!(
            read_from(&bf),
            BootstrapRead::Ok(Bootstrap::new(Path::new("E:\\我的数据\\智账")))
        );
        // 中文路径写入内容正确
        let text = std::fs::read_to_string(&bf).unwrap();
        assert!(text.contains("E:\\\\我的数据\\\\智账"), "{text}");
    }
}
