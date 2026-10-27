# CampusNet 3.1.0 交付报告（按《CampusNet 3.0.5 全面审查与稳定性升级任务书》）

- 执行时间：2026-10-20
- 基线版本：3.0.5（提交 `3a7eef1`，标签 `v3.0.5-baseline`）
- 交付版本：3.1.0（提交 `919efdc`）
- 执行环境：Windows 11 26100 / Python 3.13.9 / PyInstaller 6.22.3
- 原则：先备份与基线、根因优先、最小改动、**不伪造任何测试结果**，未验证项在第五节明确列出

---

## 一、问题根因分析

### 1.1 UI 交互问题 —— **已确认**

对三个页面 + 服务层做了逐行审计（共 107 处 `except`、15 个耗时操作入口）。

| 现象 | 根因 | 证据 |
| --- | --- | --- |
| 保存后缺少清晰反馈 | 保存成功只写"设置已保存/自动连接已开启"，**没验证守护是否在跑** | `page_settings.py` 旧 `_save_done`；`gui_service.py` 旧 `save_settings` 仅写 `auto_enabled` |
| 保存/认证/联网三者不分 | 三类结果压成两三种文案，`status` 字段（`bad-credentials`/`server-error`/`network-error`）**界面从不读** | 旧 `page_auth.py` 统一"认证失败：…" |
| 没有"处理中/成功/失败"统一状态 | 11 个验收状态中 **5 个完全缺失**：读取配置、确认联网、认证服务器无响应、配置读取失败、内部错误 | 审计表（本轮结论） |
| 出错缺少解释与建议 | 网络层错误（DNS/超时/TLS/代理/不可达）分类存在于 `checker.py`，但 UI 全部丢弃 | 旧 `page_overview.py` 只写"未认证" |
| 后台/自动连接/网络状态不统一 | 顶栏只渲染心跳的 `state`，丢弃 `detail`/`last_auth_reason`/`last_success_ts` | 旧 `main_window._tick_daemon` |
| 用户无法判断是否异常 | 失败时 hero 永久停在"正在检测…"；单槽状态栏被后续消息覆盖 | 旧 `_light_failed` 只改一个 tile |

另有三处**界面与业务不一致**的真实缺陷：`网络不可用（None）`（读错键名）、
"立即连接"按钮被 15s 轮询重新点亮（可并发两次认证）、切换页签重复进入会泄漏定时器。

### 1.2 第三方软件兼容性问题 —— **已确认（外部注入）**

- 游戏加加（GamePP，纯用户态 Electron 程序，**无内核驱动**）把 `GPP64.dll` 注入本程序进程；
- 注入后进程在数秒内以 `0xc0000005` 崩溃（崩溃模块 `GPP64.dll`，偏移 `0x9e956`）；
- 对照实验：**仅 20 行的最小 tkinter 程序同样崩溃**，退出该软件后本程序 GUI 连续运行 25s+；
- 因此该崩溃**不是本项目代码缺陷**，但本项目的"应对方式"造成了下一条问题。

### 1.3 进程崩溃 / 部分功能损坏 —— **已确认，根因是我方加固策略**

3.0.4/3.0.5 默认施加 `ProcessSignaturePolicy = MicrosoftSignedOnly`。实测证据：

| 证据 | 内容 |
| --- | --- |
| 签名归属 | 打包产物 `python313.dll`、`_tkinter.pyd`、`_ssl.pyd`、`unicodedata.pyd` 的 Authenticode **均 Valid**，签名者 `CN=Python Software Foundation` —— 合法签名，但非微软签名 |
| 运行期连带失败 | 策略生效后 import 未预热扩展：`sqlite3 → DLL load failed while importing _sqlite3: Windows 无法验证此文件的数字签名`；`asyncio`(`_overlapped`)、`winsound` 同样失败 |
| 用户可见后果 | 加载器在窗口上弹出「损坏的映像 0xc0000428」（本机 System 日志 2026-10-19 21:01–02，失败模块 `GamePPSDK\...\GPP64.dll`） |
| 收益未证实 | 加固开/关各 6 次 A/B 对照：两组均"未注入"，无法证明它能阻止真实崩溃 |

**结论**：该策略是"改完之后部分功能损坏"的机制来源；按任务书"没有明确安全收益、且兼容性代价不可接受即应移除"的要求，**3.1.0 起默认关闭**，改为显式可选。

