# RSSNews v2 热点新闻应用

这个项目是旧 `/Users/user1/Code/rssnews` 的替换版本。部署、目录结构、SQLite 状态库、RSS 静态服务、后台和 systemd 模型基本沿用旧项目；核心变化是新闻入选逻辑：

- 不再用个人关键词抓新闻。
- 主要依据 Hacker News 和 NewsAPI top-headlines。
- NewsAPI top-headlines 只取前 100 条，并按排名给基础分。
- Hacker News topstories 按排名给基础分，beststories 作为额外加分。
- RSS/Atom 和 NewsAPI 主要作为验证信号；只有与 HN topstories 前 100 匹配到同一事件时才加分。
- 只有 HN 高热/高质量内容可以直通主 feed；NewsAPI 不再直通。
- RSS 条目描述会写出当前分值、加分项、置信度和入选阈值；HN 条目会附带讨论页链接。
- `news.xml` 不做数量限制；`radar.xml` 有数量限制。

## 当前工作流

1. 云服务器按计划任务执行构建。
2. 应用抓取 HN topstories、HN beststories、NewsAPI top-headlines、CISA KEV 和配置的 RSS 源。
3. 为候选写入榜单排名、基础分、加分信号和来源信息。
4. 聚合同一事件，计算 `hotness_score` 和 `confidence_score`。
5. 将直通或高置信事件发布到 `news.xml`。
6. 将未进主新闻但分数达到 radar 阈值的候选发布到 `radar.xml`，默认最多 50 条。
7. 生成 `daily.xml`、归档 feed、`source-report.json` 和 `debug-report.json`。
8. `deploy_sync.sh` 将 `dist/` 同步到对外发布目录。

## 关键文件

- `build_rss.py`：主入口薄封装。
- `app/main.py`：主流程编排。
- `app/hotness.py`：v2 热点评分、事件聚合、直通规则和 feed 分流。
- `app/fetchers.py`：HN、NewsAPI、RSS、CISA KEV 抓取器。
- `app/db.py` / `app/schema.sql`：SQLite 数据库访问层和 schema。
- `config/sources.json`：来源、权重、NewsAPI top-headlines 和 RSS 能力配置。
- `config/hotness.json`：基础分、加分、直通阈值、主新闻/radar 阈值。
- `config/noise_rules.json`：降噪词。
- `config/hn_hot_queries.json`：HN Hot 历史热帖查询词和维护备注。
- `dist/`：本次构建输出目录。
- `state/`：运行状态目录，默认保存 `rssnews.db`。
- `deploy_sync.sh`：构建并同步输出到发布目录。
- `serve_rss.sh`：提供 RSS 静态文件、反馈 endpoint 和后台。
- `systemd/`：服务器 systemd 服务和定时任务模板。
- `plan.md`：持续更新的替换项目计划。

## 环境准备

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

至少填写：

- `BASE_URL`
- `OUTDIR`
- `PUBLISH_DIR`
- `PUBLISH_SUBPATH`
- `RSS_BIND`
- `RSS_PORT`
- `STATE_DIR`
- `DATABASE_PATH`

可选：

- `NEWSAPI_KEY`
- `ADMIN_TOKEN`

如果没有 `NEWSAPI_KEY`，应用会跳过 NewsAPI，仍使用 Hacker News、CISA KEV、RSS 源和 HN Hot 继续生成 RSS。

## 本地构建

```bash
./start.sh
```

构建会写出：

- `dist/news.xml`
- `dist/radar.xml`
- `dist/hn-hot.xml`
- `dist/daily.xml`
- `dist/source-report.json`
- `dist/debug-report.json`

## 本地测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 热点评分规则

当前实现采用“榜单基础分 + 交叉验证加分 + 直通规则 + 阈值分流”。主要参数在 `config/hotness.json`。

### 榜单基础分

NewsAPI top-headlines：

- 每次最多取前 100 条。
- 第 1 名 30 分，第 100 名 3 分。
- 中间线性递减：`base = 30 - (rank - 1) * (27 / 99)`。
- NewsAPI 不直通 `news.xml`，只提供基础分和与 HN topstories 的交叉验证信号。

Hacker News topstories：

- 每次最多取前 100 条。
- 第 1 名 30 分，第 100 名 3 分。
- 中间使用同样的线性递减公式。
- HN points/comments 不替代排名基础分，但用于 HN 直通和同分排序。

Hacker News beststories：

- 每次最多取前 50 条。
- beststories 不作为主基础榜，而是作为 HN 内部“优质度/长尾质量”加分。
- 第 1 名加 12 分，第 50 名加 2 分，中间线性递减。

RSS/Atom：

- 普通 RSS 默认不给基础分，只作为验证来源。
- 如果某个 RSS 源在 `config/sources.json` 里标记为 `hot_signal=true`，可给少量基础分。
- CISA KEV 作为安全热点信号源，当前每条基础分为 12。

### 交叉验证加分

同一事件被聚合到一个 cluster 后，会按来源组合加分。所有外部来源交叉验证都只认 HN topstories 前 100；HN beststories 不触发 RSS/NewsAPI/CISA 的交叉验证加分。

- NewsAPI + HN topstories 前 100：加 18 分。
- RSS + HN topstories 前 100：加 10 分。
- CISA KEV + HN topstories 前 100：加 12 分。
- HN beststories：按排名加 2 到 12 分。

