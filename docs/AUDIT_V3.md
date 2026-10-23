# 校园网自动连接程序 V3.0 审计报告

> 审计对象：`campus-net-login`（V1/V2 版本，代码量约 6.2k 行 Python）
> 审计方式：逐模块阅读 + 现有测试实际运行 + 打包环境实测
> 审计环境：Windows 11 26100 / Python 3.13.9 / 无 Git 历史（本次已初始化）
>
> 本报告是阶段 1 的产出。后续章节（"实施情况"）在阶段 2–6 完成后回填，
> 因此报告本身也记录了"原本的判断"与"最终做法"的差异。

---

## 1. 当前项目架构

### 1.1 目录与模块职责

```
campusnet/                 核心库（零第三方依赖，只用标准库）
├─ config.py               配置 dataclass + JSON 读写（容错合并）
├─ crypto.py               Windows DPAPI 加解密 + 命名互斥体（单实例）
├─ http.py                 极简 HTTP 客户端（Cookie、禁用系统代理、
│                          不自动跟随重定向、GBK 嗅探、可关 TLS 校验）
├─ checker.py              联网探测 / captive portal 识别
├─ portal.py               认证页解析（表单打分、字段分类、JS 接口猜测）
├─ authenticator.py        认证编排：custom → Dr.COM → 通用表单 三级级联
├─ drcom.py                Dr.COM / 哆点 ePortal 专用适配器（多套子策略）
├─ aes.py                  Dr.COM 协议所需 AES-128-ECB（复刻门户 JS）
├─ wifi.py                 netsh wlan 封装（扫描 / 连接 / 状态）
├─ daemon.py               后台守护主循环（热更新、退避、单实例）
├─ browser.py              系统 Edge/Chrome + CDP（自写最小 WebSocket）
├─ portal_js.py            注入认证页的填写脚本（启发式找输入框）
├─ webauth.py              浏览器辅助认证的编排
├─ logs.py                 日志装配（RotatingFileHandler，避免 pythonw 崩）
└─ notify.py               可选 webhook 推送

tools/                     界面、测试、一次性探测脚本
├─ gui_main.pyw            主界面（三页：概览/设置/认证）
├─ gui_service.py          界面背后的动作层（网络操作都在后台线程）
├─ gui_setup.pyw           配置向导界面（另一套窗口）
├─ gui_status.pyw          状态仪表盘界面（第三套窗口）
├─ ui_kit.py, page_*.py    自绘控件与三个页面
├─ gui_common.py           计划任务/进程/日志/白名单共用函数
├─ setup_wizard.py         命令行配置向导
├─ install_task.ps1        注册计划任务（登录触发 + 每 15 分钟自愈）
├─ uninstall_task.ps1      删除计划任务、VBS、白名单、杀进程
├─ whitelist.ps1           自动把 Python 与项目目录加入 Defender 排除项
├─ startup_autologin.vbs   启动文件夹双保险（写死 python 绝对路径）
├─ test_*.py, mock_portal.py  脚本式自测
└─ probe_*.py, live_test.py   针对特定门户的一次性探测/逆向脚本

根目录
├─ campus_login.py         后台守护入口（pythonw 静默启动）
├─ detect_portal.py        交互式探测认证页的工具
├─ 0~4-*.bat               面向用户的双击入口
├─ config.json             用户真实配置（含 DPAPI 密文）
├─ config.example.json     示例配置
├─ state.json              上次成功的认证策略
└─ logs/                   运行日志 + 认证页转储 + 门户 JS 资产
```

### 1.2 程序的实际运行流程

**后台自动认证（主链路）**

1. 计划任务在用户登录时用 `pythonw.exe campus_login.py` 启动（另有一份启动文件夹 VBS 做"双保险"）。
2. `crypto.acquire_single_instance()` 抢 `Local\CampusNetAutoLogin` 互斥体，抢占失败即退出。
3. `crypto.unprotect(cfg.password_enc)` 用 DPAPI 解出密码（失败只写日志）。
4. 循环：`wifi.ensure_connected()` 连目标 SSID → `checker.check()` 探测外网。
5. 若探测到 302 跳转或内容劫持 → 拿 `Location` 当认证页 → `PortalAuthenticator.login()`：
   custom 接口 → Dr.COM 专用策略（`plain-yau` / `plain-with-ac` / `eportal-aes`）→ 通用表单。
