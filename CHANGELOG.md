## [Unreleased]

### 派发策略三项（借鉴 workbuddy2api-hub）

- **区域连坐暂停**（新模块 `trae_restriction.py`）：同一区域里 `wave_window`（默认 600s）内
  被上游限制的**不同账号**达到 `wave_min`（默认 3）个，判定为区域级风控扫荡，整个区域
  暂停派发 `hold_seconds`（默认 1800s）。期间 `open_upstream` / SOLO 通道直接回
  `429 + Retry-After`（`RegionHold`，文案说明是连坐暂停而非模型限流），不选号、不换号、
  不打上游；**凑够一波的那一次请求自己也当场停手**（只挡下一个请求等于没挡：这一轮
  已经把同批剩下的号全试探了一遍）；7×24 定时巡检同样跳过该区域。暂停落盘到
  `usage/restriction.json`（重启不清零），到期自动恢复。凭证过期 / 4008 额度用尽 /
  上游排队 10605 / 内容审核 / 同一账号反复失败均不计入波次（避免误停整区）。阈值可由
  `TRAE_RESTRICTION_*` 或 `settings.json` 的 `restriction` 段调整，`POST /settings/save`
  可改（调小时同步截断现有暂停）。
- **会话暖号降级**：`SessionAffinity` 从「一个会话一个 uid」升级为「一队暖号」
  （`history=3`，front 优先）：粘性号不可用时逐个试历史里的暖号，并把出错的号
  **降级**（`demote`）到队尾而不是整条解绑。`open_upstream` / 信封错误 / SOLO 失败路径里
  6 处 `unbind` 全部改为降级 —— 被换下来的号往往还握着这段会话的前缀缓存。
  指定的 uid 不在暖号队里时（页轮转不绑粘性，出错的就是这种）什么都不动，
  不再顺手把无辜的队首挤下去。
- **超长会话页轮转**：估算输入（`messages + tools` 字符数 / 4）≥ `TRAE_AFFINITY_PAGE_TOKENS`
  （默认 100000）的会话不再钉死在一个号上，改为在 `TRAE_AFFINITY_PAGE_SIZE`（默认 2）个
  账号组成的一「页」里轮转（页成员按 uid 排名稳定派生、页轮转独立于整池轮转、不绑粘性）。
  页内无人可用时退回整池选号；池子不大于一页时不分页。
- 新增 `test_issue_dispatch.py` 47 项回归：暖号降级/历史/过期、页成员与页内轮转、
  连坐波次判定（去重/窗口/区域独立/落盘/阈值覆盖）、派发闸门与 403 分类策略，
  以及真 403 走派发路径的三种结局（凑够一波当场停手 / 没凑够照旧换号 / 鉴权 403 不停区）。
- **设置页面板卡片**「区域连坐暂停」：状态行显示哪个区域在暂停、还剩多久、几点自动恢复
  （本地每秒倒计时，到期自己消失）与「判定窗口内 n/阈值 个账号」；三个阈值可直接改并保存
  （调小时同步截断现有暂停）；「立即解除暂停」一键解除并清掉波次。`test_issue_dashboard.js`
  增加 9 项（倒计时格式化、区域名映射、暂停/无暂停/波次文案、卡片与接口对齐、
  状态行文案与英文包精确键一致），共 60 项。
- 修复 `--usage-dir` 下连坐暂停的状态文件仍写到默认 `usage/` 的问题（import 期接线早于
  `main()` 解析参数，现在启动时按生效目录重新接线）；状态文件读取容忍 BOM
  （带 BOM 时会被当成「没有暂停」，等于重启后立刻恢复打号）。
- 连坐判定不再把「客户端自身问题」算成账号被限制：`permission_error` /
  `authentication_error` 一类 403 与 1001 鉴权失败都排除在波次之外 ——
  否则一个没给这些号开通的模型就能把整区停 30 分钟，而且真因会被 429 盖住。
  1001 的**两种形态都认**：信封文案 `trae 1001 auth:`，以及上游直接用 403 投递的
  裸正文 `{"code":1001,...}`（后者没有信封包装，此前会被当成风控扫荡）。
