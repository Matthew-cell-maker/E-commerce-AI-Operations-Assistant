"""
rag_manager.py
负责 PDF 文档的导入、向量化存储与检索。

线程安全策略：
  - _db_lock (RLock)：保护向量库的读写，重建期间阻塞查询。
  - _rebuilding (Event)：标记重建进行中，query 函数在等待完成后再读取。
"""

import os

# 让所有发往本地 ollama 的请求绕过系统代理（用户开 VPN/代理时，
# 127.0.0.1 的请求若走代理会被拦截，导致 embedding 返回错误）。
# 必须在 langchain / httpx 初始化之前设置才生效。
_no_proxy = os.environ.get("NO_PROXY", "")
for _host in ("127.0.0.1", "localhost"):
    if _host not in _no_proxy:
        _no_proxy = f"{_no_proxy},{_host}" if _no_proxy else _host
os.environ["NO_PROXY"] = _no_proxy
os.environ["no_proxy"] = _no_proxy

import sys
import json
import shutil
import logging
import threading
import uuid
from enum import Enum

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.vectorstores import Chroma

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CHUNK_SIZE = 300
CHUNK_OVERLAP = 50
EMBED_MODEL = "nomic-embed-text"
EMBED_BATCH_SIZE = 10          # 进度回调的粒度（切片数）
REBUILD_WAIT_TIMEOUT = 60      # 查询等待重建完成的最长秒数

# 文件上传的健壮性限制
MAX_PDF_SIZE_MB = 100          # 超过此大小的 PDF 拒绝（防止内存爆/超慢）
MAX_PDF_PAGES = 500            # 超过此页数拒绝导入，避免内存占用和长时间无响应

# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------
_db_lock = threading.RLock()           # 读写向量库时加锁
_rebuild_done = threading.Event()      # 重建完成时 set()
_rebuild_done.set()                    # 初始状态：无需等待


class RagError(Exception):
    """RAG 模块专用异常，用于区分业务错误与静默空结果。"""


class QueryStatus(Enum):
    OK = "ok"
    NO_DB = "no_db"
    NO_FILES = "no_files"
    REBUILDING_TIMEOUT = "rebuilding_timeout"
    ERROR = "error"


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

def _get_data_root() -> str:
    """
    统一数据根目录 C:\\ProgramData\\EcomAssistant。
    与 main_window.get_data_root 保持一致：所有运行时数据集中存放，
    可写、纯英文路径，规避 Program Files 权限与中文路径问题。
    """
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    root = os.path.join(program_data, "EcomAssistant")
    os.makedirs(root, exist_ok=True)
    return root


def get_db_path() -> str:
    p = os.path.join(_get_data_root(), "rag_db")
    os.makedirs(p, exist_ok=True)
    return p


def get_docs_path() -> str:
    p = os.path.join(_get_data_root(), "rag_docs")
    os.makedirs(p, exist_ok=True)
    return p


def _get_index_path() -> str:
    return os.path.join(_get_data_root(), "rag_index.json")


# ---------------------------------------------------------------------------
# 索引读写
# ---------------------------------------------------------------------------

def _load_index() -> dict:
    p = _get_index_path()
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_index(index: dict) -> None:
    with open(_get_index_path(), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def get_uploaded_files() -> list[str]:
    """返回已入库的文件名列表。"""
    return list(_load_index().keys())


# ---------------------------------------------------------------------------
# 嵌入模型
# ---------------------------------------------------------------------------

def _get_embeddings() -> OllamaEmbeddings:
    # num_gpu=0：强制 embedding 模型走 CPU，不占用显存。
    # nomic-embed-text 仅 137M 参数，CPU 推理几乎无感，
    # 把宝贵的显存完整留给主对话模型，兼容 4GB 甚至无独显的机器。
    try:
        return OllamaEmbeddings(model=EMBED_MODEL, num_gpu=0)
    except TypeError:
        # 个别 langchain 版本不接受 num_gpu 参数时的降级方案
        logger.warning("OllamaEmbeddings 不支持 num_gpu 参数，回退默认配置")
        return OllamaEmbeddings(model=EMBED_MODEL)


def _get_text_splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )


