# 离线分片坐标归并 v1

本文档是 [交付契约](delivery.md) 的增量部分：在若干各自带局部原点的 `view` 导出之上，离线求解一份统一工程坐标点集。原 `camera-tracks --source ...`、交付链路与既有子命令的输入、字节和退出码完全不变。

运行依赖仅 Python 3.11+ 标准库。方案与所有 view 只读；输出是一个新建文件，已存在的目标或指向任一输入的目标都会被拒绝，写入采用同目录临时文件 + 硬链接 + 目录 `fsync`，中断不会在目标路径留下半包。

## 1. 命令

```
camera-tracks merge --plan PLAN.json --output PACKAGE.json \
                    --view FRAGMENT_ID=VIEW.json [--view ...]
```

每个带端点的分片必须恰好出现一次 `--view`；不绑定 view 的纯坐标传递节点不提供文件。成功时向标准输出打印一行 JSON 摘要并退出 0；任何错误打印 `error: <可读诊断>` 到标准错误并退出 2，不产生可被误认为成功的文件。

## 2. 联测方案 `merge plan`

UTF-8 JSON，至多 64 MiB，字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 固定 `1` |
| `kind` | 固定 `camera-tracks-merge-plan` |
| `plan_id` | 1..64 字符的 `[A-Za-z0-9_:-]` |
| `baseline` | 基准分片 ID，其原点 T 恒为零 |
| `fragments` | 1..60000 个 `{fragment_id, endpoint}` |
| `links` | 0..120000 条联测记录（单分片方案允许为空） |

- 分片 ID 全局唯一；重复 ID、重复联测记录 ID、未知分片引用、自连记录、非法结构、重复 JSON 键、越界分量（单分量绝对值 ≤ 10^12 μm）都是**输入错误**，不是联测失准。
- `endpoint` 为 `null` 表示该分片只做坐标传递、不带点表；否则为 `{dataset_id, version, fingerprint}`，三者都必须与实际 view 完全一致。
- 全部联测按无向图必须把每个分片连到基准；不连通的方案直接报错，而不是当作失准处理。
- 联测记录字段：`record_id`（唯一）、`from_fragment`、`to_fragment`、`delta`（三个整数微米分量）、`status`（`reviewed` 或 `tentative`）。记录 u→v 的语义是 `T(v) − T(u) = delta`；反方向使用时三个分量全部取负。同一对分片允许存在多条独立记录（包括互相矛盾的平行记录）。

## 3. view 输入与端点绑定

输入是 [交付契约](delivery.md) 第 5 节定义的 `camera-tracks-view` 导出。每个绑定 view 在任何网络判定**之前**完成校验：

1. 重算规范化点表指纹 `camera-tracks-points-v1`，必须等于 view 自带 `fingerprint`；
2. `dataset_id`、`version`、`fingerprint` 三个字段逐一匹配方案端点——指纹相同也不能替代另一个版本；
3. 点身份在该 view 内唯一；不同分片的 `(sequence, track_id)` 相互独立，允许重名、允许局部坐标完全相同，归并后绝不合并去重。

因此篡改一个最终被扣留分片的点表仍会报错；缺失或多余的 view 文件同样报错。扣留决策不会跳过任何输入校验。

## 4. 精确判定（整数微米，无浮点、无拟合）

先做一次 BFS 生成树并给每个分片赋整数微米坐标。非树边相对生成树位置产生残差：

- 所有残差为零：`status = "consistent"`，全部按唯一位置交付，不存在可舍弃记录。
- 否则枚举**单条**暂定记录的舍弃（已复核记录永不参与）：舍弃后剩余图仍须连通到基准，且剩余每条联测都被同一份精确位置满足。不选最先遇到的候选，不做误差拟合，不考虑两条及以上的联合修复。
- 每个可行舍弃只移动生成树的一侧（一个子树平移；非树边舍弃不平移任何点）。通过割计数与 LCA 子树区间一次性算出全部候选及其平移量，复杂度 O((V+E)·α) 级别，不做逐候选重新遍历全图。
- 不存在任何可行单条舍弃时：`status = "unexplained"`，输出可识别的“本批无法解释”包，**不交付任何归并点（基准分片也不例外）**，所有分片决定为 `not_released` 且不带位置。

## 5. 分片决定与扣留证据

仅当一个分片在**每一种**可行舍弃下 T 都完全相同时才 `released`，并给唯一 `origin_um`（三个整数微米）与 `origin_m`（最简有理文本，微米除以 10^6）。否则 `withheld`，并给恰好两条证据：

