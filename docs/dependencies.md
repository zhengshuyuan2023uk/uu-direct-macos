# 依赖版本与下载校验

最后核对：2026-10-13。本项目分发原创源码与配置模板；第三方程序从官方发布地址单独取得，不放入仓库或本项目的发布压缩包。第三方许可证见 [NOTICE](../NOTICE.md)。

## 固定下载清单

| 程序 | 架构 | 官方文件 | 字节数 | SHA-256 |
| --- | --- | --- | ---: | --- |
| sing-box 1.14.3 | Apple Silicon / arm64 | [sing-box-1.14.3-darwin-arm64.tar.gz](https://github.com/SagerNet/sing-box/releases/download/v1.14.3/sing-box-1.14.3-darwin-arm64.tar.gz) | 29163190 | `0360ce634a04c26a6b4fd957517987bb4f6c8950f63435ce3c5af2fcae5860c5` |
| sing-box 1.14.3 | Intel / amd64（x86_64） | [sing-box-1.14.3-darwin-amd64.tar.gz](https://github.com/SagerNet/sing-box/releases/download/v1.14.3/sing-box-1.14.3-darwin-amd64.tar.gz) | 31473185 | `6d002b74d03b478547349cc10af89e007868596e5aa931816d45da9f544715a9` |
| ProxyBridge macOS 4.0.0 | arm64 + x86_64 通用包 | [ProxyBridge-v4.0.0-Universal-Installer.pkg](https://github.com/InterceptSuite/ProxyBridge/releases/download/v4.0.0/ProxyBridge-v4.0.0-Universal-Installer.pkg) | 2205823 | `df64a77b9bd1a6f9d41b4c7e2169dbf86c23441c6751b65a0a55c016a6253d0a` |

上述摘要直接核对自官方 GitHub Release API 的 `assets[].digest`：[sing-box](https://api.github.com/repos/SagerNet/sing-box/releases/tags/v1.14.3)、[ProxyBridge](https://api.github.com/repos/InterceptSuite/ProxyBridge/releases/tags/v4.0.0)。GitHub 的 [资产 API 文档](https://docs.github.com/en/rest/releases/assets)说明了该字段和官方下载地址。

下载后、解压或安装前，可用系统命令 `shasum -a 256 <文件路径>` 检查文件。摘要不匹配就停止；不要为了继续安装而更新期望摘要。固定摘要用于识别审查过的文件，不等同于完整安全审计。更新依赖时应同时复核版本、官方 URL、摘要、许可证、配置兼容性和实际网络行为。

本方案使用 sing-box 的命令行核心，不需要 SFM 图形客户端。Apple Silicon 应使用 arm64 包，Intel 应使用 amd64 包；有官方 Intel 资产不代表本项目已经完成 Intel 实机验证。

## 系统要求与已验证范围

- **ProxyBridge 的官方要求是 macOS 13.0 或以上。** 固定 Mac 源码的部署目标为 13.0，架构为 arm64 与 x86_64。[官方安装文档](https://proxybridge.interceptsuite.com/docs/installation-macos/)、[固定项目配置](https://github.com/InterceptSuite/ProxyBridge/blob/b97c7ffe683ab62a6782edbebf8a3646655bdf19/MacOS/ProxyBridge/ProxyBridge.xcodeproj/project.pbxproj)
- **本方案目前已在 macOS 14.5 / Apple Silicon 验证。** Intel 和其他系统版本待实机验证。这是方案的测试范围，不是所有 macOS 13+ 的兼容保证。
- **sing-box 1.14.3 的最低版本元数据需要谨慎解释。** 普通 Darwin 构建使用 Go 1.26.8、启用 CGO，且没有显式设置部署目标。Go 1.26 的系统底线为 macOS 12，但不能据此认定这个成品的最低系统版本。[固定构建流程](https://github.com/SagerNet/sing-box/blob/v1.14.3/.github/workflows/build.yml#L469)、[Go 1.26 Darwin 说明](https://go.dev/doc/go1.26#darwin)
- 对上表摘要一致的既有 arm64 发布包进行只读检查，二进制显示 `LC_BUILD_VERSION minos 26.0 / sdk 26.5`；该包此前又已在 macOS 14.5 运行成功。这项元数据与运行结果的差异尚未解释，因此不把 12、13 或 26 中任一数值宣称为整个方案的已验证最低版本。该二进制的 SHA-256 为 `28b7200074d6380006acc4f035a404c8af3dceb65dd92d7386ed92c9224e08a2`，可供后续复核。

sing-box 另有 Intel `legacy-macos-10.13` 官方包，但它不能降低 ProxyBridge 的系统要求，本项目当前下载清单不使用该兼容分支。

## ProxyBridge 的发布与签名

v4.0.0 发布页同时承载 Windows 与 Mac：Windows 于 2026-06-25 发布，Mac 于 2026-07-05 发布；本项目固定的 Mac 包更新时间为 `2026-07-05T17:43:52Z`。因此不要直接把较早的 v4.0.0 tag 源码当作 Mac 安装包的精确构建输入。[官方发布说明](https://github.com/InterceptSuite/ProxyBridge/releases/tag/v4.0.0)

既有安装前核查已确认这个准确安装包的 Developer ID Installer 为 Sourav Kalal（Team ID `L4HJT32Z59`），并通过 Apple 公证检查。新机器仍应按系统正常流程检查安装包签名、批准系统扩展；不要移除签名、关闭 SIP 或绕过 Gatekeeper。可用 `pkgutil --check-signature <安装包路径>` 检查签名。[官方安装与校验说明](https://proxybridge.interceptsuite.com/docs/installation-macos/)

上游的 [固定 Mac 构建流程](https://github.com/InterceptSuite/ProxyBridge/blob/b97c7ffe683ab62a6782edbebf8a3646655bdf19/.github/workflows/build-mac.yml)包含签名、公证和 stapling；这不能代替对实际下载文件的校验，也不构成源码到二进制的可复现构建证明。

## 许可边界

本项目的 MIT 仅覆盖原创内容。sing-box 的上游许可为 GPL 第 3 版或之后版本，并附名称/关联条件；ProxyBridge 采用 MIT。完整版权归属和版本化许可证链接集中在 [NOTICE](../NOTICE.md)。

当前脚本通过命令行、配置文件和 SOCKS 协议使用独立程序，不复制或链接其内部源码。GNU 对独立程序、聚合与组合程序的判断还考虑通信语义，不能仅用“分别下载”推导所有未来版本的法律结论。[GNU 说明](https://www.gnu.org/licenses/gpl-faq.html#MereAggregation)

若未来附带第三方二进制、修改第三方程序或复制代码，须重新核对其分发条件。尤其分发 GPL 二进制时，NOTICE 不能替代对应源码和其他许可要求。[GPL v3](https://www.gnu.org/licenses/gpl-3.0.html)
