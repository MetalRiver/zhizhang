//! Usage Ledger Desktop Shell — R10-D
//!
//! 职责（且仅限）：
//!   1. 单实例（second launch → focus existing window）
//!   2. 探测/复用或启动本地 backend sidecar（127.0.0.1:8787，--no-open）
//!      R11：复用前必须通过 /api/v1/identity 身份握手 —— 仅接受
//!      frozen + 与本安装一致的 production backend；dev/repo 账本、
//!      其他版本、无关服务一律拒绝连接（绝不终止占用进程）。
//!   3. 等待 /api/v1/overview 返回 200 后才展示主窗口并导航
//!   4. 失败时展示最小错误页（重试 / 打开数据目录 / 退出）
//!   5. 退出时仅终止【由本次桌面实例启动的】backend（ownership）
//!
//! 不含：任何业务逻辑、数据语义、美术决策。UI 仍由 Python Core 提供的现有 Web UI 承担。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};
#[cfg(windows)]
use std::os::windows::process::CommandExt;

use tauri::{AppHandle, Emitter, Listener, Manager};
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri_plugin_updater::UpdaterExt;

mod desktop_state;
mod bootstrap;

const BACKEND_PORT: u16 = 8787;
const BACKEND_BASE: &str = "http://127.0.0.1:8787";
const READY_TIMEOUT_SECS: u64 = 90;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 更新器客户端固定 public key（minisign；私钥保存在 repo 外安全位置，§45）。
const UPDATE_PUBKEY: &str =
    "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDczMkFENENBRThENDMyQTYKUldTbU10VG95dFFxYzFhTjExU3F5SmtTaHY1U3hnbVlUUDhQZWIrMnVGZ3RoazR4Nmhzem9Td3YK";

/// 更新源（§41-43）：正式源来自 tauri.conf plugins.updater.endpoints；
/// 环境变量 USAGE_LEDGER_UPDATE_URL 仅作为测试/部署显式覆盖。
/// 两者皆无 → 「更新源未配置」，绝不虚构 URL。
fn update_endpoint_override() -> Option<String> {
    std::env::var("USAGE_LEDGER_UPDATE_URL")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// 构造 updater：优先 env 覆盖端点，否则使用 conf 配置端点。
fn build_updater(app: &AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    let builder = app.updater_builder();
    let builder = match update_endpoint_override() {
        Some(url) => {
            let ep: url::Url = url.parse().map_err(|e| format!("更新源地址无效：{e}"))?;
            builder
                .endpoints(vec![ep])
                .map_err(|e| format!("更新源配置无效：{e}"))?
        }
        None => builder,
    };
    builder.build().map_err(|e| format!("更新器初始化失败：{e}"))
}

/// 桌面生命周期全局状态（与 backend ownership 分离）。
struct LifecycleState {
    /// 一旦置位：window close 不再拦截为隐藏/询问，退出流程只走一次。
    is_quitting: AtomicBool,
    /// --autostart 启动：后台进 Tray，不弹主窗（backend 失败仍弹窗，§17）。
    start_hidden: AtomicBool,
    /// 数据迁移进行中：close/quit 必须等待原子阶段完成（§36）。
    migrating: AtomicBool,
    /// 当前生效的 data root（启动解析；First Run/迁移后更新）。
    data_root: Mutex<std::path::PathBuf>,
    /// 启动恢复态：数据根失联 / bootstrap 损坏（§17、§37-39）。backend 不启动。
    recovery: Mutex<Option<RecoveryInfo>>,
    /// backend 就绪后的一次性导航目标（如迁移完成后回设置页）。
    pending_nav: Mutex<Option<String>>,
    /// What's New 待展示（拉模式：get_desktop_state 领取即清除，§55）。
    whats_new_pending: AtomicBool,
}

#[derive(Clone, Debug)]
struct RecoveryInfo {
    /// missing_root | corrupt_bootstrap
    kind: &'static str,
    /// 失联/待重连的账本位置（展示给用户）。
    root: String,
    message: String,
}

impl LifecycleState {
    fn new(
        start_hidden: bool,
        data_root: std::path::PathBuf,
        recovery: Option<RecoveryInfo>,
    ) -> Self {
        Self {
            is_quitting: AtomicBool::new(false),
            start_hidden: AtomicBool::new(start_hidden),
            migrating: AtomicBool::new(false),
            data_root: Mutex::new(data_root),
            recovery: Mutex::new(recovery),
            pending_nav: Mutex::new(None),
            whats_new_pending: AtomicBool::new(false),
        }
    }
    fn quitting(&self) -> bool {
        self.is_quitting.load(Ordering::SeqCst)
    }
    fn hidden(&self) -> bool {
        self.start_hidden.load(Ordering::SeqCst)
    }
    fn root(&self) -> std::path::PathBuf {
        self.data_root.lock().unwrap().clone()
    }
}

/// 启动时解析 data root（§22-25、§37-39）。返回 (root, recovery)。
fn resolve_boot_data_root() -> (std::path::PathBuf, Option<RecoveryInfo>) {
    match bootstrap::resolve() {
        bootstrap::DataResolution::FromBootstrap(root) => {
            if bootstrap::root_available(&root) {
                (root, None)
            } else {
                // §37 P0：绝不静默 fallback，进入恢复界面。
                let info = RecoveryInfo {
                    kind: "missing_root",
                    root: root.to_string_lossy().to_string(),
                    message: "找不到你的账本位置".to_string(),
                };
                (root, Some(info))
            }
        }
        bootstrap::DataResolution::LegacyAdopted(root) => {
            // RC.2 老用户收编：视为已有用户（不重跑 First Run，§24/§57）。
            let mut st = desktop_state::load();
            if !st.first_run_complete {
                st.first_run_complete = true;
                let _ = desktop_state::save(&st);
            }
            (root, None)
        }
        bootstrap::DataResolution::Fresh => (bootstrap::legacy_default_root(), None),
        bootstrap::DataResolution::ReconnectNeeded { reason } => {
            let info = RecoveryInfo {
                kind: "corrupt_bootstrap",
                root: String::new(),
                message: reason,
            };
            (bootstrap::legacy_default_root(), Some(info))
        }
    }
}

// ---------------------------------------------------------------- owned backend job lifecycle (V1 blocker fix)
//
// PyInstaller onedir bootloader 会再拉起一个 python 工作子进程；旧方案
// （Child::kill + taskkill /T /F）只按 PID 事后枚举，bootloader 先退或
// taskkill 快照竞态都会留下孤儿并占住 8787。
//
// 现在的 ownership 事实源 =「本实例以 CREATE_SUSPENDED spawn 了它」：
//   CreateJobObject(KILL_ON_JOB_CLOSE)
//   → AssignProcessToJobObject(backend)   ← backend 仍处于挂起态，不可能已有子孙
//   → ResumeThread（恢复主线程）
//   → bootloader 之后创建的一切子孙自动继承 Job
// 退出时 TerminateJobObject + 关闭最后一个 Job handle → 整树系统级终止。
// Reused / dev / incompatible backend 从不被 assign 进本实例的 Job。

const CREATE_SUSPENDED: u32 = 0x0000_0004;

/// Owned backend 的系统级生命周期句柄。不得提前 drop：
/// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 在最后一个 Job handle 关闭时才触发。
#[cfg(windows)]
mod winjob {
    use std::ffi::c_void;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE};

    pub struct OwnedJob {
        handle: HANDLE,
    }

    // HANDLE 是裸整数包装；Job 句柄只在本模块内使用，可跨线程移动。
    unsafe impl Send for OwnedJob {}

    impl OwnedJob {
        pub fn create() -> Result<OwnedJob, String> {
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return Err("CreateJobObjectW 失败".to_string());
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const c_void,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    CloseHandle(handle);
                    return Err("SetInformationJobObject(KILL_ON_JOB_CLOSE) 失败".to_string());
                }
                Ok(OwnedJob { handle })
            }
        }

        /// 把 spawn 出来的 backend 根进程挂入 Job（子孙随继承加入）。
        pub fn assign_pid(&self, pid: u32) -> Result<(), String> {
            unsafe {
                let proc = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
                if proc.is_null() {
                    return Err(format!("OpenProcess({pid}) 失败"));
                }
                let ok = AssignProcessToJobObject(self.handle, proc);
                CloseHandle(proc);
                if ok == 0 {
                    return Err(format!("AssignProcessToJobObject({pid}) 失败"));
                }
                Ok(())
            }
        }

        /// 系统级整树终止（幂等：空 Job 上调用同样安全）。
        pub fn terminate(&self) {
            unsafe {
                TerminateJobObject(self.handle, 1);
            }
        }
    }

    impl Drop for OwnedJob {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.handle);
                // KILL_ON_JOB_CLOSE：最后一个 handle 关闭 → 未退出的成员整树终止。
            }
        }
    }
}