# ---------------------------------------------------------------------------
# 写操作：添加 PDF
# ---------------------------------------------------------------------------

def _validate_pdf(pdf_path: str) -> None:
    """
    上传前预检，把各种问题文件挡在处理之前，给出明确的中文提示。
    抛出 RagError（含友好原因），通过则正常返回。
    覆盖：文件不存在、空文件、非PDF、加密、超大、超页数、损坏。
    """
    # 1. 文件存在性
    if not os.path.exists(pdf_path):
        raise RagError("找不到该文件，可能已被移动或删除，请重新选择。")

    # 2. 后缀检查
    if not pdf_path.lower().endswith(".pdf"):
        raise RagError("仅支持 PDF 文件，请选择 .pdf 格式的文档。")

    # 3. 大小检查
    try:
        size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    except OSError:
        raise RagError("无法读取文件，请检查文件是否被其他程序占用。")
    if size_mb == 0:
        raise RagError("这是一个空文件（0 字节），无法处理。")
    if size_mb > MAX_PDF_SIZE_MB:
        raise RagError(
            f"文件过大（{size_mb:.0f}MB），超过 {MAX_PDF_SIZE_MB}MB 上限。\n"
            "请拆分文档后再上传，或上传更小的文件。"
        )

    # 4. 文件头校验：真正的 PDF 以 %PDF 开头（防止改名的假PDF）
    try:
        with open(pdf_path, "rb") as f:
            header = f.read(5)
        if not header.startswith(b"%PDF"):
            raise RagError("这不是有效的 PDF 文件（文件头损坏或并非真正的PDF）。")
    except RagError:
        raise
    except Exception:
        raise RagError("无法读取文件内容，文件可能已损坏。")

    # 5. 加密与页数检测：用 pypdf 在解析、切片前拦截不适合导入的文件。
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            # 尝试空密码解密（部分PDF仅加密但无密码）
            try:
                if reader.decrypt("") == 0:  # 0 = 解密失败
                    raise RagError(
                        "该 PDF 已加密（受密码保护），无法读取。\n"
                        "请先用 PDF 工具解除密码后再上传。"
                    )
            except RagError:
                raise
            except Exception:
                raise RagError(
                    "该 PDF 已加密（受密码保护），无法读取。\n"
                    "请先解除密码后再上传。"
                )
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise RagError(
                f"该 PDF 共 {page_count} 页，超过 {MAX_PDF_PAGES} 页导入上限。\n"
                "请拆分文档后分批上传，以保证知识库生成速度和稳定性。"
            )
    except RagError:
        raise
    except ImportError:
        # 没装 pypdf 时跳过加密检测（PyPDFLoader 底层依赖 pypdf，一般都有）
        pass
    except Exception as exc:
        # pypdf 打不开 = 文件损坏
        raise RagError(f"PDF 文件损坏或格式异常，无法打开。") from exc


