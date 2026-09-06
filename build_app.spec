# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— 电商运营助手 Pro
打包命令（在 007 目录下，用你的 Python 运行）：
    pyinstaller build_app.spec --noconfirm

产物：dist\\电商运营助手.exe（单文件）

注意：
  - ollama.exe 与 lib\\ 不打进 exe（体积大且需保持文件结构），
    打包后由你手动放到 exe 旁边 / 或交给 Inno Setup 一起安装。
  - keygen、private_key.pem、license.dat、models 等不会被打包。
"""

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# 需要完整收集的第三方库（它们有大量隐式子模块和数据文件，
# 不显式收集会导致打包后 import 失败）。
#
# 注意：这些依赖缺失时必须让构建失败。此前的静默忽略会让 PyInstaller
# 表面上打包成功，但客户机器在启动或首次导入 PDF 时才报模块缺失。
for pkg in [
    "langchain",
    "langchain_community",
    "langchain_core",
    "langchain_text_splitters",
    "chromadb",
    "customtkinter",
    "tkinter",
    "pypdf",              # rag_manager 中按需导入，PyInstaller 无法可靠静态发现
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:
        raise RuntimeError(
            f"无法收集必需依赖 {pkg!r}，请安装完整的构建依赖后重试。"
        ) from exc

# 项目自身的 Python 模块（与主程序同目录，确保被打包）
hiddenimports += [
    "rag_manager",
    "prompts",
    "license_core",
]

a = Analysis(
    ["main_window.pyw"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 明确排除不该进客户端的东西
        "keygen",
        "keygen_gui",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ===== 文件夹模式（onedir）=====
# EXE 只包含启动器，不内嵌库；库由后面的 COLLECT 收集到输出文件夹。
# 好处：启动快、运行时不解压到临时目录，彻底避免关闭时
# "Failed to remove temporary directory" 的提示。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,        # 关键：库不打进 exe，交给 COLLECT
    name="电商运营助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                # 不弹黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="logo.ico",              # 程序图标
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="电商运营助手",          # 输出文件夹名：dist\电商运营助手\
)
