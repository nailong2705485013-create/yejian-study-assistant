# 第三方说明

本仓库仅分发项目代码、界面资源和合成测试资料，不包含模型权重、Office、真实课件或第三方库源码。安装依赖时请遵守各组件的原始许可证，以下列表只作索引，具体以安装版本的发行包为准。

| 组件 | 用途 | 许可证 / 项目 |
| --- | --- | --- |
| LangGraph | 工作流与并行状态合并 | MIT · <https://github.com/langchain-ai/langgraph> |
| OpenAI Python SDK | 兼容 Chat Completions 的客户端 | Apache-2.0 · <https://github.com/openai/openai-python> |
| Pydantic | 严格结构校验 | MIT · <https://github.com/pydantic/pydantic> |
| pypdf | PDF 文字解析 | BSD-3-Clause · <https://github.com/py-pdf/pypdf> |
| python-docx | Word 导出 | MIT · <https://github.com/python-openxml/python-docx> |
| python-pptx | PPTX 本地解析与合成测试资料 | MIT · <https://github.com/scanny/python-pptx> |
| Pillow | 图片处理与视觉探测图 | MIT-CMU · <https://github.com/python-pillow/Pillow> |
| httpx | HTTP 传输 | BSD-3-Clause · <https://github.com/encode/httpx> |
| pywin32 | Windows COM 与 DPAPI | PSF · <https://github.com/mhammond/pywin32> |

可选浏览器测试使用 Playwright（Apache-2.0）及本机 Microsoft Edge；它们不包含在本仓库。Microsoft PowerPoint 的安装与授权由使用者自行提供。模型 API 的计费、条款与资料处理方式由所选服务决定。

`tests/fixtures/diagram.pptx` 是代码生成的测试课件，`static/sample.txt` 是示例学习资料，截图使用无真实资料、无 Key 的本地界面。
