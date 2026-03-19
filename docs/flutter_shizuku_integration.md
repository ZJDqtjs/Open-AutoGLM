# Flutter + Shizuku 本地 ADB 执行对接文档

本文档用于对接 Open-AutoGLM 新增的"云端仅规划 JSON 指令、端侧本地执行 ADB"能力。

## 1. 改造目标

改造后职责划分如下：

- 云端后端：
  - 接收前端上传的截图和当前状态
  - 调用模型规划下一步动作
  - 返回标准 `command_packet` JSON（不执行任何 ADB 命令）
- Flutter 前端：
  - 调用后端 `local` API 获取下一步指令
  - 使用 Shizuku 在本机执行 `adb shell` 指令
  - 采集执行结果 + 新截图，继续请求下一步

这样可以做到不依赖 USB 数据线，不要求云端直接连设备。

## 2. 新增后端接口

### 2.1 `POST /v1/local/next`

用途：获取下一步动作和本地可执行命令包。

请求头：

- `Content-Type: application/json`
- `x-server-token: <token>`（若后端配置了 `PHONE_AGENT_SERVER_TOKEN`）

请求体字段：

- `user_id` string，必填
  - 前端唯一用户标识（如账号 ID、设备绑定 ID）
  - 后端使用 `user_id + session_id` 做会话隔离

- `task` string，可选
  - 新建会话时必填
  - 续会话时可不传
- `session_id` string，可选
  - 首次请求不传
  - 后续步骤传首次返回的 `session_id`
- `screenshot_base64` string，必填
  - 当前屏幕截图（建议 PNG）base64，不带 data URI 前缀
- `current_app` string，建议必填
  - 前端识别到的当前 App 名称
- `screen_width` int，必填
- `screen_height` int，必填
- `extra_screen_info` object，可选
  - 扩展状态（如电量、网络、页面标记）
- `previous_step_result` object，可选
  - 上一步本地执行结果，建议回传
- 模型配置固定在后端 `local_api_config.json` 中，前端无需再传 `base_url`/`model`/`apikey`

请求示例：

```json
{
  "user_id": "user_10001",
  "task": "打开微信并搜索张三",
  "screenshot_base64": "iVBORw0KGgoAAAANSUhEUgAA...",
  "current_app": "System Home",
  "screen_width": 1080,
  "screen_height": 2400,
  "extra_screen_info": {
    "device_brand": "Xiaomi",
    "android_version": "14"
  }
}
```

后端固定配置文件示例（`local_api_config.json`）：

```json
{
  "base_url": "https://open.bigmodel.cn/api/paas/v4",
  "model": "autoglm-phone",
  "api_key": "your-bigmodel-api-key",
  "lang": "cn",
  "max_steps": 100
}
```

响应示例：

```json
{
  "session_id": "9b2006f7-f302-4509-9f07-6a0ec5ac61a3",
  "step": 1,
  "finished": false,
  "message": "Continue",
  "thinking": "我需要先打开微信...",
  "action": {
    "_metadata": "do",
    "action": "Launch",
    "app": "微信"
  },
  "command_packet": {
    "protocol_version": "1.0",
    "packet_id": "450e73ec-c7ed-4238-9686-b7868ebec25c",
    "timestamp_ms": 1773472230123,
    "session_id": "9b2006f7-f302-4509-9f07-6a0ec5ac61a3",
    "step": 1,
    "finished": false,
    "thinking": "我需要先打开微信...",
    "message": "Continue",
    "agent_action": {
      "_metadata": "do",
      "action": "Launch",
      "app": "微信"
    },
    "execution": {
      "mode": "local_adb_via_shizuku",
      "requires_user_interaction": false,
      "commands": [
        {
          "command_id": "cmd_1",
          "type": "adb_shell",
          "command": "monkey -p com.tencent.mm -c android.intent.category.LAUNCHER 1",
          "capture_output": false,
          "delay_ms_after": 1500
        }
      ],
      "client_actions": [],
      "warnings": []
    }
  },
  "duration_ms": 1290
}
```

### 2.2 `POST /v1/local/reset`

用途：提前结束并清理会话。

请求体：

```json
{
  "session_id": "9b2006f7-f302-4509-9f07-6a0ec5ac61a3"
}
```

响应体：

```json
{
  "session_id": "9b2006f7-f302-4509-9f07-6a0ec5ac61a3",
  "removed": true
}
```

## 3. `command_packet` 执行规范

