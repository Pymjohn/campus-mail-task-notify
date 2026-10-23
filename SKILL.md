---
name: campus-mail-task-notify
description: 通过校园邮箱把"任务结束"变成一封自动邮件：包装命令并计时、写日志、任务结束或日志发生变化时经校园 SMTP 发送通知。只要用户提到任务跑完通知、训练或长时间计算结束后发邮件、用学校/校园邮箱发提醒、任务完成提示自动化、监控日志变化发邮件、把耗时结果发到邮箱等意图，就应使用本技能，即使没有明确说出 "SMTP"、"脚本" 或 "邮箱"。
---

# 校园邮箱任务结束通知

把耗时任务的结束事件转换为外部可见的邮件提醒，并留下可追溯的耗时日志。核心工具是
`scripts/task_notifier.py`，只依赖 Python 标准库（3.9+），不需要安装任何包。

本文件所在目录即技能目录，下文用 `$SKILL_DIR` 指代。

## 何时使用

- 用户说"跑完告诉我""训练结束发邮件给我""任务完成提示""算完提醒我"；
- 用户希望用校园邮箱（默认大连理工 `smtp.dlut.edu.cn:465`，SSL）接收通知；
- 用户希望记录任务起止时间、耗时，并在结束时自动汇报；
- 用户已有长时间运行的进程，希望监视日志变化后发信；
- 用户想把任务结果文件作为附件发到邮箱。

## 工作流程

### 第一步：确认三要素

需要收件邮箱、发件账号、密码或客户端授权码。若用户未提供，直接询问，不要猜测或编造。

若本机已存在 `~/.config/campus_mail/credentials.txt`，优先复用；只有用户要求更换账号时才重新配置。

### 第二步：初始化与冒烟测试

```bash
python3 "$SKILL_DIR/scripts/task_notifier.py" init
python3 "$SKILL_DIR/scripts/task_notifier.py" show-config   # 确认发件人、收件人、凭据路径
python3 "$SKILL_DIR/scripts/task_notifier.py" test-mail     # 真实发送一封，确认链路可用
```

`init` 会创建 `~/.config/campus_mail/config.json` 与权限 600 的凭据模板，不会覆盖已存在的凭据。
收件人在配置 `notify.to` 中填写，也可用环境变量 `CAMPUS_MAIL_TO` 临时指定。

发送成功后再进入任务执行阶段。若返回 `553 authentication is required`，说明密码缺失或错误；若返回
`535`，通常是授权码无效。校园邮箱通常要求使用客户端授权码而非网页登录密码。

### 第三步：按任务驱动方式选择命令

**A. 任务由当前会话启动** —— 用 `run` 包装，退出码原样透传：

```bash
python3 "$SKILL_DIR/scripts/task_notifier.py" run --name 训练主模型 \
    -- python -m scripts.single_step_retrosynthesis.train -c configurations/train.json
```

需要附加结果文件时加 `--attach 路径`，可重复；只在失败时发信加 `--email-on failure`；
临时更换收件人加 `--to 地址`；命令含管道或重定向时加 `--shell`。

**B. 任务已在别处运行** —— 用 `watch` 监视其日志文件：

```bash
python3 "$SKILL_DIR/scripts/task_notifier.py" watch --interval 5 --settle 15
```

`watch` 轮询日志文件，检测到增长后等待静默再发信，避免一次任务拆成多封邮件。首次启动默认从文件末尾
开始，需要处理已有内容时加 `--from-start`；配合 cron 时用 `watch --once`。

**C. 任务已经结束** —— 用 `record` 补记：

```bash
python3 "$SKILL_DIR/scripts/task_notifier.py" record --name 数据预处理 --duration 813.5 --status success
```

`--started` 与 `--finished` 可传入 ISO 时间，程序会自动计算耗时；`--note` 写入备注。

### 第四步：汇报结果

任务结束后向用户说明：任务名称、开始与结束时间、耗时、退出码、邮件是否发送成功、日志文件路径。
不要只说"已完成"，要把耗时和邮件结果读出来。可用 `report --last N` 查看历史记录与合计耗时。

## 凭据与安全

- 凭据文件为 `KEY=VALUE` 格式，包含 `SMTP_HOST`、`SMTP_PORT`、`SMTP_SECURITY`、`SMTP_SENDER`、
  `SMTP_PASSWORD` 五项；
- 权限必须为 600，且不得写入项目仓库、不得放入技能目录；
- 任何时候都不要在回复、日志或命令输出中打印密码；
- 优先级：环境变量（`CAMPUS_MAIL_*`）> 凭据文件 > 配置文件明文字段；
- 换用其他校园邮箱时，只改凭据文件中的 host、port 与 security，无需改代码。

## 安装与更新

本技能可作为独立项目分发。在新机器上安装时，克隆仓库后执行 `./install.sh`，脚本会把仓库链接到
`~/.claude/skills/` 与 `~/.codebuddy/skills/`（若存在）。更新时在仓库目录执行 `git pull`，软链接
指向同一份源码，无需重新安装。

## 配置与路径

默认配置位于 `~/.config/campus_mail/config.json`，可设置：

- `base_dir`：`cwd` 表示日志写到当前工作目录（默认），也可写绝对路径；
- `notify.to`：收件邮箱列表；`notify.subject_prefix`：邮件主题前缀；
- `paths.*`：日志、结构化记录、通知记录与监视状态的路径，默认在 `./.task_notify/`；
- `watch.interval` 与 `watch.settle`：轮询间隔与静默时间。

日志为双份：`task_notifier.log` 供人阅读，`task_notifier.jsonl` 供程序统计；通知结果单独写入
`task_notifier_notify.log`，不参与变更监视，避免通知自触发。

## 常见问题

- **收件方没有收到**：先查垃圾邮件目录，校园邮箱发往外部域名常被拦截；再用 `test-mail --to 地址` 单独验证。
- **`未配置收件人`**：在配置 `notify.to` 中填写，或设置 `CAMPUS_MAIL_TO`。
- **凭据文件不存在**：检查 `show-config` 输出的凭据路径；可用 `CAMPUS_MAIL_CREDENTIALS` 指向已有文件。
- **`watch` 重复发信**：删除 `./.task_notify/.task_notifier_state.json` 后重启，或确认 `run` 与 `watch` 没有同时监视同一日志。
- **任务被 Ctrl+C 中断**：`run` 会记录 `任务中断` 状态并仍尝试发信，退出码为 130。