6. 每套策略提交后都用**真实联网探测**复核（`make_verifier`），成功才写 `state.json` 记住策略。
7. 失败按 `interval_offline × 1.6` 退避，封顶 `interval_max`；账号密码错误时退到最大间隔。
8. 配置 mtime 变化 → 热加载配置并重建 client/authenticator。

**图形界面**

- `gui_main.pyw`（三页控制台）与 `gui_setup.pyw`（向导）、`gui_status.pyw`（仪表盘）是**三套独立窗口**，各自有一份设置界面与杀毒白名单按钮。
- 网络操作全部走 `gui_service.py` 的后台线程，结果经 `queue.Queue` 回灌主线程。

### 1.3 现有功能清单（审计确认可用）

| 功能 | 位置 | 审计结论 |
| --- | --- | --- |
| 通用 Portal 表单认证 | `authenticator.py` + `portal.py` | 可用，逻辑清晰（表单打分 + 字段分类 + 3 种提交变体） |
| Dr.COM / 哆点 ePortal 认证 | `drcom.py` + `aes.py` | 可用，且注释中保留了逆向依据（JS 行号）；实测日志显示 `drcom:plain-yau` 成功 |
| 自定义认证接口逃生舱 | `config.CustomLogin` | 可用 |
| 浏览器辅助认证（JS 渲染门户） | `browser.py` + `webauth.py` + `portal_js.py` | 代码完整，本环境无法验证（Chromium 在受限令牌下无法启动） |
| Wi-Fi 状态 / 扫描 / 连接 | `wifi.py` | 可用，中英文 netsh 输出解析有测试覆盖 |
| 联网探测 / 门户识别 | `checker.py` | 可用，但状态粒度不足（见 §5） |
| 后台守护 + 退避重试 | `daemon.py` | 可用 |
| DPAPI 密码保护 | `crypto.py` | 设计正确（附加熵、禁用 UI、绑定用户/机器） |
| 配置容错读写 | `config.py` | 可用，但缺校验/版本/迁移 |
| Tkinter 三套界面 | `tools/gui_*.pyw` | 可用 |
| 开机自启动 | 计划任务 + 启动文件夹 VBS | **重复机制**（见 §6） |
| 日志轮转 | `logs.py` | 有 RotatingFileHandler，但未脱敏 |
| 自动化测试 | `tools/test_*.py` | 5 个脚本式测试，部分可用（见 §6.4） |

---

## 2. 程序的实际运行流程（补充：打包视角）

现有程序**没有任何打包配置**：没有 `.spec`、没有构建脚本、没有版本资源；
`1-一键安装配置.bat` 等脚本直接调用 `pythonw.exe tools\gui_*.pyw`，
并且 `startup_autologin.vbs` 里写死了本机的 Python 与脚本绝对路径。
这意味着：**在不装 Python 的机器上，整个程序完全无法启动**。

---

## 3. 当前功能清单（对外承诺 vs 实际）

README 对外承诺的功能基本都存在，但有 3 处不一致：

1. README 强烈建议双击 `4-加入白名单.bat` 加入 Defender 排除项 —— 这是被任务书明确禁止的默认行为（见 §4）。
2. README 说"卸载会移除本程序加过的杀毒白名单条目"，实际 `whitelist.ps1 -Remove` 会**不分来源**地删除 `pythonw.exe / python.exe / pyw.exe / py.exe` 四个进程排除项和若干目录排除项，可能删掉用户自己加的。
3. README 声称"双击 `0-校园网控制台.bat` 即可使用"，但前提是用户自己装好 Python。

---

## 4. 潜在安全问题（按风险排序）

### P0-1 Defender 排除策略过宽（严重）

`tools/whitelist.ps1`（395 行）在 `-Apply` 时会：

- 把 **`pythonw.exe` / `python.exe` / `pyw.exe` / `py.exe` 四个进程名**加入排除项 —— 这是机器上**任何**同名可执行文件（包括用户自己下载的恶意 Python 脚本）的通用豁免；
- 把 **Python 安装目录整目录**加入排除路径；
- 把 **整个项目目录**加入排除路径；
- 把启动文件夹里的 VBS 加入排除路径。

后果：任何落在这些目录里的恶意文件都不再被扫描。任务书 §三.1 逐条禁止这种做法（不得排除整个 Python 安装目录、不得排除整个项目目录、不得默认自动排除）。

