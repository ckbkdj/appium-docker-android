# Semantic Mobile Agent Android Bridge

这是可选的低延迟执行桥。它使用 Android `AccessibilityService` 直接读取当前 UI 树并执行点击、中文输入、手势和全局返回操作。

## 安全边界

- Manifest **不申请 `INTERNET` 权限**。
- 服务只监听 Android 本机抽象套接字 `semantic_mobile_agent`。
- Python 端通过指定设备的 `adb forward tcp:<port> localabstract:semantic_mobile_agent` 建立连接。
- 可以设置设备端令牌；配置后每条命令都必须携带相同令牌。
- 仍应只在自有、授权测试或云手机环境中启用无障碍控制。

## 构建

工程没有提交 Gradle Wrapper 二进制 JAR。使用 Android Studio 打开本目录，或使用本机已安装、与 Android Gradle Plugin 兼容的 Gradle：

```bash
cd android-bridge
gradle :app:assembleDebug
```

APK 输出位置：

```text
app/build/outputs/apk/debug/app-debug.apk
```

安装：

```bash
adb -s emulator-5554 install -r app/build/outputs/apk/debug/app-debug.apk
```

## 启用服务

在手机上打开：

```text
设置 → 无障碍 → 已安装的应用 → Semantic Mobile Agent Bridge
```

在专用模拟器/云机镜像中也可以由镜像初始化脚本预启用服务。不要在个人主力手机上无提示地自动启用。

## 可选令牌

在模拟器、root 云机或允许 `adb shell settings` 写入的设备上：

```bash
TOKEN="$(openssl rand -hex 24)"
adb -s emulator-5554 shell settings put secure semantic_mobile_agent_token "$TOKEN"
export SMA_BRIDGE_TOKEN="$TOKEN"
```

清除令牌：

```bash
adb -s emulator-5554 shell settings delete secure semantic_mobile_agent_token
```

## 连通性

Python 服务会自动建立 ADB 转发并发送 `ping`。也可手工验证套接字：

```bash
adb -s emulator-5554 forward tcp:17300 localabstract:semantic_mobile_agent
printf '{"id":"1","command":"ping"}\n' | nc 127.0.0.1 17300
```

配置令牌后，在 JSON 中增加 `"token":"..."`。

## 协议

每行一个 JSON 对象，每个响应也占一行。当前命令：

- `ping`
- `snapshot`
- `click`（节点路径）
- `set_text`（节点路径，支持中文）
- `tap`
- `swipe`
- `global`（BACK/HOME/RECENTS）
- `open_app`
- `installed_apps`

节点路径由当前快照生成，例如 `0/2/1`。路径只在同一 UI 状态内有效；Python 执行器会用状态哈希和短 TTL 防止复用过期节点。
