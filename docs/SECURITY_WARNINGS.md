# Windows 安全提示的取证与正规发布方案

> 本文只记录**实测证据**与**可执行的规范做法**，不包含"如何绕过安全提示"。
> 关闭 Defender、修改系统安全策略、伪造签名、隐藏程序行为等做法一律不采用。

## 一、先区分：用户看到的"安全提示"可能完全不是一回事

| 现象 | 实际来源 | 特征 | 本程序的现状 |
| --- | --- | --- | --- |
| "Windows 已保护你的电脑 / 未知发布者" | **SmartScreen**（应用信誉） | 只有一个"仍要运行"，来源常是"未知发布者" | 4 个 EXE **未签名**，因此一旦带上 MOTW 就会触发 |
| "Windows 安全中心：发现威胁" | **Defender 实时防护** | 有具体威胁名与隔离动作 | 本程序**不做**任何规避行为（无注入、无持久化服务、无全局白名单） |
| 第三方杀软弹窗 | Kaspersky 等 | 自家 UI | 只可能出现在它们自己的检测逻辑里，需用它们的报告核对 |
| "此应用无法在你的电脑上运行" | 架构/依赖问题 | 不是安全提示 | 打包产物为 x64、onedir，依赖随包分发 |
| "损坏的映像 / 0xc0000428" | **DLL 签名/哈希校验失败** | 有具体 DLL 路径 | 曾经是**我方加固策略**造成的（见第三节），3.1.0 已默认关闭 |

## 二、本机实测证据（2026-10-19，Windows 11 26100）

| 检查项 | 方法 | 结果 |
| --- | --- | --- |
| EXE 数字签名 | `Get-AuthenticodeSignature` + .NET `X509Certificate.CreateFromSignedFile` 双验证 | `CampusNet.exe` / `CampusNetCLI.exe` / `CampusNetService.exe` / `CampusNetSetup.exe` 全部 **NotSigned**（无嵌入签名） |
| 随包运行时 DLL | 同上，`_internal` 下 58 个 DLL/PYD 全量 | **58/58 Valid**（Microsoft 39、Python Software Foundation 18、Microsoft Windows 1） |
| Mark of the Web | `Get-Item -Stream *` 检查 `Zone.Identifier` | 发布 zip、源码 zip、git bundle 及解压后 1991 个文件：**全部无 MOTW**（用 Downloads 里真实 `ZoneId=3` 文件做过正控，方法有效） |
| SmartScreen 日志 | `Microsoft-Windows-SmartScreen/Debug` | 通道 **IsEnabled=False、0 条记录** → **本机无法据此判定是否弹过** |
| Defender 记录 | `Get-MpThreatDetection`、Defender/Operational 事件 | **无任何 CampusNet 相关记录**；但同一时间 `AntivirusEnabled=False`、服务 Stopped → **空记录不代表安全，只代表没扫描** |
| 第三方杀软 | `root/SecurityCenter2` | 已安装 Kaspersky 21.26，但服务 Stopped、无进程 → 当前无活跃扫描 |
| 版本资源 | `FileVersionInfo` | 四个 EXE 均为 `3.0.5.0`（3.1.0 已更新）；`CompanyName=CampusNet` 为自声明名，**无可核验的法人实体** |
| 双用途工具自查 | 在 `_internal` 里检索 curl/wget/netsh/schtasks/mshta/rundll32/psexec 等 | **0 命中**；无 EXE、无脚本、无 `.py` 源文件 |

**结论**：
1. "未签名"是**已确认**的事实，也是 SmartScreen 在联网干净机上弹"未知发布者"的**主要可疑原因**（高度怀疑）。
2. 本机**没有**可证明"是否弹过 SmartScreen"的证据；也**没有**任何 Defender/杀软检测记录，但这是因为安全软件当时没有运行。
3. 发布包内容干净（无附加可执行文件、无脚本、无工具副本），没有需要"修掉的可疑行为"。

## 三、已修复的"假安全提示"：加固策略造成的 0xc0000428

3.0.4/3.0.5 曾默认对自身进程施加 `ProcessSignaturePolicy=MicrosoftSignedOnly`。
当游戏加加尝试注入 `GPP64.dll` 时，系统拒绝加载它，但加载器会在我们的窗口上弹出：

```
校园网自动登录 · 控制台: CampusNet.exe - 损坏的映像
...\GamePPSDK\1.2.1615.625\GPP64.dll 没有被指定在 Windows 上运行 ...
错误状态 0xc0000428
```

证据：本机 System 日志 2026-10-19 21:01–21:02 的 `Application Popup(26)` 记录，失败模块正是
`GamePPSDK\...\GPP64.dll`，状态 `0xc0000428`（`STATUS_INVALID_IMAGE_HASH`）。