另外三处一致性问题：

- `4-加入白名单.bat` 直接以 UAC 提权执行它；
- GUI（`gui_status.pyw` 的「加入白名单」按钮、`gui_setup.pyw`/`setup_wizard.py` 的提示）把"加白"当作首选的排障手段；
- `daemon.py` 在认证失败时直接写日志建议用户"双击 4-加入白名单.bat"。

`uninstall_task.ps1` 会调用 `whitelist.ps1 -Remove`，无差别删除上述排除项 —— 会破坏用户自己添加的安全配置。

### P0-2 凭据与日志泄露（严重）

- `logs/portal_assets/home.html` 是**真实门户的注销页转储**，其中 `uid='<账号>@cmcc'` 是**明文完整账号**；同文件还含 VIP 内网 IP `<校园内网IP段>`、网关地址、方案号。
- `logs/portal_dump_*.html`（25 个文件，约 90 KB）含 `wlanuserip`、`mac`、`ss*` 等会话标识。
- `logs/campus_login.log` 里账号做了部分脱敏（`<账号>`），但仍可定位到人；日志还包含门户/AC 的内网地址。
- `tools/test_aes.py`、`tools/probe_endpoints.py` 里把真实账号写进了源码（会随发布包/仓库分发）。
- `tools/startup_autologin.vbs` 写死了 `<用户目录>\...` 与 `<项目目录>\...`，泄露用户名与目录结构。

结论：**这些文件一个都不能进发布包**，且源码里的真实账号必须替换。

### P0-3 浏览器自动填写的来源校验不完整（中高）

`webauth.py` 把 `origin` 传给注入脚本，脚本里做 `location.origin !== cfg.origin` 检查后返回 `wrong-origin`。问题在于：

1. 校验发生在**同一个脚本**里 —— 也就是账号密码已经随脚本进入了页面上下文，只是脚本自己不填。若页面被替换成恶意页面，恶意 JS 无法直接读到注入脚本的闭包变量，但**没有任何机制阻止把凭据注入到错误页面**这一步本身（攻击面比"先验证再注入"大）。
2. 校验只比 origin，不看协议：跳转到 `file://` / 浏览器错误页时行为不确定。
3. `browser.py` 用 `tempfile.mkdtemp` 建临时 profile，且**不校验调试端口属于自己启动的进程**：若端口被别的程序占用，可能连到用户的既有浏览器实例上。
4. 调试端口用 `--remote-debugging-port` 但没限制监听地址（默认绑 127.0.0.1 的 Chromium 版本没问题，但不是显式保证）。
5. 关闭时只 `terminate()` 主进程，渲染子进程可能残留并继续占用 profile 目录（里面带 cookie）。

### P0-4 HTTP / TLS 安全（中）

- `config.insecure_tls` **默认为 True**，即全局 `CERT_NONE` + `check_hostname=False`。这属于"默认关闭 TLS 校验"，违反任务书 §三.5。
- 307/308 重定向会**把 POST body（含密码）重发到新地址**，只按"跟随重定向"逻辑处理，没有跨源判断。
- 凭据提交目标没有白名单：表单 `action` 指向任意域名都会照样 POST（`page.action_url()` 只做 `urljoin`）。
- 无响应体大小上限。
- 超时是有的（默认 8s），这一点没问题。

### P0-5 提权与权限（中）

- 白名单脚本需要管理员权限并自动提权 —— 唯一需要管理员的功能，而它本身应当被移除。
- 计划任务注册 `ONLOGON` 通常需要管理员权限；`Install-ViaSchtasks` 兜底路径用 `schtasks /Create /SC ONLOGON`，普通用户权限常见失败。
- 好消息：程序其它部分（启动/探测/认证/日志）都不需要管理员。

### P0-6 其它

- `crypto.py` 的互斥体句柄与 DPAPI 处理是正确的；**没有**硬编码 AES 密钥用于本地密码存储（`aes.py` 的固定 key 只用于 Dr.COM 协议报文，`5c1d5ad4dea0e8dd` 是门户 JS 自带的协议常量，保留是对的）。
- `notify.py` 会把认证结果通过 webhook 外发（默认关闭）；消息里有失败原因，需确认不含凭据（当前不含）。

---

## 5. 可靠性问题

