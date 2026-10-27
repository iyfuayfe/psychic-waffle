"""程序入口层：每个 EXE 对应这里的一个模块。

======================  ==========================  ============================
EXE                     模块                        说明
======================  ==========================  ============================
``CampusNet.exe``        :mod:`.gui`                 图形界面主程序（windowed）
``CampusNetSetup.exe``   :mod:`.setup`               配置向导（同一个界面的引导模式）
``CampusNetService.exe`` :mod:`.service`             后台守护（windowed，无窗口）
``CampusNetCLI.exe``     :mod:`.cli`                 命令行诊断（console）
======================  ==========================  ============================

统一的约定：

* 所有入口第一件事都是 ``paths.migrate_legacy_data()`` + ``paths.ensure_dirs()``，
  保证升级后旧配置能带过来、目录一定存在；
* 不依赖 ``sys.executable`` 指向 python.exe —— 打包后它就是本程序自己，
  需要启动子进程时一律走 :func:`campusnet.paths.daemon_command`；
* 崩溃时不弹任何系统对话框，异常写进用户数据目录的日志。

注意：这个 ``__init__`` **刻意不导入子模块**。曾经在这里写了
``from . import cli, gui, service, setup``，结果：
``python -m campusnet.app.cli`` 会触发 runpy 警告，而且**命令行工具也顺带把
tkinter 加载进来**（多花几百毫秒，在没有图形组件的环境里还可能直接报错）。
需要哪个入口就 import 哪个。
"""

__all__ = ["cli", "gui", "service", "setup", "bootstrap"]