/// 恢复 CREATE_SUSPENDED 拉起的进程的全部线程（主线程 + bootloader 可能
/// 预创建的辅助线程）。返回恢复的线程数；0 = 失败。
#[cfg(windows)]
fn resume_process_threads(pid: u32) -> Result<usize, String> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, THREADENTRY32, Thread32First, Thread32Next, TH32CS_SNAPTHREAD,
    };
    use windows_sys::Win32::System::Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME};
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snap == INVALID_HANDLE_VALUE {
            return Err("CreateToolhelp32Snapshot(THREAD) 失败".to_string());
        }
        let mut entry: THREADENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
        let mut resumed = 0usize;
        if Thread32First(snap, &mut entry) != 0 {
            loop {
                if entry.th32OwnerProcessID == pid {
                    let th = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                    if !th.is_null() {
                        ResumeThread(th);
                        CloseHandle(th);
                        resumed += 1;
                    }
                }
                if Thread32Next(snap, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snap);
        if resumed == 0 {
            return Err(format!("进程 {pid} 没有可恢复的线程"));
        }
        Ok(resumed)
    }
}

/// Owned backend 的全部生命周期资源。cleanup 幂等；非 Owned 永不进入。
#[cfg(windows)]
struct OwnedBackendHandle {
    child: Option<Child>,
    job: Option<winjob::OwnedJob>,
}

#[cfg(windows)]
impl OwnedBackendHandle {
    /// 关闭顺序：Job 整树终止（系统级）→ owned PID taskkill 兜底 →
    /// Child 收尸 → 关闭 Job handle（KILL_ON_JOB_CLOSE 幂等清尾）。
    fn cleanup(&mut self) {
        if let Some(job) = self.job.as_ref() {
            job.terminate();
        }
        if let Some(mut child) = self.child.take() {
            terminate_owned_child(&mut child);
        }
        self.job = None;
    }
}

/// backend 身份契约（serve.py /api/v1/identity）。桌面壳复用前必须逐项比对。
const BACKEND_APP_ID: &str = "usage-ledger";
/// Usage Board Schema v1（ledger.build_board / serve.py BOARD_SCHEMA_VERSION）。
const BOARD_SCHEMA_VERSION: u32 = 1;

/// backend 由谁启动：Owned = 本次桌面实例拉起（退出时收尾）；Reused = 用户已在运行（绝不动它）。
#[derive(Clone, Copy, PartialEq)]
enum Ownership {
    Owned,
    Reused,
}

struct BackendState {
    child: Mutex<Option<Child>>,
    ownership: Mutex<Option<Ownership>>,
    // Owned backend 的系统级生命周期句柄（KILL_ON_JOB_CLOSE）。
    // 仅在 ownership == Owned 时存在；随 cleanup 一起关闭。
    #[cfg(windows)]
    job: Mutex<Option<winjob::OwnedJob>>,
}

impl BackendState {
    fn new() -> Self {
        Self {
            child: Mutex::new(None),
            ownership: Mutex::new(None),
            #[cfg(windows)]
            job: Mutex::new(None),
        }
    }
}

/// 轻量 HTTP 探测：只依赖标准库，不引入网络栈依赖。
/// 返回 None = 端口无人监听；Some(false) = 有响应但不是就绪的 Usage Ledger API。
fn probe_backend_ready(port: u16) -> Option<bool> {
    use std::net::ToSocketAddrs;
    let addr = ("127.0.0.1", port).to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(1200)).ok()?;
    stream
        .set_read_timeout(Some(Duration::from_millis(2500)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_millis(1200))))
        .ok()?;
    let req = format!(
        "GET /api/v1/overview HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    let ready = text.starts_with("HTTP/1.1 200")
        || text.starts_with("HTTP/1.0 200")
        || text.contains("\r\n\r\n{\"schema_version\"");
    Some(ready)
}

/// 轻量 JSON POST（无 Origin 头 —— serve.py 仅对带 Origin 的请求做校验）。
/// 返回 Some(true) = 2xx；Some(false) = 非 2xx；None = 网络/协议失败。
fn post_json(port: u16, path: &str, body: &str, timeout_ms: u64) -> Option<bool> {
    use std::net::ToSocketAddrs;
    let addr = ("127.0.0.1", port).to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(timeout_ms)).ok()?;
    stream
        .set_read_timeout(Some(Duration::from_millis(timeout_ms)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_millis(timeout_ms))))
        .ok()?;
    let req = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    Some(text.starts_with("HTTP/1.1 2") || text.starts_with("HTTP/1.0 2"))
}

fn show_main_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// 导航到后端 UI 的指定页（/settings 支持 ?tab=，见 web/v1/app.js）。
fn navigate_app_page(app: &AppHandle, path: &str) {
    show_main_window(app);
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval(&format!("window.location.replace('{BACKEND_BASE}{path}');"));
    }
}

/// 真正退出（§11）：置位 is_quitting → 等待迁移原子阶段（有界）→
/// backend quiesce（等待 active scan，有界）→ owned backend 整树收尾 →
/// 退出 Tauri（RunEvent::Exit 兜底再清一次）。
/// 幂等：is_quitting 保证 close handler 不会再拦截。
fn graceful_exit(app: &AppHandle) {
    let state = app.state::<LifecycleState>();
    if state.quitting() {
        return;
    }
    state.is_quitting.store(true, Ordering::SeqCst);
    let app = app.clone();
    std::thread::spawn(move || {
        // §36：迁移关键阶段不杀进程 —— 等待原子阶段完成（有界 120s）。
        let deadline = Instant::now() + Duration::from_secs(120);
        while app.state::<LifecycleState>().migrating.load(Ordering::SeqCst)
            && Instant::now() < deadline
        {
            std::thread::sleep(Duration::from_millis(200));
        }
        // 停止 scheduled work / 等待 active write：quiesce 端点占用扫描锁
        // 并拒绝新写请求。backend 无响应（已死/未起）时直接跳过。
        let _ = post_json(
            BACKEND_PORT,
            "/api/v1/maintenance/quiesce",
            "{}",
            4000,
        );
        // 给在途写请求一个短窗口（scan lock 已被 quiesce 持有 → 不会有新写）。
        std::thread::sleep(Duration::from_millis(300));
        kill_owned_backend(&app);
        app.exit(0);
    });
}

/// 带响应体的 JSON 请求。返回 (status_code, body)。
fn http_json(port: u16, method: &str, path: &str, body: Option<&str>, timeout_ms: u64) -> Option<(u16, String)> {
    use std::net::ToSocketAddrs;
    let addr = ("127.0.0.1", port).to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(timeout_ms)).ok()?;
    stream
        .set_read_timeout(Some(Duration::from_millis(timeout_ms.max(30000))))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_millis(timeout_ms))))
        .ok()?;
    let (head, payload) = match body {
        Some(b) => (
            format!(
                "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                b.len()
            ),
            b.as_bytes().to_vec(),
        ),
        None => (
            format!(
                "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            ),
            Vec::new(),
        ),
    };
    stream.write_all(head.as_bytes()).ok()?;
    stream.write_all(&payload).ok()?;
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf).to_string();
    let status: u16 = text
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = text
        .split_once("\r\n\r\n")
        .map(|(_, b)| b.to_string())
        .unwrap_or_default();
    Some((status, body))
}

// ---------------------------------------------------------------- identity handshake (R11)

/// serve.py /api/v1/identity 的响应体契约。
#[derive(Debug, serde::Deserialize)]
struct BackendIdentity {
    app_id: String,
    app_version: String,
    runtime_mode: String,
    data_root_kind: String,
    build_id: String,
    schema_version: u32,
    backend_pid: u64,
}

/// 本安装对「可复用 backend」的期望身份（由随包 VERSION 与运行环境推导）。
#[derive(Debug, PartialEq)]
struct ExpectedBackend {
    app_version: String,
    build_id: String,
    data_root_kind: String,
}

/// build 标签：与 serve.py identity_payload 的公式严格一致（无密码学需求，仅一致性）。
fn expected_build_tag(app_version: &str) -> String {
    format!("ulb-v{app_version}-s{BOARD_SCHEMA_VERSION}")
}

