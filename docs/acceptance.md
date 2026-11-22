# 复测位移验收 v1

本文档是 [多视图点重建契约](contract.md) 与 [离线标定更正与点云交付](delivery.md) 的增量：在交付链路之上增加一个可离线运行的验收子命令。原有 `camera-tracks --source ... --output ...` 与 `build/correct/query/export/apply/view` 的输入、输出与退出码完全不变。

```
camera-tracks accept --baseline VIEW1.json --resurvey VIEW2.json \
                     --config ZONE.json --output REPORT.json
```

测量组对同一验收区做两轮扫描后，两份点表已各自通过交付链路导出为 `view` JSON，坐标在同一工程坐标系内。现场标签可能重新贴过，因此**不能**把同名 track 或最近的点直接当作同一测点：验收区配置给出现场候选对应和少量人工确认，命令据此判定哪些必检点确实超限、哪些还需查标签、整个验收区能否签字。位移大本身正是要发现的结果，从不作为排除对应的条件。

命令只读输入：不回写点表、不做坐标拟合、不修改任何已有文件。成功时向标准输出打印一行 JSON 摘要并退出 0；任何输入错误打印 `error: <可读诊断>` 到标准错误、退出 2，此时不创建报告、也不覆盖已有报告。即使输入合法，报告也始终以"同目录临时文件 + 原子新建"创建，**已存在的报告路径一律拒绝覆盖**；输出路径不得指向任一输入文件。运行依赖仅 Python 3.11+ 标准库，无网络。

## 1. 输入：两份 view 导出与一份验收区配置

两份输入必须是现有 `view` 命令导出的完整 JSON（`kind: "camera-tracks-view"`）。命令重新校验每份视图：结构、点身份 `(sequence, track_id)`、精确有理数坐标、以及按交付契约规范化编码重算的点表指纹——篡改任何点值而保留旧指纹会被拒绝。随后逐项核对配置中指定的 `dataset_id`、`version`、`fingerprint`：**三者都须匹配**。即使两个版本的点表指纹相同，拿错版本（例如把 version 0 的文件当作 version 1）也会被拒绝。

两次测量的身份空间相互独立：基准侧与复测侧的 `(sequence, track_id)` 各自解释，互不复用。

