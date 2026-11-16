# 运维值班处置 Agent（NanoClaw 专项化改造）

> 把通用 Agent 引擎改造成**敢接生产告警**的值班助手：
> 告警进飞书 → 按真实 runbook 分步诊断 → 高风险动作强制人工签核 → 全程留痕可复盘。
>
> 本目录当前状态：**数据底座已完成**，Agent 改造进行中。

## 一、为什么要专项化

通用 Agent 框架市面上太多，缺的不是能力而是**场景约束**。在运维值班这个场景里：

- **"Agent 能执行 shell"本身是不可接受的**——必须有人工确认、必须有审计、必须能止损；
- 所以 `permissions.py` 的三档权限、`workspace/approvals/` 的挂起落盘、17 条危险命令正则、
  熔断、工具超时、预算护栏、12 类 trace，在这个场景里**从加分项变成产品本体**；
- 换个场景（写代码、做客服）这些能力都是装饰；在值班场景里它们是准入条件。

一句话叙事：**通用框架能跑 demo，但没人敢让它碰生产；我做的就是把"敢让它碰"工程化。**

## 二、数据来源（全部真实、公开、可复现）

| 数据 | 数量 | 来源 | 许可 |
| --- | --- | --- | --- |
| 真实告警定义（名称/表达式/for/severity/summary/runbook_url） | **129 条** | [kube-prometheus](https://github.com/prometheus-operator/kube-prometheus) 的 PrometheusRule | Apache-2.0 |
| 真实处置手册（Meaning / Impact / Diagnosis / Mitigation） | **97 条有步骤** | [runbooks.prometheus-operator.dev](https://runbooks.prometheus-operator.dev/) | Apache-2.0 |
| 告警 Message / Severity 权威描述 | 63 条 | [kubernetes-mixin](https://github.com/kubernetes-monitoring/kubernetes-mixin) 的 `runbook.md` | Apache-2.0 |
| 真实系统日志样本 | 2,000 行（INFO 1920 / WARN 80） | [LogHub](https://github.com/logpai/loghub) HDFS_2k | 研究用途 |
| 真实事故复盘索引 | 282 条链接 / 10 类 | [danluu/post-mortems](https://github.com/danluu/post-mortems) | — |

### 关键事实（别在面试里说错）

- 告警规则、表达式、`for` 时长、severity、runbook 处置步骤 **都是真实的**，来自上游生产配置；
- **日志**是 LogHub 的真实 HDFS 日志，**不是** K8s Pod 日志（来源不同，已在此标注）；
- **指标时序是合成的**：按告警表达式语义生成（如 `rate(...)>0.1` 生成对应速率的序列），
  目的是让「查指标」工具有可查的数据，**不是**真实监控数据；
- 因此对外表述应当是「告警与处置流程基于真实生产规则，环境数据部分为受控模拟」，不要说成"生产环境数据"。

## 三、目录结构

```
ops_agent/
├── README.md                 本文件
├── catalog/                  ★ 提交到版本库，离线可跑、测试可复现
│   ├── alerts.json           129 条规范化告警
│   └── runbooks.json         告警 → 含义/影响/诊断步骤/处置动作/风险等级
├── scripts/
│   ├── fetch_raw_data.py     下载原始数据（可复现）
│   └── build_catalog.py      解析告警规则 + 抓取 runbook 页 → catalog/
└── data/raw/                 原始数据（.gitignore 已排除，共约 0.6 MB）
    └── runbooks_html/        104 个 runbook 页面缓存
```

## 四、常用命令

```powershell
# 1) 下载原始数据（首次或想刷新时）
python ops_agent/scripts/fetch_raw_data.py

# 2) 生成 / 重新生成 catalog（页面有缓存，约 30 秒）
python ops_agent/scripts/build_catalog.py

# 3) 强制重新抓取 runbook 页面
python ops_agent/scripts/build_catalog.py --refresh
```

## 五、风险分级怎么定（重要设计说明）

`catalog/runbooks.json` 里每个处置动作带一个 `risk`，取值 `read / mutate / destructive`。

**第一版是纯文本关键字推断，实测有误判**：

- 「Remove unused images」最初被判成 `read`（关键字表里只写了 `remove the pvc`）——
  这是危险的误判方向：**把危险动作判成安全**，等于让 Agent 直接动生产；
- 「Replace physical cables」曾被判成 `destructive`（`replace` 放错了桶）——
  这个方向不危险，但会让确认流程噪音过大。

修正后的原则：**宁可误判成危险**（多一道确认），**不可误判成安全**。
当前分布：`read 134 / mutate 45 / destructive 36`（共 215 个动作）。

> **运行时不以文本为准**：文本推断只是给 catalog 做冷启动标注。
> Agent 实际执行时，风险等级由**工具声明**决定（`delete_pod` = mutate、`delete_node` = destructive、
> `query_metric` = read），文本标签仅用于「把跑偏的动作拦住」这一道兜底。

## 六、已知限制（不粉饰）

1. **25 条告警的 runbook 抓取失败**：部分 `runbook_url` 指向已下线站点（如 `coreos.com`）或返回 404，
   这些告警只有元数据、没有处置步骤；
2. **处置动作里仍有噪声**：解析自 HTML 的列表项会混进「See Debugging Pods」这类纯引用，
   以及个别代码块残片；已按长度 ≥ 12 字符 + 含字母过滤，但没有做到完全干净；
3. **风险分级是启发式的**，不是人工逐条标注——36 条 destructive 里仍有个别偏保守的（如清理镜像）；
4. **指标数据是合成的**，见上文第二节说明。

## 七、下一步

- [ ] 场景库：以 129 条告警为骨架，构造「告警 → 诊断 → 处置」的可回放场景，并埋入危险动作陷阱
- [ ] 工具改造：把 10 个通用工具收敛为领域工具包（`query_metric` / `search_log` / `run_runbook_step` / `open_change_ticket`）
- [ ] 工具声明风险等级，接入三档权限与人工签核
- [ ] 离线回放评测器：诊断准确率 / 越权拦截率 / 人工确认比例 / 步数 / 成本 / 处置时长
- [ ] `replay_trace`：把 trace 渲染成中文值班时间线（复盘报告）
