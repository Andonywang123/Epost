# Epost

Epost 是一套由三个 Codex Skills 组成的内容发布工作流：总控 Skill 负责收集素材、看板确认与授权，两个平台 Skill 分别负责小红书图文和 YouTube 英文视频的准备与发布。

## 组成

| Skill | 作用 |
| --- | --- |
| `one-click-publish` | 发布总控、素材收件、看板配置、最终授权与任务交接 |
| `xiaohongshu-publish` | 小红书图文校验、话题/活动匹配、草稿、立即发布与定时发布 |
| `youtube-publish` | YouTube 本地英文化、字幕/封面处理、草稿、立即发布与定时发布 |

这三个目录应一起保留。`one-click-publish` 是入口和协调层，平台准备及实际提交仍由对应的平台 Skill 完成。

## 仓库结构

```text
Epost/
└── skills/
    ├── one-click-publish/
    ├── xiaohongshu-publish/
    └── youtube-publish/
```

每个 Skill 都保留自己的 `SKILL.md`、`agents/openai.yaml`、脚本、资源和参考文档，可独立校验和调用。

## 本地准备

建议使用 Node.js 20 或更高版本、Python 3.10 或更高版本，以及已安装的 Google Chrome。仓库内的 Chrome 启动辅助脚本面向 macOS/zsh；其他系统可自行启动带本地 CDP 端口的专用 Chrome 配置。

安装两个平台包的 Node.js 依赖：

```bash
cd skills/xiaohongshu-publish
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci

cd ../youtube-publish
npm ci --ignore-scripts
```

YouTube 的本地翻译、字幕和封面处理还需要 Python 环境与 `ffmpeg`；具体要求见 `skills/youtube-publish/SKILL.md`。

## 连接总控与平台包

复制 `skills/one-click-publish/assets/config-example.json` 到发布工作目录中的 `config.local.json`，再把其中的占位路径替换为本机绝对路径。配置文件会绑定：

- `skills/xiaohongshu-publish/scripts/upstream_dispatch.py`
- `skills/youtube-publish/scripts/upstream_dispatch.py`
- 对应的 Python/Node 运行时、浏览器配置、账号声明与任务输出目录

本机配置、登录态、令牌、OAuth 客户端文件、发布素材和任务回执不应提交到本仓库。

## 安全边界

两个平台 Skill 都要求针对本次具体任务、动作和时间的明确授权。检查、填充编辑器、保存草稿、立即发布和定时发布是不同动作；不要把一次授权扩展到另一次提交，也不要在结果不确定时自动重复发布。

## 当前范围

- 小红书：图文内容，公开可见，支持平台草稿、立即发布和定时发布。
- YouTube：视频内容，支持本地草稿、立即发布和定时发布；默认使用专用 Chrome/YouTube Studio 路径。
- 其他平台只在总控看板中保留扩展位，未接入真实平台包前不会冒充可用。