/// 解析原始 HTTP 响应为身份。任何结构/状态异常都视为「无法识别占用者」。
fn parse_identity_response(raw: &str) -> Result<BackendIdentity, String> {
    let (status, body) = raw
        .split_once("\r\n\r\n")
        .ok_or_else(|| "identity 响应缺少头部".to_string())?;
    let status_ok = status.starts_with("HTTP/1.1 200") || status.starts_with("HTTP/1.0 200");
    if !status_ok {
        return Err(format!(
            "identity 端点响应异常（{}）",
            status.lines().next().unwrap_or("?").trim()
        ));
    }
    serde_json::from_str(body.trim())
        .map_err(|e| format!("identity 响应不是有效的 backend 身份（{e}）"))
}

/// 从随包 sidecar 的 VERSION 提取期望 (app_version, build_id)。
/// 与运行中的 backend 版本不一致 → 拒绝复用（beta 安全策略：同构建才可复用）。
fn bundled_backend_version_tag(app: &AppHandle) -> Result<(String, String), String> {
    let exe = backend_exe(app)
        .ok_or_else(|| "无法定位本安装的 backend sidecar".to_string())?;
    let ver_path = exe
        .parent()
        .ok_or_else(|| "sidecar 路径异常".to_string())?
        .join("_internal")
        .join("VERSION");
    let text = std::fs::read_to_string(&ver_path)
        .map_err(|e| format!("无法读取本安装 VERSION（{e}）"))?;
    for line in text.lines() {
        let t = line.trim_start();
        if let Some(rest) = t.strip_prefix("version") {
            let v = rest.split_whitespace().next().unwrap_or("");
            if !v.is_empty() {
                return Ok((v.to_string(), expected_build_tag(v)));
            }
        }
    }
    Err("本安装 VERSION 缺少版本行".to_string())
}

/// 期望 data_root_kind：由【解析后的 data root 路径】决定（bootstrap 契约，
/// 不再依赖本进程 env —— env 只注入给 backend 子进程）。
fn expected_data_root_kind(app: &AppHandle) -> &'static str {
    bootstrap::data_root_kind(&app.state::<LifecycleState>().root())
}

/// Reuse 兼容策略（R11 beta）：app_id 严格；仅 frozen；data_root_kind 与本安装
/// 环境一致（production / custom）；board schema 相同；app_version + build_id
/// 与随包 VERSION 一致（同构建才复用，跨版本一律要求重启旧 backend）。
fn identity_reuse_compatible(
    id: &BackendIdentity,
    expected: &ExpectedBackend,
) -> Result<(), String> {
    if id.app_id != BACKEND_APP_ID {
        return Err(format!("app_id={} 不是 {}", id.app_id, BACKEND_APP_ID));
    }
    if id.runtime_mode != "frozen" {
        return Err(format!("runtime_mode={}（仅 frozen 可复用）", id.runtime_mode));
    }
    if id.data_root_kind != expected.data_root_kind {
        return Err(format!(
            "data_root_kind={}（本安装期望 {}）",
            id.data_root_kind, expected.data_root_kind
        ));
    }
    if id.schema_version != BOARD_SCHEMA_VERSION {
        return Err(format!(
            "schema_version={}（本安装期望 {BOARD_SCHEMA_VERSION}）",
            id.schema_version
        ));
    }
    if id.app_version != expected.app_version {
        return Err(format!(
            "app_version={} 与本安装 {} 不一致",
            id.app_version, expected.app_version
        ));
    }
    if id.build_id.is_empty() || id.build_id != expected.build_id {
        return Err(format!(
            "build_id={} 与本安装 {} 不一致",
            id.build_id, expected.build_id
        ));
    }
    Ok(())
}

fn fetch_backend_identity(port: u16) -> Result<BackendIdentity, String> {
    use std::net::ToSocketAddrs;
    let addr = ("127.0.0.1", port)
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or_else(|| "无法解析 127.0.0.1".to_string())?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(1200))
        .map_err(|e| format!("连接占用者失败（{e}）"))?;
    stream
        .set_read_timeout(Some(Duration::from_millis(2500)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_millis(1200))))
        .map_err(|e| e.to_string())?;
    let req = format!(
        "GET /api/v1/identity HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    stream
        .write_all(req.as_bytes())
        .map_err(|e| format!("identity 请求发送失败（{e}）"))?;
    let mut buf = Vec::new();
    stream
        .read_to_end(&mut buf)
        .map_err(|e| format!("identity 响应读取失败（{e}）"))?;
    parse_identity_response(&String::from_utf8_lossy(&buf))
}

/// 校验「8787 上的占用者」是否可被本安装复用。失败 → 一律拒绝连接。
fn verify_running_backend(app: &AppHandle) -> Result<(), String> {
    let id = fetch_backend_identity(BACKEND_PORT)?;
    let (ver, tag) = bundled_backend_version_tag(app)?;
    let expected = ExpectedBackend {
        app_version: ver,
        build_id: tag,
        data_root_kind: expected_data_root_kind(app).to_string(),
    };
    identity_reuse_compatible(&id, &expected)
}

/// backend sidecar 可执行文件位置。
/// release：Tauri resources/binaries/usage-ledger-backend/usage-ledger-backend.exe
/// dev：仓库内 src-tauri/binaries/...（可用 USAGE_LEDGER_BACKEND_EXE 覆盖）
fn backend_exe(app: &AppHandle) -> Option<std::path::PathBuf> {
    if let Some(p) = std::env::var_os("USAGE_LEDGER_BACKEND_EXE") {
        let p = std::path::PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    let candidate = app
        .path()
        .resource_dir()
        .ok()?
        .join("binaries")
        .join("usage-ledger-backend")
        .join("usage-ledger-backend.exe");
    if candidate.is_file() {
        return Some(candidate);
    }
    if cfg!(debug_assertions) {
        let dev = std::env::current_dir()
            .ok()?
            .ancestors()
            .find_map(|a| {
                let p = a.join("src-tauri").join("binaries")
                    .join("usage-ledger-backend").join("usage-ledger-backend.exe");
                if p.is_file() { Some(p) } else { None }
            });
        return dev;
    }
    None
}

fn spawn_backend(app: &AppHandle) -> Result<Ownership, String> {
    let exe = backend_exe(app)
        .ok_or_else(|| "未找到本地核心 sidecar（usage-ledger-backend.exe）。安装可能不完整。".to_string())?;

    let mut cmd = Command::new(&exe);
    cmd.arg("--no-open")
        .arg("--port")
        .arg(BACKEND_PORT.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // bootstrap 契约：自定义数据根经 env 注入 backend（§22）。
    // 旧默认根不注入 —— backend 保持 data_root_kind=production。
    {
        let root = app.state::<LifecycleState>().root();
        if root != bootstrap::legacy_default_root() {
            cmd.env("USAGE_LEDGER_HOME", &root);
        }
    }
    // Windows：CREATE_SUSPENDED 消除 spawn→assign 竞态 —— backend 在被挂入
    // Job 之前物理上不可能创建任何子进程（PyInstaller bootloader 亦然）。
    // 挂入 Job 之后才恢复线程；bootloader 后续子孙自动继承 Job ownership。
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("本地核心启动失败：{e}"))?;

    #[cfg(windows)]
    {
        // 1) 建 Job（KILL_ON_JOB_CLOSE）
        let mut job = match winjob::OwnedJob::create() {
            Ok(j) => j,
            Err(e) => {
                let _ = child.kill();
                return Err(format!("backend 生命周期初始化失败：{e}"));
            }
        };
        // 2) 挂起态下挂入 Job（此刻不可能有子孙）
        if let Err(e) = job.assign_pid(child.id()) {
            let _ = child.kill();
            return Err(format!("backend 挂入 Job 失败：{e}"));
        }
        // 3) 先把 Job 交给状态（后续任何失败路径都能terminate）
        *app.state::<BackendState>().job.lock().unwrap() = Some(job);
        // 4) 恢复线程，backend 开始运行
        if let Err(e) = resume_process_threads(child.id()) {
            if let Some(job) = app.state::<BackendState>().job.lock().unwrap().as_ref() {
                job.terminate();
            }
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("backend 恢复执行失败：{e}"));
        }
    }

    // 立即检查：serve.py 端口预检失败（外部占用等）会很快退出。
    // （Windows 上 Job 已挂入状态：此处返回 Err 时 job 随 OwnedBackendHandle/
    //   state 生命周期兜底清理。）
    if let Some(code) = child.try_wait().ok().flatten() {
        return Err(match code.code() {
            Some(3) => format!("端口 {BACKEND_PORT} 已被其它程序占用。请释放该端口后重试。"),
            Some(2) => "检测到本产品服务已在运行，但就绪探测失败。请稍后重试。".to_string(),
            _ => format!("本地核心异常退出（退出码 {code:?}）。"),
        });
    }

    let mut state = app.state::<BackendState>();
    *state.child.lock().unwrap() = Some(child);
    *state.ownership.lock().unwrap() = Some(Ownership::Owned);
    Ok(Ownership::Owned)
}

