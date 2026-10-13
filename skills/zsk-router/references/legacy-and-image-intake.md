# 旧 Word 与图片接入

客户仍只调用 `zsk-router`。本页描述已实现的文件接入分支；能发现工具不等于该客户文件转换成功。

## 旧 `.doc`

1. 验证 OLE2 容器、WordDocument 流和 Word 文件头；加密、损坏、Excel/PPT 冒名文件或只改扩展名都不放行。
2. 优先复用已经安装的 LibreOffice（独立临时 profile）；没有该后端时，Windows 可用已注册的 Microsoft Word COM。Word 正在使用时停止自动转换，避免操作客户已打开文档。WPS 与 macOS Word 当前仅作为客户手动“另存为 DOCX”的途径，不假称存在自动化接口。
3. 中间 DOCX 必须具备有效 `word/document.xml`，再经 MarkItDown 提取非空文字。转换后保留提交原件的 SHA256/source_id；可读版记录原始格式、中间格式、中间 SHA256 和转换后端。临时中间文件清理，不保存成第二个来源。
4. 使用当前 06 保存偏好、来源登记、隐私与分类 Gate；默认只提取文字，旧 DOC 的图像内容不自动宣称已完整理解。

`legacy_conversion_unavailable`：请用 Word/WPS 另存为 DOCX，不能只改文件名。`conversion_failed`：提供未加密、未损坏的原文件。转换工具运行失败时不静默换后端。

## JPG/JPEG/PNG

只支持文字型单张资料。Pillow 验证真实图像格式，按 EXIF 方向生成完整原生分辨率 PNG；不裁切、不假称达到 300 DPI。提交的原始 JPG/PNG 与来源身份不变，标准化 PNG 只是派生的单页证据。

原件保存必须符合 06 的项目级授权，未授权时不提取或保存。同一图像运行三次本地 Tesseract（PSM 3/6/11，chi_sim+eng），至少两次文字高度一致、数字序列一致且达到既有质量门槛才继续。高置信度单次、相似文字但参数数字不同均不放行；无法稳定提取返回 `file_quality_insufficient`，该文件不写 01—05，提示提供高清图或可编辑原件。两次一致是质量门槛，不等于证明字字无误；用户核对参数时可回原图。

OCR 文字重新经过隐私检查和脱敏。Obsidian 可读版嵌入真实相对页图；飞书沿用来源 Docx 中的正文＋页图写入和回读，核对图片尺寸、数量及远端 SHA256。不把文字识别扩大成照片描述、证书真伪鉴定或图片检索。

## 准备和核验

在 ZSK 仓库或完整安装包内运行：

```bash
python3 install.py --dependencies-only --formats doc,jpg,png
python3 install.py --doctor --formats doc,jpg,png
```

其他宿主的 doctor 必须附带实际 `--host` 与 `--dest`。`--dependencies-only` 只在当前 ZSK Python 内补缺失的 `olefile` / `Pillow`，并在现有转换器所属环境补 DOCX 依赖；已可用则复用，不降级当前库。不会自动下载 Office、Tesseract 或语言包。缺少它们时准确提示所缺工具；不把本机工具检查说成已安装。

doctor 使用包内纯合成 DOC/JPG/PNG 实测，不读取客户文件。小样通过后仍需验证实际客户文件。Windows Word COM 有命令合同测试，真实 Word 环境和客户豆包宿主需要分别验收。

实现依据：[LibreOffice 命令行参数](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)、[Microsoft Word AutomationSecurity](https://learn.microsoft.com/en-us/office/vba/api/word.application.automationsecurity)、[Tesseract 安装](https://tesseract-ocr.github.io/tessdoc/Installation.html)。
