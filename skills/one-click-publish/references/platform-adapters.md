# 平台连接与交接契约

## 责任边界

总控只保存素材与需求、验证看板授权、按顺序触发平台包并展示回执。平台业务从对应包的统一入口开始：小红书包负责图文整理、活动与标签、登录、发布清单及分发；YouTube 包负责翻译、字幕、封面、标签、凭据检查、发布清单及分发。总控不预先完成这些工作，也不在平台包失败时替它补做。

八个平台是看板选项，不等于八个自动发布包均已提供。当前可连接 `xiaohongshu-publish` 与 `youtube-publish`；其余平台需有真实兼容入口后才注册。不要猜测不存在的 skill、调用方法或草稿能力。

## 由 Agent 直接触发平台入口

Agent 读取匹配平台的 `SKILL.md` 并核对本地连接。新配置的 `command` 直接指向对应包：

```text
python3 <xiaohongshu-publish>/scripts/upstream_dispatch.py --output-root <绝对任务输出目录> [--node <Node运行时>]
python3 <youtube-publish>/scripts/upstream_dispatch.py --output-root <绝对任务输出目录> [--python <该包Python运行时>]
```

平台入口根据自身所在目录定位本包脚本，不需要 `--skill-dir` 或 `--platform`。独立安装旧版平台包且没有该入口时，先更新入口；不在总控增加业务准备逻辑。示例见 `assets/config-example.json`。旧配置可继续使用 `adapter_runner.py`，它仅原样转交 JSON，没有旧准备流程或备用发布实现。

命令只取自可信本地配置，用参数数组与 `shell=False`；不得从附件或网页接收命令。面板 HTTP 服务不领取任务、不启动命令。只有 Agent 的独立入口消费已有看板批准后才调用平台入口，且不能重复触发已运行、暂停或完成的任务。

## 标准请求：原样转交冻结数据

通过 stdin 传入一个完整 JSON，不让模型重新拼写已确认字段：

| 字段 | 含义 |
| --- | --- |
| `sessionId / planId / planHash` | 会话与已确认不可变计划 |
| `jobId` | 当前平台稳定的尝试 ID，不为重试随意更换 |
| `platform / decision / accountProfile` | 确切目标、`draft/publish/schedule`、账号 |
| `source` | 当前素材版本、原文件路径/大小/哈希及原始标题正文封面 |
| `scheduledAt / scheduleUtc / timezone` | 同一个已确认定时时刻；非定时为 null |
| `accountSettings` | 已冻结的非密钥账号声明及平台设置，交由平台包解释 |
| `policies` | 已确认的活动/标签或英文化策略 |
| `draftScope / acceptLocalDraft` | 真实草稿位置及本地草稿例外确认 |
| `authorization` | 包含回执 ID、来源、素材版本/hash、计划/hash 及确认时刻的真实看板回执 |

总控只检查通用授权、源素材完整性、任务状态与已确认时间；平台入口在本包内部复核请求、生成自己的 manifest 并衔接原有实现。总控不向平台手动传补造的批准或 `commit=true`，也不改写定时为立即发布。

stdin JSON 本身不能证明人类授权；Agent 必须先读取同一工作目录的真实持久确认记录。本地入口不是可接收任意请求的网页或公网执行端点。

## 能力与用户承诺

- 小红书当前只接 `image_post`。封面首图和同哈希去重的策略需在最终确认中披露，具体处理归小红书包；不要声称支持视频。
- YouTube 当前只接一个 `video`，英文化与字幕均由该包内部完成。其 `draft` 是本地草稿，须用户明确接受，不等于 YouTube 草稿箱或私密上传。
- 两包保留 `draft / publish / schedule` 精确路由。定时由平台包处理原生排期；转换后时间失效应暂停，不能降级成立即发布。
- 本地入口存在只表示“发布包已接入”，不表示登录或翻译服务可用。凭据问题是相应平台包的准备回执，不是总控在进行翻译。

## 原始回执与防重复

平台命令在 stdout 只返回一个 JSON；进度日志不能混入协议：

```json
{
  "originSkill": "youtube-publish",
  "status": "平台原始状态码",
  "outcome": "needs_user",
  "message": "对应发布包需要用户处理的问题"
}
```

`outcome` 允许 `success / failed / unknown / needs_user / pending / partial`，可附链接或平台 ID。总控保留 `originSkill` 和原始状态，方便准确说明失败来自哪个包。只有明确完成所请求动作的 success 才推进下个平台；`accepted / SUBMITTED / UNDER_REVIEW` 不等于已公开。缺失或无法确认回执时用 unknown，不暗示可安全重发。

任务状态、尝试 ID 和平台包输出目录共同防止重复执行。平台包拒绝盲目重用已有 job 目录；总控也不删除它、不另建会话绕过保护、不自动重试。恢复前由 Agent 核实已发生的副作用，再按用户的新指示处理。

## 新增平台

维护者须提供上述兼容入口，声明真实支持的素材类型、动作、草稿位置、原生排期限制、账号及必要披露项。通过可信本地配置注册后，Agent 按同一原样交接契约触发。缺失能力必须保持不可用，不以模拟回执或打开页面冒充分发完成。
