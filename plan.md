# RSSNews v2 热点新闻替换项目计划

## 目标

`rssnews_v2` 将作为旧项目 `/Users/amor/Code/rssnews` 的替换者部署到原服务器。项目的文件结构、运行方式、部署流程、systemd 模型、SQLite 状态库、静态发布目录和后台/反馈能力可以沿用旧项目；核心差异是新闻获取与入选逻辑：

- 不再以个人关键词作为抓取和筛选入口。
- 只抓各平台、新闻 API 或新闻源已经标记为热点、头条、热门、Top、Best、Trending 的候选。
- 通过多源交叉验证识别真正热点，优先发布被多个独立来源同时指向的事件。
- 关键词只可作为诊断、解释、标签或排除噪音的辅助信号，不能作为新闻进入主 feed 的主要条件。

## 旧项目可沿用的部分

参考旧项目 `/Users/amor/Code/rssnews`：

- `build_rss.py` 保持薄入口，调用 `app.main`。
- `app/` 模块化结构继续沿用：settings、models、helpers、fetchers、pipeline、rss_writer、db、feedback、daily、feedback_server。
- `config/` 继续作为可提交的默认配置目录。
- `.env` 继续保存本地和服务器运行配置与密钥，不提交仓库。
- `dist/` 继续作为构建输出目录。
- `state/` 继续作为运行状态目录，默认保存 SQLite 数据库。
- `deploy_sync.sh` 继续负责构建并同步 `dist/` 到 `PUBLISH_DIR/PUBLISH_SUBPATH`。
- `serve_rss.sh` 继续提供静态 RSS、反馈 endpoint 和后台页面。
- `systemd/` 继续提供 `rssnews-build.service`、`rssnews-build.timer`、`rssnews-static.service`。
- 服务器部署模型继续采用旧项目的 Xray-only Host 方案：不动 Xray 的 `443`，RSS 走独立 HTTP 服务，默认发布前缀 `/rss/`。

## 新版核心工作流

1. 定时任务触发构建。
2. 抓取 NewsAPI `top-headlines` 前 100 条、Hacker News `topstories`、Hacker News `beststories` 和配置中的 RSS/Atom 源。
3. 为 NewsAPI top-headlines 和 HN topstories 按榜单排名写入基础分。
4. 将候选标准化写入 SQLite，并记录每条候选来自哪个榜单、排名、原始分数和来源类型。
5. 对候选做 URL、canonical URL、标题相似度和事件簇聚合。
6. 对每个事件簇计算热点分：
   - NewsAPI top-headlines 排名基础分
   - HN topstories 排名基础分
   - HN beststories 前排加分
   - NewsAPI 与 HN 命中同一事件的交叉验证加分
   - RSS 与 HN 命中同一事件的交叉验证加分
   - RSS 与 NewsAPI 命中同一事件的辅助验证加分
   - 发布时间接近程度和标题/摘要一致性
7. 选择高分且高置信事件进入 `news.xml`。
8. 将单源高分、低置信但值得观察的事件放入 `radar.xml`。
9. 将 Hacker News 专属高热讨论继续输出到 `hn-hot.xml` 或新版等价 feed。
10. 写出 `source-report.json` 和 `debug-report.json`，解释每个事件为什么进入或未进入主 feed。
11. 通过 `deploy_sync.sh` 发布到原服务器的发布目录。

## 热点来源策略

### 必选来源

- Hacker News：
  - `topstories`
  - `beststories`
  - `topstories` 是 HN 主基准榜，按排名给基础分。
  - `beststories` 是 HN 内部确认信号；如果同一新闻同时出现在 `beststories` 前几名，则给额外加分。
  - 可继续保留 Algolia 历史热帖查询，但它不再作为主新闻筛选的关键词来源。
- NewsAPI：
  - 优先使用 `top-headlines`。
  - 每次只取前 100 条，按返回顺序给基础分。
  - `everything` 不再按关键词广泛搜索；只在需要补充验证某个热点事件时，按事件标题、实体或 URL 域名做二次确认。

### 从旧项目继承并重新标注的来源

旧项目记录的 RSS/Atom 源包括：

- CISA Cybersecurity Advisories
- AWS News Blog
- Cloudflare Blog
- GitHub Blog
- OpenAI News
- Harvard Business Review
- Child Mind Institute

