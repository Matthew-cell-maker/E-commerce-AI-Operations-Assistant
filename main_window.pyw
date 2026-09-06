"""
main_window.pyw
电商运营助手 Pro —— 主界面入口。

职责划分：
  AI_Assistant_UI  —— CustomTkinter 主窗口，负责 UI 构建与用户交互。
  ModelDownloadWindow —— Ollama 模型首次下载进度弹窗。
  辅助函数（模块级）—— Ollama 进程管理、机器码与激活、单实例锁。
"""

import ctypes
import hashlib
import json
import logging
import os
import queue as _queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from tkinter import filedialog, messagebox

import customtkinter as ctk
import psutil
import requests
from PIL import Image

from ollama_service import (
    check_models_installed as _check_ollama_models,
    get_port_owners as _get_ollama_port_owners,
    is_ready as _ollama_api_is_ready,
    list_installed_models as _list_ollama_models,
)

from prompts import (
    SUMMARY_STEP1_CLASSIFY,
    SUMMARY_STEP2_REPORT,
    SUMMARY_TEMPLATES,
    VIDEO_DURATIONS,
    VIDEO_STYLES,
    VIDEO_TYPES,
    build_video_script_prompt,
    build_writing_prompt,
    classify_doc_type,
)
from langchain_community.document_loaders import PyPDFLoader

from rag_manager import (
    QueryStatus,
    RagError,
    add_pdf,
    clear_all,
    delete_file,
    get_docs_path,
    get_uploaded_files,
    query_rag,
    query_rag_by_file,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _setup_file_logging() -> None:
    """把日志同时写入 ProgramData 下的文件，便于打包后（无控制台）排查问题。"""
    try:
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        log_dir = os.path.join(program_data, "EcomAssistant")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "app.log")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        logger.info("===== 程序启动，日志文件：%s =====", log_path)
    except Exception:
        pass  # 文件日志失败不影响程序运行


_setup_file_logging()

# ---------------------------------------------------------------------------
# 外观常量
# ---------------------------------------------------------------------------
ctk.set_appearance_mode("Light")

CLR_BG      = "#F2F4F7"
CLR_SIDEBAR = "#E5E9F0"
CLR_TEXT    = "#2D3436"
CLR_INPUT   = "#FFFFFF"
CLR_BORDER  = "#D1D9E6"

C_GEN   = "#B8E1FF"
C_RE    = "#A9DEF9"
C_STOP  = "#D0F4DE"
C_COPY  = "#E4C1F9"
C_CLEAR = "#D1D5DB"

SEG_UNSELECTED = "#D1EAFF"
SEG_SELECTED   = "#A9DEF9"  # 修正：原值与 C_RE 重复，改用更深一阶蓝绿

APP_TITLE = "电商运营助手 Pro"

# 所有写作模式
WRITING_MODES = ["爆款标题生成", "推广软文生成", "职场战报汇报", "日常汇报", "转正述职", "年度汇报", "短视频脚本"]
REPORT_SUB_MODES = ["日常汇报", "转正述职", "年度汇报"]
VIDEO_MODE = "短视频脚本"

# ---------------------------------------------------------------------------
# 系统工具函数
# ---------------------------------------------------------------------------

def get_base_path() -> str:
    if hasattr(sys, "_MEIPASS"):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _vram_from_registry() -> float:
    """
    从注册表读取各显卡的真实显存（GB）。
    字段 HardwareInformation.qwMemorySize 是 64 位，N卡/A卡/Intel 都适用，
    不像 WMI 的 AdapterRAM 受 32 位限制（4GB 以上会错）。
    返回检测到的最大显存；完全读不到返回 -1。
    """
    best = -1.0
    try:
        import winreg
    except Exception:
        return -1.0
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except Exception:
        return -1.0
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break  # 遍历结束
        except Exception:
            i += 1
            continue
        i += 1
        if not sub.isdigit():
            continue
        # 每个子键独立处理，任何异常都不影响其他子键
        try:
            k = winreg.OpenKey(root, sub)
        except Exception:
            continue
        for field in ("HardwareInformation.qwMemorySize",
                      "HardwareInformation.MemorySize"):
            try:
                val, _ = winreg.QueryValueEx(k, field)
                gb = int(val) / (1024 ** 3)
                if gb > best:
                    best = gb
            except Exception:
                continue
        try:
            winreg.CloseKey(k)
        except Exception:
            pass
    try:
        winreg.CloseKey(root)
    except Exception:
        pass
    return best


def _vram_from_nvidia_smi() -> float:
    """用 nvidia-smi 查 N 卡显存（GB）。仅作 N 卡补充，查不到返回 -1。"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        ).decode(errors="ignore")
        vals = [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
        if vals:
            return max(vals) / 1024  # MB → GB
    except Exception:
        pass
    return -1.0


def detect_gpu_vram_gb() -> float:
    """
    检测显存（GB），覆盖 N卡/A卡/Intel。
    多源策略：先读注册表通用字段（覆盖所有显卡），失败再用 nvidia-smi（N卡补充）。
    返回值语义：
        > 0  ：检测到的显存大小
        < 0  ：检测失败/无法判断（上层给安全的中间档）
    """
    vram = _vram_from_registry()
    if vram > 0:
        return vram
    # 注册表没读到，尝试 nvidia-smi 作为 N 卡补充
    vram = _vram_from_nvidia_smi()
    if vram > 0:
        return vram
    return -1.0


# 各档位对应的模型清单（下载用）与默认模型。
MODEL_7B = "qwen2.5:7b"
MODEL_3B = "qwen2.5:3b"
MODEL_1_5B = "qwen2.5:1.5b"

# 档位元数据：用于选择界面展示。key 为档位标识。
TIER_INFO = {
    "high": {
        "name": "高配（7B 模型）",
        "model": MODEL_7B,
        "download": [MODEL_7B, MODEL_3B, MODEL_1_5B],
        "size": "约 5GB",
        "effect": "生成质量最佳，文案更细腻、更智能",
        "need": "建议显存 ≥ 7GB",
        "speed": "显存充足时流畅；显存不足会退用CPU，明显变慢",
    },
    "mid": {
        "name": "标准（3B 模型）",
        "model": MODEL_3B,
        "download": [MODEL_3B, MODEL_1_5B],
        "size": "约 2GB",
        "effect": "质量与速度均衡，日常文案足够用",
        "need": "建议显存 4~7GB",
        "speed": "多数电脑流畅运行",
    },
    "low": {
        "name": "轻量（1.5B 模型）",
        "model": MODEL_1_5B,
        "download": [MODEL_1_5B],
        "size": "约 1GB",
        "effect": "质量一般，适合简单短文案",
        "need": "无独显 / 显存 < 4GB 也能跑",
        "speed": "即使无显卡用CPU也能运行，最省资源",
    },
}


def recommend_tier() -> tuple[float, str]:
    """检测显存并返回 (显存GB, 推荐档位key)。显存<0 表示检测失败。"""
    vram = detect_gpu_vram_gb()
    logger.info("检测到显存：%.2f GB", vram)
    if vram < 0:
        return vram, "mid"   # 检测失败推荐标准档
    if vram >= 7:
        return vram, "high"
    if vram >= 4:
        return vram, "mid"
    return vram, "low"


def detect_model_plan() -> tuple[str, list[str]]:
    """
    根据显存分档，返回 (默认模型, 需要下载的模型列表)。
    （保留兼容：无界面选择时仍按推荐档自动决定。）
    """
    _, tier = recommend_tier()
    info = TIER_INFO[tier]
    return info["model"], info["download"]


def detect_target_model() -> str:
    """兼容旧调用：仅返回默认模型。"""
    return detect_model_plan()[0]


# ---------------------------------------------------------------------------
# 激活系统（基于 Ed25519 签名验证，验证逻辑见 license_core.py）
# 客户端只内置公钥，无法伪造激活码；激活码由作者用私钥(keygen.py)签发。
# ---------------------------------------------------------------------------
import license_core


def _read_machine_guid() -> str:
    """
    从注册表读取 Windows MachineGuid。
    该值在所有 Windows 版本上都存在（不依赖已被新系统移除的 wmic），
    系统安装时随机生成，全局唯一，且不随磁盘分区/换硬盘/插拔U盘变化。
    （注：重装系统会重新生成，属可接受范围。）
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        val, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        return str(val).strip()
    except Exception:
        return ""


def get_machine_code() -> str:
    """
    生成本机稳定唯一的机器码。
    以注册表 MachineGuid 为主，做 SHA256 取前 16 位。
    优点：不依赖 wmic（新版 Windows 已移除 wmic），磁盘分区/换硬盘均不变。
    """
    guid = _read_machine_guid()
    if guid:
        return hashlib.sha256(guid.encode()).hexdigest()[:16].upper()

    # 极端兜底：连注册表都读不到时，用 MAC（基本不会触发）
    return hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()[:16].upper()


def verify_activation(machine_code: str, activation_key: str) -> bool:
    """用内置公钥验证激活码对本机是否有效。"""
    return license_core.verify_activation_key(machine_code, activation_key)


# ---------------------------------------------------------------------------
# Ollama 进程管理
# ---------------------------------------------------------------------------

def _path_is_ascii_safe(path: str) -> bool:
    """检测路径是否只含 ASCII 字符。含中文等非 ASCII 会导致 llama-server 加载模型失败。"""
    try:
        path.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def get_data_root() -> str:
    """
    返回统一的数据根目录 C:\\ProgramData\\EcomAssistant。
    所有运行时数据（模型、向量库、上传文档、索引、激活记录）都存这里：
      - 可写：程序本体可装在 Program Files（只读），数据写到此处不受权限限制
      - 纯英文：ProgramData 路径与用户名无关，永不含中文，规避 llama-server 加载失败
      - 易清理：卸载时整目录删除即可
    """
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    root = os.path.join(program_data, "EcomAssistant")
    os.makedirs(root, exist_ok=True)
    return root


def _get_portable_models_path() -> str:
    """模型目录：固定 C:\\ProgramData\\EcomAssistant\\models。"""
    path = os.path.join(get_data_root(), "models")
    os.makedirs(path, exist_ok=True)
    return path


def _find_ollama_exe() -> str | None:
    """
    定位便携版 ollama.exe（仅限程序目录）。

    便携部署要求：程序目录下需自带完整的 ollama.exe 及其 lib 目录
    （含 llama-server.exe 与推理库），这样目标机器无需预装 Ollama。

    注意：仅有 ollama.exe 而缺少 llama-server.exe 时，推理/embedding 会返回 500，
    因此这里同时校验配套组件是否存在。
    """
    base = get_base_path()
    local_bin = os.path.join(base, "ollama.exe")
    if not os.path.exists(local_bin):
        return None

    # 校验配套的 llama-server.exe（不同版本可能位于 lib\\ollama\\ 或 lib\\ 或同级目录）
    server_candidates = [
        os.path.join(base, "lib", "ollama", "llama-server.exe"),
        os.path.join(base, "lib", "llama-server.exe"),
        os.path.join(base, "llama-server.exe"),
    ]
    if not any(os.path.exists(p) for p in server_candidates):
        messagebox.showerror(
            "组件不完整",
            "检测到 ollama.exe，但缺少配套的 llama-server 组件。\n"
            "请将完整的 Ollama 程序文件夹（含 lib 子目录）一起放入程序目录，"
            "而不仅仅是 ollama.exe。",
        )
        return None

    return local_bin


_started_ollama_proc = None  # 记录本程序启动的 ollama 进程，退出时清理


