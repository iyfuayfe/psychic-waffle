"""Tkinter 界面（V3.0 统一到这一套）。

* :mod:`campusnet.ui.main_window` —— 主窗口（概览 / 设置 / 认证）
* :mod:`campusnet.ui.gui_service` —— 界面背后的动作层（所有网络操作都在这里）
* :mod:`campusnet.ui.ui_kit`      —— 自绘控件与配色
* :mod:`campusnet.ui.page_*`      —— 三个页面

为什么集中成一个包：V2 同时存在 ``gui_main.pyw`` / ``gui_setup.pyw`` /
``gui_status.pyw`` 三套窗口，各有一份设置界面和杀毒白名单按钮，改动容易走偏；
打包成 EXE 时三套窗口也都要单独处理路径与依赖。V3.0 只维护一套界面，
``CampusNet.exe``（主程序）与 ``CampusNetSetup.exe``（引导模式）都是它的入口。
"""

from .main_window import App, main

__all__ = ["App", "main"]
