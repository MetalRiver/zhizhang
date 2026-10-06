"""paths.py — Usage Ledger 统一路径事实源（Round 10）。

分离 APP_ROOT（程序资源，升级可覆盖）与 DATA_ROOT（用户数据，升级保留）。

开发模式（源码仓）：
    APP_ROOT = DATA_ROOT = repo root（与 R0-R9 行为完全兼容）

Frozen 模式（PyInstaller onefdir）：
    APP_ROOT = exe 所在目录（程序资源只读）
    DATA_ROOT = %LOCALAPPDATA%\\UsageLedger（用户数据持久）

可用环境变量 USAGE_LEDGER_HOME 覆盖 DATA_ROOT（高级用户）。

所有 ledger.py / autopilot.py / serve.py 通过 `from paths import get_paths`
获取路径，禁止各自散落 path 判断。
"""
import os
import sys

_FROZEN = getattr(sys, 'frozen', False)

if _FROZEN:
    # PyInstaller onefdir: sys._MEIPASS 是临时解压目录内的 app dir
    APP_ROOT = os.path.dirname(sys.executable)
    _APPDATA = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
    DATA_ROOT = os.environ.get('USAGE_LEDGER_HOME') or \
        os.path.join(_APPDATA, 'UsageLedger')
else:
    # 开发模式：repo root = 脚本所在目录
    APP_ROOT = os.path.dirname(os.path.abspath(__file__))
    DATA_ROOT = APP_ROOT


def ensure_data_root():
    """确保 DATA_ROOT 存在（首次启动自动创建）。"""
    os.makedirs(DATA_ROOT, exist_ok=True)
    return DATA_ROOT


def data_path(name):
    """返回 DATA_ROOT 下的文件路径。"""
    return os.path.join(DATA_ROOT, name)


def app_path(*parts):
    """返回 APP_ROOT 下的资源路径（只读）。"""
    return os.path.join(APP_ROOT, *parts)


# ---- 具体路径 helpers（供 ledger / autopilot / serve 共享）----
def db_path():
    return data_path('usage.db')

def board_path():
    return data_path('usage-board.json')

def health_path():
    return data_path('health.json')

def run_state_path():
    return data_path('run-state.json')

def discovery_path():
    return data_path('discovery.json')

def resolved_sources_path():
    return data_path('resolved-sources.json')

def sources_override_path():
    return data_path('sources.json')

def pricing_manual_path():
    return data_path('pricing.json')

def pricing_auto_path():
    return data_path('pricing.auto.json')

def pricing_backup_path(tag):
    return data_path('pricing.json.' + tag)

def auto_log_path():
    return data_path('auto.log')

def schedule_path():
    return data_path('auto-schedule.json')

def replay_state_path():
    return data_path('replay-state.json')

def scan_lock_path():
    return data_path('usage-ledger-scan.lock')

def server_state_path():
    return data_path('server-state.json')

def server_log_path():
    return data_path('server.log')

def web_dir():
    return app_path('web')

def schema_path():
    return app_path('usage-board.schema.json')

def is_frozen():
    return _FROZEN


def bundled_resource_root():
    """随包只读资源的**真实运行时事实**根目录。

    frozen：PyInstaller 打包的资源根 = sys._MEIPASS（onedir 形态下即
    _internal；source-catalog.json / VERSION / web 静态资源都在这里，
    本轮构建演练已按真实 runtime 事实验证）。若未来打包形态变化导致
    _MEIPASS 缺失，保守回退 exe 目录 —— 绝不用 cwd 猜。
    dev：repo root（= APP_ROOT）。
    """
    if _FROZEN:
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass and os.path.isdir(meipass):
            return meipass
        return APP_ROOT
    return APP_ROOT


def application_resource_path(name):
    """只读程序资源（随程序发布、由版本控制的静态文件）的正式读取路径。

    资源随程序版本一起升级：frozen 永远读 bundled resource root，
    绝不读 DATA_ROOT（那里最多只有用户数据与历史运行态文件）；
    dev 读 repo root。catalog read path != state write path。
    """
    return os.path.join(bundled_resource_root(), name)
