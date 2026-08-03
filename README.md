# FastInsight

FastInsight 是一个用于 Feishu/Lark 群聊的论文与研究资讯自动分拣 skill。
它可以识别论文截图或链接，补充学术元数据，分析引用与相关工作，按研究方向
匹配负责人，并生成 Feishu 交互卡片。

## 快速开始

### 1. 准备环境

- Python 3.8 或更高版本
- 已连接的 Feishu/Lark connector
- 一份包含真实 `feishu_open_id` 的负责人名册

复制名册模板并填写真实信息：

```powershell
Copy-Item assets\colleagues.yaml.example colleagues.yaml
python scripts\validate_roster.py colleagues.yaml
```

如果已有配置好的名册，也可以直接使用：

```powershell
python scripts\validate_roster.py assets\colleagues.yaml
```

### 2. 在对话中使用

显式调用 skill：

```text
$fastinsight 处理这张论文截图，识别论文、分析研究方向，并转发给对应同事。
```

也可以发送论文链接：

```text
使用 FastInsight 分拣这篇论文，并附上引用趋势摘要：
https://example.com/paper
```

典型流程为：

`识别 → 学术验证 → 趋势分析 → 方向分类 → 生成卡片 → 转发 → 群内确认`

如果是新闻或行业资讯，FastInsight 会使用标题、描述和链接进行分拣，默认跳过
论文验证与引用分析。

## 命令行流程

### 论文验证

```powershell
python scripts\verify_paper.py --query "Attention Is All You Need" --json-out paper.json
```

输出包含：

- `best`：最匹配的论文
- `candidates`：候选结果
- `source`：数据来源
- `warnings`：失败或降级的 API

### 趋势分析

```powershell
python scripts\analyze_trends.py --paper paper.json --json-out trends.json
```

该步骤会尝试获取引用数、研究领域、相关论文和简短趋势摘要。API 不可用时，
流程仍然可以继续。

### 预览路由结果

```powershell
python scripts\route_paper.py --paper paper.json --roster colleagues.yaml --trends trends.json --dry-run
```

重点检查输出中的：

- `matched_direction`：匹配到的研究方向
- `hits`：命中的关键词
- `owners`：将接收卡片的负责人
- `fallback`：是否使用了兜底负责人
- `routable`：是否具备有效的 Feishu ID

确认无误后，去掉 `--dry-run`：

```powershell
python scripts\route_paper.py --paper paper.json --roster colleagues.yaml --trends trends.json
```

命令行脚本只负责生成 `feishu_card_payload.json`，不会直接发送消息。连接
Feishu 后，由 skill 使用 connector 将卡片发送给 `owners` 中的负责人，并在原群
中发送确认消息。

## 名册格式

```yaml
directions:
  - name: "网络安全 / AI安全"
    priority: 10
    keywords:
      - "privacy-preserving"
      - "secure inference"
      - "隐私计算"
    owners:
      - name: "张三"
        feishu_open_id: "ou_real_id"

default_owner:
  name: "组长"
  feishu_open_id: "ou_real_id"
```

说明：

- 关键词匹配 `标题 + 摘要 + venue`
- ASCII 关键词使用 token 边界，避免 `RL` 匹配到 `URL`
- `priority` 用于关键词分数相同的方向
- 缺少有效负责人时，会尝试使用 `default_owner`
- 占位符或空的 `feishu_open_id` 不会被发送

## 可选环境变量

```powershell
$env:S2_API_KEY = "your-semantic-scholar-key"
$env:CROSSREF_MAILTO = "you@example.com"
```

- `S2_API_KEY`：提高 Semantic Scholar API 的请求额度
- `CROSSREF_MAILTO`：使用 Crossref polite pool

## 常见问题

### `routable` 为 `false`

运行 roster 校验，确认方向负责人或 `default_owner` 使用真实的
`feishu_open_id`：

```powershell
python scripts\validate_roster.py colleagues.yaml
```

### 学术 API 不可用

保留截图中读取到的论文标题、作者和链接，并在群内说明引用数据暂不可用。
不要把 API 返回的候选结果当作已确认论文。

### 方向匹配不准确

为对应方向增加更有区分度的关键词，必要时配置 `priority`，然后重新执行
`route_paper.py --dry-run` 预览。

## 目录说明

- `SKILL.md`：Codex 使用的 skill 行为规范
- `agents/openai.yaml`：UI 展示信息
- `scripts/`：验证、趋势分析、路由和名册校验脚本
- `references/`：Feishu 集成、学术搜索和研究方向说明
- `assets/`：卡片模板和名册模板

## 安全注意事项

- 首次上线或修改名册后，先使用 `--dry-run`
- 不向缺失或占位 `open_id` 发送消息
- 图片不清晰或包含多篇论文时，先向发送者确认
- 默认只转发论文元数据和链接，不转发发送者身份
