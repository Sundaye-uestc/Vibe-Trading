# TODO — 待改进与待验证清单

> 整理于 2026-09-21，来源为本轮对 VibeTrading（v0.1.15 + 本地 16 个提交）的排查。
> 每项都附现象、根因、证据与建议动作，可直接开工。
>
> **2026-09-22 更新**：第一节的 9 项代码缺陷与 O-2 已全部修复并验证（新增回归测试 +
> 真实端点实测），状态标记为 **[已修]**；下方保留原始现象/根因/证据作为背景。
> **O-1** 已按你的决定处理：8 个孤儿索引行直接删除（删除前留了一致性快照），未从备份恢复会话；
> **O-3** 的残留目录已不存在，无需动作。
> 第三节的验证项已跑完，结果见该节末尾。
>
> **2026-09-29 更新**：新增第五节「A 股数据源现状与待办」——逐源实测结论、
> Tushare 积分门槛（**需你操作**）、eastmoney/akshare 被主机侧拒绝、
> mootdx 上游协议不兼容的完整排查与回退记录。
> 同时把本轮的两处代码修复（引擎时间轴、grounding 日期校验）补进第四节。

## 一、待修复（按优先级）

### P0-1 加载器注册竞态：并发的第一批取数会瞬间返回空 [已修]

- **现象**：`technical_indicators` 偶发 `<0.1s` 失败，报 `No data returned for 600127.SH`；同批次里
  `get_market_data` 抓同一标的是成功的。
- **根因**：`backtest/loaders/registry.py` 的 `_ensure_registered()` 是「先置标志再导入」且**无锁**：

  ```python
  if _registered:
      return
  _registered = True                      # ← 标志先置上
  for mod in _loader_modules:             # ← 27 个模块，导入要几百毫秒
      importlib.import_module(mod)
  ```

  线程 A 置标志后开始导入；线程 B/C/D 看见标志已为 True 立刻返回，但 `LOADER_REGISTRY` 仍是空的，
  于是 `get_loader_cls_with_fallback` 对链上每个源都抛 `NoAvailableSourceError("Unknown data source: …")`，
  `fetch_market_data` 返回空字典。
- **触发条件**：一批**只读**工具是并行执行的（`src/agent/loop.py` 的 `ThreadPoolExecutor`，最多 8 线程），
  所以冷启动后的第一批并发取数必然踩中；单独调用或重试时注册表已热，所以看起来"偶发"。
- **证据**：独立进程内 4 个同标的并发请求 → 行数 `[337, 0, 0, 0]`，失败三个耗时同为 454ms；
  DEBUG 日志出现 15 行 `Unknown data source: {tencent,mootdx,eastmoney,baostock,akshare}`；第二轮（注册表已热）全部正常。
- **建议动作**：双重检查加锁（`threading.Lock` 包住"检查 + 导入"），或把 `_registered = True` 移到导入循环之后并加锁。

### P0-2 两个数据工具尚未纳入相同调用缓存 [已修]

- **现象**：修复后同一轮运行里，`ths_hot_reason {"date":"2026-09-21"}` 仍真跑 4 次、
  `technical_indicators {"interval":"1d","lookback":120,...}` 仍真跑 3 次。
- **根因**：这两个工具没有声明 `cache_ttl`。上一轮只给 10 个工具加了
  （`get_market_data`、`tencent_quote`、`get_stock_news`、`get_sector_info`、`get_fund_flow`、
  `get_margin_trading`、`get_dragon_tiger`、`cninfo_announcements`、`get_financial_statements`、`search_symbol`）。
- **证据**：修复后那次运行共 54 次真实执行 / 71 次缓存应答；除上述两个工具外，
  其余每个不同参数都只真正执行了一次。
- **建议动作**：给 `ths_hot_reason_tool.py`、`technical_indicator_tool.py` 补 `cache_ttl = 300.0`。

### P1-1 长研究以"工具语法占位文案"收场（不希望再出现） [已修]

