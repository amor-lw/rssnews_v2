# 新闻关键词匹配规律

本文档说明当前项目的新闻抓取、关键词匹配、分类打分和 RSS 入选规则。它反映的是当前代码与配置的真实行为，主要依据：

- `config/feed_rules.json`
- `app/news_manager.py`
- `app/pipeline.py`
- `app/main.py`

## 1. 总览

当前新闻系统不是简单地“关键词命中就进入 RSS”，而是分为几个阶段：

1. 抓取候选新闻
2. 写入 SQLite 数据库
3. 对文章进行 profile 分类
4. 计算相关度、重要性和置信度
5. 决定进入 `news.xml` 还是 `radar.xml`
6. RSS 输出前做标题级去重

整体目标是：

| Feed | 定位 |
|---|---|
| `news.xml` | 低噪音、重要、值得优先阅读 |
| `radar.xml` | 值得扫一眼的候选新闻 |
| `daily.xml` | 每日摘要输出 |
| `hn-hot.xml` | Hacker News 历史高热度常驻榜单 |

当前设计重点是：

1. 重大新闻优先。
2. DevOps / 安全事件优先。
3. 个人兴趣内容降级为 radar，不再主导主 feed。
4. RSS 输出前尽量避免同一事件重复刷屏。

## 2. 抓取阶段与分类阶段的区别

项目里有两类关键词：

| 类型 | 用途 | 位置 |
|---|---|---|
| 抓取关键词 | 用来从 NewsAPI、GDELT 等外部来源拉取候选新闻 | `queries.newsapi_terms`、`queries.gdelt_terms` |
| 分类关键词 | 用来判断文章属于重大新闻、安全新闻、个人兴趣还是噪音 | `topic_profiles`、`interest_keywords`、`exclude_terms` |

需要注意：

- 抓取关键词只决定“哪些文章会被拿回来”。
- 分类关键词才决定“文章最终进入哪个 feed”。
- 一篇文章即使由某个抓取关键词抓回，也不一定会进入 `news.xml`。
- 一篇文章即使没有命中特定关键词，也可能因为高信任来源、多来源发现、重大事件规则而进入主新闻流。

## 3. 匹配文本

当前关键词匹配使用文章的：

```text
title + summary
```

也就是说，标题和摘要都会参与匹配。

## 4. 匹配方式

当前使用大小写不敏感的边界匹配。

例如：

```text
关键词: data breach
标题: Fidelity data breach settlement...
```

会命中。

再例如：

```text
关键词: RCE
文本: sources have told STAT
```

不会命中，因为 `RCE` 只是出现在 `sources` 这个普通单词内部。

这不是语义匹配，但会要求关键词左右两侧不是字母或数字。它的优点是稳定、可解释，并且可以避免 `RCE`、`CVE`、`AI`、`war` 这类短词误匹配到普通单词内部。

## 5. 当前启用的 Profile

当前启用的 profile 来自 `config/feed_rules.json`：

```json
"active_profiles": [
  "major_events",
  "devops_security",
  "personal_interest"
]
```

其中允许进入 `news.xml` 的 profile 是：

```json
"news_profiles": [
  "major_events",
  "devops_security"
]
```

所以当前行为是：

| Profile | 含义 | 默认是否进入 `news.xml` |
|---|---|---|
| `major_events` | 重大事件、地缘政治、宏观风险 | 是 |
| `devops_security` | 严重安全事件、漏洞、云服务事故 | 是 |
| `tech_industry_major` | 技术行业重大新闻、HN 高热度、关键实体事件 | 是 |
| `personal_interest` | 个人兴趣补充内容 | 否，主要进入 `radar.xml` |
| `noise` | 明确排除内容 | 否 |

## 6. 排除词规则

排除词优先级最高。

只要文章命中 `exclude_terms`，就会被标记为：

```text
profile = noise
bucket = radar
importance_score = 0
confidence_score = 0
```

实际选择 `radar.xml` 时会过滤掉 `noise`，所以这些内容通常不会进入 RSS。