- 保存 `restriction` 段时先检查 `settings.json` 可读（复用 `_settings_is_rewritable`）：
  文件解析不了就回 400 并原样保留，绝不用一个读失败得到的 `{}` 去覆盖
  `panel_password_hash` / `api_keys`（那会让面板退回默认口令、所有 Key 失效）。
  `nan` / `inf` 一类非有限数值按非法值丢弃（此前会在 `int(wave_min)` 处抛
  `ValueError`，面板保存直接 500，落盘还会写出非标准的 `NaN` / `Infinity`）。
- 超长会话判定不再在**每个请求**上深度遍历整个 prompt：先过「分页是否开启 / 有无会话键」
  的便宜判断，再只数 `content`（实测 2 万条小消息从 47ms 降到 5ms，常规请求 0.04ms）。
- 「测试账号」也受区域暂停约束（SOLO 分支不经过 `open_upstream`，之前会绕过闸门）。
- 删除 `trae_routing.pick` / `candidates`（无调用方，且 `allowed_uids` 参数会让它
  一接上就 TypeError）；连坐暂停的日志与报错文案统一走 `trae_proxy.realm_label`
  （面板另有自己的一份 `restrictionRealmLabel`）。
- 页成员排名不再缓存：单槽缓存在双区交替选号时每次都失效（等于没有缓存），
  而排序规模只有账号数，远小于一次上游往返。

### 签到到账记录不被巡检覆盖

- 修复「今日已签到」巡检把当天到账记录冲回 `+0`：`task_daily_checkin` 在 `already=True` 时保留原有的 `earned_credit` / `earned_source` / `balance` 与真实签到时间 `at`，只更新 `checked_in` / `credits` 这类状态字段。此前无条件的 `account.extra["checkin"] = {...}` 会用 `claim_checkin` already 分支的 `earned_credit=0` 覆盖，导致每次巡检都把真实到账额（以及 `at`）改回「到账 0」—— 等于把「签到到账 +0」的修复原地抵消。
- 新增 `test_checkin_earned.py` 两个回归：已签到巡检保留原记录（含 `at` 与前后余额快照）、真实领取覆盖旧记录并刷新时间。

### 账号积分更新时间

- 修复账号页「更新于 未知」：`Account.public()` 现在直接下发 `creditsUpdatedIso`（来自 `usage_updated_at`），面板不再读一个从未赋值的字段。
- 「更新于」只记录**最后成功刷新**：`account_fetch_credits` 在 usage / entitlement 两个接口都失败时保留旧时间戳，不把失败尝试伪装成刚更新。
- `test_stats_completion.py` 增加 `creditsUpdatedIso` 19 位 ISO 串回归。

### 最近请求的档位列

- SOLO 通道现在会把客户端的 `reasoning_effort` 归一化后以 `reasoning_effort_level` 下发给上游，并同时落盘 `reasoning_effort_requested` / `reasoning_effort`；「最近请求」的档位列不再为空。
- 客户端没指定档位时，按模型目录的 `default_level` 回填实际档位，面板显示「服务端默认」；历史行读取时同样补默认档，不改写日志。
- 新增 `test_issue_solo.py [2c]`：验证 requested/actual 落盘与 SOLO 下发字段。

### 签到流水去噪

- 启动 / 定时巡检的「今日已签到」只是状态检查（`claim_checkin` 先查 status，已签到直接返回 `already=True`，不会再调领取接口），不再写入 `activity.jsonl`；只有真正领取、跳过、失败才记流水。
- 手动签到仍保留「已签到」记录；`/accounts/activity` 默认隐藏旧的启动/定时「已签到」行（日志仍完整保留，传 `include_checks=1` 可看完整审计）。
- 新增 `test_checkin_activity.py` 回归：启动巡检不写 `already` 流水、手动仍写、面板过滤只隐藏自动检查。

### 最近请求的积分 / 积分拆分

