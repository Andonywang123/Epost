# Epost

[中文](#中文) · [English](#english)

## 中文

### 简介

Epost 是面向海内外热门社交平台的一键发布与管理工具，帮助自媒体创作者跨平台发布自己的作品。Epost 可自动完成语言翻译与字幕烧录，并支持不同时区的一键发布设置。

Epost 由三个相互配合的 Codex Skills 组成：

| Skill | 作用 |
| --- | --- |
| `one-click-publish` | 总控入口：素材收件、智能区分、发布看板、最终授权与任务交接 |
| `xiaohongshu-publish` | 小红书图文：校验、话题/活动匹配、草稿、立即发布与定时发布 |
| `youtube-publish` | YouTube 视频：本地英文化、字幕/封面处理、本地草稿、立即发布与定时发布 |

三个目录需要一起安装，但不要把它们的内部文件扁平合并。`one-click-publish` 是 Epost 的入口与协调层，平台准备和实际提交由对应的平台 Skill 完成。

### 安装

最简单的方式是在 Codex 中调用 `$skill-installer`，让它从本仓库安装以下三个目录：

- `skills/one-click-publish`
- `skills/xiaohongshu-publish`
- `skills/youtube-publish`

也可以手动安装：

```bash
git clone https://github.com/Andonywang123/Epost.git
mkdir -p ~/.agents/skills
cp -R Epost/skills/one-click-publish ~/.agents/skills/
cp -R Epost/skills/xiaohongshu-publish ~/.agents/skills/
cp -R Epost/skills/youtube-publish ~/.agents/skills/
```

Codex 通常会自动发现新安装的 Skill；若未出现，请重启 Codex。详见 [Codex Skills 官方说明](https://learn.chatgpt.com/docs/build-skills)。

### 使用步骤

1. 在本地 Agent 应用中安装上述三个 Skill。
2. 直接对 Agent 说“**一键发布图文**”或“**一键发布视频**”，即可启动 Epost。
3. 按提示上传自己的图片或视频，再提供封面、标题和文案。Epost 会原样储存素材、智能区分用途，并保留文件顺序。
4. 素材核对完成后，Epost 会打开发布看板。你可以选择目标平台和账号，并设置发布时间、时区、系统偏好及发布类型（立即发布、定时发布或平台支持的草稿）。
5. 核对最终摘要并点击确认后，系统会按已确认的设置逐个平台完成准备与提交，并在看板中返回真实进度和回执。
6. 第一次使用某个平台时，需要在专用浏览器中登录对应账号。登录状态有效期间，之后的发布通常可以自动完成；登录过期、验证码或平台安全校验出现时，仍需要用户重新处理。

### 平台范围

Epost 的目标平台包括小红书、抖音、微信视频号、微博、YouTube、TikTok、Instagram（ins）和 X 等，可由用户自由选择。

**当前版本只实际接入：**

- 小红书：图文内容，支持平台草稿、立即发布和定时发布。
- YouTube：视频内容，支持本地草稿、立即发布和定时发布，并可进行本地语言翻译、字幕生成/烧录和封面英文化。

抖音、微信视频号、微博、TikTok、Instagram、X 等平台会在后续版本中逐一完善。未接入真实发布包的平台不会被显示为可执行状态。

### 本地运行准备

建议使用 Node.js 20 或更高版本、Python 3.10 或更高版本，以及 Google Chrome。仓库内的 Chrome 启动辅助脚本面向 macOS/zsh；其他系统可自行启动带本地 CDP 端口的专用 Chrome 配置。

```bash
cd skills/xiaohongshu-publish
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci

cd ../youtube-publish
npm ci --ignore-scripts
```

YouTube 的本地翻译、字幕和封面处理还需要 Python 依赖及带 libass 字幕滤镜的 `ffmpeg`，具体要求见 `skills/youtube-publish/SKILL.md`。

复制 `skills/one-click-publish/assets/config-example.json` 到发布工作目录中的 `config.local.json`，把占位路径替换为本机绝对路径，并配置对应的运行时、浏览器、账号声明与任务输出目录。本机配置、登录态、令牌、OAuth 文件、发布素材和任务回执不应提交到仓库。

---

## English

### Overview

Epost is a one-click publishing and management tool for leading social platforms in China and worldwide. It helps independent creators publish their work across platforms, automatically handles language translation and burned-in subtitles, and supports timezone-aware one-click publishing settings.

Epost consists of three Codex Skills that work together:

| Skill | Purpose |
| --- | --- |
| `one-click-publish` | Main entry point for asset intake, intelligent classification, the publishing dashboard, final approval, and task handoff |
| `xiaohongshu-publish` | Xiaohongshu image posts: validation, topic/activity matching, drafts, immediate publishing, and scheduling |
| `youtube-publish` | YouTube videos: local English localization, subtitles/thumbnail processing, local drafts, immediate publishing, and scheduling |

Install all three directories together, but do not flatten or merge their internal files. `one-click-publish` is the Epost entry and coordination layer; each platform Skill owns its preparation and final submission.

### Installation

The easiest option is to invoke `$skill-installer` in Codex and ask it to install these three directories from this repository:

- `skills/one-click-publish`
- `skills/xiaohongshu-publish`
- `skills/youtube-publish`

You can also install them manually:

```bash
git clone https://github.com/Andonywang123/Epost.git
mkdir -p ~/.agents/skills
cp -R Epost/skills/one-click-publish ~/.agents/skills/
cp -R Epost/skills/xiaohongshu-publish ~/.agents/skills/
cp -R Epost/skills/youtube-publish ~/.agents/skills/
```

Codex normally discovers newly installed Skills automatically. If they do not appear, restart Codex. See the [official Codex Skills guide](https://learn.chatgpt.com/docs/build-skills).

### How to use Epost

1. Install all three Skills in your local Agent application.
2. Tell the Agent “**One-click publish an image post**” or “**One-click publish a video**” to start Epost.
3. Upload your images or video, then provide the cover, title, and copy. Epost stores the materials as supplied, intelligently classifies their purposes, and preserves file order.
4. After you verify the materials, Epost opens the publishing dashboard. Choose the target platforms and accounts, then configure the publishing time, timezone, system preferences, and action type: publish now, schedule, or a supported draft.
5. Review the final summary and confirm. The system will prepare and submit to each platform according to the approved settings, while the dashboard returns real progress and receipts.
6. The first use of each platform requires signing in through a dedicated browser. While that session remains valid, later publishing can normally run automatically. An expired login, CAPTCHA, or platform security check still requires user action.

### Platform coverage

Epost targets Xiaohongshu, Douyin, WeChat Channels, Weibo, YouTube, TikTok, Instagram, X, and other popular platforms. Users can choose the platforms they want for each release.

**Currently integrated:**

- Xiaohongshu: image posts with platform drafts, immediate publishing, and scheduling.
- YouTube: videos with local drafts, immediate publishing, scheduling, local language translation, subtitle generation/burn-in, and English thumbnail localization.

Douyin, WeChat Channels, Weibo, TikTok, Instagram, X, and other platform integrations will be added over time. A platform is never presented as executable until its real publishing package is connected.

### Local runtime setup

Node.js 20 or later, Python 3.10 or later, and Google Chrome are recommended. The bundled Chrome launcher targets macOS/zsh; on other systems, start a dedicated Chrome profile with a local CDP port.

```bash
cd skills/xiaohongshu-publish
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci

cd ../youtube-publish
npm ci --ignore-scripts
```

Local translation, subtitle, and thumbnail processing for YouTube also require Python dependencies and an `ffmpeg` build with the libass subtitle filter. See `skills/youtube-publish/SKILL.md` for details.

Copy `skills/one-click-publish/assets/config-example.json` to `config.local.json` in the publishing workspace. Replace the placeholders with absolute local paths and configure the runtimes, browser, account declaration, and job output directory. Do not commit local configuration, login sessions, tokens, OAuth files, publishing materials, or job receipts.

### Safety boundary

Each platform Skill requires explicit authorization for the exact task, action, and schedule. Inspection, editor filling, saving a draft, immediate publishing, and scheduling are different actions. Epost does not extend one approval to another submission and does not automatically repeat a publish when the result is uncertain.