验收区配置（UTF-8 JSON，一次只处理一个验收区）：

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-acceptance",
  "zone_id": "zone-196-north",
  "displacement_limit": "2",
  "baseline": {"dataset_id": "survey-196", "version": 0, "fingerprint": "…"},
  "resurvey": {"dataset_id": "survey-196-resurvey", "version": 0, "fingerprint": "…"},
  "baseline_required": [{"sequence": 10, "track_id": "marker"}, …],
  "resurvey_required": [{"sequence": 10, "track_id": "marker"}, …],
  "candidates": [
    {"baseline": {"sequence": 10, "track_id": "marker"},
     "resurvey": {"sequence": 10, "track_id": "marker"}}, …
  ],
  "confirmations": []
}
```

- `zone_id`：验收区 ID（1..64 个 `[A-Za-z0-9_:-]`）。
- `displacement_limit`：非负位移上限，精确有理数文本——整数、最简 `n/d`，或有界十进制字符串；按精确值解析，不经过浮点。
- `baseline` / `resurvey`：基准测次与复测测次各自的 `dataset_id`、`version`、`fingerprint`。
- `baseline_required` / `resurvey_required`：两侧各自的必检点清单，各 1..4096 个且**数量相同**；以 `(sequence, track_id)` 引用，每个身份必须存在于相应侧的点表内，其他点不参与验收。
- `candidates`：现场候选对应，0..32768 条；每条的两个端点都必须在相应侧的必检清单内。
- `confirmations`：人工确认对应，可为空；每条必须属于候选，且任意两条不得共用同侧端点。

以下均为输入错误（退出 2，无报告）：重复必检身份、重复候选、候选引用不存在或不在相应必检范围内的点、确认不属于候选、多条确认共用同侧端点、两侧必检数量不同、视图指纹或端点三元组不匹配、位移上限为负或格式非法。候选与确认的排列顺序不代表优先级；只改变配置数组的排列，报告字节不变（包括所选证据）。

## 2. 位移判定

位移按 XYZ 三轴欧氏距离判断，沿用导出的精确有理数坐标：比较 `Σ(Δx)² > limit²`，全部为 `Fraction` 精确运算，不依赖浮点舍入。**等于上限不算超限。**

## 3. 完整对应

一份完整对应必须覆盖两轮所有必检点、两侧每点恰好用一次、只使用候选、并兑现全部人工确认。兑现确认等价于先从两侧删去已确认端点，再在剩余候选上求完美匹配。

### 3.1 对应不完整

若合法输入没有完整对应，仍生成报告（退出 0），结论为 `incomplete_correspondence`（对应不完整，不能验收）。报告给出基准侧一个非空的缺配集合及其在剩余候选中可用的全部复测邻居：

```json
"complete": false,
"conclusion": "incomplete_correspondence",
"deficient": {
  "baseline_points": [{"sequence": 10, "track_id": "marker"}, …],
  "available_resurvey_points": […]
}
```

该集合是最大匹配的交错可达集（Dulmage–Mendelsohn），与具体匹配的选择无关：集合不含已确认的基准点，邻居数严格小于集合点数（Hall 缺配证据），已被确认占用的复测点不计入邻居。此时报告**不出**任何逐点位移结论或超限数量（无 `points`/`exceeded` 字段）。

### 3.2 存在完整对应

报告按基准点列出在至少一份完整对应中确实能与它配对的复测点（完美匹配的允许边，经强连通分量判定），每项标注是否超限；据此标明该基准点：

- `always_exceeded`：必定超限（所有可用对应都超限）；
- `never_exceeded`：必定不超限；
- `depends`：取决于对应。

另报告所有完整对应中实际可达的最小、最大超限点数，各附一份达到该端点的完整对应清单（含确认对，逐对标注是否超限）。最小/最大通过 0/1 费用的最小费用完美匹配求得，**不会**把各点单独可能的结果当成能同时发生。结论：

- `sign`：最大值为零——任何完整对应都不超限，可签字；
- `fail`：最小值大于零——任何完整对应都至少超限这么多点，不合格，即使尚不能指认具体哪个点；
- `verify_labels`：其余情况（最小为零、最大大于零）——须核实标签。

```json
"complete": true,
"conclusion": "verify_labels",
"points": [
  {"sequence": 10, "track_id": "marker", "verdict": "depends",
   "partners": [{"sequence": 10, "track_id": "marker", "exceeded": false}, …]}, …
],
"exceeded": {
  "min": {"count": 0, "correspondence": [
    {"baseline": {"sequence": 10, "track_id": "marker"},
     "resurvey": {"sequence": 10, "track_id": "marker"}, "exceeded": false}, …]},
  "max": {"count": 2, "correspondence": […]}
}
```

报告关联验收区（`zone_id`、位移上限）及两个输入端点（`dataset_id`、`version`、`fingerprint`）。证据（缺配集合、允许对应、最小/最大对应清单）由全排序输入上的确定性算法产生：有多个有效证据时取确定性规则下的一份，配置数组的排列不改变选择。

## 4. 算法与性能

身份与坐标校验复用交付链路的点表格式与精确有理数；对应分析为：

- 完整对应存在性：Hopcroft–Karp 最大匹配；
- 缺配集合：从自由基准点出发的交错可达集；
- 每点可用对应：完美匹配 + 有向图强连通分量（允许边）；
- 最小/最大超限数：0/1 费用最小费用完美匹配（先用零费用边的最大匹配初始化，再按势函数 + Dijkstra 分阶段增广，每阶段增广所有紧边路径）。

4096 点规模基准（两侧各 4096 点，坐标 `(i,0,0)`；每个基准点 `i` 的两个候选为复测 `i` 与 `(i+1) mod 4096`；位移上限 0；候选 8192 条）。复现：`python3 examples/accept_benchmark.py`（生成输入不计时）。在本容器（CPython 3.11.2，Linux x86-64，单进程，无第三方运行依赖）实测：

| 用例 | 结论 | CPU 时间 | 峰值 RSS |
| --- | --- | --- | --- |
| 无确认（每点两个可用对应，min 0 / max 4096，`verify_labels`） | 已核对 | 0.31 s | 54 MiB |
| 确认一条跨位对应（全部强制跨位，min = max = 4096，`fail`） | 已核对 | 0.31 s | 50 MiB |

两次运行都完整输出 4096 个点的逐点结果与两端证据对应清单，不做抽样。预算为每次命令 30 秒 CPU 时间、256 MiB 峰值 RSS，实测余量约两个数量级。自动化测试 `tests/test_acceptance.py` 中的 `BenchmarkAcceptanceTests` 复现这两个用例并核对全部逐点结果。

## 5. 可运行示例

`./examples/accept.sh` 完整走查：基准数据集（三点 `[2,1,4]`）与复测数据集（`[3,1,4]`、`[2,1,4]`、`[9,1,4]`）各自经 build → export → apply → view 导出视图，再分别以 `fixtures/accept-zone.json`（仅候选）与 `fixtures/accept-zone-confirmed.json`（追加一条人工确认）运行验收。第一次运行每份完整对应恰有一点超限但无法指认（`fail`，三点均 `depends`）；确认 `(30, marker)` 后，超限点被锁定在序号 30（`always_exceeded`）。

## 6. 边界

不读取图像，不做坐标拟合、点表回写或标签自动匹配；不同名、不就近假定对应。一个配置只处理一个验收区；跨区批量验收由调用方逐区运行。候选与确认完全来自现场输入，命令不补充、不删减对应关系。
