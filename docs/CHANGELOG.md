# 变更记录

本文件记录面向用户的变更。版本号唯一来源是 `campusnet/version.py`。

## 3.0.0

### 新增：正式 EXE 发布（核心目标）

- 新增四个 EXE（单入口 `entry.py`，按文件名分派角色）：
  - `CampusNet.exe` —— 图形界面主程序
  - `CampusNetSetup.exe` —— 配置向导（同一界面的引导模式）
  - `CampusNetService.exe` —— 后台守护（无窗口）
  - `CampusNetCLI.exe` —— 命令行诊断（status / once / autostart / doctor / uninstall / paths）
- 新增 `packaging/CampusNet.spec`（PyInstaller onedir）、`tools/build_exe.py`、
  `build_exe.bat`、`tools/test_exe_smoke.py`。
- **普通用户不再需要安装 Python**：`0~4-*.bat` 全部优先使用 EXE，找不到 EXE 才回落到源码模式。
- EXE 带 Windows 版本信息资源（属性页可见产品名与版本）。

### 新增：用户数据与安装目录分离

- 配置、日志、状态、备份、浏览器临时目录统一放到 `%LOCALAPPDATA%\CampusNet`；
  安装目录保持只读。可用 `CAMPUSNET_DATA_DIR` 覆盖；放 `portable.txt` 进入便携模式。
- 首次启动自动迁移旧版数据（**复制**，不删旧文件，不覆盖新配置），并留下迁移备份。

### 安全（P0）

- **删除自动加杀毒白名单的整套机制**：`tools/whitelist.ps1`、`4-加入白名单.bat`
  被移除；界面、安装向导、日志、README 中所有"加白名单 / 关杀软"的引导全部删除。
  新增只读的「安全体检」与脱敏诊断包导出（`4-安全与诊断.bat`，全程无需管理员）。
- 日志全面脱敏：账号（保留前后几位）、密码、令牌、长密文、手机号一律打码；
  自定义 `RedactingFormatter` 连 traceback 一起覆盖。
- 认证页转储默认**关闭**；开启后只写脱敏副本，并自动限制数量。
- 清理源码与测试中的真实账号、学校门户地址、写死的个人目录。
- TLS 证书**默认校验**（旧版默认关闭）；改用 `tls_insecure_hosts` 逐主机放行，
  全局 `insecure_tls` 保留但需显式开启并提示风险。
- 新增凭据投递白名单 `trusted_auth_hosts`：表单/自定义接口的目标主机不在白名单里就
  **拒绝提交**并说明原因，`refused-target` 会明确告诉用户怎么办。
- 307/308 重定向仅在严格同源时重发请求体，跨端点一律停止，避免密码被带走。
- 浏览器认证改为**两阶段**：先执行不含任何凭据的探针（校验协议、origin、错误页），
  通过后才注入带凭据的脚本；跳转到别的域名、证书错误页、非 http(s) 页面都不再注入。
- 浏览器调试端口只绑 `127.0.0.1`，并核对监听该端口的进程确实是本次启动的子进程；
  关闭时按进程树清理，临时用户目录（含 cookie）一并删除。
- 卸载不再触碰任何安全软件设置。

### 可靠性（P1）

- 联网探测状态细分：`online / redirect / hijack / dns-error / timeout / refused /
  tls-error / proxy-error / unreachable / http-error / no-targets`，
  并给出"发生了什么 + 建议怎么做"的说明；不再把 DNS 失败报成"认证失败"。
- 认证结果结构化（`AuthOutcome`）：区分成功 / 凭据错误 / 需要验证码 / 需要人工 /
  网络错误 / 服务端异常 / 不支持 / 拒绝提交，并带 `needs_user`、`retryable`。
- 后台守护改为显式状态机 + 心跳文件（`heartbeat.json`），界面能显示"正在检测 /
  正在认证 / 需要你处理"；检测系统休眠（wall clock 与 monotonic 跳变）后立即重测。
- 账号密码错误时不再高频重试（防撞锁），只在第一次推送通知。
- 配置系统加入 `config_version`、字段校验与数值范围、未知字段保留、原子保存 + `.bak`、
  损坏 JSON 明确报错且不覆盖原文件。
- 开机自启动简化为**唯一**机制：`HKCU\...\Run` 一个值（用户级，无需管理员）。
  旧的计划任务与启动文件夹 VBS 会被一键清理（只删本程序创建的精确名字）。
- 优雅停止：写 `stop.flag`，守护循环自行退出，不再 `taskkill` 打断进行中的请求。

### 界面（P2）

- 三套独立界面合并为一套（`campusnet/ui/`）：`gui_main.pyw` / `gui_setup.pyw` /
  `gui_status.pyw` 保留文件名但变成薄壳，不再维护第二份实现。
- 首次启动（尚未配置）自动进入设置页并给出引导文案。
- 设置页新增「启动与安全」区块：开机自启动开关、数据目录说明、
  「打开数据目录」「安全体检（只读）」「导出脱敏诊断包」「清理旧版自启动」。
- 状态栏与顶栏显示后台守护的实时状态（来自心跳文件）。

### 测试

- 新增 `tests/`（unittest，无需第三方库）**169 项**：安全脱敏与 URL 策略、配置校验与迁移、
  路径与打包模拟、联网状态分类、HTTP 重定向与 TLS 策略、认证结果分类、
  守护退避与状态机、自启动逻辑（假注册表）、卸载逻辑（含开着的日志句柄回归）、
  注入脚本来源守卫、发布卫生。
- 新增三个真实集成测试脚本：
  - `tools/test_local_e2e.py` —— 本机模拟门户端到端认证（源码模式 + 打包 EXE，36 项）；
  - `tools/test_system_integration.py` —— 注册表自启动 / 单实例 / 优雅停止 / 卸载（27 项）；
  - `tools/test_exe_smoke.py` —— 打包产物冒烟（25 项，含守护生命周期与 GUI 存活）。