### 1.4 后台守护问题 —— **已确认（两个真实缺陷）**

| 缺陷 | 根因 | 后果 |
| --- | --- | --- |
| 守护起不来 | 用户点过「停止」后进程被强杀，`stop.flag` 留在数据目录；下次启动守护看到它就"优雅退出"（退出码 0、不写心跳） | 界面显示"自动连接已开启"但守护从未工作 |
| 状态判定失真 | ① 只用命名互斥体判断"在跑"；② 互斥体名**不含数据目录**，属于另一个数据目录/旧版本的实例能永久占锁 | 界面显示"运行中"或"运行中但无响应"，实际当前配置无人服务 |
| 失败无原因 | 旧 `start_daemon` 只回"没有按时就绪" | 用户无法自助处理（如密码换机后解不开） |

### 1.5 Windows 安全提示 —— **部分确认**

完整取证见 [`docs/SECURITY_WARNINGS.md`](SECURITY_WARNINGS.md)。要点：

- **已确认**：4 个 EXE 全部 **未签名**（PowerShell + .NET 双验证）；
- **已确认**：发布 zip、源码 zip、bundle 及解压后 1991 个文件**全部无 MOTW**（用真实 `ZoneId=3` 文件做过正控）；
- **未确认**：本机 SmartScreen 通道 `IsEnabled=False`、0 条记录，**取不到"是否弹过"的证据**；
- **无证明力**：Defender 记录为空，但同期 `AntivirusEnabled=False`、服务 Stopped —— 空记录只说明没扫描；
- **已确认**：`_internal` 下 58/58 DLL 签名有效，且无 EXE、无脚本、无工具副本（0 命中）；
- **已确认的发布缺陷**：`CampusNetSetup.exe` 与 `CampusNet.exe` **哈希完全相同**（是副本而非安装器）；
  四个 EXE 的 `OriginalFilename` 有两条与文件名不符；`CompanyName` 为自声明字符串，无可核验发布者。

---

## 二、修改文件清单（21 个文件，+2042 / −463）

