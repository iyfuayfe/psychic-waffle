# 第三方代码与许可证说明

本项目主干（`campusnet/`、`entry.py`、`tests/`）是本项目自己写的。
运行期**不依赖任何第三方 Python 包**，只使用 Python 标准库与 Windows 自带 API。

以下部分参考或改写了别人的开源代码，按许可证要求在此说明。
**如果某项的实际许可证本文档没有核实到，会明确标注为"待确认"，不做推测。**

---

## 1. CampusConnect（MIT）

- 仓库：https://github.com/Ye9137/CampusConnect
- 本项目使用位置：`campusnet/portal_js.py`
  —— 注入到认证页的填写脚本，其**启发式思路**参考了该项目的
  `Windows/source/PortalTemplate.js` 与 `Windows/source/PortalAgreements.js`
  （在页面里定位账号 / 密码输入框、勾选必选协议、选择运营商、点击登录，
  并把状态字符串交回宿主程序）。
- 本项目做了哪些改动：按自己的配置结构与状态机重写并精简，去掉了针对特定学校的
  初始化加速逻辑、自定义下拉菜单兼容代码；新增 `wrong-origin` / `insecure-origin` /
  `page-error` 三个安全状态，以及"不带凭据的探针脚本"（`build_probe`）。
- 许可证：MIT（原文如下，必须随本项目一起分发）

```
MIT License

Copyright (c) 2026 Ye

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. Dr.COM / 哆点 ePortal 协议实现

`campusnet/drcom.py` 与 `campusnet/aes.py` 的协议细节来自**对门户公开下发的
JavaScript 的阅读**（门户页 `a40.js` / `a41.js` / `a59.js`，属于学校 / 服务商
下发给浏览器的客户端代码），不是复制其它开源项目。

其中 `aes.py` 里的固定密钥 `5c1d5ad4dea0e8dd` 是**门户 JS 自带**的协议常量
（`util.aes_en` 的默认 key），用途是把登录参数按服务端要求加密，
**与本地密码存储无关**（本地密码用的是 Windows DPAPI）。

正确性验证方式：

1. FIPS-197 官方测试向量（`tools/test_aes.py`）；
2. 与门户自己的 CryptoJS 输出逐字节比对（`tools/test_aes.py --node`，需要本机有 node）。

本项目的 AES 实现为独立编写（纯 Python，无第三方库）。

---

## 3. 构建期依赖（不随程序分发为运行依赖）

| 组件 | 版本（本次验证） | 许可证 | 用途 |
| --- | --- | --- | --- |
| PyInstaller | 6.22.3 | GPL-2.0-or-later **WITH Bootloader-exception** | 打包 EXE |
| pyinstaller-hooks-contrib | 2026.8 | Apache-2.0 | PyInstaller 钩子 |
| altgraph | 0.17.5 | MIT | PyInstaller 依赖 |
| pefile | 2024.8.26 | MIT | PyInstaller 依赖 |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | PyInstaller 依赖 |
| packaging / setuptools | 26.3 / 84.0.0 | Apache-2.0 / MIT | 构建工具链 |

**关于 GPL**：PyInstaller 的 bootloader 例外条款允许分发由它打包生成的程序，
因此本项目可以按 MIT 发布。许可证全文见 https://pyinstaller.org/en/stable/license.html

---

## 4. 随发布包分发的第三方文件

| 文件 / 组件 | 来源 | 许可证 | 说明 |
| --- | --- | --- | --- |
| Python 运行时（`python313.dll` 等） | Python Software Foundation | PSF License | 由 PyInstaller 收集，随 `_internal` 分发 |
| Tcl/Tk 8.6（`tcl86t.dll`、`tk86t.dll`、`tcl8/`） | Tcl/Tk 项目 | Tcl/Tk License（BSD 风格） | 图形界面运行时 |
| PyInstaller bootloader | PyInstaller | GPL-2.0-or-later WITH Bootloader-exception | EXE 引导程序 |
| Visual C++ 运行时（如被收集） | Microsoft | Microsoft 可再分发运行时条款 | 视构建环境而定 |

这些组件的许可证文本由各自项目提供；本仓库不重复收录其全文，
如需完整文本请查阅对应项目。**本文件不声称对整个发布包做了逐文件的许可证审计。**

---

## 5. 待确认事项

- `_internal` 中若包含由本机 Python 发行版带入的额外组件（例如某些 MSVC 运行库版本），
  其分发条款请以该发行版自带说明为准，本次未逐项核对。
- 未使用 UPX 压缩，因此不涉及 UPX 的许可证与误报问题。

---

## 6. 本项目许可证

MIT，全文见 [LICENSE](LICENSE)。该许可证与上述 MIT / Apache-2.0 / BSD /
PSF / Tcl-Tk 组件兼容；GPL 组件（PyInstaller bootloader）因带 bootloader 例外
而不影响本项目的 MIT 授权。