- **现象**：界面出现 `My final response could not be delivered: it contained tool-call syntax… Please ask me to continue.`
- **根因**：`src/session/service.py` 里 `AgentLoop(..., max_iterations=50)` 写死。预算耗尽时模型仍想调工具，
  于是把工具调用语法（DSML / `<invoke …>` / `<｜tool_calls｜>`）当正文输出；`_looks_like_tool_call_syntax`
  判定"这不是答案"，重试一次后无预算，释放 [loop.py] 的固定兜底文案。
- **结构性缺陷（关键）**：为强制纯文本而丢掉工具定义的，正是**最后一轮**——

  ```python
  is_last_iteration = (iteration == self.max_iterations)
  tool_defs = None if is_last_iteration else self.registry.get_definitions()
  ```

  而拒收工具语法后的重试条件是 `if iteration < self.max_iterations: continue`。
  也就是说：**收尾轮一旦答得不对，就没有第二次机会**，必然落到占位文案。80% 处虽有
  `wrap_up_at` 提醒，但模型仍可能继续取证并把预算烧完。
- **证据**：本次会话 `tool_call_syntax_in_answer` 仅 1 次，正位于预算耗尽的最后一轮；
  轨迹里的 iter 为会话累计值（75 - 前一段 25 = 50，恰好是上限）。
- **建议动作**（按性价比排序）：
  1. **给收尾轮留余量**（主修）：把强制纯文本轮安排在倒数第二轮，或对"被判为工具语法"的
     收尾轮放行一次**不消耗预算**的重试。这一条能直接消灭该文案，其余是降低触发概率。
  2. **减少无谓轮次**：见 P0-2 与 P1-2——预算大半烧在重复取证上，压下来才有轮次真正用于收尾。
  3. **提高/参数化轮次上限**：`src/session/service.py` 的 `max_iterations=50` 提至 80，
     或改为环境变量/会话参数。
  4. **兜底更友好（可选）**：与其只吐占位文案，可尝试剥掉 DSML 标签、保留其中的正文；
     或自动续跑一次再交还用户。

### P1-2 上下文压缩的"请重新调用"提示造成必然的重复取数 [已修]

- **现象**：同一工具同参数被反复调用（修前 `tencent_quote` 12 次、`get_stock_news` 8 次等）。
- **根因**：两层叠加——
  1. 第 1 层压缩 `_microcompact` 只保留最近 `KEEP_RECENT`（默认 3）条工具结果，更早的替换为占位符；
  2. 该占位符文案明确写着 *"If you need these values, call the tool again with the same arguments."*，
     等于指示模型重抓。10 个工具的批次只留 3 条 → 7 条立刻作废 → 下一轮重抓。
- **已做缓解**：`BaseTool.cache_ttl` + loop 复用既有 `_called_identical` 缓存（命中即返回，不再走网络）；
  `KEEP_RECENT` 已可配置（`VIBE_TRADING_KEEP_RECENT_TOOL_RESULTS`，默认仍为 3）。
- **建议动作**：把窗口默认值调到能容纳一个典型批次（如 12），或改写占位符措辞；
  两者都能减少"步骤数"本身，而缓存只能降低单步代价。

### P1-3 东财搜索类接口已失效（影响美股代码解析） [已修]

- **现象**：`AAPL.US` 经东财解析返回 `None`（并被进程内缓存为 miss）。
- **根因**：`searchapi.eastmoney.com` 与 `search-api-web.eastmoney.com` 对任何参数都返回
  **同一份 698 字节缓存体**（内容为 `passportWeb` 用户资料，与关键词无关），即接口不再受理查询。
- **证据**：两域名、多个关键词返回字节级相同的响应；A 股新闻因此改走 akshare 兜底。
- **建议动作**：美股 secid 改用其它来源（如直接依赖 yfinance 路径），或等东财恢复后移除兜底。

### P1-4 更换背景刷新即失效 [已修]