- SOLO 上游的 `token_usage` 帧实测只有 token / cache / reasoning，**没有积分字段**；因此「最近请求」的积分列现在按**账户实际消耗积分**估算：账户 entitlement 的累计 consumed ÷ 本地日志的等价花费，再按每条请求的三段（非缓存输入 / 缓存输入 / 输出）等价花费占比分摊。
- 表格用 `≈` 标注估算值，悬停提示「上游未返回单请求积分」；估算合计与账户实际消耗对得上，不把估算伪装成官方扣费。
- 上游真返回 `credit` 时优先用真实值；真实值没有三段拆分时，也按等价花费占比补出拆分。
- `/usage` 汇总、`/usage/timeseries`、`/accounts/credits/usage`、`/usage/remaining` 的积分口径同步使用估算值。

### API 等价花费修复

- 修复 `trae_pricing.compute_row` 缺失导致所有请求都记成 `cost_usd=0 / priced=false` 的问题：恢复按 `pricing/trae_pricing.json`（OpenRouter 表，含长上下文 bands）计价，「用量洞察」的 API 等价花费不再恒为 `$0.0000`。
- 历史 usage 行不重写日志：读取时按当前价表现算等价花费，旧数据也能立即显示；新行仍按落盘值直接汇总。
- 定价覆盖表 `usage/pricing-overrides.json` 同时作用于新行记账与读取兜底；`/pricing` 的 `table` / `unpriced` 改为包含生成价表，模型卡参考价恢复可用。

### SOLO 用量补全

- 「最近请求」的首字 / 速度恢复：SOLO（`/v1/chat/completions` 的 Agent 模型与 `/v1/agent/chat`）此前记账不带 `ttft_ms` / `gen_ms`，表格只能显示 `—`；现在按第一个正文/推理/工具增量计时，流式与非流式都写入真实 TTFT 与 tokens/s（拿不到首字仍记 `null`，不伪造 0）。
- SOLO 的 `token_usage` 若带缓存 / 推理细分（`cache_read_input_tokens`、`cached_tokens`、`prompt_tokens_details`、`reasoning_tokens` 等）会原样落盘，「缓存」列可显示真实命中率；上游没报时仍显示 `—`，不把未知算成 0%。

### 统计页收尾

- 修复 `--usage-dir` 下 `/usage/stats` 仍读默认 `usage/` 的问题：模块名 `__main__` 时懒 `import trae_proxy` 会重复执行模块级 `stats.configure()`，把运行期路径覆盖回默认目录；统计与调度器改为优先复用 `__main__`，并给 `stats.configure()` 加显式配置门闩。
- 热力图补齐 `heatmap.max`，前端按本机峰值分级着色；旧接口缺字段时前端也会自行兜底。
- 「本月」改为自然月 1 日 00:00，不再等同于最近 30 天。
- 近 24 小时缓存趋势改为滚动小时桶，避免跨天时把昨天同一钟点并进今天的点。
- `/usage/remaining`、`/accounts/credits/grants`、`/accounts/credits/usage`、`/pricing` 从硬编码空数据改为真实本地聚合；用量洞察的「积分包 / 获取记录」「最快耗尽账号」「积分消耗」恢复可用。
- 账号额度视图补充 `size` / `composition` / `source`，账号行与 `/credits/summary` 能显示套餐内、资源包、签到包等构成。
- `/usage/analytics` 在无输入/输出样本时把缓存命中率、推理占比返回 `None`，不再伪造成 0%。
- 新增 `test_stats_completion.py`（38 项）覆盖上述回归。

### 签到 / 权益模块收尾

