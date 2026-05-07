import customtkinter as ctk
import requests
import threading
import re
import json
import os
import uuid
import hashlib
import subprocess
import time
import sys
from datetime import datetime

# ==================== 配置区 ====================
TARGET_MODEL = "qwen2.5:7b" 
SALT = "Please contact the administrator at morse2000@163.com" 

def get_base_path():
    """统一获取当前运行目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def start_local_ollama():
    """后台静默启动引擎，并使用循环探测确保服务真正就绪"""
    try:
        base_path = get_base_path()
        ollama_bin = os.path.join(base_path, "ollama.exe")
        models_dir = os.path.join(base_path, "models")
        os.environ["OLLAMA_MODELS"] = models_dir

        try:
            requests.get("http://127.0.0.1:11434", timeout=1, proxies={"http": None, "https": None})
            return 
        except:
            pass

        if os.path.exists(ollama_bin):
            subprocess.Popen(
                [ollama_bin, "serve"],
                env=os.environ,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            # 循环探测服务是否启动
            for _ in range(10):
                try:
                    time.sleep(1)
                    requests.get("http://127.0.0.1:11434", timeout=1, proxies={"http": None, "https": None})
                    break
                except:
                    continue
    except Exception as e:
        print(f"核心服务启动失败: {e}")

def check_model_in_folder():
    """检测当前文件夹下 models 目录内是否存在模型索引文件"""
    try:
        base_path = get_base_path()
        repo, tag = TARGET_MODEL.split(":") if ":" in TARGET_MODEL else (TARGET_MODEL, "latest")
        manifest_path = os.path.join(
            base_path, "models", "manifests", "registry.ollama.ai", 
            "library", repo, tag
        )
        return os.path.exists(manifest_path)
    except:
        return False

# ==================== 1. 下载窗口====================
class ModelDownloadWindow(ctk.CTkToplevel):
    def __init__(self, master, on_finish):
        super().__init__(master)
        self.title("核心模型初始化")
        self.geometry("450x200")
        self.on_finish = on_finish
        
        # 显式禁止置顶和锁定
        self.attributes("-topmost", False)
        
        self.label = ctk.CTkLabel(self, text=f"正在拉取核心模型: {TARGET_MODEL}", font=ctk.CTkFont(size=14, weight="bold"))
        self.label.pack(pady=(30, 10))

        self.progress_bar = ctk.CTkProgressBar(self, width=350)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)

        self.status_label = ctk.CTkLabel(self, text="正在连接本地引擎...", font=ctk.CTkFont(size=12))
        self.status_label.pack()

        threading.Thread(target=self.download_thread, daemon=True).start()

    def download_thread(self):
        url = "http://127.0.0.1:11434/api/pull"
        try:
            response = requests.post(
                url, 
                json={"name": TARGET_MODEL, "stream": True}, 
                stream=True, 
                timeout=None, 
                proxies={"http": None, "https": None}
            )
            for line in response.iter_lines():
                if line:
                    data = json.loads(line)
                    status = data.get("status", "")
                    total = data.get("total", 0)
                    completed = data.get("completed", 0)

                    if total > 0:
                        p = completed / total
                        self.after(0, lambda p=p, s=status: self.update_ui(p, s))
                    else:
                        self.after(0, lambda s=status: self.status_label.configure(text=s))
                    
                    if status == "success":
                        self.after(0, self.finish)
                        return
        except Exception:
            self.after(0, lambda: self.label.configure(text="正在等待引擎响应...", text_color="#F39C12"))
            time.sleep(2)
            self.download_thread()

    def update_ui(self, p, s):
        self.progress_bar.set(p)
        self.status_label.configure(text=f"{s} | {int(p*100)}%")

    def finish(self):
        self.destroy()
        self.on_finish()

# ==================== 2. 授权激活逻辑 ====================
def get_machine_code():
    machine_id = str(uuid.getnode())
    return hashlib.sha256(machine_id.encode()).hexdigest()[:16].upper()

def generate_activation_key(machine_code):
    raw_str = machine_code + SALT
    return hashlib.md5(raw_str.encode()).hexdigest()[8:24].upper()

class ActivationWindow(ctk.CTk):
    def __init__(self, on_success):
        super().__init__()
        self.on_success = on_success
        self.title("软件授权激活")
        self.geometry("500x380")
        self.resizable(False, False)
        self.machine_code = get_machine_code()
        
        ctk.CTkLabel(self, text="🔑 软件尚未激活", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=25)
        self.code_entry = ctk.CTkEntry(self, width=350, justify="center")
        self.code_entry.insert(0, self.machine_code)
        self.code_entry.configure(state="readonly")
        self.code_entry.pack(pady=5)
        
        self.key_entry = ctk.CTkEntry(self, width=350, placeholder_text="在此粘贴激活码")
        self.key_entry.pack(pady=10)
        
        ctk.CTkButton(self, text="立即激活软件", height=40, command=self.check).pack(pady=25)

    def check(self):
        if self.key_entry.get().strip().upper() == generate_activation_key(self.machine_code):
            with open("license.dat", "w") as f: f.write(self.key_entry.get().strip().upper())
            self.destroy()
            self.on_success()

# ==================== 3. 主程序 UI ====================
class AI_Assistant_UI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("电商 AI 运营助手")
        self.geometry("1100x820")
        self.minsize(1000, 750)
        self.current_mode = "title"
        self.history_file = "history_records.json"

        # 布局布局
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # 侧边栏
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="运营助手 Pro", font=ctk.CTkFont(size=22, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(30, 20))
        
        self.tab_title = ctk.CTkButton(self.sidebar_frame, text="爆款标题生成", fg_color="#1f538d", command=self.switch_to_title)
        self.tab_title.grid(row=1, column=0, padx=15, pady=10)
        self.tab_article = ctk.CTkButton(self.sidebar_frame, text="推广软文生成", fg_color="transparent", command=self.switch_to_article)
        self.tab_article.grid(row=2, column=0, padx=15, pady=10)
        self.tab_summary = ctk.CTkButton(self.sidebar_frame, text="职场战报汇报", fg_color="transparent", command=self.switch_to_summary)
        self.tab_summary.grid(row=3, column=0, padx=15, pady=10)
        self.tab_history = ctk.CTkButton(self.sidebar_frame, text="历史生成记录", fg_color="transparent", command=self.switch_to_history)
        self.tab_history.grid(row=4, column=0, padx=15, pady=10)

        # 主内容区
        self.main_frame = ctk.CTkFrame(self, corner_radius=15, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=25, pady=25)
        self.main_frame.grid_columnconfigure(0, weight=1)
        
        self.title_label = ctk.CTkLabel(self.main_frame, text="爆款标题生成", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_label.grid(row=0, column=0, sticky="w", pady=(0, 20))

        self.summary_type_var = ctk.StringVar(value="日常汇报")
        self.summary_type_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.type_menu = ctk.CTkSegmentedButton(self.summary_type_frame, values=["日常汇报", "转正述职", "年度汇报"], variable=self.summary_type_var)
        self.type_menu.pack(side="left")

        self.product_entry = ctk.CTkEntry(self.main_frame, placeholder_text="例如：夏季防晒衣...", height=45)
        self.product_entry.grid(row=3, column=0, sticky="ew", pady=(5, 15))

        self.style_entry = ctk.CTkTextbox(self.main_frame, height=120, border_width=1)
        self.style_entry.grid(row=5, column=0, sticky="ew", pady=(5, 15))
        
        self.output_textbox = ctk.CTkTextbox(self.main_frame, font=ctk.CTkFont(size=15), border_width=1)
        self.output_textbox.grid(row=8, column=0, sticky="nsew", pady=(5, 20))
        self.main_frame.grid_rowconfigure(8, weight=1)

        self.btn_generate = ctk.CTkButton(self.main_frame, text="深度创作生成", font=ctk.CTkFont(weight="bold"), command=self.start_gen, height=45)
        self.btn_generate.grid(row=9, column=0, sticky="ew")

    def switch_to_title(self): self.title_label.configure(text="爆款标题生成"); self.summary_type_frame.grid_forget()
    def switch_to_article(self): self.title_label.configure(text="推广软文生成"); self.summary_type_frame.grid_forget()
    def switch_to_summary(self): self.title_label.configure(text="职场战报汇报"); self.summary_type_frame.grid(row=1, column=0, sticky="w")
    def switch_to_history(self): self.render_history()

    def start_gen(self):
        bg = self.product_entry.get().strip()
        if not bg: return
        self.btn_generate.configure(state="disabled", text="AI正在思考...")
        threading.Thread(target=self.fetch_ai, args=(bg, self.style_entry.get("0.0", "end").strip())).start()

    def fetch_ai(self, bg, dt):
        url = "http://127.0.0.1:11434/api/generate"
        p = f"小红书爆款专家。产品：{bg}，细节：{dt}"
        try:
            res = requests.post(url, json={"model": TARGET_MODEL, "prompt": p, "stream": False}, timeout=300, proxies={"http": None, "https": None})
            text = res.json().get("response", "生成失败")
            self.output_textbox.delete("0.0", "end"); self.output_textbox.insert("0.0", text)
        except Exception as e:
            self.output_textbox.insert("end", f"\n错误: {e}")
        finally:
            self.btn_generate.configure(state="normal", text="深度创作生成")

    def render_history(self): pass # 保持原有逻辑

# ==================== 4. 核心启动流程 ====================
def start_app():
    start_local_ollama()
    
    root = ctk.CTk()
    root.withdraw() 

    def launch_main():
        root.destroy() 
        app = AI_Assistant_UI()
        app.mainloop()

    if check_model_in_folder():
        launch_main()
    else:
        # 启动普通的下载窗口，挂在隐藏的 root 下
        ModelDownloadWindow(master=root, on_finish=launch_main)
        root.mainloop()

if __name__ == "__main__":
    m_code = get_machine_code()
    if os.path.exists("license.dat") and open("license.dat").read().strip() == generate_activation_key(m_code):
        start_app()
    else:
        ActivationWindow(on_success=start_app).mainloop()