- `tools/run_tests.py` 增加 `--integration` 与 `--all` 档位；`tools/test_web_autofill.py`
  在获得完整权限后**真实跑通**（真实 Edge + CDP）。
- 修复 `tools/test_aes.py` 在 GBK 控制台因 emoji 抛 `UnicodeEncodeError` 的假失败。

### 测试期间发现并修复的缺陷

| 缺陷 | 影响 | 修复 |
| --- | --- | --- |
| 守护进程在睡眠中不检查 `stop.flag` | 卸载/停止最长要等 `interval_max`（默认 120s），期间日志被占用导致数据删不掉 | `Daemon._sleep()` 每秒检查停止标记，1 秒内响应 |
| `daemon_running()` 泄漏互斥体句柄 | 反复探测后真正的守护反而抢不到锁、起不来 | 新增 `crypto.release_single_instance()`，探测用完立即关闭 |
| `uninstall --remove-data` 永远删不掉 `logs/`（`WinError 32`） | "卸载说删了数据但日志还在"，且整次卸载被判失败 | 删除前关闭自身日志句柄 + 重试 + 只读属性修复 |
| 旧配置迁移备份无限增殖 | 每次启动都新增一份 `backup/config.v1.*.json` | 按内容哈希去重，最多保留 5 份 |
| `campusnet/app/__init__.py` 急切导入子模块 | 命令行工具连带加载 tkinter，`-m` 执行出现 runpy 警告 | `__init__` 不再导入子模块 |

### 文档与合规

- 重写 `README.md`（区分普通用户 / 开发者 / 已知限制）。
- 新增 `docs/BUILDING.md`、`docs/CHANGELOG.md`、`docs/KNOWN_ISSUES.md`、
  `docs/AUDIT_V3.md`、`docs/WINDOWS_ACCEPTANCE.md`。
- 新增 `LICENSE`（MIT）、`requirements.txt`（空，运行期零依赖）、`requirements-dev.txt`、
  `.gitignore`；完善 `THIRD_PARTY_NOTICES.md`。

### 不兼容变更（升级须知）

1. 配置与日志位置变了（`%LOCALAPPDATA%\CampusNet`）。旧配置会自动**复制**过来，
   旧文件仍在原处，可随时回退。
2. TLS 默认开始校验证书。若学校门户是自签证书，需要在设置里把该地址加入
   `tls_insecure_hosts`，否则认证会失败（错误提示会指路）。
3. 自启动机制从"计划任务 + 启动文件夹"改为 `HKCU Run`；
   升级后第一次打开界面请确认「开机自动启动」开关状态。
4. `4-加入白名单.bat` 已删除，替换为 `4-安全与诊断.bat`。

## 3.0.1

### 修复：界面打开几秒后进程崩溃（第三方叠加软件注入）

**现象**：双击 `CampusNet.exe`，窗口出现几秒后整个进程消失，没有 Python 报错、
没有日志、事件查看器里只有一条应用崩溃记录。

**定位过程**：

