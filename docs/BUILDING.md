# 构建与打包说明（Windows）

本文只讲**如何从源码构建 EXE**。普通用户不需要看这份文档 ——
直接使用发布包里的 `CampusNet.exe` 即可，见 [README](../README.md)。

---

## 1. 环境要求

| 项目 | 要求 | 本次验证所用 |
| --- | --- | --- |
| 操作系统 | Windows 10 / 11（x64） | Windows 11 26100 |
| Python | 3.9+，**必须带 tkinter** | 3.13.9（python.org 官方安装包） |
| 第三方运行依赖 | **无** | — |
| 构建依赖 | PyInstaller 6.x | PyInstaller 6.22.3 |

检查 tkinter 是否可用：

```bat
python -c "import tkinter, sys; print('tk ok', tkinter.TkVersion, sys.version)"
```

如果报 `No module named _tkinter`，说明装的是精简版 Python，请改用
python.org 的官方安装包。

---

## 2. 三条命令完成构建

```bat
python -m pip install -r requirements-dev.txt
build_exe.bat
python tools\test_exe_smoke.py
```

`build_exe.bat` 实际做的是：

1. 检查 Python 版本与 tkinter；
2. 检查 PyInstaller（缺失时打印安装命令，不会留下半成品）；
3. 从 `campusnet/version.py` 生成 `packaging/version_info.txt`（EXE 属性页的版本信息）；
4. 调用 PyInstaller 使用 `packaging/CampusNet.spec` 构建；
5. 检查四个 EXE、跑 `CampusNetCLI.exe version/paths` 自检；
6. 写 `build/build-report.txt` 与 `build/build.log`。

产物：

```
dist\CampusNet\
├─ CampusNet.exe            图形界面主程序
├─ CampusNetSetup.exe       配置向导
├─ CampusNetService.exe     后台守护（无窗口）
├─ CampusNetCLI.exe         命令行诊断（有控制台）
├─ THIRD_PARTY_NOTICES.md   第三方许可证说明
├─ config.example.json      脱敏示例配置
└─ _internal\               Python 运行时 + 标准库 + Tcl/Tk（勿删）
```

体积：约 33 MB（含 `_internal`）。

---

## 3. 为什么用 onedir 而不是 onefile

| 维度 | onedir（本方案） | onefile |
| --- | --- | --- |
| 启动速度 | 快（直接加载） | 每次启动都要解压运行时到 `%TEMP%` |
| 排障 | 缺哪个 DLL/资源一眼看到 | 全打包在一个文件里，报错信息无从定位 |
| 杀毒误报 | 相对少 | 自解压行为更可疑，容易被反复扫描 |
| 临时目录 | 不需要 | 每次运行产生临时目录，异常退出会残留 |
| 调用系统组件 | netsh / DPAPI / PowerShell 均正常 | 同样可行，但路径定位更绕 |

`--onefile` 属于"以后可以评估"的选项，本次不做，理由是启动速度与可诊断性优先。

---

## 4. 四个 EXE 为什么来自同一个入口

`packaging/CampusNet.spec` 里只有一个 `Analysis`（一次依赖分析），
四个 `EXE()` 共用同一份 `PYZ` 与 `COLLECT`。运行期由 `entry.py`
按**自己的文件名**分派角色：

| EXE | 角色 | 控制台 | 说明 |
| --- | --- | --- | --- |
| `CampusNet.exe` | `gui` | 无 | 主界面 |
| `CampusNetSetup.exe` | `setup` | 无 | 主界面的引导模式（直接进设置页） |
| `CampusNetService.exe` | `service` | 无 | 后台守护，`--daemon` 常驻 |
| `CampusNetCLI.exe` | `cli` | 有 | 诊断命令 |

好处：依赖只分析一次（构建快、体积小），四个入口的行为不会各自漂移。
`entry.py` 也支持 `--role`，所以只复制一个 EXE 出去也能用：

```bat
CampusNet.exe --role service --daemon
```

> ⚠ 这一条很重要：**打包后 `sys.executable` 指向正在运行的 EXE**，
> 不再是 `python.exe`。因此任何"用 sys.executable 起一个 .py 脚本"的写法都会失效。
> 本项目统一走 `campusnet/paths.py` 的 `daemon_command()`：
> 打包时指向同目录的 `CampusNetService.exe`，源码模式才是 `pythonw -m campusnet.app.service`。

---

## 5. 路径与用户数据（打包后必须理解的事）

| 目录 | 位置 | 是否可写 |
| --- | --- | --- |
| 安装目录 | EXE 所在目录 | **只读**（程序不写这里） |
| 资源目录 | `_internal`（onefile 时是临时解包目录） | 只读 |
| 用户数据 | `%LOCALAPPDATA%\CampusNet` | 可写 |
| 便携模式 | 安装目录下放 `portable.txt` → 数据写到 `安装目录\userdata` | 可写（失败自动回落） |

用户数据目录内容：