```json
{
  "fragment_id": "A",
  "decision": "withheld",
  "has_table": false,
  "endpoint": null,
  "origin_um": null,
  "origin_m": null,
  "reason": "origin ambiguous: ...",
  "evidence": [
    {"record_id": "lAB", "origin_um": [1, 0, 0], "origin_m": ["1/100000", "0", "0"]},
    {"record_id": "lRA", "origin_um": [2, 0, 0], "origin_m": ["1/50000", "0", "0"]}
  ]
}
```

- 两条证据对应两个不同的可行舍弃记录，位置互不相同，且都能由方案独立复算（测试对每条证据执行独立 BFS 重解核对）。绝不只标“冲突”。
- 证据记录按记录 ID 稳定选择；仅改变方案数组顺序、对象键顺序或 view 点行顺序时，包字节完全一致（片段与证据都按 ID 规范化排序）。
- `unexplained` 时没有“位置不定”这一状态：每个分片统一是 `not_released`，证据与位置均为 `null`。

## 6. 归并点

`XYZ_工程 = XYZ_局部（米） + T（米）`，全部用 `Fraction` 精确计算并以整数或最简 `n/d` 文本输出，不做浮点舍入。点身份扩展为 `(fragment_id, sequence, track_id)`，保留所有点、不重名合并、不遗漏。只有“已放行且绑定点表”的分片贡献点；被扣留分片即使带表也不产出任何点。点按 `fragment_id`、`sequence`、`track_id` 排序。

## 7. 包结构

紧凑、键排序的 UTF-8 JSON（末尾一个换行），通过流式写出避免大批次把整包保存在内存：

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-merge-package",
  "package_format": "camera-tracks-merge-v1",
  "plan_id": "...",
  "baseline": "R",
  "status": "resolved",
  "feasible_discards": ["lAB", "lRA", "lRB"],
  "package_digest": "camera-tracks-merge-v1:<sha256>",
  "fragment_count": 4,
  "point_count": 2,
  "released_count": 2,
  "withheld_count": 2,
  "not_released_count": 0,
  "endpoints": [
    {"fragment_id": "R", "dataset_id": "...", "version": 0, "fingerprint": "..."}
  ],
  "fragments": [ /* 每个分片一个决定，按 fragment_id 排序 */ ],
  "points": [ /* 归并点，按 (fragment_id, sequence, track_id) 排序 */ ]
}
```

`package_digest` 以流式方式对规范化（紧凑、键排序、LF）内容取 SHA-256：先写格式头 `camera-tracks-merge-v1`，再依次写头部信息（方案标识、基准、状态、可行舍弃、端点）的规范 JSON、每个分片决定的规范 JSON、每个归并点的规范 JSON；与 JSON 空白和对象键排列无关，也不需要把整包保存在内存。该包与原生 `view` 是不同 `kind`，不修改任何 view 的生成规则与指纹。

## 8. 失败与中断约定

- 非法方案结构、重复 ID、未连通、端点不符、指纹不符、缺/多输入、输出已存在、输出指向输入：退出 2，目标路径不出现文件。
- 环境变量 `CAMERA_TRACKS_FAULT=merge-before-write` 在全部校验与判定完成、写临时文件之前抛出 `injected interruption`，用于证明不会在最终目录留下目标或临时文件。
- 只读输入与既有状态；不写回任何 view、方案或状态库。

## 9. 容量与实测

分片 ≤ 60000、联测 ≤ 120000、方案文件 ≤ 64 MiB、单分量绝对值 ≤ 10^12 μm。60000 分片、60002 条联测、20001 个可行舍弃的验收批量（V0..V59999，链 e_i + 长记录 f + 两条已复核 g1/g2）在单进程 CPython 3.11 实测约 **1.9 s CPU、峰值 RSS 约 100 MiB**（预算 20 s / 256 MiB），复现：

```
python3 examples/merge_benchmark.py
./examples/merge.sh
```

网络内部用紧凑定长数组（`array('i')`/`array('q')`/`bytearray` 与 CSR 邻接表）保存，分片决定用并行定长数组而非每分片字典，包体流式散列与写出。

## 10. 边界

不做平差、非线性优化或跨批网络同步；一条暂定记录要么整体成立要么整体舍弃，不支持部分分量修复。一个方案处理一个基准与一份联测网；多基准工程须由调用方先统一基准后逐网运行。