def add_pdf(pdf_path: str, on_progress=None, cancel_flag=None) -> bool:
    """
    将 PDF 导入向量库。

    参数：
        pdf_path:    源文件路径。
        on_progress: 回调 (ratio: float, message: str)，在主线程外调用。
        cancel_flag: 单元素列表 [bool]，外部置 True 时中止操作。

    返回：
        True  = 成功
        False = 用户取消
    抛出：
        RagError = 处理失败（可向用户展示）
    """

    def _progress(ratio: float, msg: str) -> None:
        if on_progress:
            on_progress(ratio, msg)

    def _cancelled() -> bool:
        return bool(cancel_flag and cancel_flag[0])

    # 上传前预检：拦截不存在/空/非PDF/加密/超大/损坏的文件
    _validate_pdf(pdf_path)

    filename = os.path.basename(pdf_path)
    docs_dir = get_docs_path()
    index = _load_index()
    is_replacement = filename in index

    # 磁盘空间检查：可用空间需大于文件的 3 倍（原件+向量库余量）
    try:
        src_size = os.path.getsize(pdf_path)
        free = shutil.disk_usage(docs_dir).free
        if free < src_size * 3:
            raise RagError(
                "磁盘剩余空间不足，无法导入该文档。\n"
                "请清理磁盘后再试。"
            )
    except RagError:
        raise
    except Exception:
        pass  # 空间检查失败不阻断主流程

    dest = os.path.join(docs_dir, filename)
    # 同名文件不能直接覆盖：旧文件仍可能被现有索引引用。先写入临时文件，
    # 等新版本确认可读、可切片后再原子替换，避免导入失败时损坏旧知识库。
    staged_dest = (
        os.path.join(docs_dir, f".{filename}.{uuid.uuid4().hex}.uploading")
        if is_replacement else dest
    )

    try:
        shutil.copy2(pdf_path, staged_dest)

        _progress(0.1, "正在读取 PDF...")
        try:
            docs = PyPDFLoader(staged_dest).load()
        except Exception as exc:
            raise RagError(f"PDF 读取失败：{exc}") from exc

        if _cancelled():
            _cleanup_file(staged_dest)
            return False

        _progress(0.3, "正在切片...")
        chunks = _get_text_splitter().split_documents(docs)
        total = len(chunks)
        if total == 0:
            raise RagError(
                "无法从该 PDF 提取文字。\n\n"
                "常见原因：这是扫描版或纯图片 PDF（整页都是图片，没有文字层）。\n"
                "建议：先用带 OCR 的工具把图片转成可选中的文字版，再上传。"
            )

        # 给每个切片注入干净的文件名元数据，用于后续精准过滤。
        # 不依赖 source 路径（路径分隔符 \ 与 / 在不同环节不一致会导致过滤失效，
        # 进而把别的文档内容也检索进来，造成多产品混淆）。
        for ch in chunks:
            ch.metadata["filename"] = filename

        _progress(0.4, "正在向量化...")
        embeddings = _get_embeddings()

        # 分批回调进度；实际向量化在 from_documents 内部一次完成，
        # 这里的循环仅用于取消检测和进度提示。
        for i in range(0, total, EMBED_BATCH_SIZE):
            if _cancelled():
                _cleanup_file(staged_dest)
                return False
            done = min(i + EMBED_BATCH_SIZE, total)
            _progress(0.4 + 0.5 * (done / total), f"准备向量化... {done}/{total}")

        # Chroma 的 from_documents 是追加写入。同名文件若直接写入会让旧、
        # 新切片并存，检索时可能返回过期内容。因此同名更新改为替换源文件、
        # 更新索引并重建整个库；重建期间查询会由 _rebuild_done 安全等待。
        if is_replacement:
            _progress(0.9, "正在替换旧版本并重建知识库...")
            with _db_lock:
                os.replace(staged_dest, dest)
                index[filename] = {"path": dest, "chunks": total}
                _save_index(index)
            _start_rebuild_async()
            _progress(1.0, "已更新，知识库正在重建...")
            return True

        _progress(0.9, "正在写入向量库...")
        with _db_lock:
            Chroma.from_documents(
                chunks, embeddings,
                persist_directory=get_db_path(),
            )

        index[filename] = {"path": dest, "chunks": total}
        _save_index(index)

        _progress(1.0, "完成！")
        return True

    except RagError:
        _cleanup_file(staged_dest)
        raise
    except Exception as exc:
        _cleanup_file(staged_dest)
        # 打印完整堆栈到控制台和日志文件，便于定位被 langchain 吞掉的真实错误
        import traceback
        tb = traceback.format_exc()
        logger.error("add_pdf 失败，完整堆栈：\n%s", tb)
        try:
            with open("rag_error.log", "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        raise RagError(f"导入失败：{exc!r}") from exc


def _cleanup_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("清理临时文件失败：%s", exc)


# ---------------------------------------------------------------------------
# 写操作：删除 / 清空
# ---------------------------------------------------------------------------

def delete_file(filename: str) -> None:
    """从索引和文件系统中删除文件，然后异步重建向量库。"""
    index = _load_index()
    if filename not in index:
        return

    file_path = index[filename]["path"]
    _cleanup_file(file_path)
    del index[filename]
    _save_index(index)

    _start_rebuild_async()


def clear_all() -> None:
    """删除所有文档、向量库和索引。"""
    _rebuild_done.set()  # 防止 rebuild 进行中时 clear_all 卡住
    with _db_lock:
        for path in (get_db_path(), get_docs_path()):
            if os.path.exists(path):
                shutil.rmtree(path)
        index_path = _get_index_path()
        if os.path.exists(index_path):
            os.remove(index_path)


# ---------------------------------------------------------------------------
# 内部：异步重建向量库
# ---------------------------------------------------------------------------

def _start_rebuild_async() -> None:
    _rebuild_done.clear()
    threading.Thread(target=_rebuild_vectorstore, daemon=True).start()


def _rebuild_vectorstore() -> None:
    """重建整个向量库。执行期间持有 _db_lock，查询会等待。"""
    try:
        with _db_lock:
            db_path = get_db_path()
            if os.path.exists(db_path):
                shutil.rmtree(db_path)

            index = _load_index()
            if not index:
                return

            embeddings = _get_embeddings()
            splitter = _get_text_splitter()
            all_chunks = []

            for filename, info in index.items():
                if os.path.exists(info["path"]):
                    try:
                        docs = PyPDFLoader(info["path"]).load()
                        file_chunks = splitter.split_documents(docs)
                        # 同样注入干净文件名元数据，保证重建后过滤仍精准
                        for ch in file_chunks:
                            ch.metadata["filename"] = filename
                        all_chunks.extend(file_chunks)
                    except Exception as exc:
                        logger.warning("重建时跳过损坏文件 %s：%s", filename, exc)

            if all_chunks:
                Chroma.from_documents(
                    all_chunks, embeddings,
                    persist_directory=db_path,
                )
    except Exception as exc:
        logger.error("向量库重建失败：%s", exc)
    finally:
        _rebuild_done.set()


# ---------------------------------------------------------------------------
# 读操作：查询
# ---------------------------------------------------------------------------

def _wait_for_rebuild() -> bool:
    """等待正在进行的重建完成。返回 False 表示超时。"""
    if not _rebuild_done.is_set():
        logger.debug("向量库重建中，等待完成...")
        return _rebuild_done.wait(timeout=REBUILD_WAIT_TIMEOUT)
    return True


def query_rag(question: str, k: int = 3) -> tuple[str, QueryStatus]:
    """
    全局语义检索。

    返回 (context_text, status)。
    status == QueryStatus.OK 时 context_text 有内容；其余情况为空字符串。
    """
    if not os.path.exists(get_db_path()):
        return "", QueryStatus.NO_DB
    if not get_uploaded_files():
        return "", QueryStatus.NO_FILES
    if not _wait_for_rebuild():
        return "", QueryStatus.REBUILDING_TIMEOUT

    try:
        with _db_lock:
            embeddings = _get_embeddings()
            vectorstore = Chroma(
                persist_directory=get_db_path(),
                embedding_function=embeddings,
            )
            results = vectorstore.similarity_search(question, k=k)
        return "\n".join(r.page_content for r in results), QueryStatus.OK
    except Exception as exc:
        logger.error("RAG 全局查询失败：%s", exc)
        return "", QueryStatus.ERROR


def query_rag_by_file(
    filename: str, question: str, k: int = 5
) -> tuple[str, QueryStatus]:
    """
    按文件过滤的语义检索。
    用干净的 filename 元数据过滤（而非 source 路径），避免路径分隔符不一致
    导致过滤失效、把别的文档内容混入的问题。

    返回 (context_text, status)。
    """
    if not os.path.exists(get_db_path()):
        return "", QueryStatus.NO_DB
    if not _wait_for_rebuild():
        return "", QueryStatus.REBUILDING_TIMEOUT

    try:
        with _db_lock:
            embeddings = _get_embeddings()
            vectorstore = Chroma(
                persist_directory=get_db_path(),
                embedding_function=embeddings,
            )
            results = vectorstore.similarity_search(
                question, k=k,
                filter={"filename": filename},
            )
        return "\n".join(r.page_content for r in results), QueryStatus.OK
    except Exception as exc:
        logger.error("RAG 按文件查询失败（%s）：%s", filename, exc)
        return "", QueryStatus.ERROR