当前排除词包括：

```text
celebrity
sports
NFL
NBA
MLB
NHL
WNBA
football
basketball
baseball
soccer
hockey
tennis
golf
Pittsburgh Steelers
lottery
horoscope
coupon
giveaway
box office
trailer
recap
reaction
rumor
leak
sponsored
sponsor content
meme coin
bitcoin price prediction
crypto price prediction
stock jumps
stock falls
```

## 7. major_events 规则

代码中的 `major_events` 会映射到配置中的：

```text
topic_profiles.major_news
```

### must_track

```text
war
conflict
ceasefire
sanction
tariff
export control
central bank
inflation
recession
supply chain
energy crisis
```

### watch

```text
election
geopolitics
Taiwan
US-China
Middle East
Russia
Ukraine
```

### 高信任来源兜底

即使没有命中关键词，如果来源是高信任媒体，也会给 `major_events` 一个最低相关度：

```text
relevance_score >= 0.35
```

当前高信任来源包括：

```text
Reuters
Associated Press
Financial Times
```

这条规则用于降低错过重大事件新闻的概率。

## 8. devops_security 规则

### must_track

```text
CVE
zero-day
remote code execution
privilege escalation
supply chain attack
software supply chain
GitHub compromised
packages compromised
package compromised
npm packages compromised
malicious package
npm malware
credential theft
credentials compromised
token theft
tokens compromised
secrets leaked
ransomware
data breach
cloud outage
Kubernetes vulnerability
```

### watch

```text
incident response
root cause analysis
postmortem
Kubernetes
Linux kernel
OpenSSL
GitHub Actions
Terraform
Cloudflare
Okta
```

### 严重安全事件增强

如果文章命中严重安全关键词，会强制提高相关度：

```text
relevance_score >= 0.7
```

严重关键词包括：

```text
cve
zero-day
0-day
remote code execution
actively exploited
known exploited
privilege escalation
supply chain attack
software supply chain
github compromised
packages compromised
package compromised
npm packages compromised
malicious package
npm malware
credential theft
credentials compromised
token theft
tokens compromised
secrets leaked
ransomware
data breach
critical vulnerability
```

这条规则用于让严重漏洞、安全事件和基础设施事故更容易进入主 feed。

裸 `RCE` 不再单独作为强触发词。它只有在同时出现安全上下文时才算严重信号，例如：

```text
CVE
exploit
actively exploited
vulnerability
critical vulnerability
patch
advisory
security advisory
PoC
CISA
remote code execution
```

### 数据泄露理赔类降噪

`data breach` 仍然是严重安全信号，但如果同一篇文章明显是泄露后的消费者理赔、索赔或集体诉讼指南，则会降级，不作为 DevOps 主新闻。

触发语境包括：

```text
settlement
compensation
file a claim
claim form
class action
lawsuit
you could be owed
how to claim
your share
```

例如：

```text
Fidelity data breach settlement offers up to $5,000: How to file a claim
```

这类文章会保留为后台/radar 候选，而不是进入 `news.xml`。

## 9. personal_interest 规则

## 9. tech_industry_major 规则

`tech_industry_major` 用于承接关键词难以完整维护、但对技术行业明显重要的事件。

典型入口：

```text
OpenAI
Anthropic
Google DeepMind
Meta AI
xAI
NVIDIA
GitHub
Microsoft
frontier model
model release
acquires
acquired
joins
joined
resigns
leaves
```

HN 还有两条热度晋级规则：

```text
HN points >= 800
或 comments >= 300
=> 未命中排除词时，直接进入 news
```

以及：

```text
HN points >= 250
或 comments >= 80
并且命中 watched_entities
=> 进入 news
```

默认关注实体包括：

```text
OpenAI
Anthropic
Google DeepMind
Meta AI
xAI
NVIDIA
GitHub
Microsoft
AWS
Cloudflare
npm
PyPI
Docker
Kubernetes
Linux
Andrej Karpathy
Ilya Sutskever
Dario Amodei
Sam Altman
```