- **现象**：设置自定义背景后，当前页面能看到；**刷新就消失**。
- **根因**：`localStorage` 配额被撑爆，且写入失败被吞掉。
  - `frontend/src/hooks/useBackground.ts` 允许上传 `MAX_SIZE = 5MB`，但 5MB 图片转成 base64 数据
    URL 后约 **6.7MB 字符**；`localStorage` 每源配额约 5MB，且按 UTF-16 存储，实际能放的字符更少。
  - `Layout.tsx` 的 `handleBgSave` 是**先** `setBg(dataUrl)`（界面立即生效）**再** `saveBackground()`，
    而写入包在 try/catch 里，异常只走 `console.error` —— 于是"当前页面看得见、刷新后读不到"。
- **证据**：`getStoredBackground()` 读的就是 `localStorage`，刷新后返回 null；本次会话此前的
  "点应用背景没响应" 问题也出自同一条链路。
- **建议动作**：写入前把图片**降采样**再存（背景按视口尺寸、`canvas.toDataURL('image/jpeg', 0.85)` 通常
  几十 KB 即可）；或把大对象改存 IndexedDB；同时把写入失败**显式反馈**给用户，而不是只打日志。

### P1-5 换头像：选完本地图片点"确定"无响应 [已修]

- **现象**：给智能体更换头像，选中本地上传的图片后点确定，界面没有任何反应（弹窗不关、头像不变）。
- **根因**：与 P1-4 同源——`frontend/src/components/chat/AvatarSelector.tsx` 的 `handleSave`
  （约 126 行起）直接 `localStorage.setItem(key, JSON.stringify(config))`，**没有 try/catch**：

  ```tsx
  localStorage.setItem(key, JSON.stringify(config));   // 大图 → QuotaExceededError
  window.dispatchEvent(new CustomEvent("avatar-changed", { detail: { tab } }));
  onClose();                                           // 抛异常后走不到这里
  ```

  配额异常在 `setItem` 处抛出，后面的事件派发与 `onClose()` 都不会执行，所以表现为"点了没反应"。
- **补充说明**：智能体与用户两个 tab 走的是同一段代码（只有存储 key 不同：`qa-agent-avatar` /
  `qa-user-avatar`），用户头像上传大图同样会中招；另外保存按钮的 `disabled` 条件是
  `!imageDataUrl`，若图片未成功读入，按钮会是禁用态，看起来也像"点不动"。
- **建议动作**：头像显示是 CSS `background-size: cover`，根本不需要原图——上传后先
  缩放到 256×256 再存；`setItem` 包 try/catch 并 toast 报错；确认按钮的禁用态给出可见提示。

### P2-1 后端未配置 logging handler [已修]

- **现象**：应用的全部 `logger.warning(...)` 走 Python 的 `lastResort` 落到 **stderr**。
- **影响**：本身无害，但正是这一点与 `start.py` 不读 stderr 组合成过死锁
  （已在 start.py 侧修好：每个流一个读取线程）。
- **建议动作**：配置一个真正的 handler（文件或 stdout），不再依赖 `lastResort`。

### P2-2 会话删除/改名不同步搜索索引 [已修]

- **现象**：经界面删除会话后，`~/.vibe-trading/sessions.db` 里仍留有该会话与消息的索引行；
  会话改名后索引里的标题也不更新。
- **根因**：`DELETE /sessions/{id}` 只删目录与 goal 记录，未清理 `SessionSearchIndex`。
- **建议动作**：删除路径补一次索引清理；改名路径补一次 `index_session` 更新。

## 二、数据与运维

### O-1 8 个会话被界面删除，可从备份恢复 [已处理：删除孤儿索引行]

- 之前恢复的 16 个会话中，现有 8 个已不在磁盘上（5 个帝欧水华 002798 + 3 个量子基金/个股），
  与后端日志里的 `DELETE /sessions/<id>` 200 请求逐条对应，属界面删除而非故障。