新版需要在配置中给每个来源增加 `hot_signal` 或 `verification_only` 能力标记：

- `hot_signal=true`：该源本身提供热点、头条、热门、安全紧急通告或高优先级列表，可直接产生热点候选。
- `verification_only=true`：该源不是热点榜单，只用于确认一个外部热点事件是否被权威来源覆盖。
- `disabled_reason` 继续用于记录不可用来源，例如旧项目中 CISA RSS 曾返回 403。

### 可保留但降级的来源

- CISA KEV：不是大众热点榜，但对已知被利用漏洞是高置信安全热点，应作为 `security_hot_signal`。
- GDELT：旧项目使用关键词查询。新版不应把 GDELT 作为关键词入口；可后续研究使用 GDELT 的主题热度、Timeline 或文档相似聚合做验证源。在没有稳定方案前，默认关闭或仅作为二次验证。

## 配置文件设计

旧项目的 `config/feed_rules.json` 应拆分或重命名为更贴合 v2 的配置，例如：

- `config/sources.json`：来源、URL、类型、启用状态、热点能力、权重、抓取限制。
- `config/hotness.json`：热点评分权重、交叉验证阈值、feed 数量限制、时间窗口。
- `config/noise_rules.json`：排除词、低质量域名、体育/娱乐/彩票/促销等噪音规则。

`config/hotness.json` 初始建议：

- `newsapi_top_headlines_limit`: `100`
- `newsapi_base_score_first`: `30`
- `newsapi_base_score_last`: `3`
- `hn_topstories_limit`: `100`
- `hn_topstories_base_score_first`: `30`
- `hn_topstories_base_score_last`: `3`
- `hn_beststories_limit`: `50`
- `hn_beststories_bonus_first`: `12`
- `hn_beststories_bonus_last`: `2`
- `hn_direct_points`: `800`
- `hn_direct_comments`: `300`
- `hn_direct_quality_score`: `0.75`
- `newsapi_direct_top_n`: `5`
- `match_bonus_newsapi_hn`: `18`
- `match_bonus_rss_hn`: `10`
- `match_bonus_rss_newsapi`: `6`
- `same_domain_duplicate_bonus`: `0`
- `max_bonus_per_source_family`: `20`
- `news_min_score`: `38`
- `news_min_confidence`: `0.65`
- `radar_min_score`: `20`
- `radar_max_items`: `50`
- `match_time_window_hours`: `48`

保留原则：

- 不再维护 `interest_keywords` 作为入选条件。
- `topic_profiles` 可移除，或仅作为后台标签/解释用途。
- `source_weights` 继续保留，但含义改为来源可信度，而非个人兴趣相关度。
- NewsAPI 配置应突出 `top-headlines` 的国家、语言、分类和 page size。
- 所有分值必须输出到 `debug-report.json`，方便后续调参。

## 数据模型计划

旧 SQLite schema 可沿用并扩展。建议新增或调整：

- `articles`：继续作为规范化文章事实表。
- `article_sources`：继续记录多来源映射；新版应更重视该表。
- `fetch_runs`：继续记录每次抓取。
- `feed_items`：继续记录当前 feed。
- 新增 `event_clusters`：
  - `id`
  - `canonical_title`
  - `representative_url`
  - `first_seen_at`
  - `last_seen_at`
  - `source_count`
  - `independent_source_count`
  - `hotness_score`
  - `confidence_score`
  - `status`
- 新增 `event_cluster_items`：
  - `cluster_id`
  - `article_guid`
  - `match_type`
  - `similarity_score`
  - `rank_in_source`
- 可选新增 `source_observations`：
  - 记录来源榜单名、榜单排名、抓取时间、原始热度指标。
  - `source_family`，例如 `hn`、`newsapi`、`rss`、`cisa_kev`。
  - `list_name`，例如 `topstories`、`beststories`、`top-headlines`。
  - `rank`
  - `base_score`
  - `bonus_score`
  - `matched_cluster_id`

## 匹配与聚合规则

同一新闻的匹配需要分层处理，避免只靠标题造成误合并：

- 强匹配：
  - canonical URL 相同。
  - 解析后的最终目标 URL 相同。
  - HN 链接指向的原文 URL 与 NewsAPI/RSS URL 相同。
