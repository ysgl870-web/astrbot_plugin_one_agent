# AstrBot 待办事项与提醒 Pro

一个面向 AstrBot 的综合待办插件：待办管理、精准提醒、截止时间、搜索统计、签到积分、小游戏，以及针对 QQ Official 的 Markdown / Ark / Keyboard 消息模式。

## 主要能力

### 待办
- `待办 内容 [高/中/低]`
- `待办列表`
- `完成 1`：当前列表第 1 项
- `完成 #12`：永久 ID 12
- `删除 编号`
- `编辑 编号 新内容`
- `改优先级 编号 高`
- `今日待办`
- `逾期待办`
- `搜索 关键词`
- `待办统计`
- `自助代办`
- `清空待办` + `确认清空`

### 提醒
`/提醒 编号 时间` 从原始消息读取完整参数，因此支持：

```text
/提醒 1 14:21
/提醒 1 18点30分
/提醒 1 今天 18:30
/提醒 #12 明天 08:00
/提醒 1 30分钟后
```

仅写 `14:21` 时，如果该时刻已经过去，会自动顺延到次日。提醒到点后由 AstrBot 按 `unified_msg_origin` 主动发回原会话。

### QQ Official

菜单发送方式提供 `auto / text / image / markdown / ark / keyboard / both`。

- `auto` 在 QQ Official 下优先尝试原生 Ark/Keyboard，未配置模板时退到 Markdown。
- `markdown` 使用 AstrBot 当前 `MessageChain.use_markdown(True)` 能力。
- `image` 使用 AstrBot HTML 转图片。
- `ark` 需要 QQ 开放平台已申请的 Ark 模板 ID，并可用 KV JSON 自定义模板变量。
- `keyboard` 需要 QQ 开放平台键盘模板 ID；失败时自动退回 Markdown。

### 小游戏
- `小游戏`
- `猜数字` / `猜数字 50`
- `石头剪刀布 石头`
- `掷骰子 20`
- `猜硬币 正面`
- `幸运抽签`
- `签到`
- `积分榜`

小游戏有冷却和每日次数上限，并将积分、胜场等数据保存于 AstrBot 数据目录。

## 管理

AstrBot 插件配置里可以设置：

- 管理员 QQ 号白名单
- 管理面板权限
- 菜单/提醒发送方式
- QQ Official 原生消息开关、Ark 模板 ID、KV JSON、Keyboard 模板 ID
- 待办数量/内容长度限制
- 提醒轮询与安全设置
- 清空二次确认
- 小游戏开关、冷却、每日次数
- 签到基础积分与随机加成

## 数据

使用 AstrBot `StarTools.get_data_dir("astrbot_plugin_one_agent")` 获取插件专属数据目录，不把运行期数据写入插件源码目录。数据文件为 JSON，并采用临时文件 + 原子替换保存；损坏时会尝试生成 `.corrupt.json` 备份。

## 当前版本

见 `CHANGELOG.md`。

## 灵感与实现参考

插件的消息链、插件元数据、平台支持声明、数据目录以及 QQ Official 消息样式适配均以 AstrBot/QQ 官方当前文档与 SDK 能力为依据。未复制第三方项目业务代码。