- 完整正文仍保存在 `D:\Projects\VibeTrading-backups\state-20260921-154528\sessions.consolidated.db`（17 会话 / 67 消息）。
- **动作**：已确认这 8 个是界面主动删除、目录不再存在的孤儿行，按决定直接清理索引
  （`sessions` 18→10、`messages` 81→62），**未**重建会话目录。删除前用 SQLite backup API 留了
  一致性快照 `D:\Projects\VibeTrading-backups\sessions.db.before-orphan-cleanup-20260922-104857`，
  需要回退时用它替换 `~/.vibe-trading/sessions.db` 即可。清理后重启后端，O-2 启动告警不再出现。

### O-2 旧会话目录消失的根因未查明 [已修]

- `agent/sessions/`（旧版本的 `SESSIONS_DIR`）在升级前已不在磁盘上，`migrate_legacy_state` 因此无物可搬。
  数据仅存于搜索索引，已手工重建一次。
- **建议动作**：加一个启动期校验——若索引里有会话而目录缺失，提示或自动重建，避免再次静默丢失。

### O-3 启动期迁移告警 [可选]

- 每次启动都会打印 `Not migrating …agent\runs\safe_run: … already exists`，
  是测试跑出的残留目录与运行根目录同名所致，无害，可直接清理该残留。

## 三、验证待办

- [x] **完整后端回归已跑完**（`$env:PYTHONUTF8="1"; python -X utf8 -m pytest tests/ -q`）：
      `12655 passed / 56 failed / 11 errors`。用 `git archive HEAD` 取纯净副本跑同一批失败文件做对照，
      两边结果**完全一致**（`48 failed / 175 passed / 11 errors`）→ 本轮改动零新增失败。
      失败全部为环境性：Windows 符号链接权限（`WinError 1314`，11 个 error 全在此列）、
      pandas 3.0 索引/键语义差异。
- [x] 前端 `npx tsc --noEmit` 干净；`npm run test:run` **65 文件 / 632 测试全通过**。
- [x] 真实端点实测：东财美股解析 `AAPL.US→105.AAPL`、`IBM.US→106.IBM`、`IMO.US→107.IMO`
      （Nasdaq 主源 + 腾讯兜底）；A 股/港股路径未受影响。索引同步经真实 API 建→改名→删验证，
      删除后 `sessions.db` 无残留行。
- [x] 提交并推送本轮改动。

### 尚未验证（需人工确认）

- 头像/背景（P1-4/P1-5）只做了单测：降采样数学、canvas 不可用分支、JPEG 导出、写盘失败分支。
  **真实浏览器的 localStorage 配额行为未驱动浏览器验证** —— 建议上传一张大图设背景后刷新确认保留。
- P1-1 的收尾轮逻辑由单测覆盖，但未跑一次真实的、把轮次预算烧光的长研究。

## 四、已修复（本轮完成，供回顾）

| 问题 | 位置 |
|---|---|
| start.py 顺序读 stdout/stderr 导致管道写满、后端线程永久阻塞 | `start.py`（每流一个线程；补 `PYTHONUTF8` 等子进程环境；修单服务模式标签错位） |
| 东财新闻接口返回 JSONP 导致解析必然失败 | `backtest/loaders/eastmoney_client.py`（识别 JSONP，回退按文本重取） |
| 东财新闻接口整体失效（缓存体）→ A 股新闻静默返回空 | `src/tools/stock_news_tool.py`（akshare 兜底；schema 变化改为显式报错；global 给可读提示） |
| 巨潮公告 HTTPS 被 403 | `src/tools/cninfo_announcements_tool.py`（HTTPS→HTTP 回退，请求头随协议一致） |
| `/skills` 被注册两次，带 `category` 的处理器被静默遮蔽 | `src/api/skills_routes.py`、`src/api/system_routes.py`（并加重复路由守卫测试） |
| 侧栏直接读 localStorage，受限环境白屏 | `frontend/src/components/layout/Layout.tsx`（`safeGet/safeSet` + 跨标签同步） |
| 设置页在 `skill_data_sources` 缺失时崩溃 | `frontend/src/pages/Settings.tsx`（防御式读取） |
| `/skills` 页面刷新返回 JSON 而非页面 | `frontend/vite.config.ts`（按 `Accept` 分流） |
| 相同参数的只读取数被反复执行 | `src/agent/tools.py`（`cache_ttl`）、`src/agent/loop.py`（TTL 生效）、10 个数据工具标注 |
| 压缩窗口不可调 | `src/agent/loop.py`（`VIBE_TRADING_KEEP_RECENT_TOOL_RESULTS`，默认不变） |
| 回测产物时间戳全部落在 1970，成交被逐笔拒绝（`no_bar`） | `backtest/engines/base.py`（`_align` 不再用 `index.asi8` 求并集——pandas 3 的微秒被 `pd.DatetimeIndex` 当纳秒解释；改为合并 datetime64 值本身，并在两处 `searchsorted` 用 `as_unit(dates.unit)` 对齐两侧单位） |
| `rebalance_mask` 显式日期选不到任何 bar | `backtest/rebalance_mask.py`（`bounds` 改用 `dates.as_unit("ns").asi8`，与恒为纳秒的 `Timestamp.value` / `offset.nanos` 同单位） |
| 表格里加粗的日期单元格使整行价格被判「无工具证据」 | `src/agent/grounding.py`（`_strip_emphasis` 剥离 Markdown 强调标记；新增 `_DASH_DATE_PAIR_RE` 屏蔽 `08-17/08-27`；补 `last_close` 价格别名） |