fn navigate_to_ui(app: &AppHandle) {
    // 测试钩子：USAGE_LEDGER_START_PAGE 可指定初始路由（如 models）。
    let page = std::env::var("USAGE_LEDGER_START_PAGE").unwrap_or_default();
    let url = if page.is_empty() {
        format!("{BACKEND_BASE}/")
    } else {
        format!("{BACKEND_BASE}/#/{page}")
    };
    if let Some(w) = app.get_webview_window("main") {
        // 一次性导航目标（迁移完成后回设置页等）；否则回账本首页。
        let pending = app
            .state::<LifecycleState>()
            .pending_nav
            .lock()
            .unwrap()
            .take();
        let url = match pending {
            Some(path) if path.starts_with('/') => format!("{BACKEND_BASE}{path}"),
            _ => url,
        };
        let _ = w.eval(&format!("window.location.replace('{url}');"));
        // --autostart：后台进入 Tray，不弹主窗（§16）；用户从 Tray 打开。
        if !app.state::<LifecycleState>().hidden() {
            let _ = w.show();
            let _ = w.set_focus();
        }
    }
}

/// 进入恢复界面（§17、§38、§60A）：本地 recovery.html，
/// autostart 隐藏启动失败也必须弹窗，绝不躲在 Tray 里失败。
fn show_recovery(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval("window.location.replace('recovery.html');");
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// 失败消息重发：boot 页 JS 监听器注册与 Rust 侧探测存在竞态
///（dev/兼容 backend 秒回时，backend-failed 可能在页面就绪前发出而丢失）。
/// showError 幂等，重发数次直至页面就绪。
fn emit_backend_failed(app: &AppHandle, msg: String) {
    let app = app.clone();
    std::thread::spawn(move || {
        // §17：autostart 隐藏启动失败时不许躲在 Tray 里失败 → 弹出主窗展示恢复页。
        if app.state::<LifecycleState>().hidden() {
            show_main_window(&app);
        }
        for _ in 0..6 {
            let _ = app.emit("backend-failed", msg.clone());
            std::thread::sleep(Duration::from_millis(700));
        }
    });
}

fn boot_sequence(app: AppHandle, is_retry: bool) {
    std::thread::spawn(move || {
        let state = app.state::<BackendState>();
        let _ = app.emit("backend-status", if is_retry { "正在重试启动本地服务…" } else { "正在检查本地服务…" });

        // 0) 恢复模式（数据根失联 / bootstrap 损坏）：backend 不启动，
        //    直接进恢复界面 —— 绝不静默 fallback / 创建空账（§37-39）。
        if app.state::<LifecycleState>().recovery.lock().unwrap().is_some() {
            show_recovery(&app);
            return;
        }

        // 1) 端口已有人监听 → 必须先验明身份；仅兼容的 frozen/本安装 backend 可复用。
        //    校验失败：不连接、不杀占用者，给出明确启动错误（Retry / Exit 由错误卡提供）。
        match probe_backend_ready(BACKEND_PORT) {
            Some(true) => match verify_running_backend(&app) {
                Ok(()) => {
                    *state.ownership.lock().unwrap() = Some(Ownership::Reused);
                    let _ = app.emit("backend-status", "本地服务已就绪，正在进入中枢…");
                    navigate_to_ui(&app);
                    return;
                }
                Err(reason) => {
                    emit_backend_failed(
                        &app,
                        format!(
                            "8787 已被不兼容的智账 backend 或其他服务占用（{reason}）。已阻止连接，未终止占用进程；请关闭该进程后重试。"
                        ),
                    );
                    return;
                }
            },
            _ => {}
        }

        // 2) 拉起 sidecar
        let _ = app.emit("backend-status", "正在启动本地账本核心…");
        if let Err(msg) = spawn_backend(&app) {
            emit_backend_failed(&app, msg);
            return;
        }

        // 3) 轮询就绪（明确 200，而不是固定 sleep）
        let deadline = Instant::now() + Duration::from_secs(READY_TIMEOUT_SECS);
        loop {
            if Instant::now() >= deadline {
                emit_backend_failed(&app,
                    format!("本地核心未能在 {READY_TIMEOUT_SECS} 秒内就绪（127.0.0.1:{BACKEND_PORT}）。"));
                return;
            }
            match probe_backend_ready(BACKEND_PORT) {
                Some(true) => {
                    // 双保险：即使是本次拉起的 backend，也要身份一致才进入。
                    // 不一致（端口在启动窗口内被第三方抢占等）→ 终止【自有】子进程并失败。
                    match verify_running_backend(&app) {
                        Ok(()) => {
                            let _ = app.emit("backend-status", "本地服务已就绪，正在进入中枢…");
                            navigate_to_ui(&app);
                            return;
                        }
                        Err(reason) => {
                            kill_owned_backend(&app);
                            emit_backend_failed(
                                &app,
                                format!(
                                    "8787 已被不兼容的智账 backend 或其他服务占用（{reason}）。已停止本次启动的本地核心，未终止占用进程。"
                                ),
                            );
                            return;
                        }
                    }
                }
                _ => {
                    // child 提前退出 → 直接失败并给出可读原因
                    if let Some(child) = state.child.lock().unwrap().as_mut() {
                        if let Ok(Some(code)) = child.try_wait() {
                            let msg = match code.code() {
                                Some(3) => format!("端口 {BACKEND_PORT} 已被其它程序占用。请释放该端口后重试。"),
                                Some(2) => "检测到本产品服务已在运行，但就绪探测失败。请稍后重试。".to_string(),
                                _ => format!("本地核心异常退出（退出码 {code:?}）。"),
                            };
                            emit_backend_failed(&app, msg);
                            return;
                        }
                    }
                    std::thread::sleep(Duration::from_millis(400));
                }
            }
        }
    });
}

fn terminate_owned_child(child: &mut Child) {
    // PyInstaller's Windows bootloader owns a second process running Python.
    // Keep the live Child handle and terminate its tree before reaping it.
    #[cfg(windows)]
    if matches!(child.try_wait(), Ok(None)) {
        // GUI-launched Tauri processes may have a trimmed PATH; use the
        // system binary explicitly so the owned tree cleanup cannot silently
        // fall back to Child::kill() (which leaves PyInstaller descendants).
        let taskkill = std::env::var_os("WINDIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::path::PathBuf::from(r"C:\Windows"))
            .join("System32")
            .join("taskkill.exe");
        let _ = Command::new(taskkill)
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn kill_owned_backend(app: &AppHandle) {
    let state = app.state::<BackendState>();
    // 只有【本实例自己 spawn 的】backend 才会被 cleanup。
    // Reused / dev / incompatible / 无 backend → 无 Job、无 kill、无 PID 查找。
    if *state.ownership.lock().unwrap() != Some(Ownership::Owned) {
        return;
    }
    #[cfg(windows)]
    {
        let mut handle = OwnedBackendHandle {
            child: state.child.lock().unwrap().take(),
            job: state.job.lock().unwrap().take(),
        };
        handle.cleanup(); // 幂等：Job terminate + taskkill 兜底 + 收尸 + 关闭 handle
    }
    #[cfg(not(windows))]
    {
        if let Some(mut child) = state.child.lock().unwrap().take() {
            terminate_owned_child(&mut child);
        }
    }
    *state.ownership.lock().unwrap() = None;
}

// ---------------------------------------------------------------- close behavior (RC.3 §7-12)

/// 弹出「关闭窗口后，你希望智账继续在后台运行吗？」对话框（独立小窗口，
/// 不依赖远端 UI 可用性）。已存在则聚焦，不重复创建。
fn show_close_ask(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("close-ask") {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let _ = tauri::WebviewWindowBuilder::new(
        app,
        "close-ask",
        tauri::WebviewUrl::App("close-ask.html".into()),
    )
    .title("智账")
    .inner_size(460.0, 232.0)
    .resizable(false)
    .maximizable(false)
    .minimizable(false)
    .skip_taskbar(true)
    .always_on_top(true)
    .center()
    .build();
}

/// close-ask 结果处理（来自 resolve_close_choice 命令）。
/// remember → 持久化到 desktop-state.json；之后按选择隐藏或退出。
fn resolve_close(app: &AppHandle, choice: &str, remember: bool) {
    let valid = choice == desktop_state::CLOSE_BACKGROUND || choice == desktop_state::CLOSE_EXIT;
    if !valid {
        return;
    }
    if remember {
        let mut st = desktop_state::load();
        st.close_behavior = Some(choice.to_string());
        if let Err(e) = desktop_state::save(&st) {
            eprintln!("desktop-state 保存失败：{e}");
        }
    }
    if let Some(w) = app.get_webview_window("close-ask") {
        let _ = w.close();
    }
    match choice {
        desktop_state::CLOSE_BACKGROUND => {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.hide();
            }
        }
        _ => graceful_exit(app),
    }
}

/// 主窗口 × 按键行为（§7-10、§36）。
fn handle_main_close(window: &tauri::Window, app: &AppHandle) {
    let lifecycle = app.state::<LifecycleState>();
    if lifecycle.quitting() {
        // 真正退出流程中的窗口销毁：不拦截（§12）。
        return;
    }
    if lifecycle.migrating.load(Ordering::SeqCst) {
        // §36：迁移关键阶段不可中断 —— 提示而非退出/隐藏。
        let _ = app.emit("migration-in-progress", "正在迁移账本，请稍候…");
        show_main_window(app);
        return;
    }
    let behavior = desktop_state::resolve_close_behavior(&desktop_state::load()).to_string();
    match behavior.as_str() {
        desktop_state::CLOSE_BACKGROUND => {
            // 关闭到托盘：窗口隐藏，backend 与 scheduled work 继续运行。
            let _ = window.hide();
        }
        desktop_state::CLOSE_EXIT => {
            let w = window.clone();
            let app = app.clone();
            std::thread::spawn(move || {
                let _ = w.hide();
                std::thread::sleep(Duration::from_millis(150));
                graceful_exit(&app);
            });
        }
        _ => {
            // ASK_ON_FIRST_CLOSE：RC.2 升级用户首次 ×，询问一次。
            // 主窗保持可见，由对话框置顶提问；取消 = 什么都不发生。
            show_close_ask(app);
        }
    }
}

// ---------------------------------------------------------------- tray (RC.3 §6)

fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItemBuilder::with_id("tray-open", "打开智账").build(app)?;
    let update = MenuItemBuilder::with_id("tray-update", "更新账本").build(app)?;
    let settings = MenuItemBuilder::with_id("tray-settings", "设置").build(app)?;
    let check = MenuItemBuilder::with_id("tray-check-updates", "检查更新").build(app)?;
    let about = MenuItemBuilder::with_id("tray-about", "关于智账").build(app)?;
    let quit = MenuItemBuilder::with_id("tray-quit", "退出智账").build(app)?;
    let menu = MenuBuilder::new(app)
        .item(&open)
        .item(&update)
        .item(&settings)
        .item(&check)
        .separator()
        .item(&about)
        .separator()
        .item(&quit)
        .build()?;

    let mut tray = TrayIconBuilder::with_id("main-tray")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("智账 · PathOrbit AI Ledger")
        .on_menu_event(|app, event| match event.id().as_ref() {
            "tray-open" => show_main_window(app),
            "tray-update" => {
                // 更新账本 = 聚焦主窗 + 触发一次增量扫描（与 UI 内按钮同通道）。
                show_main_window(app);
                std::thread::spawn(|| {
                    let _ = post_json(BACKEND_PORT, "/api/v1/scan", "{}", 15000);
                });
            }
            "tray-settings" => navigate_app_page(app, "/settings"),
            "tray-check-updates" => navigate_app_page(app, "/settings?tab=about"),
            "tray-about" => navigate_app_page(app, "/settings?tab=about"),
            "tray-quit" => graceful_exit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            // 左键：打开 / 恢复 / 聚焦当前主窗口（不得创建第二个窗口）。
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon().cloned() {
        tray = tray.icon(icon);
    }
    tray.build(app)?;
    Ok(())
}

fn main() {
    // --autostart：开机自启入口，后台进 Tray（§16）；--quit：仅由
    // single-instance 转发处理；首启直接 --quit 视为无操作。
    let start_hidden = std::env::args().any(|a| a == "--autostart");
    // Data Root 解析（§22-25、§37-39）：bootstrap pointer → RC.2 收编 →
    // First Run；失联/损坏 → 恢复模式（backend 不启动，绝不建空账）。
    let (data_root, recovery) = resolve_boot_data_root();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().pubkey(UPDATE_PUBKEY).build())
        .plugin(
            // app_name 固定为产品名：HKCU Run 值名确定，卸载钩子可精确清理（§76）。
            tauri_plugin_autostart::Builder::new()
                .app_name("智账")
                .args(["--autostart"])
                .build(),
        )
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            if args.iter().any(|a| a == "--quit") {
                // §52：安装器请求存活实例优雅退出（不 taskkill /F）。
                graceful_exit(app);
                return;
            }
            show_main_window(app);
        }))
        .manage(BackendState::new())
        .manage(LifecycleState::new(start_hidden, data_root, recovery))
        .invoke_handler(tauri::generate_handler![
            open_data_root,
            resolve_close_choice,
            get_desktop_state,
            set_close_behavior,
            set_autostart,
            get_boot_info,
            retry_recovery,
            complete_first_run,
            pick_folder,
            set_data_root,
            migrate_data_root_plan,
            migrate_data_root_execute,
            check_for_update,
            download_and_install_update
        ])
        .on_window_event(|window, event| {
            if window.label() != "main" {
                // close-ask 等辅助窗口：默认关闭行为即可。
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let w = window.clone();
                let app = window.app_handle().clone();
                std::thread::spawn(move || handle_main_close(&w, &app));
            }
        })
        .setup(|app| {
            setup_tray(app.handle())?;
            // 测试钩子：环境变量可覆盖默认窗口尺寸（1440×900），仅用于 E2E 验证。
            if let (Ok(w), Ok(h)) = (
                std::env::var("USAGE_LEDGER_WINDOW_W"),
                std::env::var("USAGE_LEDGER_WINDOW_H"),
            ) {
                if let (Ok(w), Ok(h)) = (w.parse::<f64>(), h.parse::<f64>()) {
                    if let Some(win) = app.get_webview_window("main") {
                        let _ = win.set_size(tauri::LogicalSize::new(w, h));
                    }
                }
            }
            let handle_retry = app.handle().clone();
            app.listen("backend-retry", move |_| {
                boot_sequence(handle_retry.clone(), true);
            });
            let handle_exit = app.handle().clone();
            app.listen("backend-exit", move |_| {
                kill_owned_backend(&handle_exit);
                handle_exit.exit(1);
            });
            boot_sequence(app.handle().clone(), false);
            maybe_show_whats_new(app.handle());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building 智账 desktop shell")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                kill_owned_backend(app);
            }
        });
}