1. **网络状态粒度不足**：`checker.py` 只有 `ok / redirect / hijack / unreachable` 四种 `reason`，DNS 失败、超时、TLS 失败、拒绝连接全被归到 `unreachable`，上层只能报"认证失败"。
2. **失败日志与提示误导**：把网络层问题（如 DNS 挂了）写进"自动认证未成功"分支，并把原因导向"可能被杀毒软件拦截 → 加白名单"。
3. **配置无校验**：`except: pass` 式合并，`interval_*` 可以写成 0 或负数导致忙等；`wifi_connect_mode` 非法值不提示；URL 不做合法性检查。
4. **配置无版本、无迁移**：旧配置读入时未知字段被静默丢弃（`if key not in known: continue`），新版字段永远拿默认值。
5. **配置写入非原子**：`write_text` 直接覆盖，进程被中断会留下半个 JSON；也没有 `.bak`。
6. **安装目录写用户数据**：`config.json / state.json / logs/` 都在项目根目录，打包后若装到 `Program Files` 会直接写失败。
7. **休眠/网络切换无感知**：`time.sleep(interval)` 一整段睡过去，电脑休眠唤醒后可能等满一个周期；没有检测时钟跳变。
8. **认证页转储无限增长**：`portal_dump_<epoch>.html` 每个失败一次就写一个，没有上限也没有清理。
9. **计划任务 + VBS 双启动**：靠互斥体兜底，但用户无法判断到底谁拉起的进程；VBS 写死路径在换机后静默失效。
10. **`sys.executable` 假设**：`gui_common.pythonw_path()`、`install_task.ps1` 都假定"运行的是系统 Python 脚本"，打包后会指向 EXE 自身（或直接失效）。

---

## 6. 测试覆盖情况（实测）

原有 5 个脚本式测试，本次审计实际运行结果：

| 测试 | 覆盖内容 | 审计时结果 |
| --- | --- | --- |
| `tools/test_aes.py` | FIPS-197 向量、PKCS7、往返 | **通过**（但末尾 `print("✅")` 在 GBK 控制台抛 `UnicodeEncodeError`，脚本以退出码 1 结束，属于假失败） |
| `tools/test_wifi.py` | netsh 中英文输出解析、无网卡降级 | **通过**（本机无无线网卡，真机项 SKIP） |
| `tools/test_gui_smoke.py` | 建窗口、切页、布局越界、文字截断 | **通过** |
| `tools/test_web_autofill.py` | 真浏览器 + CDP 填写 | **失败**：Chromium 在本审计环境的受限令牌下无法启动（`exit code 21`），非代码缺陷，但**本环境无法验证浏览器链路** |
| `tools/test_login_flow.py` | Dr.COM 干跑（假账号打真门户） | 未在本次审计中运行（需要真实内网环境） |

结论：**覆盖面偏窄**。缺少的正是最容易出事的部分：

- 配置加载 / 校验 / 迁移；
- 日志脱敏；
- 网络状态分类；
- 认证结果分类（成功/凭据错/验证码/网络错）；
- 退避重试与状态机；
- 路径策略与旧数据迁移（打包前提）；
- "凭据不会发到外网"这类安全断言。

---

## 7. EXE 打包可行性评估

| 项目 | 评估 | 结论 |
| --- | --- | --- |
| PyInstaller 可用性 | 本环境需 `pip install pyinstaller`（已装 6.22.3 验证） | 可行 |
| tkinter | 系统 Python 自带 Tcl/Tk 8.6，PyInstaller 有 hook | 可行 |
| 第三方依赖 | 运行期**零依赖**（只用标准库 + ctypes + tkinter） | 非常适合打包，体积可控 |
| 资源文件 | 无外部资源（图标/配置示例除外），`config.example.json` 可选带上 | 简单 |
| 路径假设 | **最大障碍**：多处硬编码项目根目录、`sys.executable` 当 Python 用 | 必须改造，已规划 |
| 多入口 | 需要 GUI / 向导 / 守护 / CLI 四个入口 | 用单入口 + 按 EXE 名分派 |
| onedir vs onefile | 需要调 netsh/DPAPI/PowerShell，onedir 更稳；onefile 启动慢且易被反复扫描 | 选 onedir |
| 权限 | 主程序、守护、CLI 均无需管理员 | 满足"不默认申请管理员" |
| 版本信息 | 需要生成 VSVersionInfo | 可行（构建脚本自动生成） |
| 构建验证 | 本环境为真实 Windows，可实际构建与冒烟 | **可以实际构建** |