## 五、A 股数据源现状与待办（2026-09-29 实测）

链路定义在 `backtest/loaders/registry.py` 的 `FALLBACK_CHAINS["a_share"]`：

```
tencent → mootdx → eastmoney → baostock → akshare → tushare → local
```

对每个源真实拉取 `600522.SH`（2026-09-14 → 09-29）后的结论：

| 源 | 实测 | 说明 |
|---|---|---|
| `tencent` | **可用** | 10 行，09-28 收 31.32 |
| `baostock` | **可用** | 与 tencent 逐日数值完全一致 |
| `tushare` | **token 已就位，账号权限不足** | 见 T-1 |
| `eastmoney` | **被拒** | 见 D-1 |
| `akshare` | **被拒** | A 股路径与 eastmoney 同主机，见 D-1 |
| `mootdx` | **不可用（已回退卸载）** | 见 D-2 |
| `sina` | **返回空** | 无异常、0 行；根因未查 |

净结果：日常回测不受影响（`tencent` 在链首且正常），但**目前没有可信的交叉验证第二来源**。

> **2026-10-11 补充**：上一轮为移除 mootdx 而卸载了 `py-mini-racer`，但该包是 akshare 的
> 依赖（`akshare/air/air_zhenqi.py` 需要 `MiniRacer`），且卸载后残留的空目录被 Python 当成
> 命名空间包，于是 `import akshare` 直接抛 `ImportError`——比 D-1 的「被拒」更彻底：akshare
> 源当前**完全不可用**，`tests/test_akshare_loader.py::TestIsForex::test_eurusd_matches`
> 因此失败。修法：`pip install py-mini-racer` 即可恢复。

### T-1 Tushare token 已配置，但账号缺 120 积分（**需你操作**）

- **现象**：token 写入 `~/.vibe-trading/.env`（第 20 行）与 `agent/.env`（第 29 行）后，
  调用返回 `抱歉，您没有接口(daily)访问权限`（`daily` 与 `trade_cal` 均如此）。
- **判定依据**：该报错是**权限**错误而非认证错误——无效 token 会返回「您的token不对，请确认」。
  说明 token 有效、账号已认证，只是积分不足。`daily` / `trade_cal` 门槛为 **120 积分**，
  新注册账号为 0 积分。