- 中匹配：
  - 域名相同，标题归一化后高度相似。
  - 不同域名但标题核心实体、数字、产品名、人名、地名高度一致。
- 弱匹配：
  - 标题相似但缺少共同实体，只能进入候选簇，不能单独触发主 feed 加分。
- 时间限制：
  - 默认只在 48 小时窗口内匹配同一事件。
  - CISA KEV、安全公告和重大长期事件可使用更长窗口，但必须有专门原因码。
- 排除：
  - 同一来源家族重复转载不增加独立来源确认分。
  - 同一域名多条相近文章只保留最高分代表项。
  - 体育、娱乐、彩票、促销、纯股价波动等噪音仍可被排除或降级。

## 热点评分草案

事件级别评分替代文章级关键词评分。初始版本采用可解释的加分模型，先不要引入过重的机器学习或复杂语义模型。

### 榜单基础分

NewsAPI top-headlines：

- 每次只取前 100 条。
- 第 1 名基础分 30 分。
- 第 100 名基础分 3 分。
- 中间按线性递减，公式为：
  - `base = 30 - (rank - 1) * (27 / 99)`
  - 最终保留 2 位小数。

Hacker News topstories：

- 默认取前 100 条。
- 第 1 名基础分 30 分。
- 第 100 名基础分 3 分。
- 中间按同样线性递减。
- HN 原始 points/comments 不替代榜单排名，但会用于直接进入规则、同分排序和解释字段。
- 如果 HN points、comments 或综合优质度达到阈值，可以直接进入 `news.xml`，但仍然完整计算 `hotness_score` 和 `confidence_score`，用于后续校验和调参。

Hacker News beststories：

- 默认取前 50 条。
- 不作为独立主基准榜，而是 HN 内部确认加分。
- 如果某条新闻已经在 HN topstories 或已聚合到某个事件簇，并且同时出现在 beststories：
  - 第 1 名加 12 分。
  - 第 50 名加 2 分。
  - 中间线性递减。

RSS/Atom 源：

- 默认不按发布时间直接生成主 feed 基础分。
- 如果 RSS 源本身是明确热点榜、紧急公告、高优先级通告，可给少量基础分，例如 8 到 15 分。
- 普通编辑流 RSS 主要作为验证源：只有匹配到 HN 或 NewsAPI 事件簇时才加分。

### 交叉验证加分

- NewsAPI top-headlines 与 HN topstories/beststories 命中同一事件：加 18 分。
- RSS 与 HN 命中同一事件：加 10 分。
- RSS 与 NewsAPI top-headlines 命中同一事件：加 6 分。
- HN topstories 与 HN beststories 命中同一事件：按 beststories 排名加 2 到 12 分。
- CISA KEV 与 HN/NewsAPI/RSS 命中同一安全事件：额外加 12 分。
- 单个来源家族的累计加分需要封顶，避免同一网站多个 feed 或重复转载刷分。

### 置信度

`hotness_score` 决定排序，`confidence_score` 决定是否能进主 feed：

- `source_consensus_score`：独立来源越多越高。
- `source_quality_score`：Reuters/AP/FT/CISA/HN/NewsAPI top-headlines 等来源按可信度加权。
- `rank_score`：在 HN top/best、NewsAPI top-headlines 或来源热门榜中的排名越靠前越高。
- `engagement_score`：HN points/comments 等互动指标。
- `freshness_score`：热点应有时间衰减，避免旧新闻长期占位。
- `semantic_consistency_score`：同簇标题/摘要越一致，越可能是真同一事件。

初始计算建议：

- `hotness_score = base_score_sum + match_bonus_sum + security_bonus - noise_penalty`
- `confidence_score` 由强匹配数量、中匹配数量、独立来源家族数量和来源质量计算。
- 强匹配权重大于中匹配；弱匹配只增加调试证据，不增加主 feed 置信度。

主 feed 初始入选建议：