1. Windows 事件日志显示崩溃模块是 `GPP64.dll`（`0xc0000005`，偏移 `0x9e956`）；
2. `GPP64.dll` 位于 `%LOCALAPPDATA%\GamePPSDK\` —— 属于**游戏加加（GamePP）**的注入 SDK；
3. 用一份只有 20 行的最小 tkinter 程序做对照：**同样在第 3 秒崩溃，同样报 GPP64.dll**；
4. A/B 验证：退出游戏加加后，本程序 GUI 连续运行 25 秒以上无异常。

**结论**：这是第三方注入 DLL 与 tkinter/GDI 的冲突，任何 Tk 桌面程序在该环境下都会闪退，
**与本项目代码无关**；后台守护不创建窗口，全程不受影响。

**本版新增的应对能力**（让问题自己说清楚，而不是让用户看到"窗口没了"）：

- 新增 `campusnet/conflicts.py`：用 Toolhelp32 枚举本进程已加载模块，识别已知的
  叠加类注入（游戏加加 / RTSS / Discord / Nahimic / FPS Monitor），给出**具体解决办法**；
  同时检查这些软件的主程序是否在运行（它们常常"看到窗口才注入"）。
- 新增 `campusnet/session.py`：GUI 启动写会话标记、正常关闭删除。若进程被第三方
  DLL 打崩，**下次启动会明确提示"上次界面在 X 时异常退出，当时检测到 GPP64.dll"**。
- GUI 启动后每 2 秒巡检一次注入情况（窗口出现后才会注入），发现即写日志、
  写会话标记、在状态栏给出解决办法。
- `CampusNetCLI.exe conflicts`：一条命令查看"有没有叠加软件在注入本程序"。
- `CampusNetCLI.exe doctor`：诊断包新增「第三方注入模块 / 上次运行」章节。
- `tools/test_exe_smoke.py`：在装有已知叠加软件的机器上，GUI 存活检查判为
  **SKIP（环境不适用）**而不是 FAIL，避免把环境问题当成产品缺陷。
- 文档：README 常见故障、`docs/KNOWN_ISSUES.md` K-00 给出明确处置步骤。

## 3.0.2

### 修复：点「保存并开启自动连接」后界面变成"未响应"

**现象**：填完 Wi-Fi 名称、账号、密码后点「保存并开启自动连接」，窗口卡住、标题栏出现
"未响应"，要等很久才恢复。

**原因**：保存这一下要连续做三件**可能很慢且不受程序控制**的事，而它们原来全在
UI 线程里同步执行：

1. `CryptProtectData`（DPAPI 加密密码）—— 域账户 / 漫游主密钥 / 安全软件挂钩 DPAPI 时
   可能往返网络或等锁；
2. 写 `%LOCALAPPDATA%\CampusNet\config.json` —— 实时防护会先扫一遍；
3. 回读校验（再解密一次）。

tkinter 只在主线程处理消息，上述任一步变慢，窗口就会失去响应。另外同一区域还有一个
**长达 6 秒的卡死点**：开机自启动开关在 UI 线程里调用 `start_daemon()`，
它会轮询等待后台进程就绪（最多 6 秒）。

**修复**：

- 保存流程整体异步化：UI 线程只读控件 + 做纯文本校验（<20ms 返回），
  加密、写盘、回读校验全部交给后台线程；期间两个保存按钮置灰、状态栏显示
  "正在保存…"，完成后自动恢复。新增 `gui_service.save_settings()`，
  并记录每一步耗时（`load_ms / encrypt_ms / write_ms / verify_ms / total_ms`）到日志。
- 界面上的"是否已保存密码"判断改为**不解密**的快速判断（`has_saved_password`），
  DPAPI 解密只在后台线程做。
- 开机自启动开关同样改为后台执行（打开时不再卡 6 秒），开关期间置灰防连点。
- `ui_kit.Toggle` 增加 `set_enabled()` 与禁用态外观。
- 新增 `tests/test_ui_responsiveness.py`（8 项）：用主线程心跳法测量"点按钮后
  最大卡顿毫秒数"，并把 DPAPI 加密人为拖慢到 3 秒、守护启动拖慢到 3 秒，
  断言主线程卡顿仍 < 300ms —— 这才是"未响应"的真正回归测试。

## 3.0.3

### 修复：无法探测附近无线网络（Windows 位置权限 + 本地化解析）

**原因（已实测定位）**：Windows 把 WLAN 信息（附近 SSID 列表、当前连接的 SSID）
归为**位置相关数据**。在位置权限不允许的进程里，`netsh wlan show interfaces`
直接返回退出码 1 并打印：

> 此 shell 命令需要位置权限才能访问 WLAN 信息。在「隐私和安全性」中的「位置」页面中启用位置服务。

我们原来只靠解析 netsh 文本，于是"一个字段都拿不到"，界面上就成了"探测不到附近网络"。
另外 netsh 输出是**本地化文本**，中文系统用「说明」而不是「描述」，导致网卡型号显示成 `-`。

**修复**：

- 新增 `campusnet/wlanapi.py`：用 ctypes 直接调用 Windows 原生 WLAN API
  （`WlanEnumInterfaces` / `WlanQueryInterface` / `WlanGetAvailableNetworkList`）。
  结构化数据、无本地化文本、也没有 netsh 的命令行权限问题；纯标准库，零新依赖。
- `wifi.interface_status()` 与 `wifi.scan()` 改为**先原生 API、失败再回落 netsh**；
  netsh 仍保留给"连接/断开"（`WlanConnect` 需要 profile XML，不值得重写）。
- 识别 netsh 的"位置权限"提示并翻译成可照做的说明（含设置路径），
  界面上不再显示空列表，而是明确写出原因 + "可以直接手动填写 SSID，不影响使用"。
- 修正中文 netsh 键名映射（新增「说明」「說明」「狀態」「訊號」「加密方式」等）。
- 新增 `CampusNetCLI.exe wifi`：一条命令看网卡状态、扫描结果，失败时打印
  netsh 退出码与原文，便于远程定位。
- 新增 `tests/test_wifi_detection.py`（14 项）。

### 修复：开机自启后"无法进行后台守护"

**原因**：后台守护进程启动后**立即退出**（退出码 3），因为配置里的密码密文
在本机解不开（换过 Windows 用户 / 电脑的典型现象）。而界面原来只会说
"后台守护启动后没有按时就绪"，完全看不出真正原因。

**修复**：

- `autostart.start_daemon()` 现在会在守护进程提前退出时**读它自己的日志**，
  把原因翻译成可操作提示，例如：
  "后台守护启动失败：配置里的密码在本机解不开（换了 Windows 用户或电脑后会出现）。
   请在「设置」里重新输入一次上网密码并保存。"
- 新增 `gui_service.password_status()`：打开「设置」页时**在后台**复查
  "已保存的密码在本机能不能解开"，解不开就在密码框下方标红提示
  （以前只要密文非空就显示"已保存密码"，误导性很强）。

### 修复：保存后没有成功提示

与 3.0.2 的保存流程相关：旧版本保存时主线程卡住，用户关掉/进程崩溃导致
磁盘上的配置从未更新（实测其 `config.json` 时间戳仍停留在很久以前、
`config_version` 为空），于是既看不到成功提示，后台也起不来。

现在保存是异步的，完成/失败都会在状态栏明确显示，并且会核对
"回读得到的密码能否解密"，三步（保存成功 / 密码不可用 / 失败原因）都有确定文案。

## 3.0.4

### 新增：进程加固，尝试让界面在游戏加加运行时也不闪退

3.0.1 只是"检测并告知"游戏加加注入导致闪退，没有真正解决。本版尝试从机制上挡住注入。

**先确认了一个关键前提**：游戏加加安装目录里**没有任何 .sys 驱动**，它是纯用户态
Electron 程序（chrome_*.pak / v8_context_snapshot / ffmpeg.dll）——这意味着它的
DLL 注入**可以在用户态被系统拒绝**。

**做法**（`campusnet/hardening.py`，两个阶段）：

1. **早期加固**（`bootstrap` 里、GUI 出现之前）：`SetProcessMitigationPolicy`
   的 `ProcessExtensionPointDisablePolicy`，禁用 AppInit_DLLs / 旧式
   `SetWindowsHookEx` 注入路径。无副作用，所以放得很早。
2. **窗口显示前的签名锁**：tkinter 的 DLL 全部加载完 → 窗口 `withdraw()` →
   施加 `ProcessSignaturePolicy = MicrosoftSignedOnly`（此后只允许加载微软签名
   DLL，游戏加加的 `GPP64.dll` 不是微软签名，注入会被系统拒绝）→ 再 `deiconify()`。
   **顺序不能反**：实测"先加策略再 import tkinter"会让界面自己起不来
   （`DLL load failed while importing _tkinter: 拒绝访问`）。

**必须解决的两个副作用**（都已处理）：

- 签名策略**不可撤销**，所以施加前会**预热**本程序需要的全部模块（含 C 扩展与
  GBK/Big5/idna 编解码器）。不预热会踩坑：实测加固后 HTTPS 报
  `unknown encoding: idna`、GBK 解码失败 —— 因为 PyInstaller 打包的 `.pyd`
  由 Python 软件基金会签名，不是微软签名。
- 施加后会立刻跑**功能自检**（https / idna / gbk / hashlib / json / zlib / subprocess），
  任何一项失败都会写进日志并在报告里说明，可用 `CAMPUSNET_NO_HARDENING=1` 关闭。

**默认策略（避免影响正常用户）**：签名锁**只在检测到已知叠加软件正在运行时**才启用
（`CAMPUSNET_HARDEN=1` 可强制开启，`CAMPUSNET_NO_HARDENING=1` 可完全关闭）。
扩展点加固则总是尝试启用。

**实测结论（诚实记录）**：

- 签名策略**确实能拒绝未微软签名的 DLL**：加固后加载 GamePP 的 `ffmpeg.dll`
  返回 `WinError 577`（无法验证数字签名）—— 机制有效。
- 加固后本程序功能自检 7/7 通过，真实 HTTPS 请求 200、GBK 编解码、WLAN API、
  DPAPI、子进程均正常。
- **但无法验证它是否解决了你的闪退**：做 A/B 对照（加固开/关各 6 次）时，
  游戏加加**并未**再向我方进程注入（两组都是"未注入"，各存活 6/6）——
  这个注入是**条件触发**的，当前状态下复现不出来。

### 其它

- `CampusNetCLI.exe conflicts` 现在会同时报告"叠加进程是否在运行"与"是否已被注入"。
- 新增 `tests/test_hardening.py`（15 项，含"未签名 DLL 必须被拒绝"的实机断言）。

## 3.0.5

### 修复：加固生效后弹出「损坏的映像 / 0xc0000428」错误框

**这张图其实是"加固成功"的证据，但也说明体验没做完。**

截图里的对话框是：

```
校园网自动登录 · 控制台: CampusNet.exe - 损坏的映像
C:\Users\...\AppData\Local\GamePPSDK\1.2.1615.625\GPP64.dll 没有被指定在 Windows 上运行，
或者它包含错误。……错误状态 0xc0000428。
```

`0xc0000428` = `STATUS_INVALID_IMAGE_HASH`，含义是"这个 DLL 的映像哈希/签名无法验证"。
3.0.4 启用的签名策略（只允许微软签名 DLL）正是让它无法验证 —— 也就是说
**游戏加加的注入被系统拒绝了**（以前这一步是直接把整个进程打崩）。

问题在于：加载器在拒绝之后，会在**我们的窗口上**弹一个模态错误框，看起来像程序出错，
而且框存在期间界面是卡住的。

**修复**：进程启动最早期调用 `SetErrorMode(SEM_FAILCRITICALERRORS |
SEM_NOOPENFILEERRORBOX)`，让加载失败**静默返回**而不是弹框给用户。注入照样被拒，
但现在不会有任何打扰。刻意没有加 `SEM_NOGPFAULTERRORBOX`：真正的崩溃仍希望留证据。

**实测验证**（`CreateRemoteThread(LoadLibraryW)` 注入游戏加加的 `GPP64.dll`）：

```
对照：允许弹窗   存活=True  GPP 已加载=False  LoadLibraryW 返回 0x0（加载被拒绝）
本版：抑制弹窗   存活=True  GPP 已加载=False  LoadLibraryW 返回 0x0（加载被拒绝）
```

即：**注入确实被挡住、DLL 没有进入我们的进程**，进程存活；本版不再弹框。

### 其它

- 加固报告与日志新增"已抑制加载失败弹窗"。
- `tests/test_hardening.py` 增至 21 项（新增 ErrorMode 标志位与弹窗抑制断言）。
- 说明：本地用 `CreateRemoteThread` 复现时**没有**出现该弹窗（弹窗只在
  游戏加加那种注入路径下出现），因此抑制效果属于"防御性修复 + 机制层面正确"，
  无法在本机端到端复现那个弹窗。

## 3.1.0

> 本版按《CampusNet 3.0.5 全面审查与稳定性升级任务书》执行：先备份基线、跑出修改前测试结果，
> 再按"根因优先、最小改动、不得伪造测试"的原则修复。

### 一、默认关闭进程加固（崩溃/兼容性问题的根因）

**结论：3.0.4/3.0.5 默认施加的 `ProcessSignaturePolicy=MicrosoftSignedOnly` 是兼容性问题的根因，已改为默认关闭。**

证据（全部为实测）：

1. 打包产物里 `python313.dll`、`_tkinter.pyd`、`_ssl.pyd`、`unicodedata.pyd` 的 Authenticode
   状态均为 **Valid**，签名者是 `CN=Python Software Foundation` —— 合法签名，但不是微软签名，
   而该策略只认微软签名。
2. 策略生效后逐个 import 未预热的扩展模块，实测失败：
   - `sqlite3` → `DLL load failed while importing _sqlite3: Windows 无法验证此文件的数字签名`
   - `asyncio` → `_overlapped` 同样失败；`winsound` 同样失败
   这意味着任何一条之前没走到的代码路径，第一次用到就会以"签名"错误失败 —— 即
   "改完之后部分功能损坏"的机制来源。
3. 该策略还会让加载器在我们的窗口上弹出「损坏的映像 0xc0000428」模态框
   （本机 System 日志 2026-10-19 21:01–21:02 的 `Application Popup` 记录，失败模块为
   `GamePPSDK\1.2.1615.625\GPP64.dll`，`0xc0000428`）。
4. 收益未被证实：加固开/关各 6 次 A/B 对照时第三方软件并未注入（两组都"未注入"），
   无法证明它能阻止真实崩溃。

改动：

- `campusnet/hardening.py` 重写为**默认 `off`**：默认不调用任何
  `SetProcessMitigationPolicy`，也不改 `SetErrorMode`（不再掩盖真实依赖缺失）。
- 加固改为**显式可选**：`CAMPUSNET_HARDEN=1`（或 `=extpoint`）才施加，含预热、自检、弹窗抑制与明确日志。
- 删除"检测到第三方进程名就自动上锁"的逻辑（任务书明确禁止按第三方进程名硬编码规避）。
  只读检测（注入模块/叠加进程）保留，仅用于诊断与提示。
- GUI 入口只在用户显式开启时才 `withdraw()→加固→deiconify()`；默认路径不隐藏窗口。

### 二、后台守护：真实健康判定 + 两个真实缺陷

- 新增 `daemon.daemon_health()`：进程锁 + 心跳新鲜度（180s 容差）+ 状态三重判定，
  区分「未运行 / 运行中正常 / 运行中但无响应」，给出可读依据。GUI 顶栏、
  概览面板、`CampusNetCLI.exe daemon` 全部改用它（以前"进程还在"就算正常）。
- **修复：遗留 `stop.flag` 导致守护永远起不来**。用户点过「停止」后进程被强杀，
  标记文件会留下；下次守护启动看到它就"优雅退出"（退出码 0、无心跳）。
  现在 `start_daemon()` 启动前清理遗留标记，`stop_daemon()` 超时也会清掉。
- **修复：运行锁是全局命名对象**。旧版互斥体名不含数据目录，导致
  "属于另一个数据目录/旧版本的实例"能永久占锁，当前配置的守护永远起不来，
  界面还一直显示"运行中但无响应"。现在互斥体名带数据目录指纹
  （`version.mutex_name(paths.data_dir_key())`），便携版与安装版、不同数据目录互不干扰。
- `start_daemon()` 成功标准从"抢到锁"改为"抢到锁且写出心跳"；失败时读取守护日志
  翻译成可操作原因（密码解不开 / 配置不完整 / 退出码 3/4 …）。

### 三、配置完整性：失败不再被静默吞掉

- **修复：配置读取失败会被默认值覆盖**。原来 `load_config()` 读失败就返回默认配置，
  保存路径同样降级后照常写盘并报"已保存"——真实的数据丢失路径。
  现在：读失败会记住原因（`last_config_error()`），保存前先解析，**解析失败直接拒绝写入**
  并说明"为避免覆盖你的原始配置，本次没有写入"；启动时界面显示「配置读取失败」。
- 所有配置"读-改-写"统一加模块级锁（`_config_lock`），避免"设为目标 SSID"、
  "记住门户地址"、"保存设置"并发互相覆盖。

### 四、UI 交互：从"简陋"到可解释

- 新增 `campusnet/status.py`：**统一状态机**，覆盖任务书点名的 11 种状态
  （未配置 / 读取配置 / 检测网络 / 执行认证 / 确认联网 / 连接成功 / 连接失败 /
  网络不可用 / 认证服务器无响应 / 配置读取失败 / 内部错误），每种都带
  可执行建议；界面只渲染，不再自己拼状态文字。
  - 无线接口存在 ≠ 已联网；HTTP 成功 ≠ 认证成功；原因未知时明确写"无法确定具体原因"。
- 新增「**测试连接**」按钮（设置页）：用已保存凭据执行"探测→认证→复核"，不修改配置，
  失败按 `status` 分流（密码错 / 网络故障 / 认证服务器无响应 / 需人工）。
- 保存成功文案改为任务书要求的分层表述：只保存时显示
  "配置保存成功。账号信息已保存，可以进行连接测试。"；
  「保存并开启自动连接」现在会**真的拉起后台守护**，并按实际结果报告
  （成功才说"自动连接已开启（后台守护运行中）"，失败降级为 WARN 并给出办法）。
- 密码框新增**显示/隐藏**切换；掩码默认保持。
- 概览新增状态面板：账号配置状态（已配置 / 未配置 / 待重设密码）、
  后台守护真实状态、最近一次连接结果与时间、当前状态摘要；新增「查看日志」入口。
- 防重复提交：`立即连接`（此前会被 15s 轮询回调重新点亮，导致可并发两次认证）、
  `检查上网状态` 均加 busy 标志与按钮忙态；失败回调统一恢复按钮
  （此前"导出诊断包/清理旧版"异常后按钮永久灰）。
- 修复若干界面与业务不一致：
  - 「网络不可用（None）」——读错键名，改由状态机渲染；
  - hero 卡在"正在检测…"（检测失败时不再更新 hero）——已修；
  - 切换页签重复进入会**泄漏定时器**（每次多一条 15s/45s 轮询链）——`_schedule` 先取消旧定时器；
  - 认证页「停止」在 UI 线程关闭浏览器会话（最长 ~3.2s 阻塞、且与后台线程竞争）——改到后台线程；
  - 门户地址写配置失败被静默——现在明确提示。

### 五、测试与验证

- 新增 `tests/test_status.py`（23 项，状态机全部映射与"不编造原因"约束）。
- `tests/test_hardening.py` 重写为 3.1.0 语义（27 项），含硬断言：
  默认模式**不改 ErrorMode**、默认模式**sqlite3/asyncio/winsound 仍可用**、
  可选加固**确实拒绝未签名 DLL**、GUI 入口必须"先判断模式再加固"。
- 全量回归：见交付报告；修改前后测试结果均已留档（`.tmp/baseline/tests-before.txt`
  与 `.tmp/tests-after-3.1.0.txt`）。

### 六、发布流程与文档补齐

- `tools/make_release_zip.py` 现在会同时生成 `dist/SHA256SUMS-<版本>.txt`：
  对外发布时把 zip 与校验清单一起给出，用户可先核对哈希（传输损坏/被替换可被发现）。
  校验清单不能替代数字签名，但它是零成本的第一步。
- 新增 `docs/SECURITY_WARNINGS.md`：Windows 安全提示的**取证结果**（谁签名、有没有 MOTW、
  SmartScreen/Defender 记录、双用途工具自查）与**正规发布方案**
  （可重复构建 + 哈希 → OV/EV 代码签名 → 误报申诉 → 分发方式），
  并明确列出"本机能做到什么/还缺什么"，不做任何降低系统安全性的操作。
- 新增 `docs/REVIEW_3.1.0.md`：本次审查与升级的交付报告
  （问题根因 A / 改动清单 B / 测试结果表 C / 发布文件 D / 待用户补充信息 E）。
- README 增补：`CampusNetCLI.exe daemon` 用法、「开了自动连接但后台没工作」排查、
  进程加固为何默认关闭及如何显式开启、两份新文档入口。
- KNOWN_ISSUES 更新 K-00（加固默认关闭）与 K-03（守护起不来的两个真实缺陷）。

### 七、测试隔离修复（避免"单独跑全绿、连起来跑红"）

- 现象：`python tools\run_tests.py --all` 时，EXE 冒烟与系统集成会因**上一个套件遗留的后台守护**
  占着运行锁而失败；单独运行各自全绿。
- 根因：① `tests.test_ui_responsiveness` 触发「保存并开启自动连接」时**真的拉起了一个
  脱离父进程的守护**（`pythonw -m campusnet.app.service`）；② runner 没有套件间清理。
- 修复：UI 测试把 `start_daemon` 换成桩（UI 响应性不应依赖真起进程）；
  `tools/run_tests.py` 在每个脚本套件前清理遗留的 `CampusNet*.exe` 与 python 守护进程、
  清空 `build/smoke-data`。
- 验证：单元测试后**无遗留守护进程**，紧接着跑 EXE 冒烟 24/24 通过，全量 `--all` 23/23 通过。

## 3.1.1

> 本版按《CampusNet 3.1.0 自动探查认证门户故障：针对性诊断与修复》执行：
> 先出根因分析报告（引用源码），再只改已确认的问题，最后跑真实测试与打包。

### 一、修复：「已经能上网」被报成「没有发现认证页面」（用户实际反馈）

**现象**：用户在另一台**已经完成认证、能正常上网**的电脑上点击「探测认证地址」，
程序提示「未探测到认证页面：当前网络可用，未发生认证页面跳转。请重新连接网络后重试，
或手动填写地址。」——把正常网络状态报成了探测失败。

**根因**（`campusnet/ui/gui_service.py` 的 `probe_portal()`）：

- 该函数只有一个布尔 `ok`，"已在线（本来就没有门户）"和"探测失败（无法判断）"
  被压成同一个失败；`page_auth.py` / `page_settings.py` 于是都按失败渲染
  （红色「探测失败」+ 黄色「未探测到认证页面」+ 一句对已在线用户有害的建议）。
- 两台电脑表现不同**与代理/DNS/杀软无关**：开发者机器的 `config.json` 里已经缓存了
  `portal_url`，代码在 `target = url or cfg.portal_url` 处直接走"打开已保存地址"，
  **根本不执行联网探测**；另一台电脑的 `portal_url` 为空，才会走探测分支。

**修复**：

- `probe_portal()` 显式返回三态 `state`：`online`（已确认能上外网、不需要门户）、
  `portal-suspected`（有证据表明被门户拦截）、`unknown`（失败/证据不足/矛盾），
  文案与严重级别统一交给 `campusnet/status.py` 的 `from_probe()` 渲染；
  `ok` 保留旧语义（只有真的发现门户才是 `True`），旧调用方行为不突变。
- 认证页与设置页改按三态渲染：已在线显示绿色的
  「网络连接正常：已确认能正常访问互联网，当前没有认证门户。（无需认证；若稍后掉线，
  再点一次「探测认证地址」即可）」，不再出现"探测失败"与"重新连接网络"。

### 二、修复：普通网页被当成认证门户（违反"不得将任意响应等同于门户"）

**根因**：`checker._looks_like_html()` 只看 `Content-Type` 或正文里的 `<html`/`<form`，
任何 200 + HTML 的响应都会被判为"内容劫持"并**产出一个门户地址**；
`probe_portal()` 还会把它写进 `portal_url` 配置。

**修复**：新增 `campusnet/portal.py: portal_page_evidence()`——要求**正向证据**才认门户：

- Dr.COM / 哆点标记（复用既有 `drcom.looks_like_drcom()` 与 `drcom_page_state()`）；
- 带密码框的登录表单；
- 认证页特征词（标题 / 可见文字）；
- 校园网网关参数（`wlanuserip` / `wlanacname` / `nasip` …）。

证据分不足阈值（或候选地址返回 4xx/5xx）→ 一律 `unknown`：**不写配置、不称"已发现门户"**，
只给出脱敏的候选地址与"请手工核对"的建议。探测阶段始终只有 GET，不提交任何凭据。

### 三、修复：探测跟随重定向会把带参数的门户地址"降级"覆盖

`page_auth._persist_portal()` 保存的是跟随重定向后的**最终页地址**（常常不含网关参数），
并会无条件覆盖配置里那份带参数的发现结果——本机日志里配置门户地址就这样从
`a79.htm?wlanuserip=…&wlanacname=…&nasip=…` 变成了裸 `a79.htm`。
新增 `campusnet/security.py: keeps_portal_query()`：同主机、旧值有查询串而新值没有时
**拒绝覆盖**并提示"已保留原地址"。

### 四、测试

- 新增 `tests/test_probe_portal.py`（28 项）覆盖任务书要求的 6 个场景：
  已认证在线 / 302 跳转门户 / 超时与 DNS 失败 / 普通网页与错误页 / 非预期跳转 /
  新电脑首次运行；全部使用 `127.0.0.1` 上的模拟 HTTP 服务与假客户端，
  **不访问任何真实外网、不使用真实账号**。
- 已登记进 `tools/run_tests.py` 的单元套件清单。
- 修复前基线（`git worktree` 取 `10916f8`）全量单元 16/16 通过；修复后 17/17 通过；
  修复前后行为对比的真实输出见 `.tmp/demo-before-after.txt`。

### 五、仍然存在的限制（未在本版修复）

- 联网探测目标仍是 3 个**依赖 DNS** 的固定域名，没有 IP 直连兜底：校园网"先拦 DNS"
  时三个目标会全部 `dns-error`，此时**自动探测不到门户**（本机日志里 `dns-error` 真实出现过）。
  本版只把这类情况的提示改成可操作的（明确引导手工填写地址），**没有**改动探测目标集合。
- `checker.check()` 仍把"200 + HTML"判为内容劫持（后台守护据此尝试认证）：
  它不会泄露凭据（`UrlPolicy` 会先校验目标主机，且找不到登录表单就会停止），
  但会多一次无意义的尝试。本版只在"探测/引导"路径上收紧判定，未改动守护的触发条件。

## 3.1.2

> 本版按第三方只读源码审查整理的《CampusNet 3.1.1 修复指南》执行：只做已确认的问题，
> 每项配一条失败路径回归测试；P2 长期项（模块拆分、类型检查、CDP 再加固等）不混入本版。

### 一、行为变化（升级须知）

1. **在界面里清空账号或密码后，保存即暂停自动认证。** 此前守护进程会继续用内存里的
   旧密码登录；现在热加载发现配置不再完整时会写 `need-user` 心跳并**退出守护**，
   与"启动时缺凭据"的处理一致。用户重新填好账号密码即可恢复。
2. **浏览器模式新增"表单提交目标主机"校验**（新状态 `wrong-form-action`）。
   此前只校验页面 origin：页面确实是学校门户，但表单 `action` / 按钮 `formaction`
   指向别的主机时，账号密码仍会被提交过去（该次提交由浏览器发出，不经过 Python 侧的
   `UrlPolicy`）。白名单与 `UrlPolicy` 同源——页面主机 + `trusted_auth_hosts`，
   只看主机名、忽略端口，因此"认证页在 A 主机、认证接口在 B 主机"的学校（K-17）
   不受影响；确有门户需要提交到别的主机时，把该主机加入 `trusted_auth_hosts` 即可。
3. **拒绝读取更高版本写过的配置**：`config_version` 大于当前结构版本时直接报错，
   不再"半读半写"（此前会静默把版本号回写成当前值，等用户回到新版本时语义错位）。

### 二、修复的真实缺陷

- **P0-1 日志初始化失败导致界面永久死锁**：`gui_service.get_logger()` 在 `except`
  分支里递归调用自身，而 `_logger_lock` 是不可重入的 `threading.Lock`——只要
  `setup_logging()` / `load_config()` 抛一次异常就会永久阻塞，界面从此点任何按钮
  都无响应，而且日志没建起来、用户拿不到线索。现在失败时退回
  `campusnet.logs.get_logger()`（保证返回可用 logger），并加无锁快路径。
  回归测试用"独立线程 + 2 秒超时"，避免测试自身被死锁挂住。
- **P0-2 清空密码后仍用旧密码**（见行为变化 1）；密码变更时若新密文解不开，改为
  **整组回退**到旧配置，不再出现"新用户名 + 旧密码"的混合状态。
- **P0-3 gzip 炸弹（解压无上限）**：`http._maybe_decompress()` 现在按**解压后**的
  字节数二次限流（读 `limit + 1` 再截断），deflate 同样处理。此前只限制压缩前的
  2 MB，可膨胀到 GB 级——对长期驻留、还会访问不可信门户页的守护进程是现实的 OOM 面。
- **P1-1 心跳 `pid` 非数字时抛 `ValueError`**：新增 `_safe_int()`，心跳被写坏或半写时
  界面状态页不再整页报错。
- **P1-2 旧版互斥体持有语义**：显式保存到 `_LEGACY_HANDLES`（**故意**持有到进程结束，
  防止旧版本同时启动），失败路径释放本次已获取的句柄；不再依赖"局部变量被丢弃但
  句柄仍开着"的隐式行为。
- **P1-3 更高版本配置被静默降级**（见行为变化 3）。
- **P1-4 用 `"HTTP 2"` 子串判断状态码**：改为记录整数 `last_status`——2xx 且外网不通
  判 `verify-failed`，其余判 `server-error`。
- **P1-5 Dr.COM 失败关键词过宽**：`"认证失败"` / `"invalid"` 从 `CREDENTIAL_HINTS`
  拆到新的 `SOFT_FAILURE_HINTS`。典型误伤 `"认证失败：当前页面已超时"` 以前会被判成
  密码错误并**提前终止所有后续策略**，用户看到"账号或密码有问题"而实际只是会话过期；
  现在只记录日志并继续尝试。匹配同时改为大小写不敏感（`Invalid Password` 也能识别）。
- **P1-6 个人信息检查正则失效**：`tests/test_release_hygiene.py` 原来写成
  `r"C:\\\\Users\\\\..."`（四个反斜杠），在正则层面表示"两个字面反斜杠"，对真实的
  `C:\Users\bob\a.py` 永远不命中——**这条检查从上线起就是空转的**。已修正为
  `r"[A-Za-z]:[\\/]+Users[\\/]+([A-Za-z0-9_.-]+)[\\/]"`，扫描范围从仅 `.py` 扩到
  全部可能进包的文本文件（`.md/.json/.bat/.js/.txt/.yml/.toml` …），并加了正例自测
  防止再次写错；测试夹具里的占位用户名（`x`、`...`、`alice` 等）不计入。
- **P1-7 发布包卫生**：删除空夹具 `test/_e.txt`；`test/config.bad.json` 原本与
  `config.test.json` **逐字节相同**（等于没有存在意义），改为真的坏配置并由测试引用；
  源码包改用 `git archive HEAD` 生成（只导出被跟踪文件，`__pycache__`、
  `.dsh-vision-router` 产物与工作区垃圾不可能再混进去）。

  > 这个问题在 3.1.1 发布时**已被实际触发**：`CampusNet-3.1.1-src.zip` 里混进了
  > 6 张 `.dsh-vision-router` 截图（文件名含 Windows 用户名）、`__pycache__` 目录与
  > `test/_e.txt`。根因是当时用 robocopy 复制**工作区**而不是导出 git 跟踪内容，
  > 而发布前的隐私扫描只扫了"计划提交的文件清单"、没扫打包产物本身。
  > 现在源码包固定用 `git archive`，并用新增的 `tools/check_release_archive.py`
  > 把"检查打包产物本身"这一步固化下来。

### 三、发布工程

- 新增 `tools/check_release_archive.py`：检查发布压缩包内是否有 `__pycache__`、
  `.git`、`logs`、`config.json` / `state.json` / `heartbeat.json`、`.pyc/.log/.tmp/.bak`
  等作业产物，以及路径里的 Windows 用户名；退出码 0（干净）/1（命中）/2（参数或文件错误）。
- `tools/make_release_zip.py` 打包前预检 `dist/CampusNet`，命中即报错且**不动**已有压缩包；
  修复后的打包流程实测可复现（重建后的 win64 包与重建前逐字节相同）。
- 新增 `.github/workflows/ci.yml`：Windows 上按 3.11 / 3.12 / 3.13 跑
  `python tools/run_tests.py`；Ubuntu 上跑 ruff 的 `E9,F63,F7,F82`（先只查真错误，
  避免历史告警把 CI 变红）。新增 `pyproject.toml` 只放 ruff 配置，不含打包元数据
  （打包仍走 `tools/build_exe.py` + `packaging/CampusNet.spec`）。
- 源码包生成方式固定为：
  `git archive --format=zip --prefix="CampusNet-<版本>-src/" -o dist\CampusNet-<版本>-src.zip HEAD`。

### 四、测试

- 新增 `tests/test_gui_service_logger.py`（3 项，含"初始化失败不死锁"的线程 + 超时回归）。
- `tests/test_daemon.py` 新增热加载凭据语义 3 项、心跳容错 1 项；
  `tests/test_http.py` 新增 gzip / deflate 解压限流 2 项；
  `tests/test_security.py` 新增表单提交主机白名单 4 项；
  `tests/test_portal_js.py` 新增提交目标守卫与两个新状态文案；
  `tests/test_config.py` 新增高版本配置拒读 2 项；
  `tests/test_authenticator.py` 新增状态码判定 2 项、Dr.COM 关键词分类 3 项；
  `tests/test_release_hygiene.py` 新增正则正例自测与夹具卫生检查。

### 五、独立复核后的补充修复

本版在提交前用仓库的 Codex 规范审查器做了一次只读对抗审查，发现并修掉了 4 个问题
（其中 1 个是"看起来修了、其实没生效"）：

1. **P1-4 只加了变量、没换判定**：`last_status` 被赋值却从未被读取，失败分类仍在用
   `"HTTP 2" in last_reason`。现在判据抽成 `classify_submit_failure()`，按整数状态码
   判定；新增用例刻意构造"文案里含 `HTTP 2`、真实状态码是 500"的输入来钉住它
   （旧实现会误判成 `verify-failed`）。
2. **浏览器守卫的两个新状态没有被消费**：`campusnet/webauth.py` 的状态机原来只把
   `agreement-required` 等状态按"需要人工"结束，新增的 `wrong-form-action` /
   `invalid-form-action` 会落到链尾一直轮询到超时，最终报"认证服务器没有响应"并把
   浏览器窗口关掉。现在提取为 `MANUAL_STATES` 常量统一处理（保留窗口、按
   `manual-required` 汇报），并有测试保证该集合与 `portal_js.STATE_TEXT` 的中文说明
   一一对应。
3. **拒读高版本配置带来新的覆盖风险**：`gui_service.save_config()` 原来无条件写盘，
   而概览页「设为目标」等路径手里可能是"读失败后回退出来的默认配置"——上一节新增的
   `ConfigError` 会让用户的高版本配置被默认值覆盖。现在写盘前用
   `config_write_problem()` 重新确认磁盘上的配置可读（首次运行仍放行），读不回来就
   拒绝写入并说明原因。
4. **回归测试本身可能挂住 CI**：`tests/test_gui_service_logger.py` 若在主线程直接调用
   `get_logger()`，缺陷存在时会永久阻塞（实测回退修复后进程卡到超时而不报错）。
   现在每个用例都在带超时的 daemon 线程里调用，并在 `setUp` 里换一把新锁；
   实测：修复状态下 3 项通过（0.002s），回退状态下 3 项失败（7.0s）且进程正常退出。
