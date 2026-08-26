---
name: semantic-mobile-control
description: 将用户自然语言编译为安全、可审计的 Android 操作，并在龙虾分配的云手机上执行。
version: 0.1.0
---

# 语义手机控制子 Agent

本 Skill 只负责理解任务、生成结构化计划、控制指定 Android 设备和返回执行状态。**设备发现、云机租约、ADB 地址修正、账号归属和屏幕展示由龙虾宿主负责。**

## 何时使用

当用户明确要求在自己的手机、测试机或已分配云手机上执行可见操作时使用，例如：

- “打开美团，准备打车去首都机场。”
- “在高德地图导航到北京南站。”
- “在微信里给张三输入‘十分钟后到’，发送前让我确认。”
- “打开淘宝搜索 1TB 固态硬盘，不要下单。”

不要用于批量骚扰、刷量、规避平台风控、绕过验证码、读取支付口令或操控不属于当前用户/租约的设备。

## 宿主必须提供

1. 当前用户被授权使用的唯一 `device.serial`，例如 `emulator-5554` 或 `10.0.0.8:5555`。
2. 可选的 `device.bridge_port`、`device.bridge_token` 和 `device.appium_url`。
3. 宿主已知但手机界面不需要重复推断的上下文，例如真实地址、WGS-84/GCJ-02 坐标、联系人消歧结果。
4. 每个用户请求稳定的 `idempotency_key`，防止网络重试造成重复叫车、重复发送或重复下单。

严禁让子 Agent 自行从 `adb devices` 中猜测另一台设备；设备缺失时直接向宿主返回错误。

## 调用流程

### 1. 规划或执行

仅需要预览时调用 `mobile_plan`。需要实际操作时调用 `mobile_execute`，保存返回的 `task_id`。

推荐请求：

```json
{
  "instruction": "使用美团打车去首都机场",
  "serial": "emulator-5554",
  "bridge_port": 27183,
  "context": {
    "destination": "北京首都国际机场 T3 航站楼"
  },
  "idempotency_key": "conversation-123:turn-18:ride"
}
```

### 2. 查询进度

调用 `mobile_task_status(task_id)`，根据 `status` 处理：

- `queued` / `planning` / `running`：继续等待宿主下一次轮询，不要重复创建任务。
- `awaiting_confirmation`：向用户展示 `pending_action.description`、目标 App、风险级别和确认原因。
- `succeeded`：总结实际完成的步骤，不要把计划目标冒充已完成结果。
- `failed`：展示错误和最后一个失败动作；宿主可让用户接管或重新下达更明确的指令。
- `cancelled`：停止轮询。

轮询间隔由宿主控制，建议界面动作期间 300–800ms；不要让 LLM 自己高频空转。

### 3. 不可逆动作确认

以下动作必须停在 `awaiting_confirmation`，并由用户在当前对话中明确批准：

- 付款、转账、提交订单、购买、提现。
- 最终呼叫车辆或确认预订。
- 发送消息、拨号、发布内容。
- 删除数据、注销账号、授权敏感权限。

展示确认时必须引用服务端返回的**当前 `pending_action`**，不得根据旧计划自行概括后直接批准。用户批准后调用：

```json
{
  "task_id": "...",
  "approved": true,
  "note": "用户确认呼叫车辆"
}
```

用户拒绝、修改目的地/收件人/金额或长时间无响应时，传 `approved=false` 或调用 `mobile_cancel`，然后创建具有新幂等键的新任务。

## 上下文压缩规则

- 不把整段聊天记录传给手机 Agent，只传最终指令和必要槽位。
- 地址、联系人、商品规格等已经由龙虾解析的值放入 `context`。
- 不传 API Key、登录密码、短信验证码、支付口令、身份证影像或 CAPTCHA 答案。
- 手机 Agent 页面正常时会复用本地成功路径；只有页面偏离才调用规划模型。

## 结果解释

`ActionResult.latency_ms` 是动作下发延迟，不是 App 完整加载、网络请求或业务完成时间。只有 `ok=true` 的实际动作才能表述为已执行；`dry_run=true` 只能表述为计划预演。

## 故障处理

- Bridge 不可用：服务会自动尝试 ADB 和 Appium。
- 中文输入失败：安装并启用 `android-bridge`，不要把中文转成剪贴板或外部输入法泄露。
- 找不到控件：服务最多有限次重新规划；仍失败时返回人工接管，不进行无限点击或坐标猜测。
- 出现登录、验证码、实名认证、支付页：暂停并让用户在可见云机中亲自完成。