结论：**技术可行，但必须先完成"路径与用户数据分离"改造**，否则打出来的包在 `Program Files` 下连日志都写不出来。

---

## 8. 建议修改清单

### 安全（P0）

1. 删除 `tools/whitelist.ps1` 与 `4-加入白名单.bat`；移除 GUI/README/`daemon.py` 中所有"加白名单"入口与提示。
2. 新增只读安全体检（`campusnet/diagnostics.py`）：读杀毒软件现状、自启动项、旧版残留，给出"看防护记录而不是关杀软"的指引；提供**脱敏**诊断包导出。
3. 日志全面脱敏（`security.redact_text` + 自定义 Formatter），账号、密码、令牌、长密文一律打码。
4. 认证页转储默认关闭；开启时只写脱敏副本并限制数量。
5. 浏览器认证改成**两阶段**：先跑不含凭据的探针确认 origin/协议，通过后才注入带凭据的脚本。
6. 调试端口只绑 127.0.0.1，并校验监听进程属于自己启动的子进程；关闭时按进程树清理。
7. TLS 默认校验证书；改为 `tls_insecure_hosts` 逐主机放行；全局开关保留但需显式开启并提示风险。
8. 307/308 只在**严格同源**时重发 body；新增 `trusted_auth_hosts` 白名单，表单/自定义接口的目标主机不在其中就拒绝提交。
9. 清理源码中的真实账号与个人路径。
10. 卸载不再触碰任何安全软件设置。

### 可靠性（P1）

11. `checker.py` 状态细分：`online / redirect / hijack / dns-error / timeout / refused / tls-error / proxy-error / unreachable / http-error / no-targets`，并提供给用户可读的"现象 + 建议"。
12. 认证结果结构化：`AuthOutcome`（status / ok / needs_user / retryable / strategy / adapter）。
13. `daemon.py` 显式状态机 + 心跳文件；休眠检测；失败日志限流；账号密码错误不撞锁。
14. 配置加版本号、校验、原子保存 + `.bak`、未知字段保留、旧配置迁移备份。
15. 路径策略：安装目录只读；用户数据统一 `%LOCALAPPDATA%\CampusNet`；旧数据首启动复制迁移（不删旧文件）。
16. 自启动简化为**唯一**机制（HKCU Run，用户级，无需管理员）；卸载精确清理 + 清理 V2 残留。

### EXE 与发布（P1）

17. 单入口 `entry.py` + 四个 EXE（`CampusNet` / `CampusNetSetup` / `CampusNetService` / `CampusNetCLI`）。
18. `packaging/CampusNet.spec`（onedir）+ `tools/build_exe.py` + `build_exe.bat`；自动生成版本资源；构建报告。
19. EXE 冒烟测试脚本（在**去掉 Python 的 PATH** 下运行）。
20. README / BUILDING / CHANGELOG / KNOWN_ISSUES / WINDOWS_ACCEPTANCE / LICENSE / requirements。

### 代码质量（P2）

21. 三套 GUI 合并为一套（`campusnet/ui/`），旧 `.pyw` 退化为薄壳。
22. `.gitignore` + 发布排除规则；`docs/` 与 `tests/` 不进发布包。
23. 补齐单元测试（配置/路径/安全/状态机/HTTP/认证结果/发布卫生）。

---

## 9. 修改优先级

| 优先级 | 内容 | 理由 |
| --- | --- | --- |
| **P0-必做** | 删除白名单机制、日志与转储脱敏、源码真实账号清理 | 直接扩大安全盲区 / 泄露凭据 |
| **P0-必做** | TLS 默认校验、凭据投递白名单、跨源重定向不重发 | 防止密码被发到第三方 |
| **P0-必做** | 浏览器两阶段校验 + 调试端口归属校验 | 自动填写的核心风险点 |
| **P1** | 路径分离 + 配置版本迁移 | 打包的**前置条件** |
| **P1** | 四个 EXE + spec + 构建/冒烟脚本 | 本次核心目标 |
| **P1** | 状态细分 + 结果结构化 + 状态机 + 自启动单一化 | 稳定性与可诊断性 |
| **P1** | 测试体系补齐 | 证明上述改动没坏东西 |
| **P2** | GUI 合并、文档、许可证、发布目录整理 | 可维护性 |

