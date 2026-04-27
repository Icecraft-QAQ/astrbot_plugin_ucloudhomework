# astrbot_plugin_ucloud_homework

AstrBot 插件 —— 查询北邮 UCloud 未完成作业，支持指令查询与定时推送。

## 功能

- `/homework` — 手动查询当前未完成作业清单
- 定时推送 — 配置 Cron 表达式后，自动向指定群/私聊推送作业提醒

## 安装

将本目录放入 AstrBot 的 `data/plugins/` 下，重启或 WebUI 重载插件。

## 配置

在 AstrBot WebUI 插件配置页填写：

| 配置项 | 说明 |
|--------|------|
| `username` | 北邮统一身份认证学号 |
| `password` | 北邮统一身份认证密码 |
| `cron_expression` | Cron 表达式，如 `0 8 * * *`（每天 8:00），留空则不启用定时推送 |
| `push_session` | 推送目标会话 ID，通过 `/sid` 指令获取，格式如 `aiocqhttp:GroupMessage:123456` |

## License

[MIT](LICENSE)