/// close-ask 对话框结果（public/close-ask.html）：choice = background|exit。
#[tauri::command]
fn resolve_close_choice(app: AppHandle, choice: String, remember: bool) {
    resolve_close(&app, &choice, remember);
}

/// 远端 UI（设置页）读取桌面生命周期状态。autostart_enabled 以 OS 注册为准（§23）。
#[tauri::command]
fn get_desktop_state(app: AppHandle) -> serde_json::Value {
    use tauri_plugin_autostart::ManagerExt;
    let st = desktop_state::load();
    let lifecycle = app.state::<LifecycleState>();
    let root = lifecycle.root();
    let autostart_enabled = app.autolaunch().is_enabled().unwrap_or(false);
    serde_json::json!({
        "close_behavior": desktop_state::resolve_close_behavior(&st),
        "first_run_complete": st.first_run_complete,
        "last_seen_version": st.last_seen_version,
        "autostart_enabled": autostart_enabled,
        "data_root": root.to_string_lossy(),
        "data_root_kind": bootstrap::data_root_kind(&root),
        "recovering": lifecycle.recovery.lock().unwrap().is_some(),
        "show_whats_new": lifecycle.whats_new_pending.swap(false, Ordering::SeqCst),
    })
}

/// 恢复页（recovery.html）读取启动恢复信息。
#[tauri::command]
fn get_boot_info(app: AppHandle) -> serde_json::Value {
    let lifecycle = app.state::<LifecycleState>();
    let rec = lifecycle.recovery.lock().unwrap().clone();
    match rec {
        Some(rec) => serde_json::json!({
            "kind": rec.kind, "data_root": rec.root, "message": rec.message,
        }),
        None => serde_json::json!({ "kind": "normal" }),
    }
}