```
%LOCALAPPDATA%\CampusNet\
├─ config.json        配置（密码是 DPAPI 密文，换用户/换机解不开）
├─ config.json.bak    上一次保存前的备份
├─ state.json         上次成功的认证策略
├─ heartbeat.json     后台守护的实时状态（界面读它显示"正在做什么"）
├─ logs\              日志（自动轮转，已脱敏）
├─ backup\            旧配置迁移备份
├─ tmp\browser\       浏览器临时用户目录（退出即删）
└─ diagnostics\       导出的脱敏诊断包
```

旧版（V1/V2）把配置放在程序目录。首次启动时
`campusnet/paths.py:migrate_legacy_data()` 会：

1. 若新位置没有 `config.json` → **复制**过去（旧文件原地保留）；
2. 新位置已有 → 跳过，**绝不覆盖**；
3. 复制 `state.json` 与历史日志（同名不覆盖）；
4. 迁移报告写进日志；配置迁移前的原始文件另存到 `backup\config.v1.*.json`。

---

## 6. 构建后的自检

### 6.1 自动自检（构建脚本内置）

```
CampusNetCLI.exe version     -> 打印 CampusNet 3.0.0
CampusNetCLI.exe paths       -> frozen = True，data_dir 指向 %LOCALAPPDATA%
```

这两个命令在**去掉了 Python 的 PATH** 下执行，因此能证明"不依赖系统 Python"。

### 6.2 完整 EXE 冒烟

```bat
python tools\test_exe_smoke.py
```

检查 25 项，包括：四个 EXE 存在、无源码泄漏、无个人配置、
CLI 五个命令、服务 `--check`、**守护生命周期（启动 → 心跳 → 优雅停止 → stop.flag 清理）**、
GUI 能启动并保持运行、GUI 不往安装目录写数据、无残留进程。
报告写到 `build\exe-smoke-report.txt`。

### 6.3 端到端认证（不需要真实校园网）

```bat
python tools\test_local_e2e.py
```

起一个本机模拟门户（Dr.COM 风格），用**真实 DPAPI 密文**驱动认证，
源码模式与打包 EXE 各跑"成功 / 密码错误 / 网络不可达 / 不支持的门户"四条路径，
共 36 项检查。全程不访问外网，也不会碰真实配置。

### 6.4 系统集成（会改 HKCU Run，结束自动还原）

```bat
python tools\test_system_integration.py
```

真实验证：开机自启动登记/取消、单实例互斥、心跳、优雅停止、
旧版残留清理、卸载（保留数据 / 删除数据），并核对"其它启动项一个没动"。
脚本会先探测本会话是否允许被测进程写注册表；不允许时（沙箱 / AppContainer）
自动切到源码模式验证同一套逻辑并把原因打印出来。

### 6.5 一次跑全部

```bat
python tools\run_tests.py --all
```

单元测试 + 旧版脚本测试 + EXE 冒烟 + 端到端 + 系统集成，共 18 组。

### 6.6 真机验收

自动化只能证明"打包产物能跑、逻辑对"。**能真的登录你的校园网**、以及
**打包 EXE 在普通用户会话下的注册表写入**必须人工验收，
清单见 [WINDOWS_ACCEPTANCE.md](WINDOWS_ACCEPTANCE.md)。

---

## 7. 常见构建问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `缺少构建依赖 PyInstaller` | 没装构建依赖 | `python -m pip install -r requirements-dev.txt` |
| `当前 Python 没有 tkinter` | 精简版 Python / 无 GUI 组件 | 重装 python.org 官方包（勾选 tcl/tk） |
| 构建成功但 GUI 闪退 | 少文件（只复制了 EXE，没带 `_internal`） | 整个 `dist\CampusNet` 目录一起复制 |
| 杀毒软件报毒 | PyInstaller 引导程序的常见误报 | 查防护记录确认是本程序，自行决定是否信任；**不要**关闭杀毒软件 |
| 中文乱码 | 控制台代码页不是 UTF-8 | 用 `CampusNetCLI.exe`（自带控制台）或设 `chcp 65001` |
| 端口/进程残留 | 上次 GUI 未正常退出 | `taskkill /F /IM CampusNet.exe` |

---

## 8. 源码模式（开发者日常）

```bat
python entry.py                 # 图形界面
python entry.py --setup         # 向导模式
python entry.py --role cli status
python entry.py --role service --once --verbose

python tools\run_tests.py       # 全部测试（单元 + 旧版脚本）
python tools\run_tests.py --exe # 额外跑 EXE 冒烟（需先构建）
python tools\run_tests.py --unit
```

旧脚本仍然可用（它们是薄壳）：

```bat
pythonw tools\gui_main.pyw
python tools\setup_wizard.py
python campus_login.py --once --verbose
```

---

## 9. 发布前检查清单

- [ ] `campusnet/version.py` 里的版本号已更新，`docs/CHANGELOG.md` 已补条目
- [ ] `python tools\run_tests.py --exe` 全绿
- [ ] `dist\CampusNet` 里没有 `config.json` / `logs/` / `state.json` / `tests/` / `tools/`
- [ ] `THIRD_PARTY_NOTICES.md` 与 `LICENSE` 已随包
- [ ] 在一台**没装 Python** 的干净 Windows 上按验收清单跑过一遍
