[README.md](https://github.com/user-attachments/files/31881693/README.md)
# 电商运营助手

一款基于本地大模型的 Windows 电商内容运营助手，支持商品文案、工作汇报、短视频脚本和 PDF 知识库检索。

## 主要功能

- 爆款标题生成
- 商品推广软文生成
- 日常汇报、转正述职、年度汇报
- 短视频脚本生成
- 导入产品说明书、竞品报告、平台规则等 PDF 文档
- 基于本地知识库生成更符合产品资料的内容
- 本地 Ollama 推理，不依赖云端 API
- 根据显卡显存选择合适的模型档位

## 运行环境

- Windows 10/11，64 位
- 建议至少 8 GB 内存
- 模型运行需要足够的磁盘空间和显存；没有独立显卡时也可以使用 CPU，但速度会较慢

## 安装使用

1. 下载 `电商运营助手_安装程序.exe`。
2. 使用纯英文路径安装，例如 `C:\Program Files\EcomAssistant`。
3. 首次启动时选择模型档位，等待模型下载完成。
4. 输入商品名称或主题，选择功能模块后点击生成。

模型默认保存到：

```text
C:\ProgramData\EcomAssistant\models
```

## 支持的模型

程序当前使用 Qwen 2.5 系列模型：

- `qwen2.5:1.5b`
- `qwen2.5:3b`
- `qwen2.5:7b`
- `nomic-embed-text`（PDF 知识库向量模型）

首次使用需要下载模型，模型文件不会放在 GitHub 源码仓库中。

## PDF 知识库

在“产品资料库”中上传 PDF 后，程序会自动完成文字提取、切片和向量化。支持的限制：

- 单个文件最大 100 MB
- 单个文件最多 500 页
- 仅支持包含文字层的 PDF；扫描图片 PDF 需要先进行 OCR

## 项目结构

```text
main_window.pyw     主窗口和用户交互
rag_manager.py      PDF 导入与知识库检索
ollama_service.py   Ollama 服务与模型查询
prompts.py          内容生成提示词
license_core.py     本地授权验证
build_app.spec      PyInstaller 打包配置
installer.iss       Inno Setup 安装配置
```

## 隐私说明

文本生成和 PDF 检索默认在本机完成。上传的 PDF、模型、向量库和授权记录保存在本机 `ProgramData` 目录中。

## 注意事项

- 请不要删除安装目录中的 `ollama.exe`、`lib` 或 `_internal` 文件夹。
- 如果 11434 端口已被其他程序占用，请先关闭该程序再启动助手。
- 当前仓库主要用于源码和构建配置；最终安装包建议通过 Release 或其他文件分发方式提供。
