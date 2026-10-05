# astrbot_plugin_one_agent · 待办事项与提醒

一个面向 AstrBot 的轻量待办管理插件，重点优化群聊可读性和提醒稳定性。

## 主要功能

- 待办新增、列表、完成、删除、今日待办、完成率统计
- 统一编号逻辑：`1` 表示当前列表第 1 项；`#12` 表示永久 ID 12
- `/提醒 编号 时间` 一次性提醒，支持 `18:30`、`晚上8点`、`今天 18:30`、`明天 08:00`、完整日期时间
- 提醒数据和待办数据分离存储，设置提醒不会覆盖待办
- 后台定时检查，到点通过原会话主动发送提醒
- 群聊菜单支持 `文字 / 图片 / 文字+图片` 三种模式
- 管理面板支持 QQ 号白名单，只有配置中的管理员可以在会话中唤出管理面板
- 清空待办支持二次确认，避免误删
- JSON 原子写入与损坏数据备份
- 会话使用 `unified_msg_origin` 隔离，避免不同聊天环境互相串数据

## 配置

在 AstrBot WebUI → 插件 → 本插件 → 配置中修改 `_conf_schema.json` 定义的项目。

最重要的几个选项：

- `admin_qq_ids`：填写管理员 QQ 号，例如 `123456789`
- `admin_only_panel`：是否限制“管理面板/管理设置”只能管理员使用
- `menu_send_mode`：`text` / `image` / `both`
- `reminder_send_mode`：提醒发送方式
- `reminder_check_interval`：提醒检查间隔
- `clear_require_confirm`：是否需要“确认清空”二次确认
- `clear_admin_only`：是否限制清空操作只能管理员使用

图片菜单依赖 AstrBot 的文转图能力。未配置或生成失败时，插件会自动回退为文字菜单。

## 常用示例

```text
待办 完成周报 高
待办列表
完成 1
完成 #3
提醒 1 18:30
提醒 #3 明天 08:00
取消提醒 1
代办菜单
管理面板
```

## 数据位置

插件运行数据使用 AstrBot 的插件数据目录，不写入插件自身目录。核心文件为 `todo_data.json`；如果检测到损坏，会尝试备份到 `todo_data.corrupt.json`。

## 安装

将插件目录放进 `data/plugins/astrbot_plugin_one_agent/`，然后在 AstrBot WebUI 中重载插件。

## 开发说明

插件元数据使用 `metadata.yaml`，配置定义使用 `_conf_schema.json`。历史更新记录单独放在 `CHANGELOG.md`，不与 README 混在一起。