---

## 10. 可能影响现有功能的变更（风险登记）

| 变更 | 潜在影响 | 缓解措施 |
| --- | --- | --- |
| 用户数据迁到 `%LOCALAPPDATA%\CampusNet` | 老用户配置"看不见了" | 首启动**复制**旧 `config.json/state.json/logs`，旧文件原地保留，写迁移报告；失败可回退 |
| TLS 默认校验 | 自签证书门户认证失败 | 提供 `tls_insecure_hosts`（逐主机）+ 全局开关；校验失败时给出明确提示与一键放行入口 |
| 凭据投递白名单 | 认证接口在别域名的学校会提交失败 | `trusted_auth_hosts` 显式配置；拒绝时给出可操作提示（README 说明） |
| 浏览器两阶段校验 | 门户跳转到新域名时不再自动填 | 明确提示"地址变了，请更新认证页地址"；窗口保留供人工完成 |
| 自启动改为 HKCU Run | 失去"每 15 分钟自愈"的计划任务 | 守护进程本来就是不死循环；GUI 读心跳可一键拉起；卸载清理旧任务 |
| 三套 GUI 合一 | 老用户习惯的 `2-检查一下.bat` 界面变化 | 保留 `.pyw` 与 `.bat` 文件名，作为转调新界面的薄壳 |
| 配置新增字段 | 旧代码读新配置会忽略未知字段 | 保留未知字段并原样写回 |
| 移除 `whitelist.ps1` | 依赖它的旧脚本会找不到文件 | 保留 `install_task.ps1 / uninstall_task.ps1` 文件名，改为转调新实现并提示已废弃 |

---

## 11. 审计阶段的实际执行记录

| 步骤 | 命令 / 动作 | 结果 |
| --- | --- | --- |
| 确认工作目录 | `Get-Location` | `<项目目录>` |
| Git 状态 | `git rev-parse` | **不是 Git 仓库** → 已 `git init` 并提交基线快照（3460 文件）作为回退点 |
| Python 环境 | `python --version` | 3.13.9，tkinter 8.6 可用 |
| 运行现有测试 | `tools/test_aes.py` 等 | AES/WiFi/GUI 通过，浏览器链路在本环境不可用；发现 `test_aes.py` 的控制台编码缺陷 |
| 环境限制 | Chromium/CDP | 受限令牌下 Chromium 启动即退出（`exit 21`），浏览器链路**无法在本环境验证** |
| 构建工具 | `pip install pyinstaller` | 成功安装 6.22.3 到工作区内的独立目录，可用于真实构建 |

**环境限制（必须记录）**：本会话运行在受限沙箱中，
`tempfile.mkdtemp` 建出的目录 ACL 不可用、Chromium 无法启动。
因此"浏览器自动填写"的**端到端**验证必须在普通 Windows 桌面上按
`docs/WINDOWS_ACCEPTANCE.md` 人工执行；本报告与最终报告都不会声称它已通过。

---

## 12. 实施情况回填（阶段 2–6 完成后）

本节记录"审计时判断"与"最终做法"的差异，避免报告与实际脱节。

| 审计时的判断 | 最终做法 | 差异原因 |
| --- | --- | --- |
| 保留 `install_task.ps1` / `uninstall_task.ps1` 作为唯一自启动实现 | 改为 HKCU Run（Python 实现），两个 ps1 变成转调薄壳 | 计划任务注册 `ONLOGON` 需要管理员权限，普通用户装不上；HKCU Run 无需提权且删除干净 |
| 三套 GUI 逐步合并 | 一次合并到 `campusnet/ui/`，三个旧 `.pyw` 变薄壳 | 三套界面各有一份设置页与白名单按钮，长期维护必然走偏；合并后行为统一且减少打包 surface |
| `insecure_tls` 改为"默认开校验 + 全局开关" | 再进一步：新增 `tls_insecure_hosts` 逐主机放行，默认只校验不放行 | 全局关校验等于对所有站点降级，逐主机才符合最小权限原则 |
| 浏览器自动填写只加"来源二次确认" | 改为**两阶段**：先跑无凭据探针，通过后才注入带凭据脚本 | 凭据一旦注入到错误页面就无法收回，探针方案把风险面降为零 |
| 未预料到的打包问题 | 打包后发现 `sys.executable` 在 EXE 里是"当前 EXE"，会导致从 GUI 拉起守护时把 GUI 再开一次 | 新增 `paths.service_exe()` + 单入口按文件名分派 + `--role` 兜底，并为此写了专门的单元测试 |
| 未预料的测试环境限制 | `tempfile.TemporaryDirectory`（0o700）在受限账户下无法清理；Chromium 无法在受限令牌下启动 | 测试改用工作区内临时目录；浏览器链路明确标注"本环境未验证"，写入 KNOWN_ISSUES 与验收清单 |