这条规则用于解决类似：

```text
I’ve joined Anthropic
Anthropic acquires Stainless
GitHub Compromised
Mini Shai-Hulud Strikes Again: 314 npm Packages Compromised
```

这类“重要性来自行业热度/关键实体，而不是传统关键词”的新闻。

## 10. personal_interest 规则

`personal_interest` 主要来自：

```text
interest_keywords
```

并且会排除已经被 `topic_profiles` 使用过的关键词，避免重复计算。

当前设计中，`personal_interest` 主要用于：

```text
radar.xml
后台候选池
后续兴趣学习和调参
```

它默认不进入 `news.xml`，避免过宽的个人兴趣关键词挤占主新闻流。

需要注意：

- `topic_profiles.ai_startup`
- `topic_profiles.healthtech`
- `topic_profiles.parenting_education`

这些 profile 当前没有直接出现在 `active_profiles` 中，因此不会作为独立分类直接主导 feed 入选。它们的关键词更多用于配置组织和避免与 `personal_interest` 重复。

## 11. 关键词命中分数

每组关键词会先计算命中数量，然后转换为分数：

```python
if hits <= 0:
    score = 0
else:
    score = min(1.0, 0.25 + 0.15 * (hits - 1))
```

对应关系：

| 命中数量 | 分数 |
|---:|---:|
| 0 | 0 |
| 1 | 0.25 |
| 2 | 0.40 |
| 3 | 0.55 |
| 4 | 0.70 |
| 5 | 0.85 |
| 6+ | 1.00 |

## 11. Profile 相关度计算

每个 profile 会分别计算：

```text
must_track score
watch score
```

然后合成：

```text
relevance_score = 0.55 * must_score + 0.35 * watch_score
```

最高不超过 `1.0`。

也就是说：

- `must_track` 权重更高。
- `watch` 是辅助信号。
- 单个关键词命中不会直接等于高相关度，需要结合其它分数。

## 12. 基础新闻优先级

在 profile 分类之前，系统还会计算一个基础优先级 `base_priority`。

它来自 `app/pipeline.py`，主要考虑：

| 因素 | 含义 |
|---|---|
| freshness | 新闻发布时间的新鲜度 |
| source_quality | 来源质量 |
| topic_relevance | 主题相关度 |
| engagement | 互动热度，主要用于 HN |

### freshness

新闻越新，分数越高。当前使用半衰期模型：

```text
score = exp(-age_hours / half_life_hours)
```

### source_quality

来源质量来自：

```text
source_weights
```

同时会叠加 domain feedback 反馈调整。

### topic_relevance

基础 topic relevance 会检查 active profile 的关键词。

标题权重更高：

```text
title: 75%
summary: 25%
```

### engagement

目前主要用于 Hacker News：

```text
HN score
HN comments
```

普通新闻源没有明显 engagement 信号。

## 13. importance_score 计算

文章最终是否重要，主要看 `importance_score`。

当前公式大致是：

```text
importance_score =
  0.40 * base_priority
+ 0.35 * relevance_score
+ 0.10 * source_weight
+ cross_source_bonus
+ source_type_bonus
```

最高不超过 `1.0`。

## 14. 多来源加成

如果同一篇文章被多个来源发现，会增加分数：

```text
cross_source_bonus = min(0.16, 0.04 * (source_count - 1))
```

含义：

| 来源数量 | 加成 |
|---:|---:|
| 1 | 0 |
| 2 | 0.04 |
| 3 | 0.08 |
| 4 | 0.12 |
| 5+ | 0.16 |

这用于帮助识别多家媒体同时报道的重大事件。

## 15. 来源类型加成

不同来源类型有额外加成或扣分：

| 来源类型 | 加成 |
|---|---:|
| 高信任 wire 来源 | +0.12 |
| GDELT | -0.06 |
| HN Hot | +0.20 |
| CISA KEV | +0.18 |

