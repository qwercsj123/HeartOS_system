# HeartOS 对话路由升级实施方案

## 1. 先说结论

你现在的核心问题，不是单纯 `GLM-4.6` 不够强，而是 **同一个用户问题会在多套路由逻辑之间被重复猜测、重复分流、上下文还不一致**，最后呈现出来就像“模型很傻”。

结合当前代码，主要症结有 4 个：

1. 前端 `sendMsg()`、`requestedStudioToolFromChat()`、`routeByLLM()`、`dispatchAgentUnified()` 同时参与分流，职责重叠。
2. 后端 `/api/chat` 只是通用聊天代理，`/api/agent/auto-run` 又单独做一遍意图识别，形成“双脑路由”。
3. 路由结果太粗，只返回 `intent/target`，没有稳定的 `confidence / args / missing_fields / need_confirm`。
4. 普通问答、专业工具调用、资料问答、图像理解，都在争抢同一个入口，缺少“先分类，再执行”的分层。

所以这次升级的目标不应该只是“把前端路由挪到后端”，而应该是：

- 建一个唯一的对话编排入口
- 先做任务分类，再做工具选择
- 把“不确定”显式暴露给用户确认
- 让 ECG 专业能力和普通问答彻底解耦


## 2. 基于现有代码的真实现状

当前仓库里已经有可复用基础，不需要推倒重来。

### 2.1 已有能力

- 后端已有统一大模型网关：
  [gateway.py](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend/app/llm/gateway.py)
- 后端已有通用聊天接口：
  [main.py](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend/app/main.py:959) 的 `POST /api/chat`
- 后端已有自动分流接口：
  [main.py](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend/app/main.py:1030) 左右的 `POST /api/agent/auto-run`
- 前端已有对话主流程：
  [index.html](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/index.html:4641) 的 `sendMsg()`
- 前端已有规则匹配与工具执行能力：
  [index.html](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/index.html:4594) 的 `requestedStudioToolFromChat()`
  和 [index.html](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/index.html:4467) 的 `dispatchAgentUnified()`

### 2.2 当前最关键的结构性问题

1. `/api/chat` 和 `/api/agent/auto-run` 并存，但语义不同。
2. 前端先用正则猜一次，再调用后端让 LLM 再猜一次。
3. `routeByLLM()` 还保留在前端，虽然主流程里已弱化，但仍是遗留风险点。
4. 现有意图识别是“单层 intent 分类”，没有先判断“用户是在提问、在执行、在追问结果、还是在闲聊”。
5. 模型没有被要求输出缺失参数，所以很多“帮我分析一下”这类话，只能硬猜。


## 3. 建议的新架构

### 3.1 一个入口，三段式编排

统一保留一个后端入口：

- `POST /api/conversation/turn`

这一层替代现在“聊天接口”和“自动路由接口”分裂的局面。前端所有对话都只进这里。

后端内部按三段执行：

1. `Conversation Guard`
   负责身份问答、平台介绍、固定回复、限流、鉴权、空输入校验。
2. `Intent Router`
   负责识别这是：
   - `tool_call`
   - `knowledge_qa`
   - `result_interpretation`
   - `image_understand`
   - `report_generate`
   - `smalltalk`
3. `Skill Dispatcher`
   如果是工具任务，再细分到 ECG 数字化、特征提取、补全、报告生成等执行器。

### 3.2 不再直接让模型“从所有工具里猜一个”

建议改成两级判断：

1. 一级分类：先判断这句话属于哪一类任务
2. 二级路由：只有当一级结果是 `tool_call` 时，才在专业工具列表里继续选具体意图

原因很重要：

- 用户很多问题其实不是要执行工具，只是在问“这个功能是什么”“这个结果怎么看”
- 如果一上来强制 Function Calling，模型会被迫乱选一个工具，误触发概率会很高

### 3.3 加入显式状态机

每一轮对话建议输出统一结构：

```json
{
  "type": "tool_result | need_confirm | ask_missing | chat",
  "stage": "classified | confirmed | executed | fallback",
  "intent": "ecg_auto_digitize",
  "confidence": 0.91,
  "args": {
    "source_ids": ["src_1"]
  },
  "missing_fields": [],
  "message": "已识别为自动数字化任务，正在处理。"
}
```

这比现在只有 `reply + action` 强很多，因为前端终于知道“该直接执行、该确认、还是该追问缺参数”。


## 4. 推荐的意图体系

### 4.1 一级任务分类

- `tool_call`
- `knowledge_qa`
- `result_interpretation`
- `report_generate`
- `image_understand`
- `smalltalk`
- `fallback`

### 4.2 二级专业工具意图

- `ecg_auto_digitize`
- `ecg_manual_digitize`
- `ecg_feature_extract`
- `ecg_reconstruct`
- `ecg_result_analyze`
- `rag_search`
- `agent_run`