### 阶段产出对照

| 阶段 | 产出 | 状态 |
| --- | --- | --- |
| 1 审计 | 本文档 | 完成 |
| 2 P0 修复 | 白名单机制删除、日志/转储脱敏、真实账号清理、TLS 逐主机、凭据投递白名单、浏览器两阶段校验、调试端口归属校验 | 完成 |
| 3 可靠性 | 状态细分、`AuthOutcome`、守护状态机 + 心跳、休眠检测、配置版本/校验/迁移、原子保存 | 完成 |
| 4 EXE 化 | `entry.py` 四入口、`packaging/CampusNet.spec`、`tools/build_exe.py`、`build_exe.bat`、路径与数据目录分离 + 迁移、`tools/test_exe_smoke.py` | 完成并实测（25/25 通过） |
| 5 发布流程 | 自启动单一化、卸载脚本、发布目录整理、LICENSE / THIRD_PARTY_NOTICES / requirements、README / BUILDING / CHANGELOG / KNOWN_ISSUES / 验收清单 | 完成 |
| 6 验收 | 单元测试 145 项 + 旧版脚本 3 项 + EXE 冒烟 25 项，全部实测 | 完成（浏览器链路与真实认证见"未验证项"） |

### 未验证项（诚实记录）

1. **真实校园网认证**：本环境没有可用的校园网账号（用户配置里的 DPAPI 密文在
   当前 Windows 用户下无法解密，日志中可见 `CryptUnprotectData 调用失败`），
   因此没有做真实认证。需按 `docs/WINDOWS_ACCEPTANCE.md` 第 4 节人工验收。
2. **浏览器辅助认证端到端**：Chromium 在受限令牌下启动即退出（`exit code 21`），
   本环境无法执行；脚本层与状态机层已用单元测试覆盖。
3. **无 Python 的干净机器**：本机装有 Python。冒烟测试通过"清空 PATH / 清空
   PYTHONHOME、PYTHONPATH"来模拟，`frozen = True` 已确认，但严格的验证仍需一台
   真的没装 Python 的机器。

---

## 13. 阶段 7：拿到完整权限后的复测（补充）

上一轮审计受沙箱限制，浏览器链路与系统集成无法验证。提升为完全访问后补做了三项
真实测试，并因此发现并修复了 4 个缺陷（详见 `docs/CHANGELOG.md` 3.0.0 节）。

### 13.1 浏览器辅助认证（真实 Edge 155）

`tools/test_web_autofill.py` 全部通过，覆盖：注入脚本结构、真实打开无地址栏窗口、
填账号/密码（含空格与 `@`）、勾选必选协议、选择运营商、提交、页面回 `authenticated`、
**origin 不匹配时拒绝填写**、`webauth.run` 全流程（自己开窗→填写→提交→复核联网→
关窗→清理临时用户目录）。

### 13.2 本机端到端认证（真 HTTP + 真 DPAPI，不联网）

新增 `tools/test_local_e2e.py`：用 `tools/mock_portal.py` 起一个 Dr.COM 风格的模拟门户
（GBK 页面、无 form、参数内嵌 JS、JSONP 登录接口），用**真实 DPAPI 密文**驱动认证，
源码模式与打包 `CampusNetService.exe` 各跑四条路径，**36/36 通过**：

* 成功：探测到 302 → 识别 Dr.COM → 提交 → 门户记录登录 → 复核 200 → 记住策略；
  同时断言日志里**没有明文密码**、账号已脱敏；
* 密码错误：明确报"账号或密码"、不无限重试、门户记录失败；
* 门户不可达：状态归类为网络层（`unreachable/timeout/refused`），不是"认证失败"；
* 关闭 Dr.COM 适配后：明确报"找不到登录表单"并给出页面标题，不谎报成功。

### 13.3 系统集成（真 HKCU Run + 真进程）

