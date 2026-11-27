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
| `scripts/verify_paper.py` 等 | `scripts/` 验证 / 路由 / 发布模块 |
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
uv sync --locked
cp .env.example .env   # 填入 FEISHU_APP_ID / FEISHU_APP_SECRET / FastResearch 配置
./run.sh
```

项目使用 uv 管理环境，`.python-version` 固定 Python 3.12.12，`pyproject.toml` 声明依赖，`uv.lock` 锁定解析结果。
`uv sync --locked` 创建项目自己的 `.venv`；`run.sh` 统一执行 `uv run --locked`，不再借用其他项目的虚拟环境。
如果尚未安装 uv，先按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装。
`.env` 中的飞书凭据、Research 地址和服务身份需要按实际环境填写；示例不包含可用密钥。
检查运行入口而不建立飞书连接：`uv run --locked python feishu_bot.py --help`。

升级依赖时使用 `uv lock --upgrade-package 包名`，随后重新测试并执行
`uv export --locked --no-dev --no-emit-project --output-file requirements.txt`。
`requirements.txt` 仅为 FastDeploy 的 vendor 构建从锁文件导出，请勿手工维护；FastCAS SDK 已包含在主依赖中。

探测群历史权限：

```bash
uv run --locked python feishu_bot.py --check oc_xxxxxxxxxxxxx
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

论文分发配置统一在 FastResearch 工作台的「论文分发设置」维护，保存到 Research 数据库。
执行端每篇论文通过 `GET /api/fastinsight/roster` 读取一次配置快照；设置页保存的修改对下一篇论文生效，
结果中的 `roster_revision` 记录本次使用的配置版本。配置读取失败会停止本次分发，不会回退到本地 YAML。

所有命中关键词的方向成员共同接收，按 `receiver_ref` 去重；同名但账号不同的成员不会被合并。
没有关键词命中时直接使用默认接收人，不再调用 LLM 做语义分发。已命中方向但没有成员连线时不投递；
未设置默认接收人且没有匹配方向时也不投递。每位接收成员需要在设置页填写 Research 收件账号（`receiver_ref`），
它是账号 ID，不是姓名或飞书 Open ID。飞书 ID 仅保留为可选元数据。

### 从旧配置迁移

先升级 FastResearch（提供数据库配置及服务读取接口），再升级 FastInsight。新数据库可以直接在设置页配置，
也可以执行一次性导入：

```bash
# FASTRESEARCH_ADMIN_SESSION 使用现有 Research 管理员会话，通过环境变量提供。
uv run --locked python scripts/import_roster.py assets/colleagues.yaml --endpoint https://research.example.com
uv run --locked python scripts/validate_roster.py --endpoint https://research.example.com
```

导入仅允许写入空配置，使用版本校验防止并发覆盖，不修改源文件。之后旧文件及 `FASTINSIGHT_ROSTER` 环境变量
不再参与运行。旧配置若缺少 `receiver_ref`，请在设置页补全；执行端不会按同名账号猜测接收人。
迁移脚本使用管理员身份，运行时读取和投递使用下文的服务身份，两者不要混用。

正常预览直接读取数据库：

```bash
uv run --locked python scripts/route_paper.py --paper paper.json --trend-text "论文趋势分析" --dry-run
```

仅离线调试时可以显式传入 `--roster-json snapshot.json`。自动分发会内部生成本次配置的临时 JSON 快照，
该快照不是持久化配置来源。
微信公众号、小红书链接会先读取正文，从中识别 DOI / arXiv / 论文标题，再走学术验证。正文里没有论文链接时，若配置了 `OPENAI_API_KEY`，会用 LLM 从文章里抽取论文元数据。

取消：

```
@fastinsight cancel
```

## 环境变量

必填：

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- `FASTRESEARCH_URL`（默认 `http://127.0.0.1:8787`）
- 完整 FastCAS 服务身份（见下文），或显式启用的旧版 `FASTINSIGHT_INGEST_KEY`

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
scripts/roster_client.py # 读取 Research 数据库配置
```

## 服务身份

`uv sync --locked` 已安装固定的 `fastcas-sdk==0.1.3`，无需另外安装依赖。服务读取和投递共用 `insight:publish` scope：

- FastInsight 配置 `FASTINSIGHT_FASTCAS_ISSUER`、`FASTINSIGHT_FASTCAS_CLIENT_ID`、`FASTINSIGHT_FASTCAS_CLIENT_SECRET`，清空 `FASTINSIGHT_INGEST_KEY`。
- FastResearch 的 `FASTRESEARCH_FASTCAS_INGEST_CLIENT_ID` 必须等于该服务 client ID；`FASTRESEARCH_FASTCAS_INGEST_ALLOWED_ACCOUNT_IDS` 列出允许投递的账号 ID。
- 配置读取校验服务 client ID、scope、audience 和令牌实时状态；投递仍逐个校验账号授权。运行时不需要管理员会话或数据库密码。
- 兼容旧密钥时，Research 必须显式设置 `FASTRESEARCH_ALLOW_LEGACY_FASTINSIGHT_KEY=true`，两端配置同一个 `FASTINSIGHT_INGEST_KEY`。旧密钥模式同样使用 `receiver_ref` 投递。
- `FASTRESEARCH_URL` 使用 HTTPS；仅本机开发允许 HTTP 环回地址。配置接口不跟随重定向。

## 验证

```bash
uv run --locked python -m unittest discover -s tests -v
# 两个仓库均检出时，从 FastResearch 目录运行真实跨仓库合约测试：
FASTINSIGHT_SOURCE_DIR=../FastInsight go test ./internal/httpapi -run TestFastInsightDatabaseRoutingIntegration -v
```

跨仓库测试使用临时数据库和本地服务，验证迁移、配置更新、默认分发和同名账号隔离，不向实际成员发送内容。
