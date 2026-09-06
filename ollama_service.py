"""Ollama 本地服务的无界面基础设施。

本模块不依赖 CustomTkinter，便于未来单元测试和其他入口复用。
"""

import logging

import psutil
import requests

OLLAMA_API_URL = "http://127.0.0.1:11434"


def is_ready(timeout: float = 2) -> bool:
    """检查本地 Ollama 的模型 API 是否已可用。"""
    try:
        response = requests.get(
            f"{OLLAMA_API_URL}/api/tags",
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def get_port_owners(port: int, logger: logging.Logger | None = None) -> list[str]:
    """返回监听端口的进程描述；不会终止或修改任何进程。"""
    owners: list[str] = []
    seen_pids: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (
                conn.status == psutil.CONN_LISTEN
                and conn.laddr
                and conn.laddr.port == port
                and conn.pid
                and conn.pid not in seen_pids
            ):
                seen_pids.add(conn.pid)
                try:
                    name = psutil.Process(conn.pid).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    name = "未知进程"
                owners.append(f"{name}（PID {conn.pid}）")
    except Exception as exc:
        if logger:
            logger.warning("检查端口占用失败：%s", exc)
    return owners


def check_models_installed(required_models: list[str], embed_model: str,
                           timeout: float = 5) -> bool:
    """检查指定主模型及嵌入模型是否都已安装。"""
    try:
        response = requests.get(
            f"{OLLAMA_API_URL}/api/tags",
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        installed = {item.get("name", "") for item in response.json().get("models", [])}
    except (requests.RequestException, ValueError):
        return False

    def has_model(model_name: str) -> bool:
        if ":" in model_name:
            return any(name == model_name or name == model_name + ":latest" for name in installed)
        return any(name == model_name or name.startswith(model_name + ":") for name in installed)

    return all(has_model(model) for model in [*required_models, embed_model])


def list_installed_models(prefix: str = "qwen", timeout: float = 5) -> list[str]:
    """返回指定前缀的已安装模型名称。"""
    try:
        response = requests.get(
            f"{OLLAMA_API_URL}/api/tags",
            timeout=timeout,
            proxies={"http": None, "https": None},
        )
        names = [item.get("name", "") for item in response.json().get("models", [])]
        return sorted(name for name in names if name.startswith(prefix))
    except (requests.RequestException, ValueError):
        return []