- 签到与权益刷新现在会写入 `usage/activity.jsonl`：成功、已签到、失败、国际版跳过、权益刷新都有记录，并带 `trigger`（手动 / 启动 / 定时）与细状态；「签到 / 权益流水」不再永远为空。
- `/accounts/activity` 返回 `summary`（成功 / 失败 / 跳过 / 累计积分），「签到 / 权益操作成功率」卡片有真实数据。
- Trae 没有独立 Pro 福利包接口：看板文案从「领取 Pro 福利包」改为「刷新权益 / 用量」，避免把权益刷新伪装成领取；活动/兑换码仍在官方客户端处理。
- 签到中心新增「自动签到」状态行：显示调度是否开启、每日时刻、上次/下次巡检；调度暂停时明确提示。
- 修复 `/scheduler/config` 404：支持自定义整点签到/保活时刻、0~1 小时窗口抖动、签到错峰；支持 `0 9,21 * * *` 这类整点 cron，非法表达式返回 400。
- 调度器状态补齐 `checkin_times` / `keepalive_times` / `cron` / `timezone`，并持久化自定义小时。
- `trae_tasks.usage_dir()` 优先读取 `__main__.USAGE_DIR`，`--usage-dir` 下签到流水与 `tasks.json` 不再写到默认目录。
- 新增 `test_checkin_activity.py`（9 项）覆盖流水、汇总、自动签到触发与排程配置。

## [1.6.0] - 2026-11-12

### ✨ 新增功能

#### 浏览器 OAuth 登录

- 新增浏览器 native_ide + S256 PKCE 授权登录（CN / INTL），本机随机端口回调、面板会话绑定、一次性授权码交换、取消、过期及脱敏状态查询。
- 新增远程浏览器/容器部署的回调地址粘贴完成入口；移除旧设备授权探测，保留桌面、JSON 和 refreshToken 导入。
- OAuth 凭证先原子落盘再报告成功；重新登录保留账号启停、权重及路由配置；登录设备密钥与设备标识随账号持久化并用于后续请求。
- 新增隔离 OAuth 协议、真实本地 HTTP 和前端状态机回归，无真实账号登录；说明见 [docs/OAUTH.md](docs/OAUTH.md)。
- 安全约束：`state`/`login_trace_id` 双重绑定且单次消费，回调仅接受对应会话的 `127.0.0.1` 回环监听，交换与用户信息仅允许各区域官方主机，公共状态接口不返回任何令牌、PKCE verifier 或私钥。

## [1.5.1] - 2026-11-11

### 问题修复

- 修复添加账号读取不存在的 OAuth state 导致断连；不支持的设备授权返回 501 JSON，面板提供桌面凭证、JSON 和 refreshToken 三种真实导入入口（#2）。
- 修复桌面/JSON 导入返回结构不匹配、显式路径被忽略、macOS/Linux 凭证路径不正确及扫描结果被前端误过滤（#5）。
- 修复成功请求缺失 usage 时漏记、SOLO 未记账、未知缓存命中被算成 0%，以及用量统计日期窗口和日志路径不一致（#6）。
- 模型上下文配置实现持久化、读取/清除和模型列表回显，并在 raw/SOLO 入口实施本地 UTF-8 保守预算准入；不截断历史，不伪造上游窗口参数（#6）。
- 管理 POST 在执行前检查面板会话；SOLO 不再回退使用禁用账号，补齐成功/失败记账及配置预算检查。
- 修复所有 5 个 shell 启动/防火墙脚本的 Git 执行位；镜像增加脚本 chmod 和运行资源完整性检查（#4）。

### 运行参数与回归

- 每号默认并发由 8 调整为 2；后台签到默认错峰 45~120 秒，定时窗口增加 0~25 分钟抖动，保留手动同步接口短间隔兼容。
- 新增账号导入、上下文、用量、HTTP、SOLO、调度、前端和打包回归；CI 覆盖 Python 3.11 / 3.13 / 3.14。
- 本机离线回归：基线 240 项、上下文/桌面/调度 84 项、用量 64 项、HTTP 95 项、SOLO 52 项、前端 53 项、打包 63 项通过；Windows 下 5 项 POSIX 文件执行位检查跳过。

## [1.5.0] - 2026-11-09

### ✨ 新增功能