- **建议动作**：登录 [tushare.pro](https://tushare.pro) → 个人主页 → **完善个人信息**，
  免费获得 120 积分，`daily` 即解锁；**之后无需再改任何配置**。
- **注意**：`src/preflight.py:213` 的 `_check_tushare` 只校验「token 非空且非占位符」，
  不验证接口权限，所以预检会显示 `OK — token configured`，这个绿色是**虚的**。
- **环境变量优先级**：`src/providers/llm.py:1229` 按
  `~/.vibe-trading/.env` → `agent/.env` → `cwd/.env` 顺序加载且 `override=False`，先到先得。
  （本轮首次写入时曾用 `re.subn` 只替换正则前缀，把占位符尾巴 `your-tushare-token` 留在行内，
  导致 `agent/.env` 的值变成 74 字符；已改为整行替换并复核为 56 字符。）

### D-1 eastmoney / akshare 被主机侧拒绝（确认与代理无关）

- **现象**：`push2his.eastmoney.com` 返回
  `RemoteDisconnected('Remote end closed connection without response')`，连续 3 次复现。
- **排除代理**：Windows 系统代理已开启（`127.0.0.1:7897`，`urllib.request.getproxies()` 可见），
  但**走代理与显式绕过代理（`Session.trust_env = False`）得到同样的 `RemoteDisconnected`**；
  且 `Find-NetRoute` 显示访问 TDX/东财时走 **WLAN 直连（ifIndex 16）**，未经 TAP/VPN 网卡。
- **根因推断**：`backtest/loaders/_http.py` 模块文档即写明该源「按源 IP 限流、突发请求会被临时封禁」，
  与现象吻合。首次探测时曾成功返回一次（09-28 收 31.32），之后持续被拒。
- **影响**：`akshare` 的 A 股路径同样走该主机，因此两者一起失效。
- **建议动作**：等待封禁自然解除（可用 `_http.py` 的 `*_MIN_INTERVAL` 环境变量降低突发频率）；
  长期由 T-1 的 tushare 补位。

### D-2 mootdx 上游协议不兼容（已尝试并回退）

- **依赖陷阱**：`mootdx` 要求 `httpx<0.26.0` / `tenacity<9.0.0`，而本 venv 是 `httpx 0.28.1`
  （被 `openai`、`mcp`、`langsmith`、`langgraph-sdk`、`vibe-trading-ai` 依赖）与 `tenacity 9.1.4`
  （被 `langchain-core` 依赖）。**常规安装会连带降级整个 LLM 栈**，只能用 `--no-deps`。
- **Python 3.14 崩溃**：`python -m mootdx bestip` 直接失败——它走 asyncio 路径调用
  `asyncio.get_event_loop()`，该「隐式取当前事件循环」的行为在 3.14 已移除；
  改用 `mootdx.server.bestip(sync=True)` 才能选服务器并写入配置。
- **根因在上游**：配置完成后，`bars()` 对 6 台可达 TDX 服务器**全部返回 0 行**；
  绕过 mootdx 直接用 tdxpy 验证：`get_security_count` 返回 **24281**（握手成功、协议帧正常），
  但 `get_security_bars` / `get_security_quotes` 全空。`mootdx 0.11.7` 与 `tdxpy 0.2.7`
  均已是**最新版**，且 TDX 流量未经过代理。
  → tdxpy 的响应解析与当前通达信服务器协议不兼容，**项目侧无法修复**。
- **当前状态**：已 `pip uninstall` 回退（`mootdx` / `tdxpy` / `prettytable` / `py-mini-racer`）
  并删除 `~/.mootdx/`；`httpx` / `tenacity` / `pandas` 均复原。
  之所以回退而非保留：装上游标表会把一个**取不到数据**的源当作「可用」插在链路第 2 位
  （`baostock` 之前），只在 `tencent` 失效时才暴露，属于净负面改动。
- **将来若 tdxpy 修复协议**，重新安装即可：
  `pip install --no-deps mootdx tdxpy prettytable py-mini-racer`，再跑 `bestip(sync=True)`。

### D-3 附带确认：中天科技 09-28 收 31.32（跌停）

- `tencent` 与 `baostock` 两条路径一致返回 `2026-09-28 close = 31.32`；
  与 `eastmoney` 首次成功那一次的单点值也相同 → **三处吻合**。
- 相对 09-24 的 34.80 为 **-10.0%**，即 A 股主板跌停。此前存疑的数字至此证实。