GDELT 扣分的原因是它容易带来大量转载、地方站和重复事件，需要更谨慎。

## 16. confidence_score 计算

置信度大致由这些因素组成：

```text
关键词相关度
来源权重
多来源发现
```

公式大致是：

```text
confidence_score =
  0.55 * relevance_score
+ 0.25 * source_weight
+ cross_source_bonus
```

最高不超过 `1.0`。

## 17. news / radar 入选规则

默认 bucket 是：

```text
radar
```

只有满足条件才进入：

```text
news
```

### major_events

进入 `news.xml` 的阈值：

```text
importance_score >= 0.46
```

### devops_security

进入 `news.xml` 的阈值：

```text
importance_score >= 0.42
```

### 严重安全事件放宽

如果是 `devops_security` 且命中严重安全关键词，可以放宽：

```text
importance_score >= 0.36
```

### personal_interest

配置里虽然有阈值：

```text
personal_interest >= 0.75
```

但因为 `personal_interest` 不在 `news_profiles` 中，所以通常不会进入 `news.xml`。

## 18. RSS 输出限制

默认限制来自环境变量：

```text
NEWS_MAX_ITEMS=30
RADAR_MAX_ITEMS=30
HN_HOT_MAX_ITEMS=50
```

如果环境变量没有设置，则使用默认值。

## 19. RSS 输出前去重

数据库会保存完整候选和来源映射，但 RSS 输出前会再次去重。

当前去重逻辑是：

1. 对标题进行归一化。
2. 小写化。
3. 去掉标点。
4. 压缩空格。
5. 如果归一化标题长度大于等于 28，则用标题 fingerprint 去重。
6. 否则退回使用 URL 去重。

如果多个候选被认为是同一事件，会保留分数最高的一条。

这用于解决类似下面这种重复新闻：

```text
Fidelity data breach settlement...
```

同一新闻可能被多个地方站、转载站、GDELT 结果重复收录。现在 RSS 层会尽量只保留一条。

注意：

- 后台调试页面仍可能看到多个候选。
- `articles` 表仍会保存多个来源和候选信息。
- RSS 里会尽量保持低重复。

## 20. NewsAPI Everything 调用规律

有 `NEWSAPI_KEY` 时，系统优先调用 NewsAPI Everything。

Everything 使用：

```text
queries.newsapi_terms
```

作为 OR 查询词。

当前关键词包括：

```text
breaking news
war
conflict
ceasefire
sanction
tariff
export control
central bank
inflation
recession
supply chain
energy crisis
zero-day
critical vulnerability
ransomware
data breach
cloud outage
Kubernetes
Linux kernel
OpenSSL
GitHub Actions
Cloudflare outage
OpenAI
AI regulation
AI safety
```

Everything 同时会结合：

```text
newsapi.domains
```

限制来源域名。

发布时间窗口由：

```text
FETCH_HOURS
```

控制。

排序方式是：

```text
publishedAt
```

## 21. NewsAPI Top Headlines 调用规律

Top Headlines 是 fallback。

只有 Everything 没有抓到文章时，才会调用 Top Headlines。

Top Headlines 不使用 `queries.newsapi_terms`。

当前按语言抓取：

```json
"top_headlines_languages": ["en", "zh"]
```

类别当前为：

```text
category = null
```

## 22. GDELT 调用规律

GDELT 使用：

```text
queries.gdelt_terms
```

当前包括：

```text
war
conflict
sanction
tariff
central bank
zero-day
ransomware
data breach
cloud outage
critical vulnerability
supply chain attack
```

这些词会被分批组合成 OR 查询。

GDELT 更像是补充雷达源，因为它覆盖面广，但重复和转载较多，所以在分类阶段会有来源类型扣分。

## 23. CISA KEV 规则

CISA KEV 是安全漏洞类高价值来源。

它不是普通关键词新闻源，而是已知被利用漏洞目录。

如果启用：

```text
CISA_KEV_ENABLED=true
```

抓取结果会进入数据库，并在分类时获得：

