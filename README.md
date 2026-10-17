# 山东理工大学 校园宽带自动连接（Windows）

针对山东理工大学校园宽带门户（`http://111.17.200.130/`，城市热点 **Dr.COM / 哆点 ePortal**）实现的自动认证程序。

开机自启，掉线自动重连，全程后台静默运行 —— **不弹窗、不干扰你正常上网**。

| 特性 | 怎么做到的 |
|---|---|
| 后台自动登录 | 开机自动启动，掉线按退避间隔自动重连，无需人工干预 |
| 完全不弹窗 | 用 `pythonw.exe` 运行（无控制台窗口）；DPAPI 调用强制 `CRYPTPROTECT_UI_FORBIDDEN`（连系统凭据框都不会弹）；异常就地写日志；GUI 里刻意不用任何 messagebox；子进程一律 `CREATE_NO_WINDOW` |
| 零第三方依赖 | 只用 Python 标准库 + Windows 自带 API，不需要 pip install 任何东西 |

> **免责声明**：本项目仅供学习交流与个人便利使用。请遵守学校网络管理规定，合理使用校园网。使用产生的任何后果由使用者自行承担。

---

# 一、怎么用（30 秒，不需要懂电脑）

### 前提

装 Python 3.8 或以上版本（安装时**务必勾选** `Add python.exe to PATH`）。

### 第 1 步：双击 `1-一键安装配置.bat`

会打开一个小窗口。它会**自己检测**你的校园网、**自己读出你的账号**并填好。你只需要：

1. 看一眼账号对不对（不对就改）
2. 填上网密码（输入时不显示字符，这是正常的）
3. 点蓝色的「**保存并启用自动登录**」按钮

看到"设置完成"就结束了。

### 第 1.5 步（强烈建议，10 秒）：双击 `4-加入白名单.bat`

程序要在后台联网并开机自启，偶尔会被杀毒软件误判拦截（表现就是**有时不生效**）。加一次白名单，以后就稳了：

- 弹出「用户账户控制」窗口时点「**是**」；
- **Windows Defender** 由脚本自动处理：把 `pythonw.exe` / `python.exe` 和程序文件夹、Python 安装目录加入排除项；
- 如果装了 **360 / 火绒 / 腾讯电脑管家** 等第三方杀毒：它们没有公开的自动接口，脚本会检测出来并打印**手动添加的步骤**，照着点几下就行。

以后随时可以在「2-检查一下.bat」的仪表盘里看到白名单状态，也有「加入白名单」按钮。

### 第 2 步：完事了

以后每次登录 Windows，它都会自己在后台跑。**不用再打开任何东西。**

> 自启用了**双保险**：① 计划任务 `CampusNetAutoLogin`（登录时触发 + 每 15 分钟自愈）；② 启动文件夹里的 `campus-net-autologin.vbs`（登录时静默拉起）。两个都有单实例互斥，不会重复登录。卸载走 `3-卸载.bat` 会一起清掉。

---

# 二、怎么知道它在正常工作

双击 **`2-检查一下.bat`**，会打开一个"仪表盘"窗口，用大白话告诉你：

- 网络：能上网 / 上不了网
- 后台程序：正在运行 / 没在运行
- 开机自启：已设置 / 未设置
- 杀毒白名单：已加入 / 未加入（没加入就点窗口里的「加入白名单」按钮）
- 上次自动登录时间
- 最近的运行记录

窗口里还有「**立即测试登录**」按钮，点一下就知道现在到底行不行。

---

# 三、想卸载

双击 **`3-卸载.bat`**。会删掉开机自启和后台进程，并移除本程序加过的杀毒白名单条目；你的账号配置和日志会保留。

---

# 四、出问题了怎么办

| 现象 | 怎么办 |
|---|---|
| 双击 bat 一闪而过 / 提示 Python not found | 电脑没装 Python。装一个 3.8 以上版本（安装时**务必勾选** `Add python.exe to PATH`），再双击一次 |
| 检查窗口显示"没有在运行" | 双击 `1-一键安装配置.bat` 重新设置一次即可 |
| 后台程序偶尔不生效（可能被杀毒软件拦了） | 双击 `4-加入白名单.bat`，UAC 窗口点「是」；360 / 火绒 / 电脑管家等第三方杀毒按它打印的步骤手动加 |
| 检查窗口的"网络"一直是红的 | 点「立即测试登录」看具体原因 |
| 提示"账号或密码不对" | 双击 `1-` 重新填一次密码 |
| 开着 Clash / v2ray 时不上网 | 程序已默认绕过系统代理；如果还是不行，先关掉代理软件试试 |
| 想看详细过程 | 双击 `2-检查一下.bat` 点「打开日志文件夹」，或用下面的命令行方式 |
| 换电脑 / 重装系统后失效 | 密码和电脑绑定，这是安全特性。双击 `1-` 重新设置一次即可 |