- NewsAPI top-headlines 前 5 条直接进入 `news.xml`。
- HN 热度或优质度达到阈值直接进入 `news.xml`，初始阈值可沿用旧项目经验：`points >= 800` 或 `comments >= 300`，并逐步替换为 `hn_quality_score >= 0.75`。
- 直接进入的条目仍然必须计算完整分值、匹配证据和原因码；这些分值不用于阻止进入，只用于后续反馈调整。
- `hotness_score >= 38` 且 `confidence_score >= 0.65`；或
- NewsAPI top-headlines 与 HN 命中同一事件；或
- HN topstories 前排同时进入 beststories 前排，并且分数达到阈值；或
- RSS 权威源与 HN 命中同一事件；或
- 安全紧急类来源如 CISA KEV 可单源直通，但必须进入专门原因码。
- `news.xml` 不做数量限制。所有满足直接进入或主 feed 阈值的事件都发布，用于积累样本、观察误判，并支持后期分数反馈调整。

`radar.xml` 初始入选建议：

- 单源高热，但尚未被其他来源验证。
- HN 高讨论但没有新闻源确认。
- NewsAPI top-headlines 中排名靠前但缺少第二来源。
- 来自权威源但事件聚合置信度不足。
- `hotness_score >= 20` 但未达到主 feed 置信度阈值。
- `radar.xml` 必须做数量限制，初始建议最多 50 条。
- radar 排序按 `hotness_score`、`confidence_score`、发布时间依次排序，超过数量限制的候选只保留在数据库和 `debug-report.json`，不发布到 RSS。

### 评分解释输出

每个事件在 `debug-report.json` 中至少输出：

- `hotness_score`
- `confidence_score`
- `base_scores`
- `match_bonuses`
- `matched_sources`
- `representative_article`
- `cluster_items`
- `match_reasons`
- `feed_decision`
- `reason_summary`

## Feed 结构

沿用旧项目的公开路径，方便替换部署：

- `/rss/news.xml`：真正热点，低噪音主入口。
- `/rss/news.xml` 不做数量限制，所有直接进入或达到主 feed 阈值的事件都发布。
- `/rss/radar.xml`：待验证热点和早期信号，有数量限制，默认最多 50 条。
- `/rss/hn-hot.xml`：Hacker News 高热讨论或历史热帖。
- `/rss/daily.xml`：每日热点快照索引。
- `/rss/archive/YYYY-MM-DD/news.xml`：每日归档。
- `/rss/source-report.json`：来源健康和抓取数量。
- `/rss/debug-report.json`：事件簇、评分、交叉验证解释。

## 实施阶段

### Phase 1：项目骨架迁移

状态：completed

任务：
- 从旧项目迁移可复用文件结构。
- 保留部署脚本、systemd 模板、README 结构和测试入口。
- 移除旧关键词驱动的默认配置。

验收标准：
- 新项目可以本地运行空流水线。
- `./start.sh`、`./deploy_sync.sh`、`./serve_rss.sh` 的使用方式与旧项目一致。

### Phase 2：热点来源配置

状态：completed

任务：
- 建立 `config/sources.json`。
- 建立 `config/hotness.json`，写入 NewsAPI、HN 和 RSS 的基础分/加分配置。
- 为 Hacker News、NewsAPI top-headlines、CISA KEV 和旧 RSS 源标注能力。
- 将旧项目 RSS 源导入新配置，并区分 `hot_signal` 与 `verification_only`。

验收标准：
- 不依赖关键词即可得到一批热点候选。
- `source-report.json` 能清楚显示每个来源是否可用、抓取多少条、是否作为热点信号。
- 配置中明确 NewsAPI top-headlines 只取前 100 条。

### Phase 3：抓取器改造

状态：completed

任务：
- 保留 HN `topstories` / `beststories` 抓取。
- NewsAPI 默认使用 `top-headlines`，每次最多取前 100 条。
- RSS 抓取器增加来源能力和榜单排名记录。
- CISA KEV 作为安全热点信号源。
- 暂停或重写 GDELT 关键词抓取。

验收标准：
- 所有候选都有 `source_type`、`source_rank`、`hot_signal_type`、`base_score` 和原始来源记录。

### Phase 4：事件聚合与交叉验证

状态：completed

任务：
- 实现 URL 规范化、标题归一化和相似标题聚类。
- 将同一事件的多篇报道聚合成事件簇。
- 实现 NewsAPI/HN/RSS 的强匹配、中匹配、弱匹配分层。
- 写入 `event_clusters` 和 `event_cluster_items`。
- 为每个事件簇计算独立来源数和一致性分数。