- **全新看板 UI 重构**：移植自包含单页 CPAMC 动效系统与仪表盘，支持流式吞吐监控、折线/柱状图表实时渲染。
- **路由与算力编排工作台**：新增账号编排优先层（Tiers）、调度策略（轮询 / 加权轮询 / 优先填满）、会话亲和性管理及模型决策实时命中预览。
- **深度用量统计分析**：单次日志扫描计算 6 大核心 KPI、Top 账号/提供商/模型贡献排行、近 24 小时缓存命中走势与 365 天活跃热力图。
- **多语言（i18n）动态切换**：新增国际化包结构，支持中/英双语无刷新即时切换。
- **Trae 业务深度适配**：完美融合国际版额度预警与自动降级至国内版备用模型，以及网关配置快照一键备份与恢复。

## [1.4.3] - 2026-11-05

### 问题修复
- SOLO 通道 device_id / machine_id 缺省值改为按账号 UID 派生，避免多个部署共用同一硬编码设备导致 4017 风控。
- 账号级 extra.device_id / machine_id 仍然优先。
- 版本号升级到 **1.4.3**。

## [1.4.2] - 2026-11-05

### 🐛 问题修复

- SOLO 通道（Agent 模型）遇到上游 `progress_notice` 的字符串 data 帧时抛
  `AttributeError: 'str' object has no attribute 'get'`，请求 502（实测 kimi-k3 连续 8 次失败 4 次）。
- 带 `tools` 的请求统一回 4001：上游 `FunctionDefinition.tools[].function.parameters` 要 string，
  网关直接送 OpenAI 的 dict。

### ✨ 新增功能

- SOLO 通道支持工具调用：上游 `tool_calls` 增量帧按 index 聚合，回填标准 OpenAI
  `tool_calls`（非流式）与 `delta.tool_calls`（流式），`finish_reason` 相应改为 `tool_calls`。
- SOLO 通道支持多轮工具往返：保留 `assistant.tool_calls` 消息，并给 `tool` 回执补上
  `tool_call_id` + `name`（name 由历史 `assistant.tool_calls` 的 id→name 映射）。
  修复第二轮回灌时报 `4027 tool_call_id is not found ... tool name: ''`。
- 工具调用严格校验：`arguments` 必须是**完整 JSON 对象**（截断/畸形直接报错，不自动修复）；
  上游 `finish_reason` 不是 `stop`（如 `length` 截断）时不再包装成可执行的 `tool_calls`。

### ✅ 测试

- `_test_trae.py` 新增 [6] SOLO 通道分组：非对象 data 帧、tools.parameters 归一、
  tool_calls 聚合、工具消息保留与各类缺字段报错，共 26 项断言。

- 版本号升级到 **1.4.2**。

## [1.4.1] - 2026-10-23

### 问题修复
- 账号测试不再使用非法 model=auto，CN/INTL 分别选择合法 raw 模型。
- 账号池装载时清理已过期冷却，避免 no usable account。
- 统计页补齐卡片、图表和热力图动效；日期区间可交互切换。
- 版本号升级到 1.4.1。

## [1.4.0] - 2026-10-23

### 新增功能
- 接入 Trae SOLO 纯 HTTP 通道：/api/agent/v3/llm_utils_chat。
- /v1/chat/completions 自动识别 20 个 SOLO/Agent 模型。
- 新增 /v1/agent/chat 显式入口，支持流式与非流式。
- 新增统计概览：KPI、Top 账号/提供商、365 天热力图、缓存窗口和趋势图。
- 版本号升级到 1.4.0。

## [1.1.5] - 2026-10-22

### ✨ 新增功能

- 模型页区分「网关可用」与「仅 App」：官方客户端目录存在、但 raw_chat model_list 未返回的模型不再标成「不可用」。
- 模型页统计改为「网关可用 N 个 · 仅 App M 个」。
- 前端按 upstream_key/id 去重，避免同一模型出现两行。
- 工具提示明确说明：官方 App 可见、网关 raw_chat 未接入、直接请求会 4023。

### 🐛 问题修复

- 修复国内版账号可在 App 使用、但看板显示「不可用」的误导。
- 修复 DeepSeek-V4-Flash / V4.1-Flash 等目录模型的状态文案不准确。
- 修复模型目录与动态清单叠加时可能出现重复行的问题。