这里我建议把文档里的 `chat_qa` 再拆细一点，至少拆成：

- `knowledge_qa`
- `result_interpretation`
- `smalltalk`

否则用户问“这个 ECGOmics 结果怎么看”，系统很容易被归到普通聊天，失去专业性。


## 5. 核心接口设计

### 5.1 新接口

`POST /api/conversation/turn`

请求体建议：

```json
{
  "message": "帮我分析一下这张心电图",
  "context": {
    "conversation_id": "conv_xxx",
    "selected_source_ids": ["file-001"],
    "has_image": true,
    "has_csv": false,
    "has_xml": false,
    "last_tool_intent": "",
    "last_tool_result_id": ""
  },
  "history": [
    { "role": "user", "content": "..." },
    { "role": "assistant", "content": "..." }
  ],
  "client_hint": {
    "intent": "",
    "source_type": "image"
  }
}
```

响应体建议：

```json
{
  "type": "need_confirm",
  "intent": "ecg_feature_extract",
  "confidence": 0.72,
  "args": {
    "source_ids": ["file-001"]
  },
  "missing_fields": [],
  "message": "我理解你想对所选心电数据做 ECG 特征提取，是否开始？"
}
```

### 5.2 确认接口

`POST /api/conversation/confirm`

```json
{
  "intent": "ecg_feature_extract",
  "args": {
    "source_ids": ["file-001"]
  }
}
```

### 5.3 为什么不建议继续复用现在的 `/api/chat`

因为当前 [schemas.py](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend/app/schemas.py) 里的 `ChatRequest/ChatResponse` 是“通用聊天代理模型”，字段中心是：

- `system`
- `messages`
- `provider`
- `model`

它适合做底层 LLM 网关，不适合做“对话编排协议”。

建议：

- `/api/chat` 保留为底层模型调用接口
- 新建 `/api/conversation/turn` 作为产品级对话入口


## 6. 后端落地建议

### 6.1 目录结构

建议在 `heartos_backend/app` 下新增：

- `conversation/router.py`
- `conversation/dispatcher.py`
- `conversation/prompts.py`
- `conversation/schemas.py`
- `conversation/service.py`

这样可以把当前 [main.py](/Users/chen/Documents/heartos/HeartOS_system/HeartOS/heartos_backend/app/main.py) 里臃肿的对话逻辑拆出来。

### 6.2 路由器实现建议

不要让 GLM 直接输出自由 JSON，优先走 Function Calling 或严格 schema 化输出。

推荐两个模型调用：

1. `classify_turn()`
   输入：用户问题 + 来源摘要 + 最近 4 轮历史
   输出：一级分类、置信度、是否需要工具
2. `route_tool_intent()`
   输入：同样上下文 + 工具列表
   输出：具体工具意图、置信度、args、missing_fields

### 6.3 参数补全比“自报 confidence”更重要

文档里强调 `confidence` 是对的，但仅靠模型自报置信度不够。

真正决定体验的，是它能不能识别：

- 有没有选中文件
- 选中的文件能不能做该任务
- 当前问题是在“执行工具”还是“解释结果”

所以建议让模型同时输出：

```json
{
  "intent": "ecg_feature_extract",
  "confidence": 0.81,
  "args": {
    "source_ids": ["file-001"]
  },
  "missing_fields": [],
  "reason": "用户明确表达要进行特征提取，且已存在可用 ECG 数据源"
}
```

### 6.4 调度器要纯后端化

现有工具执行端点可以继续保留：

- `/api/ai-ecg-digitize`
- `/api/ecgomics/analyze`
- `/api/ecg-reconstruct`

但是执行决策应统一在后端完成，而不是后端只告诉前端“你去调哪个接口”。

更理想的形式是：

- 前端只发送对话请求
- 后端完成意图判断
- 后端直接调用工具服务
- 前端只负责展示结果

这样才是真正的“智能编排”，也能避免前端逻辑越来越重。

### 6.5 会话记忆不要直接塞全部历史

建议只传最近 6 到 8 轮摘要，并额外维护结构化会话状态：

- 当前选中来源
- 最近一次执行的工具
- 最近一次工具结果摘要
- 当前是否处于“等待确认”状态

这比把长历史原样扔给模型更稳。


## 7. 前端改造建议

### 7.1 需要保留的

- `getHeartosCannedReply()`
- `requestedStudioToolFromChat()` 作为极速规则层
- 现有右侧 Studio 工具执行 UI

### 7.2 需要下线或降级的

- `routeByLLM()` 应彻底删除
- `dispatchAgentUnified()` 逐步从 `/api/agent/auto-run` 迁移到 `/api/conversation/turn`
- 普通聊天不要再直接拼超长 `srcContent` 给底层模型