验收标准：
- 同一事件不会因多个来源重复刷屏。
- `debug-report.json` 能展示一个事件由哪些来源共同确认。
- HN 与 NewsAPI/RSS 命中同一事件时能看到明确的加分原因。

### Phase 5：热点评分与 feed 选择

状态：completed

任务：
- 用事件级 `hotness_score` 和 `confidence_score` 替代旧关键词相关性。
- 实现 NewsAPI top-headlines 和 HN topstories 的排名基础分。
- 实现 HN beststories、NewsAPI-HN、RSS-HN、RSS-NewsAPI 的交叉验证加分。
- 实现 NewsAPI 前 5 直接进入主 feed。
- 实现 HN 热度/优质度阈值直接进入主 feed。
- 实现 `news` / `radar` 分流。
- 主 feed 不做数量限制；radar 做数量限制。
- 保留必要的噪音排除规则，但不让兴趣关键词决定入选。

验收标准：
- `news.xml` 主要由多源确认热点组成。
- `news.xml` 包含所有直接进入或达到主 feed 阈值的事件，不因数量上限被截断。
- `radar.xml` 承接单源高热和低置信事件，并按 `radar_max_items` 截断。
- 每条 RSS item 的描述中能用一句话解释主要分数来源，例如“NewsAPI #4 + HN topstories #11 + RSS match”。

### Phase 6：后台、反馈与诊断

状态：pending

任务：
- 沿用旧后台页面和反馈 endpoint。
- 后台增加事件簇视图，展示来源共识、评分和入选原因。
- 反馈从“调来源/关键词”改为“调来源可信度、域名可信度、噪音规则和事件簇误合并”。

验收标准：
- 可以在后台解释每条主 feed 新闻为什么是真热点。
- 可以通过反馈修正低质量来源或误聚类问题。

### Phase 7：测试与回归样本

状态：completed

任务：
- 增加 HN、NewsAPI、RSS、CISA KEV 的抓取单元测试。
- 增加 URL 规范化和标题聚类测试。
- 增加事件评分和 feed 分流测试。
- 增加排名分递减测试：NewsAPI #1 为 30 分，#100 为 3 分。
- 增加交叉验证加分测试：NewsAPI 与 HN 匹配、RSS 与 HN 匹配、HN topstories 与 beststories 匹配。
- 用旧项目 `dist/debug-report.json` 或保存样本构建离线 fixture。

验收标准：
- 本地 `python3 -m unittest discover -s tests -p 'test_*.py'` 可通过。
- 聚类和交叉验证逻辑有稳定测试覆盖。

### Phase 8：替换部署

状态：pending

任务：
- 在服务器上备份旧 `/opt/rssnews`、`state/rssnews.db` 和 `.env`。
- 将新项目同步到原应用目录，或先部署到 `/opt/rssnews_v2` 做并行验证。
- 沿用旧 `.env` 中的 `BASE_URL`、`OUTDIR`、`PUBLISH_DIR`、`PUBLISH_SUBPATH`、`RSS_BIND`、`RSS_PORT`、`STATE_DIR`、`DATABASE_PATH`。
- 先手动运行 `deploy_sync.sh` 验证输出。
- 再切换 systemd service/timer 到新项目。

验收标准：
- 原订阅地址 `/rss/news.xml`、`/rss/radar.xml`、`/rss/daily.xml` 可继续访问。
- 新 feed 内容来自热点交叉验证逻辑。
- 旧项目可回滚。

## 已知风险

- NewsAPI `top-headlines` 的覆盖面受账号、国家、语言和来源限制影响。
- 很多 RSS 源只是编辑发布流，不一定代表热点，需要谨慎标注。
- 事件聚类如果只靠标题相似度，可能误合并相近但不同的事件。
- 只抓热点会降低个人兴趣覆盖率，这是本版产品目标变化，不应再用关键词补回。
- 旧数据库可迁移，但 v2 的事件簇表更适合重新初始化后观察一段时间。

## 当前下一步

1. 迁移旧项目骨架到 `rssnews_v2`。
2. 建立新版配置文件。
3. 实现热点候选抓取。
4. 实现事件聚合与交叉验证。
5. 本地生成 RSS 和诊断报告。
6. 并行部署到服务器验证。
7. 替换旧项目的 systemd 构建任务。