**3.1.0 起该策略默认关闭**（理由与证据见 `docs/CHANGELOG.md` 3.1.0 节）：
它会拒绝所有非微软签名的 DLL，包括本程序自己的 `python313.dll`、`_tkinter.pyd`
（签名者为 Python Software Foundation，签名**有效**）。这是"程序损坏"的机制来源。

## 四、正规发布方案（按性价比排序）

### 第 1 步（必做，零成本）：可重复构建 + 哈希清单

- 使用固定版本的工具链（Python 版本、PyInstaller 版本写进 `requirements-dev.txt`）；
- `python tools/build_exe.py` → `python tools/make_release_zip.py`
  （3.1.0 起会自动生成 `dist/SHA256SUMS-<版本>.txt`）；
- 对外发布时**同时给出** zip 和校验清单，让用户能验证"文件没被改过"。

**能解决**：传输损坏、被替换。
**不能解决**：SmartScreen 信誉、杀软误报。

### 第 2 步（推荐）：代码签名证书

需要你（发布者）自己申请并持有证书，我无法代办：

| 证书类型 | 作用 | 现实约束 |
| --- | --- | --- |
| **OV**（组织验证） | 文件显示企业发布者名；SmartScreen 信誉可逐步积累 | 需要营业执照等主体材料；新证书**不会立刻**消除提示 |
| **EV**（扩展验证） | 通常可获得即时较高的 SmartScreen 信誉 | 成本更高，需硬件令牌/云 HSM |

签名做法（拿到证书后）：

```bat
:: 用签名工具给四个 EXE 签名并加时间戳（时间戳保证证书过期后签名仍有效）
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a ^
  dist\CampusNet\CampusNet.exe ^
  dist\CampusNet\CampusNetCLI.exe ^
  dist\CampusNet\CampusNetService.exe ^
  dist\CampusNet\CampusNetSetup.exe
```

注意事项：
- **不要**伪造证书、冒用他人主体，也不要改系统策略；
- 签名要签在**打包之后**（打包会重写 PE 文件）；
- 签完再生成 zip 和校验清单。

**能解决**：显示真实发布者名、显著降低 SmartScreen 拦截、可被用户/安全软件追溯。
**不能解决**：所有杀软一律放行（信誉是累积的，新证书初期仍可能提示）。

### 第 3 步（可选）：向安全厂商提交误报

若确认是误报（有签名、有哈希、能提供样本），走厂商官方渠道提交：

- Microsoft：<https://www.microsoft.com/wdsi/filesubmission>（提交样本，选择"误报"）
- Kaspersky：<https://opentip.kaspersky.com/>（提交文件复核）
- VirusTotal 只用于自查，**不是**申诉渠道。

提交时附：文件 SHA256、下载来源、程序用途说明、无恶意行为的说明（不写"请放行"这种话）。

### 第 4 步：分发方式也会影响提示

- 通过**自己控制的 HTTPS 站点**分发，比网盘/微信/邮件更不容易被加上 MOTW；
- 但任何"下载到本地"的 zip 都可能被浏览器/邮件网关打上 MOTW（`Zone.Identifier`），
  这时"是否签名"就成了决定性因素；
- 因此**不要把本机的"不弹窗"当成结论**：本机没有 MOTW，所以不弹很正常。

## 五、本仓库当前做到了什么 / 还缺什么

| 项 | 状态 |
| --- | --- |
| 版本信息（FileVersion/ProductName/FileDescription） | ✅ 已写入（`packaging/version_info.txt`，构建时自动生成） |
| 发布包 SHA256 校验清单 | ✅ 3.1.0 起自动生成 |
| 无多余可执行文件/脚本/工具副本 | ✅ 实测 0 命中 |
| 不要求管理员权限、不加安全软件排除项 | ✅ 设计如此（自启动走 `HKCU\...\Run`） |
| 无明文凭据（DPAPI 加密、日志脱敏） | ✅ 有自动化测试守护 |
| 可重复构建（固定工具链版本） | ⚠️ 部分：版本已在 requirements 固定，但未做位级可重复构建 |
| 发布者身份可核验 | ❌ 缺：`CompanyName` 只是自声明字符串，无签名证书 |
| 代码签名 | ❌ 缺：需要你自己的 OV/EV 证书 |
| SmartScreen 信誉 | ❌ 未知：本机取证通道不可用，需联网干净机实测 |

## 六、需要你提供的材料（用于闭环）

1. 一台**联网且从未运行过本程序**的 Windows 10/11 机器上，从你实际分发渠道下载 zip 后，
   双击 `CampusNet.exe` 时的截图（要含弹窗标题与按钮）。
2. 同一台机器上执行 `Get-FileHash CampusNet.exe -Algorithm SHA256` 的结果，
   与本仓库 `dist/SHA256SUMS-<版本>.txt` 对照。
3. 若出现杀软拦截：安全软件名称、版本、检测名、被拦文件路径（截图或导出日志）。
4. 若要上签名：证书类型（OV/EV）与获取方式；我据此补充签名步骤与"签后打包"的顺序说明。