**日志文件是 UTF-8。** 如果用 PowerShell 看日志，必须加 `-Encoding UTF8`，否则中文会乱码：

```powershell
Get-Content logs\campus_login.log -Tail 30 -Encoding UTF8
```

---

# 五、命令行的用法（给想深挖的人，平时完全不需要）

在项目根目录下执行：

```powershell
python tools\setup_wizard.py             # 文字版配置向导（等价于双击 1-）
python campus_login.py --once --verbose   # 手动跑一次，控制台看详细过程
python campus_login.py --check            # 只看现在通不通，不做认证
python detect_portal.py                   # 探测门户结构（换学校时用）
python campus_login.py --config <路径>    # 用指定配置文件
```

非交互式一键配置：

```powershell
python tools\setup_wizard.py --yes --username 你的账号 --password 你的密码
```

计划任务管理：

```powershell
schtasks /Query /TN CampusNetAutoLogin /V /FO LIST
schtasks /Run   /TN CampusNetAutoLogin
schtasks /End   /TN CampusNetAutoLogin
```

---

# 六、可选：登录失败的手机通知

程序按要求不弹窗，代价是"出问题了也不知道"。如果你想补上这一点，可以在配置窗口的「高级设置」里填一个**企业微信或钉钉群机器人**的 webhook 地址，登录失败时它会推消息到你手机。

不填就完全不启用，没有任何影响。

---

# 七、它是怎么判断"要不要登录"的

和 Windows 系统自带的联网探测同一套思路：

1. 请求 `http://www.msftconnecttest.com/connecttest.txt`，**不跟随重定向**
2. 拿到 `Microsoft Connect Test` → 已在线，什么都不做
3. 被 **302** 到别的地址 → 那个 `Location` 就是认证页（Dr.COM 还会在 URL 里带上 `wlanuserip`、`wlanacname`，这些会被解析出来回填到登录请求里）

3 个探测地址依次尝试，比只看一个地址稳。

之后的行为：

- 在线 → 每 60 秒巡检一次，**只在状态变化时写日志**，不刷屏
- 离线 → 立刻认证；失败按 1.6 倍退避，上限 120 秒
- 账号密码错 → 立刻停止重试并把间隔拉到最大（**避免账号被撞锁**）

---

# 八、针对山东理工大学门户的逆向结论

拉取了门户页和它的 JS（`a41.js` / `a43.js`）之后确认：

| 项 | 值 |
|---|---|
| 门户类型 | 城市热点 Dr.COM / 哆点 ePortal（页面标记 `Dr.COMWebLoginID_1.htm`） |
| 门户首页 | `http://111.17.200.130/`，GBK，**JS 渲染，页面里一个 `<form>` 都没有** |
| 认证接口 | `http://111.17.200.130:801/eportal/portal/login`（实测存在） |
| 响应格式 | `dr1003({"result":1,"msg":"Portal协议认证成功！"})` |
| 账号字段 | `user_account`，格式 `,0,账号` |
| 密码字段 | `user_password` |
| 账号后缀 | 校园用户**不带后缀**（页面 `uid` 里那个 `@cmcc` 只是 AC 内部标记，登录时**不要加**）；中国移动 `@cmcc`、电信 `@dx`、联通 `@lt` |

因为门户是 JS 渲染的，**通用 HTML 表单识别对它无效**（实测"表单数量: 0"），所以专门写了 `campusnet/drcom.py`，内含 **5 套候选策略**依次尝试，每套都用**真实联网结果**验证，成功的那套记进 `state.json` 下次优先用。

**关键设计**：服务端说"成功"不算数，必须真的能上外网才算成功。所以即使某一套策略在某个校区不生效，它自己会找到能用的那条路。