匹配规则：

- 强匹配：canonical URL 相同。
- 中匹配：同域名且标题相似度 `>= 0.62`，或不同域名但共享实体较多且相似度达标。
- 弱匹配：只进入调试证据，不触发主加分。
- 默认只匹配 48 小时窗口内的候选。

### HN 直通规则

HN 直通满足任一条件即可进入 `news.xml`：

- `points >= 800`
- `comments >= 300`
- `hn_quality_score >= 0.75`

HN 优质分公式：

```text
points_part = min(points / 800, 1.0)
comments_part = min(comments / 300, 1.0)
hn_quality_score = min(0.72 * points_part + 0.28 * comments_part, 1.0)
```

注意：当前代码会对 HN topstories 和 HN beststories 都计算该直通条件；因此 beststories 里高 points/comments 的帖子也可能进入 `news.xml`。这是一个已知可调点，后续可改成只有 topstories 可以直通，beststories 只加分。

### NewsAPI 入选规则

- NewsAPI 没有直通规则。
- NewsAPI 候选会按排名得到基础分。
- 只有分数和置信度达到主新闻阈值，或与 HN topstories 前 100 交叉验证后达到阈值，才进入 `news.xml`。
- 命中 `config/noise_rules.json` 中的降噪词会被排除。

### 分数阈值

非直通事件按分数分流：

- `hotness_score = base_score + bonus_score - noise_penalty`
- 进入 `news.xml`：`hotness_score >= 38` 且 `confidence_score >= 0.65`
- 进入 `radar.xml`：`hotness_score >= 20`，但未达到主新闻条件
- `radar.xml` 默认最多 50 条
- `news.xml` 不做数量限制

### RSS 条目分值展示

每个 RSS item 的描述会包含：

- `hotness_score`
- `base_score`
- `bonus_score`
- `noise_penalty`
- `confidence_score`
- 具体加分项，例如 `newsapi_hn_match +18`
- 直通原因，例如 `hn_direct_quality`
- 当前入选阈值：`news` 直通或 `hotness >= 38 and confidence >= 0.65`，`radar` 为 `hotness >= 20`

HN 条目如果有 `hn_id`，描述里会额外加入 HN discussion 链接。

### 置信度

`confidence_score` 由以下因素组成：

- 独立来源家族数量，例如 HN、NewsAPI、RSS、CISA KEV。
- 强匹配/中匹配数量。
- 基础分规模。
- 最高来源可信度。

它用于阻止只有单一弱信号、低一致性的事件靠分数进入主新闻。

### 降噪

降噪词位于 `config/noise_rules.json`。匹配范围包括：

- 标题
- 摘要
- 来源名

当前策略是：命中降噪词的事件不会进入 `news.xml` 或 `radar.xml`。这主要用于挡掉 NewsAPI 前排里的体育、娱乐、彩票、促销等内容。

## Feed 结构

- `/rss/news.xml`：主热点新闻，不做数量限制。
- `/rss/radar.xml`：待验证热点和低置信候选，默认最多 50 条。
- `/rss/hn-hot.xml`：HN 历史热帖。
- `/rss/daily.xml`：每日热点快照索引。
- `/rss/archive/YYYY-MM-DD/news.xml`：每日归档。
- `/rss/source-report.json`：来源健康和抓取统计。
- `/rss/debug-report.json`：事件簇、评分、匹配证据和入选原因。

## HN Hot 查询词

`hn-hot.xml` 是独立的 Hacker News 历史热帖 feed。它和主新闻流水线分开：

- `hn-hot.xml` 使用关键词查询 HN Algolia 历史故事。
- 这些关键词只维护在 `config/hn_hot_queries.json`。
- 这些关键词不会用于 `news.xml` 或 `radar.xml` 的入选判断。
- `news.xml` / `radar.xml` 仍以当前 HN topstories、NewsAPI top-headlines 和 RSS 交叉验证为主。
- `hn-hot.xml` 的每个条目会写明入选原因：HN points/comments、命中的查询组、实际触发的关键词/关键词组，以及该查询组内排名。

当前配置中的查询组：

- `ai-learning`：AI/ML/LLM 学习资源历史热帖。
- `devops-classics`：Kubernetes、Postgres、Linux、observability、distributed systems 等工程基础设施经典帖。

维护原则：

- 每个查询组只覆盖一个长期阅读主题。
- 查询词使用简洁的 HN Algolia `OR` 表达式。
- 调整关键词时先改 `config/hn_hot_queries.json`，不要在 Python 代码里维护第二份业务词表。

## 服务器部署

部署模型沿用旧项目：Xray-only Host。

- 不改动 Xray 的 `443`。
- RSS 使用独立 HTTP 服务。
- 默认应用目录：`/opt/rssnews`。
- 默认发布目录：`/opt/rssnews/public`。
- 默认发布子路径：`rss`。

手动发布：

```bash
/bin/bash /opt/rssnews/deploy_sync.sh
/bin/bash /opt/rssnews/serve_rss.sh
```

systemd 模板位于 `systemd/`：

```bash
sudo cp systemd/rssnews-build.service /etc/systemd/system/
sudo cp systemd/rssnews-build.timer /etc/systemd/system/
sudo cp systemd/rssnews-static.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rssnews-static.service
sudo systemctl enable --now rssnews-build.timer
```