/// 系统文件夹选择对话框（First Run 数据位置 / 更改位置）。
#[tauri::command]
async fn pick_folder(app: AppHandle) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = std::sync::mpsc::channel();
    app.dialog()
        .file()
        .set_title("选择账本保存位置")
        .pick_folder(move |path| {
            let _ = tx.send(path.map(|p| match p {
                tauri_plugin_dialog::FilePath::Path(pb) => pb.display().to_string(),
                other => other.to_string(),
            }));
        });
    rx.recv().ok().flatten()
}

/// 目标位置校验（First Run / 重连共用）。返回给 UI 的说明信息。
fn validate_new_data_root(path: &str) -> Result<(), String> {
    let p = std::path::PathBuf::from(path);
    if path.trim().is_empty() {
        return Err("路径为空".into());
    }
    // 盘符相对路径（如 "C:foo"）解析结果随进程工作目录漂移，必须拒绝。
    if !p.is_absolute() {
        return Err("请选择完整的绝对路径（如 D:\\智账数据）".into());
    }
    // 不允许安装目录内外嵌套（§32）
    if let Ok(exe_dir) = std::env::current_exe().map(|e| e.parent().map(|p| p.to_path_buf()).unwrap_or(e)) {
        let abs = |pp: &std::path::Path| {
            std::path::absolute(pp).unwrap_or_else(|_| pp.to_path_buf())
        };
        let (a, b) = (abs(&p), abs(&exe_dir));
        if a.starts_with(&b) || b.starts_with(&a) {
            return Err("不能选择应用安装目录内或包含安装目录的位置".into());
        }
    }
    // 可创建 + 可写
    std::fs::create_dir_all(&p).map_err(|e| format!("无法创建目录：{e}"))?;
    let probe = p.join(".zhizhang-write-test");
    std::fs::write(&probe, b"ok").map_err(|e| format!("目录不可写：{e}"))?;
    let _ = std::fs::remove_file(&probe);
    Ok(())
}

/// 在新数据根重启 backend（First Run 选位置 / 重连 / 迁移后共用）。
fn restart_backend_at(app: &AppHandle, root: std::path::PathBuf) {
    {
        let state = app.state::<LifecycleState>();
        *state.data_root.lock().unwrap() = root;
        *state.recovery.lock().unwrap() = None;
    }
    kill_owned_backend(app);
    // 重启期间页面保持在 boot/迁移画面；boot_sequence 完成后导航。
    boot_sequence(app.clone(), false);
}

/// First Run / 重连：设定账本位置（无历史数据需要迁移 —— First Run 中
/// 位置选择先于首次扫描）。目标若已有有效账本则直接采用（重连场景）。
#[tauri::command]
fn set_data_root(app: AppHandle, path: String) -> Result<serde_json::Value, String> {
    validate_new_data_root(&path)?;
    let p = std::path::PathBuf::from(&path);
    let adopt = bootstrap::has_valid_ledger(&p);
    bootstrap::write(&p)?;
    if adopt {
        // 采用已有账本 = 已有用户（§25 收编语义）。
        let mut st = desktop_state::load();
        st.first_run_complete = true;
        let _ = desktop_state::save(&st);
    }
    restart_backend_at(&app, p.clone());
    Ok(serde_json::json!({
        "adopted": adopt,
        "data_root": p.to_string_lossy(),
    }))
}

/// 恢复页「重新尝试」：重新探测账本位置；可用则清恢复态并正常启动。
#[tauri::command]
fn retry_recovery(app: AppHandle) -> serde_json::Value {
    let root = app.state::<LifecycleState>().root();
    if bootstrap::root_available(&root) {
        *app.state::<LifecycleState>().recovery.lock().unwrap() = None;
        boot_sequence(app.clone(), false);
        return serde_json::json!({ "ok": true });
    }
    serde_json::json!({ "ok": false, "message": "账本位置仍不可用" })
}

/// First Run 完成（首次扫描成功后调用）：记录 first_run_complete +
/// last_seen_version = 当前版本（§58：新用户不再弹 What's New）。
#[tauri::command]
fn complete_first_run(app: AppHandle) -> Result<(), String> {
    let mut st = desktop_state::load();
    st.first_run_complete = true;
    st.last_seen_version = Some(app.package_info().version.to_string());
    desktop_state::save(&st)
}

// ---------------------------------------------------------------- updater（RC.3 §40-52）

/// 检查更新（§40、§43）：仅用户显式触发（无默认后台检查，§41）。
/// 未配置更新源 → not_configured（如实展示，不虚构 URL）。
#[tauri::command]
async fn check_for_update(app: AppHandle) -> Result<serde_json::Value, String> {
    // 无 conf 端点且无 env 覆盖 → 如实报告未配置
    if app.updater_builder().build().is_err()
        && update_endpoint_override().is_none()
    {
        return Ok(serde_json::json!({ "status": "not_configured" }));
    }
    let update = build_updater(&app)?
        .check()
        .await
        .map_err(|e| format!("暂时无法检查更新：{e}"))?;
    Ok(match update {
        None => serde_json::json!({ "status": "up_to_date" }),
        Some(u) => serde_json::json!({
            "status": "available",
            "version": u.version,
            "current": u.current_version,
            "notes": u.body,
        }),
    })
}

/// 下载 → 签名验证 → 准备安装 → 安装并重启（§49）。
/// 签名由 tauri updater 在下载完成时强校验（§44/§50）：无效即报错、
/// 绝不安装，也不提供「仍要安装」通道。
#[tauri::command]
async fn download_and_install_update(app: AppHandle) -> Result<(), String> {
    let update = build_updater(&app)?
        .check()
        .await
        .map_err(|e| format!("暂时无法检查更新：{e}"))?
        .ok_or_else(|| "没有可用更新".to_string())?;

    // §49：下载进度事件（UI 显示进度条）
    let progress_app = app.clone();
    let mut downloaded: u64 = 0;
    let bytes = update
        .download(
            move |chunk, total| {
                downloaded += chunk as u64;
                let _ = progress_app.emit(
                    "update-download-progress",
                    serde_json::json!({ "downloaded": downloaded, "total": total }),
                );
            },
            || {},
        )
        .await
        .map_err(|e| format!("下载或签名验证失败：{e}"))?;

    // §51：安装前进入 UPDATE_INSTALL —— 停 scheduled scan、等待在途写、
    // 收尾 owned backend（Tray 隐藏导致的文件锁一并解决）。
    let _ = post_json(BACKEND_PORT, "/api/v1/maintenance/quiesce", "{}", 4000);
    std::thread::sleep(Duration::from_millis(300));
    kill_owned_backend(&app);

    // Windows：install 启动更新安装器后退出本应用。
    update
        .install(bytes)
        .map_err(|e| format!("安装失败：{e}"))?;
    Ok(())
}

/// What's New（§55-58）：仅升级用户（first_run_complete）且
/// last_seen_version != 当前版本时展示一次；启动即记录版本游标。
fn maybe_show_whats_new(app: &AppHandle) {
    let current = app.package_info().version.to_string();
    let mut st = desktop_state::load();
    if !st.first_run_complete {
        return; // 真正新用户 First Run 未完成：不弹（§56/§58）
    }
    if st.last_seen_version.as_deref() == Some(current.as_str()) {
        return; // 已展示过
    }
    st.last_seen_version = Some(current.clone());
    let _ = desktop_state::save(&st);
    // 拉模式：标志由 UI 在 boot 时经 get_desktop_state 领取（领取即清除）。
    // 推事件存在「页面未就绪丢事件」竞态（backend 就绪常 >5s），不可靠。
    app.state::<LifecycleState>()
        .whats_new_pending
        .store(true, Ordering::SeqCst);
}

/// 迁移演练（代理到 backend maintenance API，供「更改位置」确认弹层展示）。
#[tauri::command]
fn migrate_data_root_plan(target: String) -> Result<serde_json::Value, String> {
    let body = serde_json::json!({ "target": target }).to_string();
    let (status, text) = http_json(
        BACKEND_PORT,
        "POST",
        "/api/v1/maintenance/migrate/plan",
        Some(&body),
        20000,
    )
    .ok_or_else(|| "本地服务无响应".to_string())?;
    let v: serde_json::Value = serde_json::from_str(text.trim())
        .map_err(|e| format!("plan 响应解析失败：{e}"))?;
    if status == 200 {
        Ok(v)
    } else {
        Err(v.get("message")
            .and_then(|m| m.as_str())
            .unwrap_or("迁移演练失败")
            .to_string())
    }
}

