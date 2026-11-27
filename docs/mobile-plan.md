# 一键签手机版（OneDuo Mobile）方案

状态：方案阶段。签名核心已通过 iOS 编译检查，其余部分未实现、未在真机上跑过。

## 目标
在手机上完成签名、安装和续签，不需要电脑，也不需要 NAS。只有第一次安装需要电脑。

## 已验证（2026-11-24）
- `cargo check -p plume_core --features tweaks --target aarch64-apple-ios` 结果 EXIT=0：签名核心能编译到 iPhone 平台
- Apple ID 登录用远程 anisette（`remote-anisette-v3`，默认 `mac-ani.jkcoxson.com`），不依赖电脑上的苹果组件
- 设备通信库 `idevice`（MIT）自带 `TcpProvider`（直连 IP + 配对文件）和 Swift/C 绑定，iOS 上可以用
- Impactor（MIT）、idevice（MIT）都能商用闭源；**不引用 SideStore 的代码（AGPL）**

## 架构
```
┌ OneDuo Mobile (SwiftUI) ───────────────────────────┐
│ 首页：App 列表 / 到期天数 / 一键续签                 │
│ 快捷指令动作：续签全部（App Intents）                 │
│        │ C 接口（cbindgen）                          │
│ ┌ oneduo_ffi.a (Rust) ─────────────────────────┐   │
│ │ plume_core：登录 / 证书 / 描述文件 / 签名       │   │
│ │ plume_utils：重签 IPA                         │   │
│ │ idevice TcpProvider ──> 10.7.0.1:62078 ─┐     │   │
│ └─────────────────────────────────────────┼─────┘   │
│ 回环 VPN：付费签的版本内置 PacketTunnel 扩展； │         │
│   免费 ID 版本用 App Store 的 LocalDevVPN ──┘         │
└──────────────── iOS lockdownd / installation_proxy ─┘
```
原理：手机通过回环 VPN 连到自己的 lockdownd，带着配对文件，系统就把它当成"已信任的电脑"，再调用 installation_proxy 装包。SideStore 和 StikDebug 用的是同一套原理。

## 模块拆分
1. **`crates/oneduo_ffi`（新）**：用 Rust 包一层 C 接口，提供这些函数：`login / submit_2fa / sign_ipa / install_ipa / list_installed / renew_all`，进度通过回调返回。编译成 `aarch64-apple-ios` 静态库，打包为 xcframework
2. **改 `plume_utils::Device::install_app`**：现在写死只走 usbmuxd，要加一个 `TcpProvider` 分支（地址 10.7.0.1，用导入的配对文件）
3. **`ios/OneDuoMobile`（新，XcodeGen）**：主 App 加 `PacketTunnel` 扩展。扩展做的事只有一件：把 10.7.0.1 的流量原样转回本机
4. **桌面版加「搬到手机」**：沿用 `nasmove.py`，打包三样东西：配对记录、Apple ID 登录态（`~/.config/PlumeImpactor`）、安装记录。通过 AFC 写进手机版的 Documents（house_arrest）。**沿用同一张证书，不再申请新证书**，否则会挤掉桌面版签过的 App
5. **续签调度**：用 App Intents 提供「续签全部」，用户在快捷指令里设每天自动跑；再加一个后台任务（BGProcessingTask）作兜底。逻辑：找出离到期不到 3 天的 App，**先续其他 App，最后续自己**

## 自我续签
- 免费 Apple ID 签的版本：最后一步把自己重新签一遍，再交给 installation_proxy。系统换新版时会把它杀掉，但安装由系统接手、会装完。重新打开时检查到期日确认续签成功，失败就通知用户
- 付费证书签的版本（自己人和登记过 UDID 的朋友）：有效期一年，不用续自己

## 风险（Demo 阶段逐项验证）
| 风险 | 验证方法 | 不通的后果 |
|---|---|---|
| iOS 26.4+ 改了配对和回环行为 | iPhone19,4 实测 | 致命，换方案 |
| 远程 anisette 服务器挂了或被墙 | 自建 anisette-v3（Docker 放 NAS） | 可以修 |
| 自签时被系统杀掉，安装半途中断 | 用自签 Demo 反复跑 10 次 | 有风险，要设计回滚 |
| 免费账号每周只能申请 10 个 App ID | 只签 DouDuo / GuoDuo，复用 App ID | 能控制 |
| VPN 扩展要付费账号才有的能力，免费 ID 签的包带不了 | 免费 ID 用户改用 App Store 上现成的 LocalDevVPN（idevice 作者出的，SideStore 也在用）；后续可以自己上架一个只做回环的 VPN App | 已有现成解法 |

## 里程碑
- **M0 技术验证（约 1 天）**：做 FFI 和最小 SwiftUI 界面，配对文件手动导入，在测试机上完成「登录 → 签 DouDuo → 经回环装上」
- **M1（2～3 天）**：自我续签、快捷指令动作、桌面版「搬到手机」
- **M2（2 天）**：界面和桌面版对齐，补教程和官网页面，接进 Gitee 发版流程

## 给别人用的操作流程（目标体验）
**第一次（约 5 分钟，需要电脑和数据线）**
1. 电脑上装一键签（官网下载，现有流程）
2. 手机用线连电脑，点「信任」
3. 一键签里登录 Apple ID（建议用小号），输双重认证验证码
4. 点「装手机版」：一键签自动装好手机版，并把配对文件和登录态一起搬过去

**手机上（约 1 分钟）**
5. 打开手机版，按提示允许添加 VPN 配置（一次性）。用免费 ID 的人还要先去 App Store 装一个 LocalDevVPN（免费），打开它点连接
6. 按提示添加快捷指令自动化：每天 3:00 运行「续签全部」，选立即运行（一次性，教程放截图）

**之后**
- 平时什么都不用管，每天半夜自动续签
- 装新 App：在手机版里点一下就行
- 要回电脑重来的情况：刷机或升级大版本、点过「还原位置与隐私」、Apple ID 改了密码、手机连续 7 天续签都失败
