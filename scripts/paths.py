# -*- coding: utf-8 -*-
"""路径与环境自检：skill 代码与运行期数据分离（跨平台，无绝对路径硬编码）

  SKILL_DIR   skill 安装目录（含 config.json / templates / scripts）
              —— 只读、随 skill 分发，**不放运行期产物**
  DATA_ROOT   运行期数据根（data/ 下放快照与缓存，out/ 下放报告）
              —— 与 skill 目录分离，位置可换、可备份、不入库

DATA_ROOT 解析优先级（从高到低）：
  1. 环境变量 STOCK_REVIEW_HOME
  2. 默认 ~/.stock-review

数据根禁止落在 skill 目录内 —— 否则每日快照、4MB+ 缓存会混进 skill 包，
既破坏可移植性，也会在打包/分发时泄露本地数据。
"""
import importlib.util
import os
import sys

# ---------------- 代码根（随 skill 走） ----------------
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(SKILL_DIR, "config.json")
TPL_PATH = os.path.join(SKILL_DIR, "templates", "report.html")

# ---------------- 数据根（随环境走） ----------------
ENV_HOME = "STOCK_REVIEW_HOME"
DEFAULT_HOME = os.path.join(os.path.expanduser("~"), ".stock-review")

# ---------------- 运行依赖 ----------------
PY_MIN = (3, 9)
CORE_DEPS = ("requests", "akshare", "pandas")
EXTRA_DEPS = ("lxml", "bs4")


def resolve_data_root():
    """解析数据根，并校验其不在 skill 目录内。"""
    env = (os.environ.get(ENV_HOME) or "").strip()
    root = os.path.abspath(os.path.expanduser(env) if env else DEFAULT_HOME)
    skill = os.path.normcase(SKILL_DIR).rstrip("\\/")
    cur = os.path.normcase(root)
    if cur == skill or cur.startswith(skill + os.sep):
        raise SystemExit(
            "[路径错误] 数据根不能位于 skill 目录内。\n"
            f"  skill 目录 : {SKILL_DIR}\n"
            f"  数据根     : {root}\n"
            f"  解决办法   : 设环境变量 {ENV_HOME} 指向 skill 目录之外的位置，"
            f"或留空使用默认值 {DEFAULT_HOME}")
    return root


DATA_ROOT = resolve_data_root()
HIST_DIR = os.path.join(DATA_ROOT, "data", "history")
AI_DIR = os.path.join(DATA_ROOT, "data", "ai")
CACHE_DIR = os.path.join(DATA_ROOT, "data", "cache")
HOLD_PATH = os.path.join(DATA_ROOT, "data", "holdings.json")
OUT_DIR = os.path.join(DATA_ROOT, "out")

DATA_DIRS = (HIST_DIR, AI_DIR, CACHE_DIR, OUT_DIR)


# ---------------- 目录 ----------------
def ensure_dirs():
    """建好数据根下的全部目录（首次运行 / 换数据根时必需）。"""
    for d in DATA_DIRS:
        os.makedirs(d, exist_ok=True)


# ---------------- 依赖自检 ----------------
def has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def missing_deps():
    return [n for n in CORE_DEPS if not has_module(n)]


def pip_hint():
    req = os.path.join(SKILL_DIR, "requirements.txt")
    venv = os.path.join(DATA_ROOT, ".venv")
    nix = venv.replace("\\", "/")
    return (
        "请任选一种方式安装依赖（Python >= %d.%d）：\n" % PY_MIN
        + "\n"
        "  A) 装进当前解释器（最省事）\n"
        f"     \"{sys.executable}\" -m pip install -r \"{req}\"\n"
        "\n"
        "  B) 装进数据根下的独立虚拟环境（推荐，不动系统 Python）\n"
        f"     \"{sys.executable}\" -m venv \"{venv}\"\n"
        f"     # Linux / macOS : \"{nix}/bin/pip\" install -r \"{req}\"\n"
        f"     # Windows       : \"{venv}\\Scripts\\pip.exe\" install -r \"{req}\"\n"
        "     之后统一用该虚拟环境里的 Python 运行本 skill 的脚本：\n"
        f"     # Linux / macOS : \"{nix}/bin/python\" <skill目录>/scripts/fetch_report_data.py\n"
        f"     # Windows       : \"{venv}\\Scripts\\python.exe\" <skill目录>/scripts/fetch_report_data.py\n"
        "\n"
        "  C) 依赖已经装好、只是用错了解释器\n"
        "     → 换成装了依赖的那个 Python 重跑即可\n"
        "       （本 skill 的脚本与 cwd 无关，可用任意解释器 + 绝对路径调用；\n"
        "         也可先跑 --show-paths 自检，看当前解释器是否满足依赖）"
    )


def ensure_deps():
    """缺少核心依赖时，打印可照做的安装指引后退出（而不是抛裸 traceback）。"""
    miss = missing_deps()
    if not miss:
        return
    print("=" * 64)
    print("[依赖缺失] 当前解释器缺少：" + "、".join(miss))
    print(f"  解释器   : {sys.executable}")
    print(f"  Python   : {sys.version.split()[0]}")
    print(f"  skill目录: {SKILL_DIR}")
    print("=" * 64)
    print(pip_hint())
    print("=" * 64)
    raise SystemExit(2)


def _writable(path):
    """判断 path（或其最近的已存在父目录）是否可写。"""
    p = path
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return os.path.exists(p) and os.access(p, os.W_OK)
    except Exception:  # noqa: BLE001
        return False


def describe():
    """运行环境自检：路径 / Python / 依赖 / 可写性。首次运行先跑这个。"""
    deps = "  ".join(f"{n} {'✓' if has_module(n) else '✗'}" for n in CORE_DEPS + EXTRA_DEPS)
    src = (f"环境变量 {ENV_HOME}" if (os.environ.get(ENV_HOME) or "").strip() else "默认值")
    py = f"{sys.version.split()[0]}  ({'OK' if sys.version_info[:2] >= PY_MIN else '需 >= %d.%d' % PY_MIN})"
    return (
        "运行环境自检\n"
        + "-" * 64 + "\n"
        f"Python     : {py}\n"
        f"解释器     : {sys.executable}\n"
        f"依赖       : {deps}" + ("   ← 缺失，见下方提示" if missing_deps() else "") + "\n"
        f"skill 目录 : {SKILL_DIR}\n"
        f"数据根     : {DATA_ROOT}  (来源: {src})  可写: {'✓' if _writable(DATA_ROOT) else '✗'}\n"
        f"  快照     : {HIST_DIR}\n"
        f"  AI归因   : {AI_DIR}\n"
        f"  缓存     : {CACHE_DIR}\n"
        f"  报告     : {OUT_DIR}"
    )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(describe())
    if missing_deps():
        print()
        print(pip_hint())