/// 执行安全迁移（§29-36）：quiesce → execute → 原子切 bootstrap →
/// 新根重启 backend。任一步失败 → resume + 原账本原位可用。
#[tauri::command]
fn migrate_data_root_execute(app: AppHandle, target: String) -> Result<serde_json::Value, String> {
    let lifecycle = app.state::<LifecycleState>();
    if lifecycle.migrating.load(Ordering::SeqCst) {
        return Err("已有迁移正在进行".into());
    }
    lifecycle.migrating.store(true, Ordering::SeqCst);
    let result = migrate_execute_inner(&app, &target);
    // 迁移原子阶段结束（成功或失败）—— close/quit 不再被阻塞。
    app.state::<LifecycleState>().migrating.store(false, Ordering::SeqCst);
    result
}

fn migrate_execute_inner(app: &AppHandle, target: &str) -> Result<serde_json::Value, String> {
    // 1) 停止 scheduled work / 禁止新写
    let (qs, _) = http_json(BACKEND_PORT, "POST", "/api/v1/maintenance/quiesce", Some("{}"), 8000)
        .ok_or_else(|| "本地服务无响应，迁移未开始".to_string())?;
    if qs != 200 && qs != 409 {
        return Err(format!("无法进入维护状态（{qs}），迁移未开始"));
    }

    // 2) 复制 + 目标验证（backend 持有扫描锁执行）
    let body = serde_json::json!({ "target": target }).to_string();
    let (es, text) = http_json(
        BACKEND_PORT,
        "POST",
        "/api/v1/maintenance/migrate/execute",
        Some(&body),
        600000,
    )
    .ok_or_else(|| "本地服务无响应，迁移中止".to_string())?;
    if es != 200 {
        // §35：bootstrap 不动，恢复写路径，原账本继续可用
        let _ = http_json(BACKEND_PORT, "POST", "/api/v1/maintenance/resume", Some("{}"), 8000);
        let msg = serde_json::from_str::<serde_json::Value>(text.trim())
            .ok()
            .and_then(|v| v.get("message").and_then(|m| m.as_str()).map(|s| s.to_string()))
            .unwrap_or_else(|| format!("迁移失败（{es}）"));
        return Err(msg);
    }

    // 3) 原子切换 bootstrap → 新根重启（resume 不再需要：backend 将被收尾）
    let root = std::path::PathBuf::from(target);
    bootstrap::write(&root).map_err(|e| format!("bootstrap 切换失败：{e}"))?;
    let mut st = desktop_state::load();
    st.first_run_complete = true;
    let _ = desktop_state::save(&st);
    let lifecycle = app.state::<LifecycleState>();
    *lifecycle.pending_nav.lock().unwrap() = Some("/settings?tab=privacy".into());
    restart_backend_at(app, root.clone());
    Ok(serde_json::json!({
        "data_root": root.to_string_lossy(),
    }))
}

/// 设置「关闭主窗口时保持后台运行」开关（background=开，exit=关）。
/// 写入即结束 ASK_ON_FIRST_CLOSE 状态。
#[tauri::command]
fn set_close_behavior(behavior: String) -> Result<(), String> {
    if behavior != desktop_state::CLOSE_BACKGROUND && behavior != desktop_state::CLOSE_EXIT {
        return Err(format!("非法 close_behavior：{behavior}"));
    }
    let mut st = desktop_state::load();
    st.close_behavior = Some(behavior);
    desktop_state::save(&st)
}

/// 设置「登录 Windows 后启动智账」开关。返回 OS 注册的真实结果（§23）。
#[tauri::command]
fn set_autostart(app: AppHandle, enable: bool) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    let launcher = app.autolaunch();
    let result = if enable { launcher.enable() } else { launcher.disable() };
    result.map_err(|e| format!("开机启动注册失败：{e}"))?;
    Ok(app.autolaunch().is_enabled().unwrap_or(false))
}