### 7.3 sendMsg 的目标形态

`sendMsg()` 只做四件事：

1. 收集输入和已选来源
2. 调用统一后端对话接口
3. 按 `type` 渲染 `tool_result / need_confirm / ask_missing / chat`
4. 把工具结果交给现有展示组件

这样前端就从“决策者”变回“展示层”。


## 8. 比老师文档还需要补上的 6 个关键点

### 8.1 增加 `ask_missing`

用户经常会说：

- “帮我分析一下”
- “开始处理”
- “跑一下这个”

这类输入不该直接失败，也不该乱猜，应该返回：

- 缺少哪个文件
- 需要哪种格式
- 当前选中的文件为什么不匹配

### 8.2 区分“问功能”和“执行功能”

比如：

- “ECGOmics 是什么” 是问知识
- “帮我做 ECGOmics” 才是执行

这个区分是你当前误路由最多的地方。

### 8.3 区分“解释结果”和“重新计算”

比如用户说：

- “这个结果说明什么”
- “这个补全后的波形靠谱吗”

这不该再触发工具，而应该走 `result_interpretation`。

### 8.4 为 ECG 高风险任务加确认门槛

建议：

- `ecg_manual_digitize`：可直接执行
- `ecg_auto_digitize`：高置信度直接执行
- `ecg_feature_extract`：需确保数据源兼容
- `ecg_reconstruct`：建议默认确认后执行

因为补全与分析的“误触发成本”高于普通聊天。

### 8.5 加可观测性

至少记录：

- 原始用户输入
- 一级分类结果
- 二级意图结果
- confidence
- 是否确认
- 最终调用哪个工具
- 成功/失败/耗时

否则你后面没法系统性调优。

### 8.6 建评测集

不要只靠主观感觉判断“聪不聪明”。

建议建 60 条测试语料：

- 20 条工具执行
- 15 条概念问答
- 10 条结果解释
- 10 条模糊表达
- 5 条恶意或无关输入

每次改 prompt 或工具描述，都跑一次离线评测。


## 9. 分阶段实施计划

### 第一阶段：统一后端对话编排层（3 到 4 天）

- 新建 `conversation` 模块
- 新建 `/api/conversation/turn`
- 把 `/api/agent/auto-run` 里的路由逻辑迁过去
- 保留 `/api/chat` 作为底层模型网关
- 给路由结果补上 `type/confidence/args/missing_fields`

验收标准：

- 用户任意一句话都只经过一个后端入口
- 不再需要前端 `routeByLLM()`

### 第二阶段：前端接线与确认气泡（2 到 3 天）

- `sendMsg()` 切到新接口
- 渲染 `need_confirm`
- 渲染 `ask_missing`
- 兼容现有 Studio 展示区域

验收标准：

- 模糊输入不会误触发
- 缺参数时会追问而不是报错

### 第三阶段：工具执行彻底后端化（3 到 5 天）

- 后端 dispatcher 直接调工具服务
- 前端不再决定调哪个分析接口
- 工具输出统一封装返回

验收标准：

- 前端只负责展示
- 新增工具时不需要再改很多聊天逻辑

### 第四阶段：评测与调优（2 天）

- 建立测试语料
- 调整一级分类 prompt
- 调整工具 description
- 统计误判样例并迭代


## 10. 你这个项目最值得优先做的最小闭环

如果你现在时间紧，我建议不要一次做完全部重构，先做这个最小版本：

1. 保留现有前端规则匹配
2. 新建 `/api/conversation/turn`
3. 把 `/api/agent/auto-run` 的逻辑迁进去
4. 增加 `confidence + need_confirm + ask_missing`
5. 前端删掉 `routeByLLM()`

只做这 5 步，用户体验就会明显提升，而且风险可控。


## 11. 我对老师这份文档的专业判断

这份文档的大方向是对的，尤其是：

- API Key 后移到后端
- 前端只保留轻规则
- 后端做语义路由
- 置信度低时要求确认

但如果照文档原样直接做，仍然会有 3 个隐患：

1. 仍然把“所有问题”压成单层工具选择，普通问答和结果解读会继续误路由。
2. 过度依赖模型自报 `confidence`，但没有 `missing_fields`，交互还是会生硬。
3. 工具执行仍然偏前端驱动，长期会越来越难维护。

所以我的建议是：

**保留老师方案的方向，但升级为“对话编排层”而不是“单次工具路由层”。**


## 12. 下一步建议

如果你愿意，我下一步可以直接继续帮你做两件事里的任意一个：

1. 按这个方案，直接在当前仓库里把后端 `/api/conversation/turn` 和前端 `sendMsg()` 的第一版改出来。
2. 把这份方案进一步整理成更正式的“答辩/汇报版”文档，适合发老师或放进项目材料。