> 参数集对照了同为 Dr.COM 系统的开源实现（[无锡学院项目](https://github.com/Senquan007/Wxxy_network_auto_login)）做了修正：`callback` 必须是 `dr1003`；`result:0 + ret_code:2 + "已经在线"` 是**成功**而不是失败。

---

# 九、目录结构

```
campus-net-login/
├─ 1-一键安装配置.bat      ← 双击这个
├─ 2-检查一下.bat          ← 出问题双击这个
├─ 3-卸载.bat
├─ 4-加入白名单.bat        ← 推荐双击一次：加入杀毒白名单
├─ campus_login.py         后台守护入口（计划任务跑的就是它）
├─ detect_portal.py        门户探测工具
├─ config.example.json     配置样例
├─ campusnet/
│  ├─ crypto.py            DPAPI 加解密 + 单实例互斥（纯 ctypes）
│  ├─ config.py            配置与状态读写
│  ├─ logs.py              日志（只写文件，pythonw 下不碰 stdout）
│  ├─ http.py              HTTP 客户端（禁代理 / 禁自动重定向 / GBK 嗅探）
│  ├─ checker.py           联网判定 + captive portal 探测
│  ├─ portal.py            通用 HTML 登录表单解析引擎
│  ├─ drcom.py             Dr.COM / 哆点 专用适配器 ★
│  ├─ aes.py               ePortal 用的 AES 实现
│  ├─ authenticator.py     认证编排：custom → Dr.COM → 通用表单
│  ├─ notify.py            可选：手机通知
│  └─ daemon.py            守护主循环
├─ tools/
│  ├─ gui_setup.pyw        一键设置窗口
│  ├─ gui_status.pyw       状态窗口
│  ├─ gui_common.py        GUI/命令行共用（计划任务、进程状态、日志）
│  ├─ setup_wizard.py      文字版配置向导
│  ├─ install_task.ps1     注册开机自启（普通权限即可）
│  ├─ uninstall_task.ps1   卸载
│  ├─ whitelist.ps1        杀毒白名单（Defender 自动加 + 第三方杀毒手动指引）
│  ├─ startup_autologin.vbs 启动文件夹静默拉起（路径自适应，无硬编码）
│  ├─ mock_portal.py       本地模拟门户（自测用）
│  └─ probe_*.py           协议探测脚本（逆向/换校区时用）
├─ test/
│  └─ make_config.py       生成自测配置（含 DPAPI 加密的密码）
└─ logs/campus_login.log   运行日志（UTF-8，运行时生成）
```

### 运行时会生成、但不在仓库里的文件

| 文件 | 说明 |
|---|---|
| `config.json` | 你的账号配置，密码是 DPAPI 密文 |
| `state.json` | 记住哪套认证策略可用 |
| `logs/` | 运行日志 |

自测用配置也不在仓库里（因为 DPAPI 密文与本机绑定），需要时自己生成：

```powershell
python tools\mock_portal.py --port 8899          # 另开一个窗口
python test\make_config.py --port 8899           # 生成 test\config.test.json
python campus_login.py --config test\config.test.json --once --verbose
```

---

# 十、已知限制（诚实说明）

1. **已在山东理工大学校园宽带实测可用**（2026-09）：主动注销会话 → 断网 → 程序自动探测跳转 → `chkstatus` 取会话 → 明文 `user_account=,0,账号` 登录 → 服务端回「Portal协议认证成功」→ 回连验证 200 → 恢复上网，全链路可用。

   真实登录的关键结论（写死进 `campusnet/drcom.py`）：
   - 登录前必须先调 `page/loadConfig` 拿 **`rcn`**（会话 nonce，每次都会变），登录时必须原样带上，否则服务端只会回「当前页面已超时，请重新刷新页面」。
   - 账号用 `,0,` 前缀 + 裸账号，**不要**加 `@cmcc`。

2. **认证页有验证码的话程序会明确报错并停止**，不会硬撞。这种情况需要联系学校网络信息中心。

3. **只自动登录，不自动注销**，也不会干预你的手动操作。

4. **认证失败不会弹任何提示**（这是设计目标）。所以出问题时请用 `2-检查一下.bat`。

5. 计划任务以「当前用户 + 仅登录时运行」注册，所以**必须登录 Windows 后才会自动联网**。要做到"开机未登录也自动认证"，得改用 SYSTEM 运行，但那样 DPAPI 解不开密码，需要换成明文或机器级加密 —— 有安全代价，需要的话自己改。

6. 不同校区 / 不同批次的门户参数（`wlanacname`、`nasip`）可能不同。换环境后跑一次 `python detect_portal.py`，让程序重新探测。

---

# 十一、安全说明

- `config.json` 里只有 DPAPI **密文**，明文密码不落盘、不进日志
- 密文与本机 + 当前 Windows 用户绑定：文件被拷走、或换个 Windows 账户，都解不开
- 日志里的账号是打码的
- 代价：换电脑或重装系统后要重新设置一次

---

# 十二、License

MIT
