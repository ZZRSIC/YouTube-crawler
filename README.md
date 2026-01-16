# YouTube 字幕下载 + 文本去噪（yt-dlp）

这个仓库用于：

- 从 YouTube 频道/播放列表/单个视频链接批量拉取字幕（含自动字幕）
- 将 `.vtt` 转成更易用的 `.txt`
- 对生成的文本做基础清洗去噪（去头信息、合并空行、统一空格、去填充词、去紧邻重复句）
- 可选输出同名 `.json` 元数据（标题/日期/链接）

## 运行环境

- Python 3.10+
- macOS / Linux / Windows 均可

## 安装依赖

在仓库根目录执行：

```bash
pip install -r requirements.txt
```

## 快速开始

1) 准备配置文件（推荐）

- 复制 [.env.example](.env.example) 为 `.env`，并按需修改：
	- `INPUT_LIST`：保存“视频列表”的文件路径
	- `DEST_DIR`：字幕下载与输出目录

2) 运行脚本

```bash
python download_captions.py
```

脚本会提示你输入 `YouTube频道/播放列表/视频链接`，并做两件事：

- 拉取视频列表并写入 `INPUT_LIST`（格式：`序号. 标题 -> URL`）
- 下载字幕到 `DEST_DIR`，并转换输出 TXT

## 输出文件说明

默认输出到 `DEST_DIR`：

- `{标题}_{日期}.txt`：清洗后的字幕文本
- `{标题}_{日期}.json`：可选元数据（由环境变量控制）

`.json` 中会包含 `title / upload_date / video_url`，便于你后续回溯来源或做入库。

## .env 配置说明

### 路径

- `INPUT_LIST`：例如 `./youtube_video_links_test.txt`
- `DEST_DIR`：例如 `./test`

### 拉取范围

- `TOP_N=5`：只处理前 5 条
- `TOP_N=all` / `TOP_N=0` / `TOP_N=-1`：处理全部

### Cookie / 访问参数（可选）

在部分视频/地区/频控场景下可能需要：

- `YOUTUBE_COOKIES=/path/to/cookies.txt`
- `YDL_COOKIES_FROM_BROWSER=chrome`（或其它浏览器）
- `YDL_RATELIMIT=1M`（限速，避免触发频控）

### 文本清洗（去噪）

- `TXT_WRITE_METADATA_JSON=1`：生成同名 `.json` 元数据；设为 `0` 则不生成
- `TXT_REMOVE_INLINE_FILLERS=0`：
	- `0`（默认）：只删除“单独一行”的填充词（更保守，误删更少）
	- `1`：连句子中的填充短语也尝试删除（更激进，可能误删一些正常用法）
- `TXT_FILLERS=aha,mmhm,you know`：逗号分隔的自定义填充词/短语（可选）
- `TXT_FILLERS_FILE=./fillers.txt`：一行一个填充词/短语（可选，推荐）

## 常见问题

### 1) 报错/提示找不到 `yt_dlp`

说明当前 Python 环境没安装依赖：

```bash
pip install -r requirements.txt
```

### 2) 为什么 TXT 里没有 Title/Date/Link 头信息？

脚本会把这些信息从正文中移除，避免污染文本；如需保留，请开启 `TXT_WRITE_METADATA_JSON=1` 使用 `.json` 读取。
