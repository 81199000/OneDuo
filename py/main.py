"""一键签 入口（默认网页界面，图标清晰；ONETAP_TK=1 用旧的 tkinter 界面）"""
import os

if os.environ.get("ONETAP_TK"):
    from app.ui import main
else:
    from app.webui import main

if __name__ == "__main__":
    main()