### 🎨 体验优化

- 「仅 App」模型不再整体变灰，保留完整参数/价格/能力信息。
- 模型页统计更贴近真实：5 个网关可用 + 20 个仅 App。

### ⚠️ 其他变更

- 版本号升级到 **1.1.5**。
- 网关仍只支持 raw_chat 模型；仅 App 模型需要 Agent 通道，当前未接入。
## [1.1.4] - 2026-10-22

### ✨ 新增功能

- 账号页标题旁新增「总可用积分 899.61」，一眼可见。
- 积分汇总保留两位小数，不再把 899.61 截断为 899。

### 🐛 问题修复

- 修复账号页只有单个账号行显示积分、没有汇总入口的问题。
- 修复 `/credits/summary` 的 `by_realm` / `totals` 使用整数截断的问题。

### 🎨 体验优化

- 账号池始终展示国内版 + 国际版，区域徽标区分。
- 总积分、签到积分、套餐三个信息分层显示。

### ⚠️ 其他变更

- 版本号升级到 **1.1.4**。
## [1.1.3] - 2026-10-22

### ✨ 新增功能

- 账号行显示「总积分」：直接读取 Trae v2 `ide_user_ent_usage` 的 `usage_summary`。
- 总积分 = `total_amount - consumed_amount`（当前实测 900 - 0.39 = 899.61）。
- 保留签到积分与套餐展示，不再把签到积分误当总积分。
- 账号池同时显示国内版与国际版账号，不再只显示当前区域。

### 🐛 问题修复

- 修复只读取本地 entitlement、看不到 Trae 总可用积分的问题。
- 修复国内版使用 v1 entitlement 缺少 `usage_summary` 的问题；CN 改用 v2。
- 修复积分汇总把 899.61 截断成 899 的问题，保留两位小数。
- 修复账号池被当前视图过滤、国际版账号不显示的问题。

### 🎨 体验优化

- 账号行格式：`总积分 899.61 · 签到 100 · 免费`。
- 刷新权益覆盖双区全部账号。

### ⚠️ 其他变更

- 版本号升级到 **1.1.3**。
## [1.1.2] - 2026-10-22

### ✨ 新增功能

- 账号积分真实刷新：`/accounts/credits/fetch` 现在调用 Trae `fetch_usage` / `fetch_entitlement` 与国内版签到状态。
- 账号行显示签到积分、套餐、剩余/上限/已用额度。
- `/credits/summary` 增加 `checkin` / `checkin_at` 字段。

### 🐛 问题修复

- 修复「刷新权益」实际调用签到任务、从未刷新积分的错误。
- 修复 `Account.fetch_credits()` 只读本地快照、不请求上游的问题。
- 修复账号页 `credits` 字段缺失，导致积分显示不出来的问题。
- 修复刷新后上游 entitlement 未写回账号、重启后又变回旧值的问题。

### 🎨 体验优化

- 刷新按钮显示成功/失败账号数量与首个错误原因。
- 国际版无签到接口时仍展示套餐/额度，不误报失败。

### ⚠️ 其他变更

- 版本号升级到 **1.1.2**。

## [1.1.1] - 2026-10-20

### ✨ 新增功能

- 更新检测改为页面加载强制刷新，避免 6 小时缓存挡住新 Release。
- 后端检查缓存缩短为 5 分钟；顶部徽标与设置页会更快出现更新提示。

### 🐛 问题修复

- 修复 1.1.0 中自动检查命中旧缓存、发布新版本后不能立即提示的问题。

### 🎨 体验优化

- 更新提示与一键更新流程保持同一入口，减少等待。

### ⚠️ 其他变更

- 版本号升级到 **1.1.1**。

## [1.1.0] - 2026-10-20

### ✨ 新增功能

- 看板自动检测 GitHub 最新 Release，有更新时顶部显示「有新版本 → 一键更新」。
- 设置页「运行信息」增加一键更新按钮，展示当前版本与目标版本。
- 一键更新流程：下载 release tarball → 安全解压 → 自动备份 → 替换代码 → 自动重启网关。
- 更新完成后页面自动轮询 `/health`，版本一致后自动刷新。
- 更新失败时保留 `update.log` 与 `update-backups/update-*.tar.gz`，可手动回滚。
- Docker 部署检测：容器内拒绝自更新，提示使用 `docker compose pull`。

