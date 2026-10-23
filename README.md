# campus-mail-task-notify

把耗时任务的结束事件转换为校园邮箱通知，并留下可追溯的耗时日志。工具以 Claude/CodeBuddy 技能的形式分发，
核心是一个只依赖 Python 标准库的命令行程序。

## 特性

- **任务边界驱动**：以进程退出码或任务日志变化作为完成信号，不依赖对话内容推断；
- **双份日志**：人类可读文本日志 + JSON Lines 结构化记录，便于统计与二次处理；
- **校园邮箱 SMTP**：默认 `smtp.dlut.edu.cn:465`（SSL），也支持 STARTTLS 与其他校园邮箱；
- **凭据外置**：账号密码存放在用户级 `KEY=VALUE` 文件，权限 600，不进入版本库；
- **三种驱动方式**：命令包装、日志监视、事后登记；
- **纯标准库**：Python 3.9+，无需安装依赖。

## 安装

```bash
git clone https://github.com/<your-account>/campus-mail-task-notify.git
cd campus-mail-task-notify
./install.sh
```

`install.sh` 会把仓库链接到 `~/.claude/skills/campus-mail-task-notify`，并在存在
`~/.codebuddy/skills` 时建立同样的链接。也可手动复制目录到任一技能目录。

安装后在新的会话中即可通过自然语言触发，例如"训练跑完用校园邮箱发邮件告诉我"。

## 快速开始

```bash
# 1) 生成用户级配置与凭据模板
python3 scripts/task_notifier.py init

# 2) 编辑 ~/.config/campus_mail/credentials.txt，填入发件邮箱与密码/授权码
#    编辑 ~/.config/campus_mail/config.json，在 notify.to 中填写收件邮箱

# 3) 验证 SMTP 链路
python3 scripts/task_notifier.py test-mail
```

包装任务：

```bash
python3 scripts/task_notifier.py run --name 训练主模型 \
    -- python -m scripts.single_step_retrosynthesis.train -c configurations/train.json
```

任务结束后输出状态、耗时与邮件结果，并在运行目录的 `.task_notify/` 下写入日志。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `init [--force]` | 生成配置与凭据模板 |
| `run --name NAME [--attach F] [--email-on always\|failure\|never] [--to ADDR] -- CMD` | 包装命令并计时 |
| `record --name NAME [--duration S] [--started ISO] [--status ...]` | 登记已结束任务 |
| `watch [--interval S] [--settle S] [--once] [--from-start]` | 监视日志变更并通知 |
| `report [--last N]` | 打印最近记录与耗时统计 |
| `test-mail [--to ADDR]` | 发送测试邮件 |
| `show-config` | 打印生效配置（密码打码） |

全局选项：`--config PATH`、`--dry-run`、`--version`。

## 凭据与配置

凭据文件默认位于 `~/.config/campus_mail/credentials.txt`：

```
SMTP_HOST=smtp.dlut.edu.cn
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_SENDER=你的校园邮箱
SMTP_PASSWORD=密码或客户端授权码
```

优先级：环境变量（`CAMPUS_MAIL_HOST`、`CAMPUS_MAIL_PORT`、`CAMPUS_MAIL_SECURITY`、
`CAMPUS_MAIL_SENDER`、`CAMPUS_MAIL_PASSWORD`、`CAMPUS_MAIL_TO`）> 凭据文件 > 配置中的明文值。

配置文件默认位于 `~/.config/campus_mail/config.json`，主要字段：

- `base_dir`：`cwd` 表示日志写在运行目录，也可写绝对路径；
- `notify.to`：收件邮箱列表；`notify.subject_prefix`：主题前缀；
- `paths.*`：日志、JSONL、通知记录与监视状态路径；
- `watch.interval`、`watch.settle`：轮询间隔与静默时间；
- `max_log_bytes`：单个日志文件上限，超过后轮转为 `.1`。

## 设计说明

任务完成信号有三种来源，按可靠性排序：

1. **进程退出**：`run` 直接等待子进程结束，可取得真实退出码与耗时，最可靠；
2. **任务日志变化**：任务由其他工具启动时，让任务写入固定日志，`watch` 轮询该日志；
3. **事后登记**：任务已结束但未记录时，用 `record` 补记。

不使用对话日志作为信号源：对话记录不是任务事件流，无法可靠区分"任务结束"与"对话继续"，
也无法取得退出码与准确耗时。

`watch` 采用轮询而非 inotify，可在 NFS 与容器环境中工作；检测到增长后等待静默再发信，
并用状态文件记录已通知位置，避免重复通知。通知结果写入独立文件，不参与变更监视，防止自触发。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖时长格式化、凭据解析、配置优先级、路径解析、日志写入与轮转、邮件构造与命令行接口，
不访问网络，也不写入用户配置目录。CI 在 Python 3.9–3.13 上运行同一套测试。

## 安全

- 凭据文件权限必须为 600，且不得提交到版本库；
- 程序不会在日志或终端输出密码；
- 邮件正文不包含凭据，只包含任务元数据与日志尾部。

## 许可证

MIT，见 [LICENSE](LICENSE)。