新增 `tools/test_system_integration.py`，注册表相关检查在源码模式下
**27/27 通过**：自启动登记（命令指向守护而非界面、不含密码、不动其它启动项）、
启动守护、单实例互斥、心跳文件、日志脱敏、优雅停止、取消自启动、卸载（保留数据 /
`--remove-data` 删除数据）、**HKCU Run 还原核对**；另有 1 项（旧版计划任务清理）
因本机残留已被前一次测试真实清理而跳过。
另外单独验证了旧版启动文件夹 `campus-net-autologin.vbs` 能被 `cleanup-legacy` 删除。

### 13.4 复测中发现并修复的缺陷

| 缺陷 | 怎么发现的 | 修复 |
| --- | --- | --- |
| 守护在睡眠中不检查 `stop.flag`，卸载最长要等 `interval_max`（默认 120s） | 系统集成测试 `stop 返回成功` 超时 | `Daemon._sleep()` 每秒检查停止标记；新增回归测试 |
| `daemon_running()` 泄漏互斥体句柄，反复探测后守护抢不到锁 | 阅读代码 + 写"探测 30 次后仍能抢锁"的测试 | 新增 `crypto.release_single_instance()`，探测用完即关 |
| `uninstall --remove-data` 必定删不掉 `logs/`（`WinError 32`） | 系统集成测试 `数据目录已被删除` 失败 | 删除前关闭自身日志句柄 + 重试 + 只读属性修复；新增回归测试 |
| 无版本号的旧配置每次启动都新增一份迁移备份（实测 3 次启动 3 份） | 检查 `%LOCALAPPDATA%\CampusNet\backup\` | 按内容哈希去重、最多保留 5 份 |
| `campusnet/app/__init__.py` 急切导入子模块，命令行工具连带加载 tkinter | 运行 `python -m campusnet.app.cli` 出现 runpy 警告 | 改为不导入子模块 |

### 13.5 本轮仍然**未**验证的内容

1. **真实校园网账号认证**：不掌握可用凭据（用户配置里的 DPAPI 密文在当前用户下解不开），
   因此只用模拟门户做了端到端验证。真实门户的认证需要用户按
   `docs/WINDOWS_ACCEPTANCE.md` 第 4 节自行验收。
2. **打包 EXE 的注册表 / 启动项写入**：本会话对新建的未签名二进制施加了令牌限制，
   实测同一个最小 PyInstaller EXE 用 5 行 winreg 代码也会拿到 `WinError 5`，
   而 CPython 可以写入 —— 说明是会话策略而非产品缺陷。系统集成因此自动切换到
   源码模式验证同一套逻辑。这一条必须在普通用户会话中复验（验收清单第 6 节）。
3. **真正没有 Python 的机器**：本机装有 Python，冒烟测试通过清空 PATH /
   PYTHONHOME / PYTHONPATH 模拟，`frozen = True` 已确认，但仍建议在干净机器上复验。

### 13.6 打包 EXE 的浏览器模式：为何判定为"环境限制"

用最小 PyInstaller 探针（与发布包同样的构建参数）复现了这个现象，证据链：

| 观察 | 说明 |
| --- | --- |
| CDP 握手成功、`/json/list` 能列出目标 | 打包进程能正常 CreateProcess 启动 Edge、能连调试端口 |
| 目标列表里有 `type=page` 且 URL 正确 | 浏览器进程与页面对象都建起来了 |
| `Runtime.evaluate` 返回 `{'code': -32000, 'message': 'Target crashed'}` | **渲染进程**崩溃 |
| 完全相同的代码在源码模式（同一台机器、同一个 Edge）下全部通过 | 不是代码问题 |
| 同一会话里，被测 EXE 连 5 行 winreg 代码都会 `WinError 5`，连 `%LOCALAPPDATA%` 都写不进去 | 该会话对新起的未签名二进制施加令牌限制，Chromium 渲染进程的子进程/沙箱初始化在这种限制下失败 |

因此结论是：**打包 EXE 的浏览器模式需要在普通用户桌面复验**，本会话无法完成。
顺带在这次排查中给浏览器启动参数补了 `--disable-extensions`
`--disable-component-extensions-with-background-pages` `--disable-client-side-phishing-detection`：
登录窗口不该加载用户装的扩展（实测临时 profile 里确实混进了多个 chrome-extension
background page），改完目标列表只剩一个 page，攻击面更小。