| 文件 | 修改内容 | 原因 | 可能影响 |
| --- | --- | --- | --- |
| `campusnet/hardening.py` | 重写：默认 `off`；不调 `SetProcessMitigationPolicy`、不改 `SetErrorMode`；加固改为 `CAMPUSNET_HARDEN=1` 显式可选（含预热/自检/弹窗抑制/明确日志）；删除按第三方进程名自动上锁 | 1.3 根因；任务书禁止按进程名硬编码规避 | 进程安全属性回到系统默认；无兼容性副作用 |
| `campusnet/daemon.py` | 新增 `daemon_health()`（锁 + 心跳新鲜度 180s + 状态三重判定）、`STATE_TEXT`、`DaemonHealth` | "不能仅凭进程存在判断正常" | 守护状态判定与文案统一 |
| `campusnet/autostart.py` | 启动前清理遗留 `stop.flag`；`start_daemon` 成功标准改为"锁 + 心跳"；失败读守护日志翻译原因；`stop_daemon` 超时也清理标记 | 1.4 | 开机自启/手动启停 |
| `campusnet/version.py` / `paths.py` | `mutex_name(dir_key)`、`data_dir_key()`（SHA1 前 8 位） | 全局命名对象互相占锁 | 便携版/安装版/多数据目录互不干扰 |
| `campusnet/status.py`（新，351 行） | 统一状态机：11 种状态 + 严重程度 + 可执行建议；从 `CheckResult`/`AuthOutcome`/心跳映射；未知原因不编造 | 1.1 | 三个页面 + CLI 统一渲染 |
| `campusnet/ui/gui_service.py` | 新增 `update_config()`（锁内 RMW、坏配置拒写）、`last_config_error()`、`test_connection()`、`record_last_result()`/`last_result()`/`last_result_text()`、`password_status()`；保存路径读失败即中止；配置读改写统一加锁 | 1.1 + 配置完整性 | 配置读写、测试连接、结果留痕 |
| `campusnet/ui/page_settings.py` | 新增「测试连接」按钮与密码显示/隐藏；保存并开启自动连接会**真的拉起守护**并据实报告；保存成功文案分层（只保存 vs 已开启）；失败回调恢复按钮 | 1.1 | 设置页交互 |
| `campusnet/ui/page_overview.py` | 状态面板（账号状态 / 后台守护 / 最近结果+时间 / 当前摘要）、「查看日志」、「测试/立即连接」busy 防重入、hero 失败态修复、定时器去重、按状态机渲染 | 1.1 | 概览页 |
| `campusnet/ui/page_auth.py` | 失败原因按 `status` 分流（密码错/网络/服务器/需人工）、门户地址写配置失败不再静默、关闭浏览器会话移出 UI 线程 | 1.1 + 主线程阻塞 | 认证页 |
| `campusnet/ui/main_window.py` | 加固门控（默认不隐藏窗口）、守护健康渲染、启动时显示配置读取失败、`daemon_health()` 缓存、`set_connection_state()` | 1.1 / 1.3 | 全局 |
| `campusnet/ui/ui_kit.py` | 新增 `severity_color()`；`Toggle.set_enabled()`（3.0.2 起） | 统一颜色映射 | 全部页面 |
| `campusnet/app/bootstrap.py` | 加固默认路径只记录决策、不施加策略；日志明确"未启用（默认）" | 1.3 | 启动流程 |
| `campusnet/app/cli.py` | 新增 `daemon` 子命令（真实健康状态 + 判断依据 + 自启动状态） | 可验证性 | 命令行 |
| `tests/test_status.py`（新） | 23 项：11 状态覆盖、映射正确性、"不编造原因"、忙态 | 回归 | — |
| `tests/test_hardening.py`（重写） | 27 项：默认不改 ErrorMode、默认 `sqlite3/asyncio/winsound` 可用、可选加固确实拒绝未签名 DLL、GUI 先判断模式再加固 | 回归 | — |
| `tests/test_daemon.py` / `test_ui_responsiveness.py` | 互斥体按数据目录、保存文案分层适配 | 适配新行为 | — |
| `tools/run_tests.py` | 套件间清理遗留守护进程与测试数据 | 修掉"单独跑全绿、连起来跑失败"的环境污染 | 测试稳定性 |
| `tools/make_release_zip.py` | 自动生成 `SHA256SUMS-<版本>.txt` | 规范发布流程 | 发布流程 |
| `docs/CHANGELOG.md` | 追加 3.1.0（含完整证据与取舍） | 交付说明 | — |
| `docs/SECURITY_WARNINGS.md`（新） | 安全提示取证 + 正规发布方案（签名/哈希/误报申诉/分发） | 任务书第六节 | — |

---

## 三、测试结果

### 3.1 汇总

| 项目 | 修改前（3.0.5 基线） | 修改后（3.1.0） |
| --- | --- | --- |
| 全量套件（`python tools\run_tests.py --all`） | **22/22 组通过** | **23/23 组通过** |
| 单元测试（`tests/`） | 222 项 | **255 项**（1 skip） |
| EXE 冒烟 | 24/24（1 skip） | **24/24（1 skip）** |
| 本机端到端认证 | 36/36 | 36/36 |
| 系统集成（含守护/自启/卸载） | 27/27（1 skip） | **27/27（1 skip）** |

留档：`.tmp/baseline/tests-before.txt`（修改前）、`.tmp/tests-after-3.1.0.txt`（修改后）。

### 3.2 逐项结果