/// 打开账本数据目录（设置/恢复页「打开文件夹」）。
/// 失联的自定义 root 不创建（§37）：改开其父目录，绝不制造空账本假象。
#[tauri::command]
fn open_data_root(app: AppHandle) {
    let dir = app.state::<LifecycleState>().root();
    let target = if dir.is_dir() {
        dir
    } else {
        dir.parent().map(|p| p.to_path_buf()).unwrap_or(dir)
    };
    #[cfg(windows)]
    let _ = Command::new("explorer").arg(target).spawn();
    #[cfg(not(windows))]
    let _ = target;
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---------------------------------------------------------------- Job lifecycle focused tests
    // 全部使用真实进程树（powershell 父 → powershell 子），不 mock bool。

    #[cfg(windows)]
    fn pid_alive(pid: u32) -> bool {
        let s = format!(
            "if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}"
        );
        Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &s])
            .creation_flags(CREATE_NO_WINDOW)
            .status()
            .unwrap()
            .success()
    }

    /// spawn 一个挂起态的 powershell 进程，挂入新 Job 后恢复执行；
    /// 进程本身会再拉起一个孙 powershell（真实 PyInstaller 树形态），
    /// 并把孙 PID 写到 stdout。
    #[cfg(windows)]
    fn spawn_job_child() -> (Child, winjob::OwnedJob, u32) {
        use std::io::BufRead;
        let script = "$c = Start-Process powershell.exe -ArgumentList @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 120') -PassThru -WindowStyle Hidden; Write-Output $c.Id; Start-Sleep -Seconds 120";
        let mut cmd = Command::new("powershell.exe");
        cmd.args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED)
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = cmd.spawn().expect("spawn suspended child");
        let job = winjob::OwnedJob::create().expect("create job");
        job.assign_pid(child.id()).expect("assign before resume");
        resume_process_threads(child.id()).expect("resume");
        let mut line = String::new();
        std::io::BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        let grandchild: u32 = line.trim().parse().expect("grandchild pid");
        (child, job, grandchild)
    }

    #[cfg(windows)]
    #[test]
    fn owned_job_kills_full_tree() {
        // 场景 1：owned tree（parent + child + grandchild）→ cleanup → 全部退出
        let (child, mut job, grandchild) = spawn_job_child();
        let parent_pid = child.id();
        assert!(pid_alive(parent_pid));
        assert!(pid_alive(grandchild));
        let mut handle = OwnedBackendHandle { child: Some(child), job: Some(job) };
        handle.cleanup();
        std::thread::sleep(std::time::Duration::from_millis(800));
        assert!(!pid_alive(grandchild), "grandchild 存活 —— Job 未覆盖子孙");
        // parent（bootloader 位）也应退出
        assert!(!pid_alive(parent_pid), "owned parent 存活 —— cleanup 未生效");
    }

    #[cfg(windows)]
    #[test]
    fn job_drop_kills_tree_on_close() {
        // 场景 10：不显式 terminate，仅 drop（关闭最后一个 handle）→ 整树终止
        let (child, job, grandchild) = spawn_job_child();
        let pid = child.id();
        assert!(pid_alive(grandchild));
        drop(job);
        drop(child);
        std::thread::sleep(std::time::Duration::from_millis(1200));
        assert!(!pid_alive(grandchild), "drop 后子孙存活 —— KILL_ON_JOB_CLOSE 未生效");
        assert!(!pid_alive(pid));
    }

    #[cfg(windows)]
    #[test]
    fn unassigned_process_survives_job_terminate() {
        // 场景 6/7/8 的系统级边界：只有被 assign 的进程属于本实例 Job。
        // 未 assign 的进程（= Reused / dev / incompatible external 的模型）
        // 在本实例 terminate Job 时必须存活。
        let (mut child, job, _gc) = spawn_job_child();
        let outsider = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 120"])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .expect("outsider");
        let outsider_pid = outsider.id();
        job.terminate();
        let mut handle = OwnedBackendHandle { child: Some(child), job: Some(job) };
        handle.cleanup();
        std::thread::sleep(std::time::Duration::from_millis(800));
        assert!(!pid_alive(handle_child_pid(&handle).unwrap_or(0)) || handle.child.is_none(),
                "owned child 仍在 —— cleanup 未生效");
        assert!(pid_alive(outsider_pid), "未 assign 的进程被误杀 —— ownership 边界被破坏");
        // 清理测试外部进程
        let _ = Command::new("taskkill.exe")
            .args(["/PID", &outsider_pid.to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }

    #[cfg(windows)]
    fn handle_child_pid(handle: &OwnedBackendHandle) -> Option<u32> {
        // cleanup 后 child 已被 take 走；本 helper 只在 cleanup 前使用
        handle.child.as_ref().map(|c| c.id())
    }

    #[cfg(windows)]
    #[test]
    fn owned_cleanup_idempotent_and_after_exit() {
        // 场景 4/5：backend 已提前退出 + 重复 cleanup → 不 panic、不误伤
        let quick = Command::new("cmd.exe")
            .args(["/C", "exit 0"])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .unwrap();
        let job = winjob::OwnedJob::create().unwrap();
        let _ = job.assign_pid(quick.id()); // 已退出/正退出：允许失败，不得 panic
        let mut handle = OwnedBackendHandle { child: Some(quick), job: Some(job) };
        handle.cleanup();
        handle.cleanup(); // 幂等
        handle.cleanup();
    }

    #[cfg(windows)]
    #[test]
    fn assign_pid_rejects_dead_pid_without_panic() {
        // 场景 9 的前置：PID 已不存在 → assign 返回 Err（绝不按 PID 查找后误杀）
        let mut quick = Command::new("cmd.exe")
            .args(["/C", "exit 0"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let dead = quick.id();
        let _ = quick.wait(); // 确认退出
        let job = winjob::OwnedJob::create().unwrap();
        let r = job.assign_pid(dead);
        assert!(r.is_err(), "对已退出 PID 的 assign 不应成功（PID 复用风险面）");
    }

    #[cfg(windows)]
    #[test]
    fn external_compatible_process_not_killed_by_cleanup() {
        // 场景 6（external compatible backend 模型）：一个「与 owned 无关」的
        // 独立兼容进程，未 assign 进本实例 Job → 本实例 cleanup 后必须存活。
        let ext = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 120"])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .unwrap();
        let ext_pid = ext.id();
        let (child, job, _gc) = spawn_job_child();
        let mut handle = OwnedBackendHandle { child: Some(child), job: Some(job) };
        handle.cleanup();
        std::thread::sleep(std::time::Duration::from_millis(800));
        assert!(pid_alive(ext_pid), "external backend 被 cleanup 误杀 —— Release Gate 失败");
        let _ = Command::new("taskkill.exe")
            .args(["/PID", &ext_pid.to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }

    #[cfg(windows)]
    #[test]
    fn owned_cleanup_terminates_nested_process() {
        use std::io::BufRead;
        let script = "$auditChild = Start-Process powershell.exe -ArgumentList @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 120') -PassThru -WindowStyle Hidden; Write-Output $auditChild.Id; Start-Sleep -Seconds 120";
        let mut parent = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", script])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap();
        let mut line = String::new();
        std::io::BufReader::new(parent.stdout.take().unwrap()).read_line(&mut line).unwrap();
        let descendant: u32 = line.trim().parse().unwrap();
        terminate_owned_child(&mut parent);
        let check = format!("if (Get-Process -Id {descendant} -ErrorAction SilentlyContinue) {{ exit 1 }}");
        let status = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &check])
            .creation_flags(CREATE_NO_WINDOW).status().unwrap();
        assert!(status.success(), "owned descendant remains alive");
    }

    const OK_BODY: &str = r#"{"app_id":"usage-ledger","app_version":"0.9.0-product-pages.r9","runtime_mode":"frozen","data_root_kind":"production","build_id":"ulb-v0.9.0-product-pages.r9-s1","schema_version":1,"backend_pid":1234}"#;

    fn resp(status: &str, body: &str) -> String {
        format!("HTTP/1.0 {status}\r\nContent-Type: application/json\r\n\r\n{body}")
    }

    fn expected() -> ExpectedBackend {
        ExpectedBackend {
            app_version: "0.9.0-product-pages.r9".into(),
            build_id: expected_build_tag("0.9.0-product-pages.r9"),
            data_root_kind: "production".into(),
        }
    }

    #[test]
    fn build_tag_matches_python_contract() {
        // serve.py identity_payload: 'ulb-v%s-s%d' % (app_version, BOARD_SCHEMA_VERSION)
        assert_eq!(expected_build_tag("0.9.0-product-pages.r9"), "ulb-v0.9.0-product-pages.r9-s1");
    }

    #[test]
    fn parse_ok() {
        let id = parse_identity_response(&resp("200 OK", OK_BODY)).expect("must parse");
        assert_eq!(id.app_id, "usage-ledger");
        assert_eq!(id.runtime_mode, "frozen");
        assert_eq!(id.data_root_kind, "production");
        assert_eq!(id.schema_version, 1);
        assert_eq!(id.backend_pid, 1234);
    }

    #[test]
    fn parse_rejects_non_200() {
        let raw = resp("404 Not Found", "{\"error\":\"not_found\"}");
        assert!(parse_identity_response(&raw).is_err());
    }

    #[test]
    fn parse_rejects_garbage() {
        assert!(parse_identity_response(&resp("200 OK", "<html>hello</html>")).is_err());
    }

    #[test]
    fn parse_rejects_missing_field() {
        let body = r#"{"app_id":"usage-ledger","runtime_mode":"frozen"}"#;
        assert!(parse_identity_response(&resp("200 OK", body)).is_err());
    }

    #[test]
    fn parse_rejects_wrong_type() {
        let body = OK_BODY.replace("\"schema_version\":1", "\"schema_version\":\"1\"");
        assert!(parse_identity_response(&resp("200 OK", &body)).is_err());
    }

    #[test]
    fn compatible_frozen_production_same_build() {
        let id = parse_identity_response(&resp("200 OK", OK_BODY)).unwrap();
        assert!(identity_reuse_compatible(&id, &expected()).is_ok());
    }

    #[test]
    fn reject_dev_mode() {
        let body = OK_BODY.replace("\"runtime_mode\":\"frozen\"", "\"runtime_mode\":\"dev\"");
        let id = parse_identity_response(&resp("200 OK", &body)).unwrap();
        let e = identity_reuse_compatible(&id, &expected()).unwrap_err();
        assert!(e.contains("runtime_mode"), "{e}");
    }

    #[test]
    fn reject_repo_data_root() {
        let body = OK_BODY.replace("\"data_root_kind\":\"production\"", "\"data_root_kind\":\"repo\"");
        let id = parse_identity_response(&resp("200 OK", &body)).unwrap();
        let e = identity_reuse_compatible(&id, &expected()).unwrap_err();
        assert!(e.contains("data_root_kind"), "{e}");
    }

    #[test]
    fn reject_other_app_id() {
        let body = OK_BODY.replace("\"app_id\":\"usage-ledger\"", "\"app_id\":\"other-product\"");
        let id = parse_identity_response(&resp("200 OK", &body)).unwrap();
        let e = identity_reuse_compatible(&id, &expected()).unwrap_err();
        assert!(e.contains("app_id"), "{e}");
    }

    #[test]
    fn reject_schema_mismatch() {
        let body = OK_BODY.replace("\"schema_version\":1", "\"schema_version\":2");
        let id = parse_identity_response(&resp("200 OK", &body)).unwrap();
        let e = identity_reuse_compatible(&id, &expected()).unwrap_err();
        assert!(e.contains("schema_version"), "{e}");
    }

    #[test]
    fn reject_version_mismatch() {
        let body = OK_BODY.replace("0.9.0-product-pages.r9-s1", "0.8.0-old-s1");
        let body = body.replace("\"app_version\":\"0.9.0-product-pages.r9\"", "\"app_version\":\"0.8.0-old\"");
        let id = parse_identity_response(&resp("200 OK", &body)).unwrap();
        assert!(identity_reuse_compatible(&id, &expected()).is_err());
    }

    #[test]
    fn reject_build_id_mismatch() {
        let body = OK_BODY.replace("ulb-v0.9.0-product-pages.r9-s1", "ulb-v0.9.0-product-pages.r9-s9");
        let id = parse_identity_response(&resp("200 OK", &body)).unwrap();
        assert!(identity_reuse_compatible(&id, &expected()).is_err());
    }

    #[test]
    fn custom_home_expects_custom_kind() {
        // 环境感知策略只由 expected.data_root_kind 表达；这里验证比较逻辑本身。
        let body = OK_BODY.replace("\"data_root_kind\":\"production\"", "\"data_root_kind\":\"custom\"");
        let id = parse_identity_response(&resp("200 OK", &body)).unwrap();
        let exp = ExpectedBackend {
            data_root_kind: "custom".into(),
            ..expected()
        };
        assert!(identity_reuse_compatible(&id, &exp).is_ok());
        assert!(identity_reuse_compatible(&id, &expected()).is_err());
    }
}
