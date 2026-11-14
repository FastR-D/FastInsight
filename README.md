# FastInsight Feishu Bot

把 OpenClaw FastInsight skill 里的**飞书输入通道**单拎出来：只用飞书官方 Python SDK（`lark-oapi`）的 **WebSocket 长连接** 接收 `im.message.receive_v1`，在源会话里通过 `@fastinsight` 开启前后 1 分钟的图片批处理。

这是单一功能，不是 OpenClaw 本体：

- 不私聊 / DM 负责人
- 不给负责人发飞书卡片
- 识别 → 学术验证 → 趋势分析 → 多方向分类 → **只发布到 FastResearch**
- 所有命中方向的负责人都会收到同一篇论文；同一人跨方向命中时只发布一次
- 成功后只在**源会话**回一条短确认

不需要公网 webhook。SDK 负责心跳和重连。

## 和 OpenClaw skill 的对应关系

| OpenClaw FastInsight | 本仓库 |
| --- | --- |
| `scripts/feishu_bridge.py` | `fastinsight/bot.py`（长连接入口，已补全 `@fastinsight` 状态机） |
| `scripts/pending_capture.py wait --notify` | 长连接事件 + 前后 1 分钟群历史轮询 |
| `scripts/verify_paper.py` 等 | `scripts/` 原样拷贝 |
| 源会话确认 | `FeishuApp.send_text()`（原生 SDK） |

群未 @ 的纯图片经常不会推送给机器人。发送 `@fastinsight` 后，bot 会处理该消息**前 1 分钟到后 1 分钟**内的所有图片和链接，并用 `GET /im/v1/messages` 轮询兜底。纯链接消息按优先级选择一条论文链接；图片附链接时按一个图片处理项分发，链接用于识别和补全元数据，避免同一论文重复发布。同一图片按 `image_key` 去重；超过时间窗的消息不再处理。

## 飞书开放平台

1. 创建企业自建应用，打开机器人能力。
2. 权限管理勾选后必须到 **版本管理 → 创建版本并发布**（只勾选不发布不会生效）：
   - `im:message` 源会话提示 / 确认
   - `im:message:receive` 接收消息事件
   - `im:message.group_msg` **读取群历史**（没有它就看不到未 @ 的下一张图）
   - `im:resource` 下载消息图片
   - 可选 `optical_char_recognition:image` 飞书 OCR
3. 事件订阅选择 **使用长连接接收事件**，订阅 `im.message.receive_v1`。
4. 如需群里未 @ 的图片也走事件推送，打开「接收群聊中的所有消息」。

同一飞书应用的长连接通常只能有一条。启动前请停掉 OpenClaw 飞书通道和旧的 `feishu_bridge.py`，否则会 `EXCEED_CONN_LIMIT`。

## 运行

```bash
cd /path/to/fastinsight
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填入 FEISHU_APP_ID / FEISHU_APP_SECRET / FastResearch 配置
./run.sh
```

`run.sh` 优先用本仓库 `.venv`，若尚未装好 `lark-oapi` 会回退到 OpenClaw FastInsight skill 的 venv。

探测群历史权限：

```bash
.venv/bin/python feishu_bot.py --check oc_xxxxxxxxxxxxx
```

群里：

```
@fastinsight
# bot 开启前后 1 分钟的图片批处理
# 会处理命令前 1 分钟、命令附件和命令后 1 分钟内的图片与链接
```

带 URL 可跳过等待：

```
@fastinsight https://arxiv.org/abs/1706.03762
@fastinsight https://mp.weixin.qq.com/s/...
@fastinsight http://xhslink.com/o/...
```

图片无需同时包含论文标题和链接。FastInsight 会从截图正文推断论文正式标题或高区分度检索词，
再通过 Crossref、OpenAlex、Semantic Scholar、arXiv 和网页搜索查找对应论文。

论文分发先匹配 `colleagues.yaml` 中的关键词；所有命中方向的负责人共同接收。关键词全部未命中时，
LLM 会根据人员研究方向进行保守的语义分发。默认置信度阈值为 `0.75`，可通过
`FASTINSIGHT_SEMANTIC_ROUTING_THRESHOLD` 调整；低于阈值或 LLM 不可用时发送给 `default_owner`。

微信公众号、小红书链接会先读取正文，从中识别 DOI / arXiv / 论文标题，再走学术验证。正文里没有论文链接时，若配置了 `OPENAI_API_KEY`，会用 LLM 从文章里抽取论文元数据。

取消：

```
@fastinsight cancel
```

## 环境变量

必填：

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- `FASTRESEARCH_URL`（默认 `http://127.0.0.1:8787`）
- `FASTINSIGHT_INGEST_KEY`
- `FASTINSIGHT_ROSTER`（默认 `assets/colleagues.yaml`）

强烈建议（截图理解和 2–5 句趋势分析）：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`（兼容 OpenAI 的网关即可）
- `OPENAI_MODEL` / `VISION_MODEL`

没有视觉模型时，会尝试飞书 OCR；单张图片仍无法识别时会返回该图片的处理错误，但不会重复处理。没有 LLM 时不会发布（skill 禁止「暂无趋势数据」）。

## 目录

```
feishu_bot.py          # 入口
fastinsight/bot.py     # 长连接 + @fastinsight 状态机
fastinsight/feishu_api.py
fastinsight/capture.py
fastinsight/parse.py
fastinsight/article.py
fastinsight/pipeline.py
scripts/               # 从 OpenClaw skill 拷贝的验证 / 路由 / 发布
assets/colleagues.yaml
```

## 可选 FastCAS 服务身份

启用 FastCAS 投递时，在 Python ≥3.10 环境运行 `python -m pip install -r requirements-fastcas.txt`，固定 `fastcas-sdk==0.1.2`。原脚本/飞书路径不依赖此可选安装，服务权限仍按接收端的 scope 与收件人限制校验。
