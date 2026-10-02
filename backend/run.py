"""本地开发入口：在 PyCharm 中右键运行或调试此文件。"""

import os
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    backend_dir = Path(__file__).resolve().parent
    # .env 按工作目录读取，固定到后端目录，避免依赖 PyCharm 的默认设置。
    os.chdir(backend_dir)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        app_dir=str(backend_dir),
        host="0.0.0.0",
        port=8000,
        # 使用单进程，方便 PyCharm 断点调试；修改代码后手动重启。
        reload=False,
    )