### 🐛 问题修复

- 修复 GitHub tarball 下载 `415 Unsupported Media Type`：改用 `application/vnd.github+json`。
- 修复更新检查没有返回 `tarball_url`，导致无法一键下载的问题。
- 修复看板顶部更新徽标只能跳 Releases、不能直接更新的问题。

### 🎨 体验优化

- 更新按钮显示目标版本号；下载、重启、完成都有状态提示。
- 更新包解压排除 `accounts/`、`usage/`、`.git/`、`update-backups/`，不会碰账号数据。
- 更新前自动生成 tar.gz 备份，更新日志写入 `update.log`。

### ⚠️ 其他变更

- 版本号升级到 **1.1.0**。
- 更新源固定为 `shuishuipingan/trae2api-hub` 的 GitHub Release。

# Changelog

所有重要变更都记录在这里。版本号遵循 [Semantic Versioning](https://semver.org/)。

## [1.0.0] - 2026-10-19

### ✨ 新增功能

- 国际版直连：使用当前客户端版本头 + `Authorization: Bearer`，不再依赖 Trae 桌面版、调试端口或 Agent 桥。
- 双区账号池：国内版 / 国际版独立选号、权重、冷却、会话亲和与自动换区。
- 模型目录：国内 25 个 / 国际 10 个，保留官方展示名、上下文、最大输出、思考档位与 Free / 升级标记。
- API Key 治理：模型白名单、每日请求上限、每日 Token 上限，超限返回 429。
- 额度预警：`/quota` / `/v1/quota` 暴露 `notify_usage` 剩余额度，面板顶部与账号行告警。
- 自动降级：额度耗尽或模型需升级时自动切换备用模型/出口，响应头 `X-Trae-Degraded`。
- 按 Key 图形化用量页：Token 趋势、模型分布、缓存命中率、平均延迟、日配额进度与最近请求。
- 配置备份 / 恢复：`/config/export` 与 `/config/restore`，恢复前自动写回滚备份。
- Docker / GHCR：多架构镜像（linux/amd64 + linux/arm64），Compose 一键部署。
- 8 页签玻璃拟态看板：总览、账号池、模型库、数据指标、Key 用量、维护任务、运行日志、设置。

### 🐛 问题修复

- 修复国际版旧版本头导致的 `4015`。
- 修复国际版使用 `Cloud-IDE-JWT` 导致的 `4023 model_unknown`。
- 修复 `4008 quota exceeded` 被误判为上游瞬态故障的问题；现在映射为 429 + `Retry-After: 60`，不冷却账号。
- 修复额度错误导致账号被错误冷却、模型被误标不可用的问题。
- 修复国际版免费额度耗尽后无法区分“风控”和“额度”的问题。
- 修复配置恢复时先写 settings、后校验账号可能导致半恢复的问题。
- 修复 API Key 编辑保存时模型白名单被静默丢弃的问题。

### 🎨 体验优化

- 看板新增额度告警条与 Key 用量页，移动端导航支持 8 个页签。
- 模型表增加「升级 / 目录」徽标与官方禁用原因。
- API Key 编辑行增加模型白名单、每日请求、每日 Token 三个可视化字段。
- 账号行显示国际版剩余额度与「额度已用尽」状态。
- README 重构，补充看板预览、Docker、模型状态、Key 配额与自动降级文档。
- 增加 `tests` 与 `docker-publish` GitHub Actions。

### ⚠️ 其他变更

- 项目版本从内部迭代版本统一为 **1.0.0**。
- 国际版 Agent 桥保留为可选排障 fallback，默认关闭。
- 免费额度、模型可用性与升级策略以 Trae 官方实时返回为准。
- 使用本项目即表示你理解并遵守 Trae 官方服务条款及所在地法律。
