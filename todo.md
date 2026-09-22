# TODO — 待改进与待验证清单

> 整理于 2026-09-21，来源为本轮对 VibeTrading（v0.1.15 + 本地 16 个提交）的排查。
> 每项都附现象、根因、证据与建议动作，可直接开工。
>
> **2026-09-22 更新**：第一节的 9 项代码缺陷与 O-2 已全部修复并验证（新增回归测试 +
> 真实端点实测），状态标记为 **[已修]**；下方保留原始现象/根因/证据作为背景。
> 仍未处理：**O-1**（8 个会话是否从备份恢复，待定）；**O-3** 的残留目录已不存在，无需动作。
> 第三节的验证项已跑完，结果见该节末尾。

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

### O-1 8 个会话被界面删除，可从备份恢复 [可选]

- 之前恢复的 16 个会话中，现有 8 个已不在磁盘上（5 个帝欧水华 002798 + 3 个量子基金/个股），
  与后端日志里的 `DELETE /sessions/<id>` 200 请求逐条对应，属界面删除而非故障。
- 完整正文仍保存在 `D:\Projects\VibeTrading-backups\state-20260921-154528\sessions.consolidated.db`（17 会话 / 67 消息）。
- **动作**：需要的话按索引重建这 8 个会话目录（跳过已存在的 id）。

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