`execution.commands` 中每一项目前为 `adb_shell` 类型。

字段说明：

- `command_id`: 命令唯一 ID，用于日志和回传
- `command`: 需要在设备本地执行的 `adb shell` 子命令
- `capture_output`: 是否需要采集 stdout/stderr
- `delay_ms_after`: 执行后建议延时

`execution.client_actions` 为客户端动作，不是 adb 指令。当前可能包括：

- `restore_input_method`: 输入完成后恢复原输入法
- `delay`: 纯延时
- `user_interaction`: 需要用户接管
- `sensitive_confirmation`: 敏感操作确认
- `noop`: 无实际执行

`execution.warnings` 为可执行性告警，前端应记录并展示。

## 4. Flutter 端建议执行流程

1. 初始化：获取 Shizuku 权限
2. 首次调用 `/v1/local/next`（携带 `task` + 首帧截图）
3. 依次执行 `command_packet.execution.commands`
4. 执行 `client_actions`
5. 采集新截图、当前 app、执行日志
6. 继续调用 `/v1/local/next`（携带 `session_id`）
7. `finished == true` 时结束循环

建议每步都回传 `previous_step_result`，便于模型感知执行是否成功。

## 5. `previous_step_result` 建议结构

建议前端按如下格式回传（示例）：

```json
{
  "ok": true,
  "executed_at": 1773472230999,
  "commands": [
    {
      "command_id": "cmd_1",
      "command": "monkey -p com.tencent.mm -c android.intent.category.LAUNCHER 1",
      "exit_code": 0,
      "stdout": "",
      "stderr": "",
      "duration_ms": 80
    }
  ],
  "client_actions": [
    {
      "type": "delay",
      "duration_ms": 1500,
      "ok": true
    }
  ]
}
```

## 6. Flutter 伪代码（核心循环）

```dart
Future<void> runLocalAgent(String task) async {
  String? sessionId;
  Map<String, dynamic>? previousResult;

  while (true) {
    final screenshotBase64 = await captureScreenBase64();
    final currentApp = await detectCurrentApp();
    final size = await getScreenSize();

    final body = {
      'user_id': currentUserId,
      if (sessionId == null) 'task': task,
      if (sessionId != null) 'session_id': sessionId,
      'screenshot_base64': screenshotBase64,
      'current_app': currentApp,
      'screen_width': size.width,
      'screen_height': size.height,
      if (previousResult != null) 'previous_step_result': previousResult,
    };

    final resp = await dio.post('/v1/local/next', data: body);
    sessionId = resp.data['session_id'] as String;

    final packet = resp.data['command_packet'] as Map<String, dynamic>;
    final exec = packet['execution'] as Map<String, dynamic>;

    final result = await executePacketViaShizuku(exec);
    previousResult = result;

    if (resp.data['finished'] == true) {
      break;
    }
  }
}
```

## 7. Shizuku 执行建议

- 命令执行方式：建议统一通过 `sh -c` 执行 `adb shell` 子命令
- 对 `capture_output = true` 的命令保留 stdout/stderr
- 对 `delay_ms_after` 必须执行延时，避免 UI 未稳定导致下一步失败
- 对 `restore_input_method`：
  - 从 `settings get secure default_input_method` 命令输出中得到旧 IME
  - 执行 `ime set <old_ime>` 恢复

## 8. 错误处理建议

- HTTP 非 200：重试或中断流程
- 常见状态码建议：
  - `400`: 请求参数错误（如新建会话缺少 `task`）
  - `403`: `user_id` 与 `session_id` 所属用户不匹配
  - `401`: 模型鉴权失败（key 无效或无权限）
  - `429`: 模型服务限流
  - `502`: 上游连接中断（网络/代理/网关）
  - `504`: 上游超时
- 本地命令 exit code 非 0：
  - 记录 stdout/stderr
  - 将失败信息写入 `previous_step_result`
  - 再请求下一步，由模型自适应纠错
- 会话丢失（404 `session_id not found`）：
  - 重新开始新会话

`403` 响应示例（会话归属校验失败）：

```json
{
  "detail": "user_id does not match session owner"
}
```

## 9. 安全建议

- 生产环境必须启用 `PHONE_AGENT_SERVER_TOKEN`
- `api_key` 固定放在后端 `local_api_config.json`，前端不传敏感凭据
- 前端记录执行日志时注意脱敏（输入内容、账户信息）