| 测试项目 | 结果 | 测试环境 | 备注 |
| --- | --- | --- | --- |
| 主程序启动 | 通过 | 实际环境 | 源码 + 打包 EXE；GUI 冒烟全通过 |
| 无配置时的界面 | 通过 | 实际环境 | 首启引导 + 状态机「未配置账号」 |
| 保存合法配置 | 通过 | 实际环境 | DPAPI 加密 + 原子写 + 回读校验 |
| 保存失败处理 | 通过 | 实际环境 | 含"配置损坏拒绝写入"实证：原文件字节保持不变 |
| 重启后读取配置 | 通过 | 实际环境 | `tests.test_config` + `test_paths` |
| 密码加密/解密 | 通过 | 实际环境 | DPAPI 往返；换机不可解有明确提示 |
| 错误密码/认证失败 | 通过 | 本机模拟门户 | 按 `status` 分流为「连接失败·账号或密码不正确」 |
| 网络不可用 | 通过 | 实际环境 | 状态机「网络暂时不可用」+ 按 DNS/超时/TLS 给建议 |
| 认证服务器超时 | 通过 | 模拟 | 映射为「认证服务器没有响应」 |
| 网络恢复后重试 | 通过 | 模拟 | 守护 cooldown 状态 + 有限重试 |
| 自动连接开关 | 通过 | 实际环境 | 保存后真启动守护并据实报告 |
| 后台服务启动/停止 | 通过 | 实际环境 | `daemon` 子命令实证：运行中·网络正常 → 已停止 |
| 服务异常退出 | 通过 | 实际环境 | 心跳过期判「运行中但无响应」 |
| 配置文件损坏 | 通过 | 实际环境 | 拒绝覆盖 + 界面显示「配置读取失败」 |
| 日志脱敏 | 通过 | 实际环境 | `tests.test_security` + EXE 冒烟断言 |
| 退出后资源清理 | 通过 | 实际环境 | 会话标记、定时器、后台线程（daemon） |
| 连续启动/退出、重复点击 | 通过 | 实际环境 | busy 标志 + 按钮禁用 + 定时器去重 |
| 进程稳定性 | 通过 | 实际环境 | 默认模式 `sqlite3/asyncio/winsound` 均可导入 |
| 第三方软件兼容性 | **部分验证** | 实际环境 | 加固不再改变进程属性；本轮 GamePP **未注入**，无法断言崩溃已解决 |
| Windows 安全提示 | **要素已确认 / 弹窗未确认** | 实际环境 | 未签名、无 MOTW 已确认；SmartScreen 行为需干净机实测 |
| Windows 发布构建 | 通过 | 实际环境 | 4 入口齐全，18.57 MB / 994 文件 |

---

## 四、发布文件

| 文件 | 说明 |
| --- | --- |
| `dist/CampusNet-3.1.0-win64.zip` | Windows 发布包（`CampusNet.exe` / `CampusNetCLI.exe` / `CampusNetService.exe` / `CampusNetSetup.exe` 四个入口全部保留） |
| `dist/CampusNet-3.1.0-src.zip` | 完整工程源码（不含任何个人数据） |
| `dist/CampusNet-3.1.0-git.bundle` | 完整 Git 历史（可 `git clone` 还原） |
| `dist/SHA256SUMS-3.1.0.txt` | 上述三个交付物的 SHA256 |
| `docs/CHANGELOG.md` | 3.1.0 变更与取舍 |
| `docs/SECURITY_WARNINGS.md` | 安全提示取证与正规发布方案 |

- **未覆盖 3.0.5**：3.0.5 发布包原样保留；基线另存于 `.tmp/baseline/`（bundle + 源码 zip + 用户配置快照 + 修改前测试日志 + git log）。
- 构建步骤：`python tools\build_exe.py` → `python tools\make_release_zip.py`（3.1.0 起自动出校验清单）。

---

## 五、未完成 / 无法在当前环境验证的事项

| 项 | 原因 | 需要你做什么 |
| --- | --- | --- |
| 真实校园网认证 | 本机无学校账号；端到端测试用本机模拟门户 | 连校园网，在「设置」点「测试连接」，把状态栏文字与 `CampusNetCLI.exe daemon` 输出发回 |
| 第三方软件兼容性（GamePP） | 加固策略能挡住 DLL 加载（实测 `LoadLibraryW` 返回 0），但本轮 GamePP **未触发注入**，无法验证崩溃是否消失 | 保持 GamePP 运行，双击新版观察 ≥30s；若闪退，提供 `CampusNetCLI.exe conflicts` 与日志末尾 30 行 |
| SmartScreen 实际行为 | 本机无 MOTW、SmartScreen 通道禁用（0 记录） | 在**另一台**通过实际分发渠道拿到 zip 的机器上截图 |
| Defender / 杀软检测结论 | 本机 Defender 全程关闭、Kaspersky 服务停止，空记录无证明力 | 需要 VirusTotal 复核或 Kaspersky 人工复核（我未上传任何文件） |
| 代码签名 | 需要发布者自己的 OV/EV 证书，无法代办 | 提供证书后可完成签名并给出"签后打包"顺序 |
| `CampusNetSetup.exe` 是主程序副本 | 属发布质量缺陷，重做安装器超出本轮"最小改动"范围 | 告知是否需要在下一版做成真正的安装/配置向导 |
| Windows 10 兼容性 | 本次仅在 Windows 11 26100 实测 | 如有 Win10 机器，按 `docs/WINDOWS_ACCEPTANCE.md` 走一遍 |