```text
source_type_bonus = +0.18
```

这类内容更容易进入 `devops_security`。

## 24. HN Hot 规则

HN Hot 是独立功能，不混入普通 `news.xml`。

RSS 路径是：

```text
/rss/hn-hot.xml
```

它使用 Hacker News Algolia API 查询历史高热度帖子。

默认查询包括：

| Query | 主题 |
|---|---|
| `ai-learning` | AI 学习、机器学习课程、LLM 学习 |
| `devops-classics` | Kubernetes、Postgres、Linux、observability、distributed systems |

HN Hot 不看日期范围，只看历史总热度：

```text
points DESC, comments DESC
```

每个 query 默认保留 top 20，总 RSS 默认最多 50 条。

RSS 标题会带前缀：

```text
[HN Hot]
```

原因说明类似：

```text
HN 历史热帖：x points / y comments，匹配 ai-learning
```

## 25. reason_summary 生成规律

RSS 中展示的人类可读原因来自 `reason_summary`。

大致逻辑：

| 场景 | 原因说明风格 |
|---|---|
| 命中排除词 | 命中排除词，降噪处理 |
| 严重安全事件 | 严重安全/基础设施事件，命中特定关键词 |
| 重大新闻 | 重大新闻候选，说明命中词、来源数和分数 |
| 个人兴趣 | 个人兴趣候选，说明命中词 |
| HN Hot | HN 历史热帖，说明 points/comments 和 query |

目标是让 RSS 里能看到“为什么收进来”，而不是只看到一串分数。

## 26. 当前规则的实际取向

当前系统已经从“个人兴趣关键词驱动”转成了四层结构：

### 第一层：重大事件

关注：

```text
战争
冲突
停火
制裁
关税
出口管制
央行
通胀
衰退
能源危机
供应链风险
```

### 第二层：DevOps / 安全事件

关注：

```text
CVE
zero-day
remote code execution
software supply chain
npm packages compromised
GitHub compromised
勒索软件
数据泄露
云服务事故
Kubernetes
Linux kernel
OpenSSL
GitHub Actions
Cloudflare
Okta
```

### 第三层：技术行业重大新闻

关注：

```text
HN 高热度直通
OpenAI
Anthropic
GitHub
npm
Kubernetes
Cloudflare
关键人物加入/离职
收购
重大模型发布
```

### 第四层：个人兴趣

关注：

```text
AI
startup
healthtech
parenting
education
```

但这层当前主要作为 radar 和后台观察池，不再强行进入主新闻流。

## 27. 已知限制

当前规则还有一些天然限制：

1. 关键词匹配是边界匹配，不是真正的语义理解。
2. 没有做完整的事件聚类。
3. RSS 层可以减少重复，但数据库里仍会保留多个候选。
4. `personal_interest` 的质量高度依赖关键词质量，目前适合继续观察和调参。
5. GDELT 覆盖面很广，但噪音和重复较多。
6. `topic_terms` 更偏旧的基础打分和报告用途，v1 分类主要看 `topic_profiles`、`interest_keywords` 和 `exclude_terms`。

## 28. 调参建议

如果希望进一步降低噪音，可以优先调整：

1. 增加 `exclude_terms`。
2. 提高 `major_events` 和 `devops_security` 的 news 阈值。
3. 减少 `queries.newsapi_terms` 中过宽的抓取词。
4. 减少 GDELT 查询词。
5. 将 `personal_interest` 保持在 radar，不加入 `news_profiles`。

如果希望减少漏报，可以优先调整：

1. 增加 `major_news.must_track`。
2. 增加 `devops_security.must_track`。
3. 增加高信任来源。
4. 降低 `major_events` 或 `devops_security` 的 news 阈值。
5. 增强多来源发现的权重。

## 29. 一句话总结

当前新闻规则的核心是：

```text
用关键词扩大候选池，用 profile 和分数控制 RSS 噪音，用重大事件和安全事件规则保护主 feed，用 radar 承接不确定但可能有价值的内容。
```