def _shutdown_local_ollama() -> None:
    """关闭由本程序启动的 ollama 进程（不影响用户自己运行的 ollama）。"""
    global _started_ollama_proc
    if _started_ollama_proc is None:
        return
    try:
        if _started_ollama_proc.poll() is None:
            _started_ollama_proc.terminate()
            try:
                _started_ollama_proc.wait(timeout=5)
            except Exception:
                _started_ollama_proc.kill()
        logger.info("已关闭本程序启动的 ollama 进程")
    except Exception as exc:
        logger.warning("关闭 ollama 进程失败：%s", exc)
    finally:
        _started_ollama_proc = None


def _read_tail(path: str, n_chars: int) -> str:
    """读取文件末尾若干字符，用于展示错误日志。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read()
        return data[-n_chars:] if len(data) > n_chars else data
    except Exception:
        return "（无法读取日志）"


def start_local_ollama() -> bool:
    """
    启动 ollama 服务。
    - 若已有可用服务在 11434 运行（如用户自己装了 ollama），直接复用，不重启不杀进程。
    - 否则启动自带便携版，捕获其输出；若进程中途崩溃能立即拿到真实错误，
      而不是干等到超时（CUDA 不兼容/缺运行库/lib 损坏等问题这样才能暴露）。
    """
    # 已有可用服务则复用
    if _ollama_is_ready():
        logger.info("检测到已运行的 ollama 服务，直接复用")
        return True

    bin_path = _find_ollama_exe()
    if not bin_path:
        messagebox.showerror(
            "缺少组件",
            "未找到 ollama.exe。\n请先安装 Ollama（https://ollama.com），"
            "或将完整版 ollama.exe 放入程序目录。",
        )
        return False

    # 服务不可用且端口已被监听时，绝不擅自结束其他程序。
    # 用户可能在运行自己的 Ollama 或其他本地服务，强制 terminate 会造成数据
    # 丢失或中断工作。明确展示占用者，由用户自行决定如何处理。
    port_owners = _get_ollama_port_owners(11434, logger)
    if port_owners:
        owner_text = "、".join(port_owners)
        logger.warning("端口 11434 已被占用：%s", owner_text)
        messagebox.showerror(
            "AI 服务端口被占用",
            "端口 11434 已被其他程序占用，且该服务无法作为 Ollama 正常响应。\n\n"
            f"占用进程：{owner_text}\n\n"
            "请关闭或重启该程序后再试。为避免影响您的其他工作，"
            "本应用不会自动结束任何进程。",
        )
        return False

    try:
        env = os.environ.copy()
        # 模型固定到 C:\ProgramData\EcomAssistant\models（英文路径，规避中文 bug）
        models_dir = _get_portable_models_path()
        env["OLLAMA_MODELS"] = models_dir
        # 本地回环请求绕过代理，避免用户开 VPN/代理时 127.0.0.1 被拦截
        no_proxy = env.get("NO_PROXY", "")
        for host in ("127.0.0.1", "localhost"):
            if host not in no_proxy:
                no_proxy = f"{no_proxy},{host}" if no_proxy else host
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy

        # 捕获 ollama 输出到日志，崩溃时可读取真实错误
        log_path = os.path.join(get_data_root(), "ollama_serve.log")
        try:
            log_file = open(log_path, "w", encoding="utf-8", errors="ignore")
        except Exception:
            log_file = None

        proc = subprocess.Popen(
            [bin_path, "serve"],
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=(log_file or subprocess.DEVNULL),
            stderr=(subprocess.STDOUT if log_file else subprocess.DEVNULL),
        )
        global _started_ollama_proc
        _started_ollama_proc = proc

        # 等待就绪，同时检测进程是否中途崩溃。
        # 全新电脑首次初始化 GPU/CUDA 较慢，给足 90 秒。
        for _ in range(90):
            if _ollama_is_ready():
                return True
            if proc.poll() is not None:
                # 进程已退出 = 启动失败
                detail = _read_tail(log_path, 1500)
                logger.error("ollama 启动后立即退出，日志：\n%s", detail)
                messagebox.showerror(
                    "AI 服务启动失败",
                    "AI 引擎未能启动，通常与显卡驱动或系统运行库有关。\n\n"
                    "请尝试：\n"
                    "1. 更新显卡驱动到最新版本；\n"
                    "2. 安装 Microsoft Visual C++ 运行库；\n"
                    "3. 暂时关闭杀毒软件后重试。\n\n"
                    f"错误日志：{log_path}",
                )
                return False
            time.sleep(1)

        messagebox.showerror(
            "启动超时",
            "AI 服务启动超时。\n\n"
            "可能原因：\n"
            "1. 首次启动需初始化，请稍候重试；\n"
            "2. 杀毒软件/防火墙拦截，请允许本程序联网；\n"
            "3. 端口 11434 被占用。\n\n"
            f"详细日志：{os.path.join(get_data_root(), 'ollama_serve.log')}",
        )
        return False
    except Exception as exc:
        logger.error("启动 Ollama 失败：%s", exc)
        messagebox.showerror("启动失败", f"启动 AI 服务时出错：\n{exc}")
        return False


def _get_port_owners(port: int) -> list[str]:
    """兼容旧调用：返回正在监听指定端口的进程描述。"""
    return _get_ollama_port_owners(port, logger)


def _ollama_is_ready() -> bool:
    """兼容旧调用：检查 Ollama 模型 API 是否就绪。"""
    return _ollama_api_is_ready()


def check_models_installed(required_models: list[str]) -> bool:
    """
    检查给定的模型清单 + 向量模型是否都已安装。
    直接询问 ollama 服务（/api/tags），不依赖固定目录路径。
    """
    return _check_ollama_models(required_models, "nomic-embed-text")


def check_model_in_folder(target_model: str) -> bool:
    """兼容旧调用：检查单个主模型 + 向量模型。"""
    return check_models_installed([target_model])


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------

def acquire_single_instance_lock() -> socket.socket | None:
    """绑定本地端口实现单实例。成功返回 socket，失败返回 None。
    使用 SO_REUSEADDR 降低残留占用导致误判的概率。"""
    try:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lock.bind(("127.0.0.1", 54321))
        lock.listen(1)
        return lock
    except socket.error:
        return None


def focus_existing_window() -> None:
    for title in (APP_TITLE, "激活软件"):
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return


# ---------------------------------------------------------------------------
# 档位选择弹窗
# ---------------------------------------------------------------------------

class TierSelectWindow(ctk.CTkToplevel):
    """
    首次使用时让用户选择模型档位。
    显示检测到的显存、推荐档位、各档位效果与速度说明，由用户自主选择后下载。
    """

    def __init__(self, master, vram_gb: float, recommended: str, on_choose, on_cancel=None):
        super().__init__(master)
        self.title("选择 AI 模型档位")
        self.geometry("560x600")
        self.configure(fg_color=CLR_BG)
        self.on_choose = on_choose
        self.on_cancel = on_cancel
        self._selected = recommended
        self._done = False
        # 用户点叉关闭：取消整个流程，避免 root 卡死
        self.protocol("WM_DELETE_WINDOW", self._on_user_close)

        # 顶部：检测结果
        if vram_gb < 0:
            vram_text = "未能自动识别显卡显存"
        else:
            vram_text = f"检测到显卡显存：约 {vram_gb:.1f} GB"

        ctk.CTkLabel(
            self, text="🎯 选择 AI 模型档位",
            font=ctk.CTkFont(size=22, weight="bold"), text_color=CLR_TEXT,
        ).pack(pady=(20, 4))
        ctk.CTkLabel(
            self, text=vram_text,
            font=ctk.CTkFont(size=14), text_color="#2980B9",
        ).pack(pady=(0, 2))
        ctk.CTkLabel(
            self, text=f"为你推荐：{TIER_INFO[recommended]['name']}（你也可以自行选择）",
            font=ctk.CTkFont(size=12), text_color="#7F8C8D",
        ).pack(pady=(0, 12))

        # 三个档位卡片
        self._tier_var = ctk.StringVar(value=recommended)
        for key in ("high", "mid", "low"):
            self._make_tier_card(key, recommended)

        # 底部按钮
        btn = ctk.CTkButton(
            self, text="开始下载所选档位", height=40,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#3D7BF0", hover_color="#2156C8",
            command=self._confirm,
        )
        btn.pack(pady=16)

    def _make_tier_card(self, key: str, recommended: str):
        info = TIER_INFO[key]
        is_rec = (key == recommended)

        card = ctk.CTkFrame(self, fg_color=CLR_INPUT, corner_radius=12,
                            border_width=2, border_color=CLR_BORDER)
        card.pack(fill="x", padx=24, pady=6)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 2))

        radio = ctk.CTkRadioButton(
            top, text=info["name"], variable=self._tier_var, value=key,
            font=ctk.CTkFont(size=15, weight="bold"), text_color=CLR_TEXT,
            command=lambda: None,
        )
        radio.pack(side="left")

        if is_rec:
            ctk.CTkLabel(
                top, text="  ✓ 推荐", text_color="#27AE60",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(side="left")

        ctk.CTkLabel(
            top, text=f"下载{info['size']}", text_color="#95A5A6",
            font=ctk.CTkFont(size=11),
        ).pack(side="right")

        desc = (f"效果：{info['effect']}\n"
                f"适配：{info['need']}\n"
                f"速度：{info['speed']}")
        ctk.CTkLabel(
            card, text=desc, justify="left", anchor="w",
            font=ctk.CTkFont(size=12), text_color="#5D6D7E",
        ).pack(fill="x", padx=34, pady=(0, 10))

    def _confirm(self):
        if self._done:
            return
        self._done = True
        key = self._tier_var.get()
        info = TIER_INFO[key]
        logger.info("用户选择档位：%s，默认模型 %s", key, info["model"])
        self.destroy()
        self.on_choose(info["model"], list(info["download"]))

    def _on_user_close(self):
        """用户未选择就关闭：取消整个首次流程。"""
        if self._done:
            return
        self._done = True
        try:
            self.destroy()
        except Exception:
            pass
        if self.on_cancel:
            self.on_cancel()


# ---------------------------------------------------------------------------
# 模型下载弹窗
# ---------------------------------------------------------------------------

class ModelDownloadWindow(ctk.CTkToplevel):
    """首次使用时下载 Ollama 模型的进度弹窗，支持按显存档位下载多个模型。"""

    def __init__(self, master, models_to_pull: list[str], default_model: str,
                 on_finish, on_cancel=None):
        super().__init__(master)
        self.title("助手初始化")
        self.geometry("440x320")
        self.minsize(400, 280)
        self.configure(fg_color=CLR_BG)
        # 去重并保留顺序，附加向量模型
        seen = set()
        self._models = []
        for m in list(models_to_pull) + ["nomic-embed-text"]:
            if m not in seen:
                seen.add(m)
                self._models.append(m)
        self.on_finish = on_finish
        self.on_cancel = on_cancel
        self._done = False  # 防止回调重复触发

        # 用户点窗口右上角叉时，也要让外层流程能继续（否则 root 卡死）
        self.protocol("WM_DELETE_WINDOW", self._on_user_close)

        card = ctk.CTkFrame(self, fg_color=CLR_INPUT, corner_radius=15)
        card.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            card, text="🚀 智能助手初始化",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=CLR_TEXT,
        ).pack(pady=(25, 10))

        self.status_label = ctk.CTkLabel(
            card, text="正在连接...",
            font=ctk.CTkFont(size=14, weight="bold"), text_color="#2980B9",
        )
        self.status_label.pack(pady=5)

        model_list_text = "、".join(self._models)
        ctk.CTkLabel(
            card,
            text=f"将下载：{model_list_text}\n首次下载较大，请保持网络通畅。",
            font=ctk.CTkFont(size=11), text_color=CLR_TEXT,
            wraplength=360, justify="center",
        ).pack(pady=(5, 15))

        self.progress_bar = ctk.CTkProgressBar(card, width=340, height=12)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=(0, 20))

        threading.Thread(target=self._download_all, daemon=True).start()

    def _on_user_close(self) -> None:
        """用户中途关闭下载窗：确认后取消整个首次流程。"""
        if self._done:
            return
        if messagebox.askyesno(
            "确认取消",
            "模型尚未下载完成，确定要取消吗？\n取消后可重新打开程序继续下载。",
        ):
            self._done = True
            try:
                self.destroy()
            except Exception:
                pass
            if self.on_cancel:
                self.on_cancel()

    def _download_all(self) -> None:
        total_count = len(self._models)
        for idx, model_name in enumerate(self._models, start=1):
            if self._done:
                return  # 已被用户取消
            ok = self._pull_model(model_name, idx, total_count)
            if not ok:
                self.after(0, self._on_download_failed, model_name)
                return
        self.after(0, self._on_all_done)

    def _on_all_done(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self.destroy()
        except Exception:
            pass
        self.on_finish()

    def _on_download_failed(self, model_name: str) -> None:
        if self._done:
            return
        self._done = True
        messagebox.showerror(
            "模型下载失败",
            f"模型「{model_name}」下载失败。\n\n"
            "请检查：\n"
            "1. 网络连接是否正常；\n"
            "2. 是否能访问模型下载源；\n"
            "3. 磁盘空间是否充足。\n\n"
            "请重新打开程序再试。",
        )
        try:
            self.destroy()
        except Exception:
            pass
        # 下载失败也要让外层流程退出，避免 root 卡死
        if self.on_cancel:
            self.on_cancel()

    def _pull_model(self, model_name: str, idx: int, total_count: int) -> bool:
        """下载单个模型。成功返回 True，多次重试仍失败返回 False。"""
        if "nomic" in model_name:
            display = "RAG向量组件"
        else:
            display = f"主引擎 {model_name}"
        prefix = f"({idx}/{total_count}) "
        self.after(0, lambda: (
            self.progress_bar.set(0),
            self.status_label.configure(text=f"{prefix}正在连接获取 {display}..."),
        ))

        MAX_RETRIES = 5
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    "http://127.0.0.1:11434/api/pull",
                    json={"name": model_name, "stream": True},
                    stream=True,
                    proxies={"http": None, "https": None},
                    timeout=30,
                )
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if data.get("error"):
                        logger.warning("下载 %s 返回错误：%s", model_name, data["error"])
                        break
                    total = data.get("total", 1) or 1
                    ratio = data.get("completed", 0) / total
                    self.after(0, lambda r=ratio, d=display, p=prefix: (
                        self.progress_bar.set(r),
                        self.status_label.configure(
                            text=f"{p}正在下载 {d}... {int(r * 100)}%"
                        ),
                    ))
                    if data.get("status") == "success":
                        return True
            except Exception as exc:
                logger.warning(
                    "下载 %s 第 %d 次失败：%s", model_name, attempt + 1, exc
                )
                self.after(0, lambda a=attempt: self.status_label.configure(
                    text=f"网络异常，正在重试（{a + 1}/{MAX_RETRIES}）..."
                ))
                time.sleep(3)
        return False


# ---------------------------------------------------------------------------
# 占位符辅助 Mixin
# ---------------------------------------------------------------------------

class PlaceholderMixin:
    """为 Entry 和 Textbox 提供统一的占位符管理。"""

    def _init_placeholders(self):
        self._p_placeholder: dict[str, bool] = {m: True for m in WRITING_MODES}
        self._s_placeholder: dict[str, bool] = {m: True for m in WRITING_MODES}

        self._p_placeholder_text: dict[str, str] = {
            "爆款标题生成": "请输入产品名称，例如：无线蓝牙耳机",
            "推广软文生成": "请输入产品名称，例如：防晒霜 SPF50",
            "职场战报汇报": "请输入岗位名称，例如：电商运营专员",
            "日常汇报":     "请输入岗位名称，例如：电商运营专员",
            "转正述职":     "请输入岗位名称，例如：电商运营专员",
            "年度汇报":     "请输入岗位名称，例如：电商运营专员",
            "短视频脚本":   "请输入产品名称，例如：无线蓝牙耳机",
        }
        self._s_placeholder_text: dict[str, str] = {
            "爆款标题生成": "请输入补充信息，例如：适合学生党、超长续航20小时、支持主动降噪",
            "推广软文生成": "请输入补充信息，例如：主打清爽不油腻、适合油皮、夏日户外必备",
            "职场战报汇报": "请输入补充信息，例如：本月完成销售额120万、超额完成KPI 15%",
            "日常汇报":     "请输入补充信息，例如：本月完成销售额120万、超额完成KPI 15%",
            "转正述职":     "请输入补充信息，例如：试用期完成项目3个、获得客户好评5次",
            "年度汇报":     "请输入补充信息，例如：全年完成销售额500万、超额完成KPI 20%",
            "短视频脚本":   "请输入补充信息，例如：乌木柠檬香、男生打篮球场景、想要幽默风格",
        }

    def _show_entry_placeholder(self, mode: str) -> None:
        if self._p_placeholder.get(mode, False):
            self.ent_p._entry.configure(takefocus=False)
            self.ent_p.delete(0, "end")
            self.ent_p.insert(0, self._p_placeholder_text[mode])
            self.ent_p.configure(text_color="#9CA3AF")

    def _on_entry_click(self, _event=None) -> None:
        mode = self.current_mode
        if self._p_placeholder.get(mode, False):
            self._p_placeholder[mode] = False
            self.ent_p.delete(0, "end")
            self.ent_p.configure(text_color=CLR_TEXT)
            self.ent_p._entry.configure(takefocus=True)
        self.ent_p._entry.focus_set()

    def _show_textbox_placeholder(self, mode: str) -> None:
        if self._s_placeholder.get(mode, False):
            self.txt_s.delete("0.0", "end")
            self.txt_s.insert("0.0", self._s_placeholder_text[mode])
            self.txt_s.configure(text_color="#9CA3AF")

    def _on_textbox_click(self, _event=None) -> None:
        mode = self.current_mode
        if self._s_placeholder.get(mode, False):
            self._s_placeholder[mode] = False
            self.txt_s.delete("0.0", "end")
            self.txt_s.configure(text_color=CLR_TEXT)

    def _read_entry(self) -> str:
        """返回 entry 的真实内容（排除占位符）。"""
        if self._p_placeholder.get(self.current_mode, False):
            return ""
        return self.ent_p.get().strip()

    def _read_textbox(self) -> str:
        """返回 textbox 的真实内容（排除占位符和残留占位符文本）。"""
        if self._s_placeholder.get(self.current_mode, False):
            return ""
        text = self.txt_s.get("0.0", "end-1c").strip()
        return "" if "请输入补充信息" in text else text


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class AI_Assistant_UI(PlaceholderMixin, ctk.CTk):

    def __init__(self, target_model: str):
        super().__init__()
        self.target_model = target_model

        self.title(APP_TITLE)
        self.geometry("1150x850")
        self.minsize(960, 700)
        self.configure(fg_color=CLR_BG)

        ico_path = os.path.join(get_base_path(), "logo.ico")
        if os.path.exists(ico_path):
            self.iconbitmap(ico_path)

        # 状态
        self.stop_flag = False
        self._upload_progress: float | None = None
        self._upload_msg: str | None = None
        self.current_task_id = 0
        self.current_mode = "爆款标题生成"
        self.states: dict[str, dict] = {
            m: {"product": "", "style": "", "output": "", "generating": False}
            for m in WRITING_MODES
        }
        self._summary_expanded: dict[str, bool] = {}
        self._summary_cache: dict[str, str] = {}

        self._init_placeholders()
        self._build_layout()

        # 关闭窗口时优雅退出：停止生成、关闭自带的 ollama 进程，避免后台残留
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        """关闭主窗口时的清理：停止任务并尝试关闭由本程序启动的 ollama。"""
        try:
            self.stop_flag = True
        except Exception:
            pass
        try:
            _shutdown_local_ollama()
        except Exception:
            pass
        self.destroy()


    # ------------------------------------------------------------------
    # 布局构建
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main_area()

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=CLR_SIDEBAR)
        sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar = sidebar

        # Logo
        logo_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo_frame.grid(row=0, column=0, padx=20, pady=(36, 36))
        logo_png = os.path.join(get_base_path(), "logo.png")
        if os.path.exists(logo_png):
            try:
                pil_img = Image.open(logo_png)
                ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(48, 48))
                ctk.CTkLabel(logo_frame, image=ctk_img, text="").pack(side="left", padx=(0, 10))
            except Exception:
                pass
        ctk.CTkLabel(
            logo_frame, text="运营助手 Pro",
            font=("Microsoft YaHei", 20, "bold"), text_color=CLR_TEXT,
        ).pack(side="left")

        btn_cfg = {
            "anchor": "w", "height": 45,
            "font": ("Microsoft YaHei", 14, "bold"), "text_color": CLR_TEXT,
        }
        self.nav_btns: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("t1", "✨ 爆款标题生成", self.switch_to_title),
            ("t2", "📝 推广软文生成", self.switch_to_article),
            ("t3", "📊 职场战报汇报", self.switch_to_summary),
            ("t5", "🎬 短视频脚本",   self.switch_to_video),
            ("t4", "📚 产品资料库",   self.switch_to_rag),
        ]
        for row_idx, (key, label, cmd) in enumerate(nav_items, start=1):
            btn = ctk.CTkButton(
                sidebar, text=label,
                fg_color="#A2D2FF" if row_idx == 1 else "transparent",
                hover_color="#D1D9E6", command=cmd, **btn_cfg,
            )
            btn.grid(row=row_idx, column=0, padx=15, pady=10, sticky="ew")
            self.nav_btns[key] = btn

        sidebar.grid_rowconfigure(9, weight=1)

        # 底部：内存/模型信息
        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        ctk.CTkLabel(
            sidebar,
            text=f"内存: {ram_gb}GB  | 模型: {self.target_model}",
            font=("Microsoft YaHei", 11), text_color=CLR_TEXT,
        ).grid(row=10, column=0, padx=15, pady=(0, 5), sticky="sw")

        self.model_var = ctk.StringVar(value=self.target_model)
        # 下拉框始终列出全部三档；未下载的标注「(需下载)」，选中即触发下载
        self._installed_set = set(self._list_installed_qwen_models())
        dropdown_values = self._build_model_options()
        # 当前模型在下拉框里显示为「已装」形式
        self.model_var.set(self.target_model)
        self.model_menu = ctk.CTkOptionMenu(
            sidebar,
            values=dropdown_values,
            variable=self.model_var,
            font=("Microsoft YaHei", 11),
            fg_color=CLR_INPUT, text_color=CLR_TEXT,
            button_color="#9CA3AF", button_hover_color="#6B7280",
            dropdown_fg_color=CLR_INPUT, dropdown_text_color=CLR_TEXT,
            corner_radius=4, width=190,
            command=self._on_model_change,
        )
        self.model_menu.grid(row=11, column=0, padx=15, pady=(0, 20), sticky="sw")

    # 全部档位模型，从大到小
    ALL_TIER_MODELS = ("qwen2.5:7b", "qwen2.5:3b", "qwen2.5:1.5b")
    NEED_DOWNLOAD_SUFFIX = "（需下载）"

    def _build_model_options(self) -> list[str]:
        """构建下拉框选项：全部三档，未安装的加「（需下载）」后缀。"""
        opts = []
        for m in self.ALL_TIER_MODELS:
            if m in self._installed_set:
                opts.append(m)
            else:
                opts.append(m + self.NEED_DOWNLOAD_SUFFIX)
        return opts

    def _list_installed_qwen_models(self) -> list[str]:
        """查询 ollama 已安装的 qwen 系列模型，供下拉框切换。"""
        try:
            resp = requests.get(
                "http://127.0.0.1:11434/api/tags",
                timeout=5,
                proxies={"http": None, "https": None},
            )
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            qwen = sorted(n for n in names if n.startswith("qwen"))
            return qwen or [self.target_model]
        except Exception:
            return [self.target_model]

    def _build_main_area(self) -> None:
        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew", padx=35, pady=35)
        self.main.grid_columnconfigure(0, weight=1)

        i_style = {
            "fg_color": CLR_INPUT, "border_color": CLR_BORDER,
            "text_color": CLR_TEXT, "border_width": 2,
        }
        lbl_style = {"font": ("Microsoft YaHei", 13), "text_color": "#6B7280"}

        # 标题行
        self.lbl_title = ctk.CTkLabel(
            self.main, text="爆款标题生成",
            font=("Microsoft YaHei", 26, "bold"), text_color=CLR_TEXT,
        )
        self.lbl_title.grid(row=0, column=0, sticky="w", pady=(0, 7))

        # 报告子类型选择器（默认隐藏）
        self.sum_frame = ctk.CTkFrame(self.main, fg_color="transparent")
        self.sum_var = ctk.StringVar(value="日常汇报")
        self.seg = ctk.CTkSegmentedButton(
            self.sum_frame,
            values=REPORT_SUB_MODES,
            variable=self.sum_var,
            fg_color=CLR_BG,
            selected_color=SEG_SELECTED,
            unselected_color=SEG_UNSELECTED,
            text_color=CLR_TEXT,
            font=("Microsoft YaHei", 18, "bold"),
            command=self._on_report_type_change,
        )
        self.seg.pack()

        # 短视频脚本专属控件（默认隐藏）：类型 / 风格 / 时长 / AI提示词开关 / 合规开关
        self.video_frame = ctk.CTkFrame(self.main, fg_color="transparent")
        # 第一行：三个下拉
        _vrow1 = ctk.CTkFrame(self.video_frame, fg_color="transparent")
        _vrow1.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(_vrow1, text="类型", font=("Microsoft YaHei", 12),
                     text_color=CLR_TEXT).pack(side="left", padx=(0, 3))
        self.video_type_var = ctk.StringVar(value=VIDEO_TYPES[0])
        ctk.CTkOptionMenu(
            _vrow1, values=VIDEO_TYPES, variable=self.video_type_var,
            width=110, font=("Microsoft YaHei", 12),
            fg_color=CLR_INPUT, text_color=CLR_TEXT,
            button_color="#9CA3AF", button_hover_color="#6B7280",
            dropdown_fg_color=CLR_INPUT, dropdown_text_color=CLR_TEXT,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkLabel(_vrow1, text="风格", font=("Microsoft YaHei", 12),
                     text_color=CLR_TEXT).pack(side="left", padx=(0, 3))
        self.video_style_var = ctk.StringVar(value=VIDEO_STYLES[0])
        ctk.CTkOptionMenu(
            _vrow1, values=VIDEO_STYLES, variable=self.video_style_var,
            width=110, font=("Microsoft YaHei", 12),
            fg_color=CLR_INPUT, text_color=CLR_TEXT,
            button_color="#9CA3AF", button_hover_color="#6B7280",
            dropdown_fg_color=CLR_INPUT, dropdown_text_color=CLR_TEXT,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkLabel(_vrow1, text="时长", font=("Microsoft YaHei", 12),
                     text_color=CLR_TEXT).pack(side="left", padx=(0, 3))
        self.video_duration_var = ctk.StringVar(value=f"{VIDEO_DURATIONS[1]}秒")
        ctk.CTkOptionMenu(
            _vrow1, values=[f"{d}秒" for d in VIDEO_DURATIONS],
            variable=self.video_duration_var,
            width=80, font=("Microsoft YaHei", 12),
            fg_color=CLR_INPUT, text_color=CLR_TEXT,
            button_color="#9CA3AF", button_hover_color="#6B7280",
            dropdown_fg_color=CLR_INPUT, dropdown_text_color=CLR_TEXT,
        ).pack(side="left")

        # 第二行：两个开关
        _vrow2 = ctk.CTkFrame(self.video_frame, fg_color="transparent")
        _vrow2.pack(fill="x", pady=(2, 0))
        self.video_ai_prompt_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            _vrow2, text="附带视频AI画面提示词",
            variable=self.video_ai_prompt_var,
            font=("Microsoft YaHei", 12), text_color=CLR_TEXT,
            progress_color="#3D7BF0",
        ).pack(side="left", padx=(0, 16))

        self.video_compliance_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            _vrow2, text="启用合规约束",
            variable=self.video_compliance_var,
            font=("Microsoft YaHei", 12), text_color=CLR_TEXT,
            progress_color="#3D7BF0",
            command=self._on_video_compliance_toggle,
        ).pack(side="left")

        # 合规输入框（默认隐藏，开启合规开关时显示）
        self.video_compliance_box = ctk.CTkTextbox(
            self.video_frame, height=44,
            font=("Microsoft YaHei", 12),
            fg_color=CLR_INPUT, border_color=CLR_BORDER,
            text_color=CLR_TEXT, border_width=1,
        )
        self._video_compliance_hint = "填写红线，如：不喷人脸人体、不夸大医疗功效、画面真实不夸张"

        # 产品/岗位输入
        self.lbl_p = ctk.CTkLabel(self.main, text="产品名称", **lbl_style)
        self.lbl_p.grid(row=2, column=0, sticky="w", pady=(0, 2))
        self.ent_p = ctk.CTkEntry(self.main, height=50, **i_style)
        self.ent_p.grid(row=3, column=0, sticky="ew", pady=(0, 5))
        self.ent_p._entry.configure(takefocus=False)
        self.ent_p.bind("<Button-1>", self._on_entry_click)
        self._show_entry_placeholder("爆款标题生成")

        # 补充信息
        self.lbl_s = ctk.CTkLabel(
            self.main,
            text="补充信息（选填）— 描述产品卖点、适用人群或特色功能，信息越详细效果越好",
            **lbl_style,
        )
        self.lbl_s.grid(row=4, column=0, sticky="w", pady=(0, 2))
        self.txt_s = ctk.CTkTextbox(self.main, height=70, **i_style)
        self.txt_s.grid(row=5, column=0, sticky="ew", pady=(0, 5))
        self.txt_s.bind("<Button-1>", self._on_textbox_click)
        self._show_textbox_placeholder("爆款标题生成")

        # RAG 开关与文件列表
        self._build_rag_selector()

        # 输出区
        self.txt_o = ctk.CTkTextbox(
            self.main,
            font=("Microsoft YaHei", 15),
            fg_color=CLR_INPUT, border_color=CLR_BORDER,
            text_color=CLR_TEXT, border_width=2,
        )
        self.txt_o.grid(row=8, column=0, sticky="nsew", pady=(15, 20))
        self.main.grid_rowconfigure(8, weight=1)
        # 让输出内容可以方便地用鼠标选中复制：配置选中高亮颜色 + 右键菜单
        self._setup_output_copy(self.txt_o)

        # 操作按钮
        self._build_action_buttons()

    def _build_rag_selector(self) -> None:
        self.rag_toggle_var = ctk.BooleanVar(value=False)
        self.rag_toggle_switch = ctk.CTkSwitch(
            self.main,
            text="📚 开启资料库定向参考 (选中后可指定文档)",
            variable=self.rag_toggle_var,
            font=("Microsoft YaHei", 13, "bold"),
            command=self._on_rag_toggle,
        )
        self.rag_toggle_switch.grid(row=6, column=0, sticky="w", pady=(10, 5))

        self.rag_selection_container = ctk.CTkFrame(self.main, fg_color="transparent")

        self.rag_list_expanded = ctk.BooleanVar(value=True)
        self.rag_expand_btn = ctk.CTkButton(
            self.rag_selection_container,
            text="▼ 隐藏文档列表", width=100, height=24,
            fg_color="transparent", text_color="#6B7280", hover_color="#E5E7EB",
            font=("Microsoft YaHei", 11),
            command=self._toggle_rag_list,
        )
        self.rag_expand_btn.pack(anchor="w", pady=(0, 5))

        # 固定高度容器防止列表撑开布局
        self.rag_files_wrapper = ctk.CTkFrame(
            self.rag_selection_container, height=70, fg_color="transparent"
        )
        self.rag_files_wrapper.pack(fill="x", expand=False)
        self.rag_files_wrapper.pack_propagate(False)

        self.rag_files_frame = ctk.CTkScrollableFrame(
            self.rag_files_wrapper,
            fg_color="transparent",
            border_width=1,
            border_color=CLR_BORDER,
        )
        self.rag_files_frame.pack(fill="both", expand=True)

        self.rag_file_vars: dict[str, ctk.BooleanVar] = {}

    def _setup_output_copy(self, textbox: "ctk.CTkTextbox") -> None:
        """让输出框可方便地选中复制：
        1) 配置选中高亮颜色（否则选中了也看不出蓝色）；
        2) 加右键菜单（复制 / 全选）；
        3) 确保点击能获取焦点以便选择。
        """
        try:
            inner = textbox._textbox  # CTkTextbox 内部真正的 tk.Text
        except Exception:
            return

        # 1) 选中高亮：蓝底白字，选中立刻可见
        try:
            inner.configure(
                selectbackground="#3D7BF0",
                selectforeground="#FFFFFF",
                inactiveselectbackground="#3D7BF0",  # 失焦时仍显示选中
                exportselection=True,
            )
        except Exception:
            pass

        # 2) 右键菜单
        import tkinter as _tk

        menu = _tk.Menu(inner, tearoff=0)

        def _copy_sel():
            try:
                sel = inner.get("sel.first", "sel.last")
            except Exception:
                sel = ""
            if sel:
                self.clipboard_clear()
                self.clipboard_append(sel)

        def _select_all():
            inner.tag_add("sel", "1.0", "end-1c")
            inner.focus_set()

        menu.add_command(label="复制", command=_copy_sel)
        menu.add_command(label="全选", command=_select_all)

        def _popup(event):
            inner.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        # 3) 绑定：右键弹菜单；左键点击先获取焦点
        inner.bind("<Button-3>", _popup)
        inner.bind("<Button-1>", lambda e: inner.focus_set(), add="+")
        # Ctrl+A 全选
        inner.bind("<Control-a>", lambda e: (_select_all(), "break")[1])
        inner.bind("<Control-A>", lambda e: (_select_all(), "break")[1])

    def _build_action_buttons(self) -> None:
        self.btns = ctk.CTkFrame(self.main, fg_color="transparent")
        self.btns.grid(row=9, column=0, sticky="ew")
        self.btns.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        b_f = ("Microsoft YaHei", 13, "bold")
        self.b_g = ctk.CTkButton(
            self.btns, text="🚀 深度创作", height=50,
            fg_color=C_GEN, text_color=CLR_TEXT, font=b_f,
            command=lambda: self.start_gen("main"),
        )
        self.b_g.grid(row=0, column=0, padx=3)

        self.b_r = ctk.CTkButton(
            self.btns, text="🔄 重新生成", height=50,
            fg_color=C_RE, text_color=CLR_TEXT, font=b_f,
            state="disabled", command=lambda: self.start_gen("re"),
        )
        self.b_r.grid(row=0, column=1, padx=3)

        self.b_s = ctk.CTkButton(
            self.btns, text="🛑 停止生成", height=50,
            fg_color=C_STOP, text_color=CLR_TEXT, font=b_f,
            state="disabled", command=self.do_stop,
        )
        self.b_s.grid(row=0, column=2, padx=3)

        self.b_c = ctk.CTkButton(
            self.btns, text="📋 复制文案", height=50,
            fg_color=C_COPY, text_color=CLR_TEXT, font=b_f,
            command=self.do_copy,
        )
        self.b_c.grid(row=0, column=3, padx=3)

        self.b_cl = ctk.CTkButton(
            self.btns, text="🧹 清空内容", height=50,
            fg_color=C_CLEAR, text_color=CLR_TEXT, font=b_f,
            command=self.do_clear,
        )
        self.b_cl.grid(row=0, column=4, padx=3)

    # ------------------------------------------------------------------
    # 导航切换
    # ------------------------------------------------------------------

    def _highlight_nav(self, active_key: str) -> None:
        for key, btn in self.nav_btns.items():
            btn.configure(fg_color="#A2D2FF" if key == active_key else "transparent")

    def _save_current_state(self) -> None:
        """保存当前模式的输入框内容到 states。"""
        cur = self.current_mode
        product = self._read_entry()
        supplement = self._read_textbox()
        self.states[cur].update({"product": product, "style": supplement})

    def _restore_mode_inputs(self, mode: str) -> None:
        """将 states 中保存的内容填回输入框。"""
        state = self.states[mode]
        if state["product"]:
            self.ent_p.delete(0, "end")
            self.ent_p.configure(text_color=CLR_TEXT)
            self.ent_p._entry.configure(takefocus=True)
            self.ent_p.insert(0, state["product"])
        else:
            self._show_entry_placeholder(mode)

        self.txt_s.delete("0.0", "end")
        if state["style"]:
            self.txt_s.configure(text_color=CLR_TEXT)
            self.txt_s.insert("0.0", state["style"])
        else:
            self._show_textbox_placeholder(mode)

        self.txt_o.delete("0.0", "end")
        if state["output"]:
            self.txt_o.insert("0.0", state["output"])
        elif state["generating"]:
            self.txt_o.insert("0.0", "⏳ AI 正在思考中，请稍候...\n\n")

        self.update_ui_state(is_thinking=state["generating"])

    def _show_writing_panel(self, mode: str, nav_key: str, title: str,
                             lbl_p_text: str, lbl_s_text: str,
                             show_report_seg: bool = False,
                             show_video_controls: bool = False) -> None:
        """切换到写作面板的通用逻辑。"""
        if hasattr(self, "rag_frame") and self.rag_frame.winfo_exists():
            self.rag_frame.destroy()

        self._save_current_state()

        # 重新显示主界面组件
        self.lbl_p.grid(row=2, column=0, sticky="w", pady=(0, 2))
        self.ent_p.grid(row=3, column=0, sticky="ew", pady=(0, 5))
        self.lbl_s.grid(row=4, column=0, sticky="w", pady=(0, 2))
        self.txt_s.grid(row=5, column=0, sticky="ew", pady=(0, 5))
        self.rag_toggle_switch.grid(row=6, column=0, sticky="w", pady=(10, 5))
        self.txt_o.grid(row=8, column=0, sticky="nsew", pady=(15, 20))
        self.btns.grid(row=9, column=0, sticky="ew")

        if show_report_seg:
            self.sum_frame.grid(row=0, column=0, sticky="w", padx=(190, 0))
        else:
            self.sum_frame.grid_forget()

        # 视频脚本专属控件：显示在标题下方（row=1）
        if show_video_controls:
            self.video_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
            self.main.grid_rowconfigure(1, weight=0, minsize=0)
        else:
            self.video_frame.grid_forget()

        if self.rag_toggle_var.get():
            self._refresh_rag_selection_list()
            self.rag_selection_container.grid(
                row=7, column=0, sticky="ew", padx=10, pady=(0, 10)
            )
        else:
            self.rag_selection_container.grid_forget()

        self._highlight_nav(nav_key)
        self.lbl_title.configure(text=title)
        self.lbl_p.configure(text=lbl_p_text)
        self.lbl_s.configure(text=lbl_s_text)
        if not show_video_controls:
            self.main.grid_rowconfigure(1, weight=0, minsize=0)

        self.current_mode = mode
        self._restore_mode_inputs(mode)
        self.focus_set()

    def switch_to_title(self) -> None:
        self._show_writing_panel(
            mode="爆款标题生成", nav_key="t1", title="爆款标题生成",
            lbl_p_text="产品名称",
            lbl_s_text="补充信息（选填）— 描述产品卖点、适用人群或特色功能，信息越详细标题越精准",
        )

    def switch_to_article(self) -> None:
        self._show_writing_panel(
            mode="推广软文生成", nav_key="t2", title="推广软文生成",
            lbl_p_text="产品名称",
            lbl_s_text="补充信息（选填）— 描述产品亮点、使用场景或目标客群，信息越详细文案越贴切",
        )

    def switch_to_summary(self) -> None:
        current_report = self.sum_var.get()
        self._show_writing_panel(
            mode=current_report, nav_key="t3", title="职场战报汇报",
            lbl_p_text="岗位 / 工作内容",
            lbl_s_text="补充信息（选填）— 填写具体工作数据、项目成果或完成事项，有数据报告更有说服力",
            show_report_seg=True,
        )

    def switch_to_video(self) -> None:
        self._show_writing_panel(
            mode=VIDEO_MODE, nav_key="t5", title="短视频脚本",
            lbl_p_text="产品名称",
            lbl_s_text="补充信息（选填）— 描述产品卖点、目标人群、想要的场景或风格，越详细脚本越贴切",
            show_video_controls=True,
        )

    def switch_to_rag(self) -> None:
        if hasattr(self, "rag_frame") and self.rag_frame.winfo_exists():
            self.rag_frame.destroy()

        self._highlight_nav("t4")
        self.lbl_title.configure(text="产品资料库")
        self.sum_frame.grid_forget()
        self.video_frame.grid_forget()

        for widget in (self.lbl_p, self.ent_p, self.lbl_s, self.txt_s,
                       self.rag_toggle_switch, self.rag_selection_container,
                       self.txt_o, self.btns):
            widget.grid_forget()

        self._show_rag_panel()

    # ------------------------------------------------------------------
    # 报告子类型切换
    # ------------------------------------------------------------------

    def _on_report_type_change(self, choice: str) -> None:
        self._save_current_state()
        self.current_mode = choice
        self._restore_mode_inputs(choice)

    def _on_video_compliance_toggle(self) -> None:
        """合规开关：开启时显示输入框，关闭时隐藏。"""
        if self.video_compliance_var.get():
            self.video_compliance_box.pack(fill="x", pady=(4, 0))
            # 显示占位提示
            if not self.video_compliance_box.get("0.0", "end").strip():
                self.video_compliance_box.delete("0.0", "end")
                self.video_compliance_box.insert("0.0", self._video_compliance_hint)
                self.video_compliance_box.configure(text_color="#9CA3AF")
                self._video_compliance_is_hint = True
                self.video_compliance_box.bind("<Button-1>", self._on_compliance_box_click)
        else:
            self.video_compliance_box.pack_forget()

    def _on_compliance_box_click(self, _event=None) -> None:
        """点击合规输入框：清除占位提示。"""
        if getattr(self, "_video_compliance_is_hint", False):
            self.video_compliance_box.delete("0.0", "end")
            self.video_compliance_box.configure(text_color=CLR_TEXT)
            self._video_compliance_is_hint = False

    def _read_video_compliance(self) -> str:
        """读取合规约束文本（排除占位提示）。"""
        if not self.video_compliance_var.get():
            return ""
        if getattr(self, "_video_compliance_is_hint", False):
            return ""
        text = self.video_compliance_box.get("0.0", "end").strip()
        return text if text != self._video_compliance_hint else ""

    # ------------------------------------------------------------------
    # UI 状态更新
    # ------------------------------------------------------------------

    def update_ui_state(self, is_thinking: bool = False) -> None:
        has_output = len(self.states[self.current_mode]["output"]) > 5
        if is_thinking:
            self.b_g.configure(state="disabled", text="⏳ 生成中...")
            self.b_r.configure(state="disabled")
            self.b_s.configure(state="normal")
        else:
            self.b_g.configure(state="normal", text="🚀 深度创作")
            self.b_r.configure(state="normal" if has_output else "disabled", text="🔄 重新生成")
            self.b_s.configure(state="disabled", text="🛑 停止生成")

    # ------------------------------------------------------------------
    # RAG 选择器
    # ------------------------------------------------------------------

    def _toggle_rag_list(self) -> None:
        if self.rag_list_expanded.get():
            self.rag_files_wrapper.pack_forget()
            self.rag_expand_btn.configure(text="▶ 展开文档列表")
            self.rag_list_expanded.set(False)
        else:
            self.rag_files_wrapper.pack(fill="x", expand=False)
            self.rag_expand_btn.configure(text="▼ 隐藏文档列表")
            self.rag_list_expanded.set(True)

    def _on_rag_toggle(self) -> None:
        if self.rag_toggle_var.get():
            self._refresh_rag_selection_list()
            self.rag_selection_container.grid(
                row=7, column=0, sticky="ew", padx=10, pady=(0, 10)
            )
            expanded = self.rag_list_expanded.get()
            if expanded:
                self.rag_files_wrapper.pack(fill="x", expand=False)
                self.rag_expand_btn.configure(text="▼ 隐藏文档列表")
            else:
                self.rag_files_wrapper.pack_forget()
                self.rag_expand_btn.configure(text="▶ 展开文档列表")
        else:
            self.rag_selection_container.grid_forget()

    def _refresh_rag_selection_list(self) -> None:
        for w in self.rag_files_frame.winfo_children():
            w.destroy()
        self.rag_file_vars.clear()

        files = get_uploaded_files()
        if not files:
            ctk.CTkLabel(
                self.rag_files_frame,
                text="暂无文档，请先在左侧「产品资料库」上传 PDF",
                text_color="#9CA3AF", font=("Microsoft YaHei", 12),
            ).grid(row=0, column=0, sticky="w", padx=5, pady=5)
            return

        MAX_COLS = 3
        self.rag_files_frame.grid_columnconfigure(tuple(range(MAX_COLS)), weight=1)

        for i, filename in enumerate(files):
            var = ctk.BooleanVar(value=True)
            self.rag_file_vars[filename] = var
            display = f"📄 {filename}" if len(filename) < 25 else f"📄 {filename[:22]}..."
            ctk.CTkCheckBox(
                self.rag_files_frame, text=display, variable=var,
                font=("Microsoft YaHei", 12),
                checkbox_width=20, checkbox_height=20,
            ).grid(row=i // MAX_COLS, column=i % MAX_COLS, sticky="w", pady=6, padx=8)

    # ------------------------------------------------------------------
    # 资料库管理面板
    # ------------------------------------------------------------------

    def _show_rag_panel(self) -> None:
        self.rag_frame = ctk.CTkFrame(self.main, fg_color="transparent")
        self.rag_frame.grid(row=1, column=0, sticky="nsew", rowspan=9)
        self.rag_frame.grid_rowconfigure(5, weight=1)
        self.rag_frame.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)

        hint = (
            "上传后，AI生成标题和软文时会自动参考文档内容\n\n"
            "✅ 适合上传：产品说明书、竞品报告、平台规则、品牌手册\n"
            "❌ 不建议：图片为主的文档、超过500页的文档"
        )
        ctk.CTkLabel(
            self.rag_frame, text=hint,
            font=("Microsoft YaHei", 13), text_color=CLR_TEXT, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 20))

        btn_row = ctk.CTkFrame(self.rag_frame, fg_color="transparent")
        btn_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 20))
        ctk.CTkButton(
            btn_row, text="📂 选择并上传PDF", height=45,
            fg_color=C_GEN, text_color=CLR_TEXT,
            font=("Microsoft YaHei", 13, "bold"),
            command=self._upload_pdf,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            btn_row, text="🗑 全部清理", height=45,
            fg_color=C_CLEAR, text_color=CLR_TEXT,
            font=("Microsoft YaHei", 13, "bold"),
            command=self._clear_all_rag,
        ).pack(side="left")

        self.rag_progress_label = ctk.CTkLabel(
            self.rag_frame, text="",
            font=("Microsoft YaHei", 12), text_color="#2980B9",
        )
        self.rag_progress_label.grid(row=2, column=0, sticky="w")

        self.rag_progress_bar = ctk.CTkProgressBar(self.rag_frame, width=400, height=12)
        self.rag_progress_bar.set(0)
        self.rag_progress_bar.grid(row=3, column=0, sticky="w", pady=(5, 15))
        self.rag_progress_bar.grid_remove()

        ctk.CTkLabel(
            self.rag_frame, text="已上传文档：",
            font=("Microsoft YaHei", 13, "bold"), text_color=CLR_TEXT,
        ).grid(row=4, column=0, sticky="w", pady=(0, 8))

        self.rag_list_frame = ctk.CTkScrollableFrame(self.rag_frame, fg_color="transparent")
        self.rag_list_frame.grid(row=5, column=0, columnspan=2, sticky="nsew")

        self._refresh_rag_list()

        # 恢复上传中的进度条状态
        if self._upload_progress is not None:
            self.rag_progress_bar.grid()
            self.rag_progress_bar.set(self._upload_progress)
            self.rag_progress_label.configure(
                text=f"{self._upload_msg}  {int(self._upload_progress * 100)}%"
            )

    def _refresh_rag_list(self) -> None:
        for w in self.rag_list_frame.winfo_children():
            w.destroy()

        files = get_uploaded_files()
        if not files:
            ctk.CTkLabel(
                self.rag_list_frame, text="暂无文档，请上传PDF",
                font=("Microsoft YaHei", 12), text_color="#9CA3AF",
            ).pack(anchor="w")
            return

        for filename in files:
            self._build_file_row(filename)

    def _build_file_row(self, filename: str) -> None:
        """为已上传文件构建一行 UI（含总结折叠面板）。"""
        file_container = ctk.CTkFrame(self.rag_list_frame, fg_color="transparent")
        file_container.pack(fill="x", pady=3)

        row = ctk.CTkFrame(
            file_container, fg_color=CLR_INPUT,
            border_color=CLR_BORDER, border_width=1, corner_radius=8,
        )
        row.pack(fill="x")

        ctk.CTkLabel(
            row, text=f"📄 {filename}",
            font=("Microsoft YaHei", 12), text_color=CLR_TEXT,
        ).pack(side="left", padx=12, pady=8)

        summary_panel = ctk.CTkFrame(
            file_container, fg_color=CLR_INPUT,
            border_color=CLR_BORDER, border_width=1, corner_radius=8,
        )
        self._summary_expanded.setdefault(filename, False)

        btn_text = "总结 ▼" if self._summary_expanded[filename] else "总结 ▶"
        summary_btn = ctk.CTkButton(
            row, text=btn_text, width=70, height=28,
            fg_color="#D0F4DE", text_color="#1a7a3a",
            font=("Microsoft YaHei", 11),
            command=lambda fn=filename, p=summary_panel, b=None: None,  # 下方重绑
        )
        summary_btn.pack(side="right", padx=(4, 4))

        # 重绑 command，传入 btn 引用
        summary_btn.configure(
            command=lambda fn=filename, p=summary_panel, b=summary_btn:
                self._toggle_summary(fn, p, b)
        )

        ctk.CTkButton(
            row, text="删除", width=60, height=28,
            fg_color="#FECACA", text_color="#7F1D1D",
            font=("Microsoft YaHei", 11),
            command=lambda f=filename: self._delete_one(f),
        ).pack(side="right", padx=(0, 8))

        # 如果已展开，恢复展示
        if self._summary_expanded[filename]:
            summary_panel.pack(fill="x", pady=(2, 0))
            if filename in self._summary_cache:
                ctk.CTkLabel(
                    summary_panel, text=self._summary_cache[filename],
                    font=("Microsoft YaHei", 12), text_color=CLR_TEXT,
                    justify="left", wraplength=600,
                ).pack(padx=12, pady=10, anchor="w")

    def _toggle_summary(
        self, filename: str,
        panel: ctk.CTkFrame,
        btn: ctk.CTkButton,
    ) -> None:
        expanded = self._summary_expanded.get(filename, False)
        if expanded:
            panel.pack_forget()
            self._summary_expanded[filename] = False
            btn.configure(text="总结 ▶")
        else:
            self._summary_expanded[filename] = True
            btn.configure(text="总结 ▼")
            panel.pack(fill="x", pady=(2, 0))
            self._render_summary_content(filename, panel)

    def _render_summary_content(self, filename: str, panel: ctk.CTkFrame) -> None:
        """清空面板并填充总结内容（已缓存则直接显示，否则后台生成）。"""
        for w in panel.winfo_children():
            w.destroy()

        if filename in self._summary_cache:
            ctk.CTkLabel(
                panel, text=self._summary_cache[filename],
                font=("Microsoft YaHei", 12), text_color=CLR_TEXT,
                justify="left", wraplength=600,
            ).pack(padx=12, pady=10, anchor="w")
            return

        loading_label = ctk.CTkLabel(
            panel, text="📋 正在提炼核心参数，请稍候...",
            font=("Microsoft YaHei", 12), text_color="#2980B9",
        )
        loading_label.pack(side="left", padx=12, pady=10)

        cancel_flag = [False]
        cancel_btn = ctk.CTkButton(
            panel, text="取消", width=55, height=28,
            fg_color="#FECACA", text_color="#7F1D1D",
            font=("Microsoft YaHei", 11),
        )
        cancel_btn.configure(
            command=lambda: (
                cancel_flag.__setitem__(0, True),
                cancel_btn.destroy(),
                loading_label.configure(text="⚠️ 已取消"),
            )
        )
        cancel_btn.pack(side="left", pady=10)

        threading.Thread(
            target=self._generate_summary,
            args=(filename, panel, loading_label, cancel_btn, cancel_flag),
            daemon=True,
        ).start()

    def _generate_summary(
        self,
        filename: str,
        panel: ctk.CTkFrame,
        loading_label: ctk.CTkLabel,
        cancel_btn: ctk.CTkButton,
        cancel_flag: list,
    ) -> None:
        """后台两阶段生成文档总结。独立函数，不再深度嵌套。"""

        def set_status(text: str) -> None:
            self.after(0, lambda: loading_label.configure(text=text)
                       if loading_label.winfo_exists() else None)

        def cancelled() -> bool:
            return cancel_flag[0]

        try:
            # ── 阶段 1：识别文档类型并提取关键词 ──────────────────────────
            set_status("🔍 正在识别文档类型与框架...")
            framework_context = self._extract_framework_text(filename)

            if cancelled():
                return

            prompt1 = SUMMARY_STEP1_CLASSIFY.format(framework_context=framework_context)
            res1_text = self._call_ollama_blocking(prompt1, timeout=120)

            if cancelled():
                return

            # 阶段1完全失败（空返回）：提示并中止，不用空内容硬撑
            if not res1_text.strip():
                set_status("❌ AI 分析失败，请确认模型已就绪后重试")
                return

            # ── 阶段 1.5：路由分析结果 ──────────────────────────────────
            doc_type = classify_doc_type(res1_text)
            set_status(f"📥 已识别为【{doc_type}】，正在提取核心内容...")

            context, status = query_rag_by_file(filename, res1_text, k=6)
            if status == QueryStatus.ERROR:
                logger.warning("总结阶段 RAG 查询失败，使用空上下文继续")

            if cancelled():
                return

            # ── 阶段 2：流式生成总结报告 ────────────────────────────────
            set_status(f"✍️ 正在生成【{doc_type}】深度报告...")
            template = SUMMARY_TEMPLATES.get(doc_type, SUMMARY_TEMPLATES["通用文档"])
            prompt2 = SUMMARY_STEP2_REPORT.format(
                doc_type=doc_type,
                core_content=context,
                target_template=template,
            )
            self._stream_summary_to_panel(filename, panel, loading_label, cancel_flag, prompt2)

        except FileNotFoundError as exc:
            logger.warning("文档文件缺失：%s", exc)
            set_status("❌ 文件不存在，请重新上传该文档")
        except requests.exceptions.ConnectionError:
            set_status("❌ 连接 Ollama 失败，请确认服务正在运行")
        except RagError as exc:
            logger.error("文档总结 RAG 错误（%s）：%s", filename, exc)
            set_status(f"❌ {exc}")
        except Exception as exc:
            logger.error("文档总结失败（%s）：%s", filename, exc)
            set_status("❌ 总结失败或超时，请重试")
        finally:
            self.after(0, lambda: cancel_btn.destroy()
                       if cancel_btn.winfo_exists() else None)

    def _extract_framework_text(self, filename: str) -> str:
        """提取 PDF 前 1200 字用于框架识别。
        文件路径与 rag_manager 保持一致，均来自 get_docs_path()。
        """
        file_path = os.path.join(get_docs_path(), filename)
        if not os.path.exists(file_path):
            raise FileNotFoundError(
                f"文档 {filename} 不在本地资料库目录中（{get_docs_path()}），"
                "请重新上传该文件。"
            )
        try:
            docs = PyPDFLoader(file_path).load()
            raw = ""
            for doc in docs:
                raw += doc.page_content + " "
                if len(raw) > 1500:
                    break
            return re.sub(r"\s+", " ", raw)[:1200]
        except Exception as exc:
            logger.warning("提取框架文本失败（%s）：%s", filename, exc)
            raise RagError(f"PDF 读取失败：{exc}") from exc

    def _call_ollama_blocking(self, prompt: str, timeout: int = 120) -> str:
        """非流式调用 Ollama，返回完整响应文本。
        默认超时 120 秒：首次调用时模型需从磁盘加载到显存，可能较慢。
        网络异常或返回非预期格式时返回空字符串，由调用方决定如何提示。"""
        payload = {
            "model": self.target_model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": 8192},
        }
        try:
            resp = requests.post(
                "http://127.0.0.1:11434/api/generate",
                json=payload,
                timeout=timeout,
                proxies={"http": None, "https": None},
            )
            return resp.json().get("response", "").strip()
        except Exception as exc:
            logger.warning("非流式调用 Ollama 失败：%s", exc)
            return ""

    def _stream_summary_to_panel(
        self,
        filename: str,
        panel: ctk.CTkFrame,
        loading_label: ctk.CTkLabel,
        cancel_flag: list,
        prompt: str,
    ) -> None:
        """流式接收总结内容并实时更新面板标签。"""
        payload = {
            "model": self.target_model,
            "prompt": prompt,
            "stream": True,
            "options": {"num_ctx": 8192},
        }
        full_text = ""
        summary_label: ctk.CTkLabel | None = None

        with requests.post(
            "http://127.0.0.1:11434/api/generate",
            json=payload, stream=True, timeout=300,
            proxies={"http": None, "https": None},
        ) as resp:
            for raw in resp.iter_lines():
                if cancel_flag[0]:
                    break
                if not raw:
                    continue
                chunk = json.loads(raw).get("response", "")
                full_text += chunk
                self._summary_cache[filename] = full_text
                current = full_text

                def update_label(text=current, lbl=loading_label):
                    nonlocal summary_label
                    try:
                        if summary_label is None or not summary_label.winfo_exists():
                            if lbl.winfo_exists():
                                lbl.destroy()
                            summary_label = ctk.CTkLabel(
                                panel, text=text,
                                font=("Microsoft YaHei", 12), text_color=CLR_TEXT,
                                justify="left", wraplength=600,
                            )
                            summary_label.pack(padx=12, pady=10, anchor="w")
                        else:
                            summary_label.configure(text=text)
                    except Exception:
                        pass

                self.after(0, update_label)

    # ------------------------------------------------------------------
    # PDF 上传
    # ------------------------------------------------------------------

    def _upload_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="选择PDF文件", filetypes=[("PDF文件", "*.pdf")]
        )
        if not path:
            return

        self.rag_progress_bar.grid()
        self.rag_progress_bar.set(0)
        self.rag_progress_label.configure(text="准备中...")
        self._cancel_flag = [False]

        if not hasattr(self, "_cancel_btn") or not self._cancel_btn.winfo_exists():
            self._cancel_btn = ctk.CTkButton(
                self.rag_frame, text="❌ 取消上传", height=32,
                fg_color="#FECACA", text_color="#7F1D1D",
                font=("Microsoft YaHei", 12, "bold"),
                command=self._cancel_upload,
            )
        self._cancel_btn.grid(row=2, column=1, sticky="e", padx=(10, 0))

        def on_progress(ratio: float, msg: str) -> None:
            self._upload_progress = ratio
            self._upload_msg = msg
            if hasattr(self, "rag_progress_bar") and self.rag_progress_bar.winfo_exists():
                self.after(0, lambda: self.rag_progress_bar.set(ratio))
                self.after(0, lambda: self.rag_progress_label.configure(
                    text=f"{msg}  {int(ratio * 100)}%"
                ))

        def on_finish(success: bool, error_msg: str = "") -> None:
            # 同名文件替换会在后台重建整个向量库；保留最后一条进度消息，
            # 避免把“正在重建”误覆盖成普通的“上传完成”。
            last_progress_msg = self._upload_msg or ""
            self._upload_progress = None
            self._upload_msg = None
            if hasattr(self, "_cancel_btn") and self._cancel_btn.winfo_exists():
                self.after(0, self._cancel_btn.grid_remove)
            if not hasattr(self, "rag_progress_bar") or not self.rag_progress_bar.winfo_exists():
                return
            if success:
                if "重建" in last_progress_msg:
                    success_msg = "✅ 文件已替换，知识库重建中..."
                else:
                    success_msg = "✅ 上传完成，可用于检索"
                self.after(0, lambda msg=success_msg: self.rag_progress_label.configure(text=msg))
                self.after(0, self._refresh_rag_list)
                self.after(2000, lambda: self.rag_progress_bar.grid_remove())
            else:
                if error_msg:
                    # 用弹窗显示完整错误（提示可能多行，小标签显示不全）
                    self.after(0, lambda: self.rag_progress_label.configure(text="⚠️ 上传失败"))
                    self.after(0, lambda: messagebox.showwarning("无法导入文档", error_msg))
                else:
                    self.after(0, lambda: self.rag_progress_label.configure(text="⚠️ 上传已取消"))
                self.after(0, self._refresh_rag_list)
                self.after(2000, lambda: self.rag_progress_bar.grid_remove())

        def run() -> None:
            try:
                result = add_pdf(path, on_progress, self._cancel_flag)
                self.after(0, lambda: on_finish(result))
            except RagError as exc:
                self.after(0, lambda: on_finish(False, str(exc)))
            except Exception as exc:
                logger.error("PDF 上传异常：%s", exc)
                self.after(0, lambda: on_finish(False, "未知错误，请重试"))

        threading.Thread(target=run, daemon=True).start()

    def _cancel_upload(self) -> None:
        if hasattr(self, "_cancel_flag"):
            self._cancel_flag[0] = True
        if hasattr(self, "_cancel_btn") and self._cancel_btn.winfo_exists():
            self._cancel_btn.grid_remove()
        self._upload_progress = None
        self._upload_msg = None

    def _delete_one(self, filename: str) -> None:
        if messagebox.askyesno("确认删除", f"确定要删除 {filename} 吗？"):
            delete_file(filename)
            self._refresh_rag_list()
            messagebox.showinfo("成功", f"已删除 {filename}")

    def _clear_all_rag(self) -> None:
        if messagebox.askyesno("确认", "确定要清理所有文档吗？"):
            clear_all()
            self._summary_cache.clear()
            self._summary_expanded.clear()
            self._refresh_rag_list()
            messagebox.showinfo("成功", "已清理所有文档")

    # ------------------------------------------------------------------
    # AI 生成
    # ------------------------------------------------------------------

    def start_gen(self, _src: str) -> None:
        # 并发保护：当前模式已在生成中，忽略重复触发（防狂点）
        if self.states.get(self.current_mode, {}).get("generating"):
            return

        product = self._read_entry()

        # 空输入：明确提示，而不是静默无反应
        if not product:
            messagebox.showinfo("提示", "请先输入商品信息或主题内容，再点击生成。")
            try:
                self.ent_p.focus_set()
            except Exception:
                pass
            return

        # 纯符号/无意义输入检测：至少要有中文、字母或数字
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", product):
            messagebox.showinfo("提示", "输入内容似乎无效，请输入有意义的商品信息或主题。")
            return

        supplement = self._read_textbox()

        # 超长输入保护：避免远超模型上下文导致异常或极慢
        MAX_INPUT_CHARS = 4000
        if len(product) + len(supplement) > MAX_INPUT_CHARS:
            if not messagebox.askyesno(
                "内容较长",
                f"输入内容较长（{len(product) + len(supplement)} 字），"
                f"超出建议长度（{MAX_INPUT_CHARS} 字）。\n\n"
                "过长内容可能导致生成变慢或质量下降，是否仍要继续？",
            ):
                return
            # 用户坚持，则截断到上限，保护模型
            if len(product) > MAX_INPUT_CHARS:
                product = product[:MAX_INPUT_CHARS]
            supplement = supplement[: max(0, MAX_INPUT_CHARS - len(product))]

        report_type     = self.sum_var.get()
        is_rag_enabled  = self.rag_toggle_var.get()
        selected_files  = [f for f, v in self.rag_file_vars.items() if v.get()]
        mode            = self.current_mode

        # 视频模式：收集专属参数，打包存起来供 _run_ai 使用
        if mode == VIDEO_MODE:
            dur_str = self.video_duration_var.get().replace("秒", "").strip()
            try:
                dur_val = int(dur_str)
            except ValueError:
                dur_val = 30
            self._video_params = {
                "video_type": self.video_type_var.get(),
                "style": self.video_style_var.get(),
                "duration": dur_val,
                "with_ai_prompt": self.video_ai_prompt_var.get(),
                "compliance": self._read_video_compliance(),
            }
        else:
            self._video_params = None

        self.stop_flag = False
        self.current_task_id += 1
        task_id = self.current_task_id
        self.states[mode]["generating"] = True
        self.update_ui_state(True)

        threading.Thread(
            target=self._run_ai,
            args=(product, supplement, task_id, mode,
                  report_type, is_rag_enabled, selected_files),
            daemon=True,
        ).start()

    def _run_ai(
        self,
        product: str,
        supplement: str,
        task_id: int,
        mode: str,
        report_type: str,
        is_rag_enabled: bool,
        selected_files: list[str],
    ) -> None:
        rag_context = self._collect_rag_context(
            mode, product, supplement, is_rag_enabled, selected_files
        )

        # 视频模式：用视频脚本提示词构建器
        if mode == VIDEO_MODE and getattr(self, "_video_params", None):
            vp = self._video_params
            full_prompt = build_video_script_prompt(
                video_type=vp["video_type"],
                style=vp["style"],
                product=product,
                supplement=supplement,
                duration=vp["duration"],
                with_ai_prompt=vp["with_ai_prompt"],
                compliance=vp["compliance"],
                kb_context=rag_context,
                model_name=self.target_model,
            )
        else:
            base_prompt = build_writing_prompt(
                mode, product, supplement, report_type, model_name=self.target_model
            )
            rag_section = (
                f"\n\n参考资料（来自文档检索）：\n{rag_context}" if rag_context else ""
            )
            full_prompt = base_prompt + rag_section

        full_output = ""
        try:
            if mode == self.current_mode:
                self.after(0, lambda: self.txt_o.delete("0.0", "end"))
                self.after(0, lambda: self.txt_o.insert("end", "⏳ AI 正在思考中，请稍候...\n\n"))

            # stop_event 与 self.stop_flag 双重控制：
            # stop_flag 由按钮设置；stop_event 用于在 iter_lines
            # 阻塞期间通过关闭连接来立即解除阻塞。
            stop_event = threading.Event()

            def _stream_lines(resp, out_queue):
                """在独立线程里消费流，把每行放入队列；停止时放入 None。"""
                try:
                    for raw in resp.iter_lines():
                        if stop_event.is_set():
                            break
                        out_queue.put(raw)
                except Exception:
                    pass
                finally:
                    out_queue.put(None)  # 哨兵：通知主循环结束

            line_queue = _queue.Queue()

            with requests.post(
                "http://127.0.0.1:11434/api/generate",
                json={
                    "model": self.target_model,
                    "prompt": full_prompt,
                    "stream": True,
                    "options": {
                        "num_ctx": 4096,  # 上下文窗口，Ollama 会根据可用资源自动分配推理后端
                    },
                },
                stream=True,
                timeout=(10, 300),  # (连接超时10s, 读取超时300s)：首次加载大模型较慢
                proxies={"http": None, "https": None},
            ) as resp:
                if mode == self.current_mode:
                    self.after(0, lambda: self.txt_o.delete("0.0", "end"))

                reader = threading.Thread(
                    target=_stream_lines, args=(resp, line_queue), daemon=True
                )
                reader.start()

                # 记录本次任务的 id，UI 回调执行时校验，
                # 一旦停止或切换任务，已排队的旧回调会被直接丢弃，实现"立即停"。
                def _safe_insert(text: str, tid: int) -> None:
                    if self.stop_flag or tid != self.current_task_id:
                        return
                    if mode == self.current_mode:
                        self.txt_o.insert("end", text)

                while True:
                    # 主动停止，或本任务已被新任务取代（切模式/新生成）→ 关闭流释放算力
                    if self.stop_flag or task_id != self.current_task_id:
                        stop_event.set()
                        resp.close()   # 关闭连接，立即终止 Ollama 推理与流
                        # 清空队列里积压但还没显示的内容，避免"刹不住"
                        try:
                            while True:
                                line_queue.get_nowait()
                        except _queue.Empty:
                            pass
                        break
                    try:
                        raw = line_queue.get(timeout=0.05)
                    except _queue.Empty:
                        continue       # 每 0.05s 检查一次 stop_flag
                    if raw is None:
                        break          # 流正常结束
                    if not raw:
                        continue
                    try:
                        chunk = json.loads(raw).get("response", "")
                    except json.JSONDecodeError:
                        continue
                    full_output += chunk
                    self.states[mode]["output"] = full_output
                    self.after(0, lambda c=chunk, t=task_id: _safe_insert(c, t))

                reader.join(timeout=2)

        except requests.exceptions.ConnectionError:
            # 主动停止或任务被取代会关闭连接而触发此异常，此时不应误报为连接失败
            if (not self.stop_flag and task_id == self.current_task_id
                    and mode == self.current_mode):
                self.after(0, lambda: (
                    self.txt_o.delete("0.0", "end"),
                    self.txt_o.insert("end", "❌ 连接 Ollama 失败，请检查服务是否正在运行。"),
                ))
        except requests.exceptions.Timeout:
            if (not self.stop_flag and task_id == self.current_task_id
                    and mode == self.current_mode):
                self.after(0, lambda: (
                    self.txt_o.delete("0.0", "end"),
                    self.txt_o.insert("end", "❌ 请求超时，模型响应过慢，请重试。"),
                ))
        except Exception as exc:
            if not self.stop_flag:
                logger.error("AI 生成异常：%s", exc)
                if mode == self.current_mode:
                    self.after(0, lambda: (
                        self.txt_o.delete("0.0", "end"),
                        self.txt_o.insert("end", "❌ 生成出错，请重试。若反复出现，请重启程序。"),
                    ))
        finally:
            # 正常结束但输出全空：提示用户（通常是模型加载异常/显存不足偶发）
            if (not self.stop_flag and not full_output.strip()
                    and mode == self.current_mode):
                self.after(0, lambda: (
                    self.txt_o.delete("0.0", "end"),
                    self.txt_o.insert(
                        "end",
                        "⚠️ 模型未返回内容，可能是临时异常或显存不足。\n"
                        "请重试一次；若反复出现，可在左下角切换到更小的模型。",
                    ),
                ))
            self.states[mode]["output"] = full_output
            self.states[mode]["generating"] = False
            if mode == self.current_mode:
                self.after(0, self.update_ui_state)

    def _collect_rag_context(
        self,
        mode: str,
        product: str,
        supplement: str,
        is_rag_enabled: bool,
        selected_files: list[str],
    ) -> str:
        """整合 RAG 检索结果。返回拼装好的上下文字符串。
        开关关闭 → 完全不使用知识库（返回空）。
        开关开启但未选文档 → 同样返回空（提示用户去选）。
        开关开启且选了文档 → 按选中文档定向检索。"""
        # 开关关闭：不使用知识库，直接返回空
        if not is_rag_enabled:
            return ""

        # 开关开启但没选任何文档：无可检索对象
        if not selected_files:
            return ""

        # 报告模式用补充信息搜索，更能命中业务数据
        search_query = (
            (supplement if supplement else "核心技术 硬件参数 功能指标 故障异常 测试数据")
            if mode in REPORT_SUB_MODES
            else product
        )

        contexts = []
        for filename in selected_files:
            result, status = query_rag_by_file(filename, search_query, k=8)
            if status == QueryStatus.ERROR:
                logger.warning("文件 %s RAG 查询失败", filename)
                continue
            if result and len(result.strip()) > 10:
                contexts.append(f"【来自文档 {filename} 的参考片段】：\n{result}")
            else:
                logger.debug("文件 %s 未检索到与 '%s' 强相关的内容", filename, product)

        return "\n\n".join(contexts)

    # ------------------------------------------------------------------
    # 按钮操作
    # ------------------------------------------------------------------

    def do_stop(self) -> None:
        self.stop_flag = True
        # 自增任务 id，使所有已排队但尚未执行的旧 UI 回调立即失效，
        # 配合 _safe_insert 的校验，达到"按下即停"的效果。
        self.current_task_id += 1
        self.states[self.current_mode]["generating"] = False
        self.update_ui_state(is_thinking=False)

    def do_copy(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.txt_o.get("0.0", "end-1c").strip())
        messagebox.showinfo("成功", "文案已复制")

    def do_clear(self) -> None:
        self.stop_flag = True
        self.txt_o.delete("0.0", "end")
        self.states[self.current_mode] = {
            "product": "", "style": "", "output": "", "generating": False
        }
        self._p_placeholder[self.current_mode] = True
        self._show_entry_placeholder(self.current_mode)
        self._s_placeholder[self.current_mode] = True
        self._show_textbox_placeholder(self.current_mode)
        self.update_ui_state()

    def _on_model_change(self, choice: str) -> None:
        # 选中未下载的档位：询问并触发下载
        if choice.endswith(self.NEED_DOWNLOAD_SUFFIX):
            real_model = choice[: -len(self.NEED_DOWNLOAD_SUFFIX)]
            if not messagebox.askyesno(
                "下载模型",
                f"模型 {real_model} 尚未下载。\n\n"
                "下载较大，需保持网络通畅，期间请勿关闭程序。\n"
                "（提示：更大的模型在显存不足时会用CPU运行，速度较慢）\n\n"
                "是否现在下载？",
            ):
                # 用户取消：把下拉框恢复到当前实际模型
                self.model_var.set(self.target_model)
                return
            self._download_model_inline(real_model)
            return

        # 选中已安装的模型：直接切换
        self.target_model = choice
        messagebox.showinfo("切换成功", f"已切换到 {choice}\n下次生成时生效")

    def _download_model_inline(self, model_name: str) -> None:
        """在主界面内下载指定模型，完成后切换并刷新下拉框。"""
        def _on_done():
            # 下载成功：更新已安装集合、刷新下拉框、切换为新模型
            self._installed_set.add(model_name)
            self.target_model = model_name
            self.model_menu.configure(values=self._build_model_options())
            self.model_var.set(model_name)
            messagebox.showinfo("下载完成", f"{model_name} 已就绪，已为你切换。")

        def _on_cancel():
            # 下载失败/取消：下拉框恢复当前模型
            self.model_var.set(self.target_model)

        # 只下这一个模型（ModelDownloadWindow 会自动附带 nomic 检查，但已装则跳过）
        ModelDownloadWindow(
            self, [model_name], model_name,
            on_finish=_on_done, on_cancel=_on_cancel,
        )


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

def _launch_main(target_model: str) -> None:
    AI_Assistant_UI(target_model).mainloop()


def _models_ready_for_tier(chosen_models: list[str]) -> bool:
    """该档位要求的模型是否已全部安装（含向量模型）。"""
    return check_models_installed(chosen_models)


def start(default_model: str, models_to_pull: list[str]) -> None:
    """
    首次启动流程（单一根窗口、单一 mainloop，避免嵌套 mainloop 导致卡死）：
      1. 启动 ollama
      2. 若已装齐某档模型 → 直接进主界面
      3. 否则弹档位选择 → 下载 → 进主界面
    关键：首次流程全程用一个隐藏 root 承载子窗口；root.mainloop() 退出后
    再启动主窗口，二者不重叠。
    """
    if not start_local_ollama():
        messagebox.showerror("启动失败", "无法启动 Ollama 服务，请检查 ollama.exe 是否存在。")
        return

    # 用一个可变容器把"选定的模型"传出 mainloop
    result = {"model": None}

    root = ctk.CTk()
    root.withdraw()

    # 判断是否已有「可直接使用」的完整档位：
    # 高/中/低任意一档的模型全部就绪，即视为非首次，直接用最大的已装模型。
    installed_qwen = _list_installed_qwen_models_global()
    embed_ok = check_models_installed([])  # 仅查 nomic
    ready_tier_model = None
    if embed_ok and installed_qwen:
        # 优先用已装的最大模型（7b > 3b > 1.5b）
        for m in (MODEL_7B, MODEL_3B, MODEL_1_5B):
            if m in installed_qwen:
                ready_tier_model = m
                break

    if ready_tier_model:
        # 非首次：模型已就绪，直接进主界面
        root.destroy()
        _launch_main(ready_tier_model)
        return

    # 首次或模型不全：检测显存、弹档位选择
    vram, recommended = recommend_tier()

    def _finish(model_to_use: str) -> None:
        """所有准备完成，记录结果并退出首次流程的 mainloop。"""
        result["model"] = model_to_use
        try:
            root.quit()      # 退出 mainloop（不 destroy，留到 mainloop 后统一清理）
        except Exception:
            pass

    def _cancel() -> None:
        """用户取消首次流程：不进主界面，干净退出。"""
        result["model"] = None
        try:
            root.quit()
        except Exception:
            pass

    def _after_choice(chosen_default: str, chosen_models: list[str]) -> None:
        if _models_ready_for_tier(chosen_models):
            _finish(chosen_default)
        else:
            ModelDownloadWindow(
                root, chosen_models, chosen_default,
                on_finish=lambda: _finish(chosen_default),
                on_cancel=_cancel,
            )

    TierSelectWindow(root, vram, recommended,
                     on_choose=_after_choice, on_cancel=_cancel)
    root.mainloop()

    # mainloop 结束：清理首次流程的 root，再启动主窗口
    try:
        root.destroy()
    except Exception:
        pass

    if result["model"]:
        _launch_main(result["model"])


def _list_installed_qwen_models_global() -> list[str]:
    """查询已安装的 qwen 模型（模块级，供 start 使用）。"""
    return _list_ollama_models("qwen")


def _show_activation_window(machine_code: str) -> None:
    act = ctk.CTk()
    act.title("激活软件")
    act.geometry("520x480")
    act.configure(fg_color="#E5E9F0")

    ctk.CTkLabel(
        act, text="请将机器码发送给卖家以获取激活码",
        font=("Microsoft YaHei", 13, "bold"), text_color="#333333",
    ).pack(pady=(28, 8))

    ctk.CTkLabel(
        act, text=f"机器码: {machine_code}",
        font=("Microsoft YaHei", 12), text_color="#333333",
    ).pack(pady=(0, 5))

    def copy_machine_code() -> None:
        act.clipboard_clear()
        act.clipboard_append(machine_code)
        messagebox.showinfo("成功", "机器码已复制！")

    ctk.CTkButton(
        act, text="📋 点击复制机器码",
        fg_color="#B8E1FF", text_color="#333333",
        font=("Microsoft YaHei", 12, "bold"),
        command=copy_machine_code,
    ).pack(pady=5)

    entry = ctk.CTkTextbox(
        act, width=440, height=120,
        fg_color="#FFFFFF", text_color="#333333",
        font=("Consolas", 12),
    )
    entry.pack(pady=20)

    def verify_key() -> None:
        key = entry.get("0.0", "end").strip()
        if verify_activation(machine_code, key):
            with open(_get_license_path(), "w", encoding="utf-8") as f:
                f.write(key)
            act.destroy()
            default_model, models_to_pull = detect_model_plan()
            start(default_model, models_to_pull)
        else:
            messagebox.showerror("激活失败", "激活码不正确，请确认机器码与激活码匹配。")

    ctk.CTkButton(act, text="立即激活", command=verify_key).pack(pady=20)
    act.mainloop()


def _get_license_path() -> str:
    """激活记录文件路径，存于统一数据目录。"""
    return os.path.join(get_data_root(), "license.dat")


def _is_activated(machine_code: str) -> bool:
    """检查 license.dat 是否存有对本机有效的激活码（重新验证签名）。"""
    license_path = _get_license_path()
    if not os.path.exists(license_path):
        return False
    try:
        with open(license_path, "r", encoding="utf-8") as f:
            saved_key = f.read().strip()
        return verify_activation(machine_code, saved_key)
    except Exception:
        return False


if __name__ == "__main__":
    app_lock = acquire_single_instance_lock()
    if not app_lock:
        focus_existing_window()
        sys.exit(0)

    machine_code = get_machine_code()

    if _is_activated(machine_code):
        default_model, models_to_pull = detect_model_plan()
        start(default_model, models_to_pull)
    else:
        _show_activation_window(machine_code)

    # 保持 socket 存活直到进程退出
    _ = app_lock
