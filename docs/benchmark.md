# 训练材料与盲测基准发布 v1

本文档是 [离线标定更正与点云交付](delivery.md) 的增量：在已交付的 `view` 点表之上，离线登记一个基准项目，发布训练材料、签发盲测题本、登记预测成绩，并在迟到关联出现时审计旧成绩、撤销受影响的题本。原有 `camera-tracks --source ... --output ...`、`build/correct/query/export/apply/view/accept/resurvey/earthwork/merge` 的输入、字节与退出码完全不变。

运行依赖仅是 Python 3.11+ 标准库。所有输入与既有状态只读或原子替换，无网络、无共享数据库、无账户。一个项目是一个本地 UTF-8 JSON 状态库，由命令行独占读写；只支持同一项目串行写入。

## 1. 命令

```
camera-tracks bench --config CONFIG.json --project PROJECT.json \
                    --view DATASET_ID=VIEW.json [--view ...]
camera-tracks bench-release          --project PROJECT.json --request RELEASE.json
camera-tracks bench-issue            --project PROJECT.json --request ISSUE.json
camera-tracks bench-relate           --project PROJECT.json --request RELATE.json
camera-tracks bench-score            --project PROJECT.json --request SCORE.json
camera-tracks bench-scores           --project PROJECT.json [--booklet BOOKLET_ID]
camera-tracks bench-audit            --project PROJECT.json [--booklet BOOKLET_ID]
camera-tracks bench-status           --project PROJECT.json
camera-tracks bench-export-training  --project PROJECT.json --id RELEASE_ID  --output OUT.json
camera-tracks bench-export-booklet   --project PROJECT.json --id BOOKLET_ID  --output OUT.json
```

成功时向标准输出打印一行 JSON 摘要并退出 0；任何错误打印 `error: <可读诊断>` 到标准错误并退出 2，目标路径不出现新文件、状态库不变。导出是只读操作：**已存在的目标路径一律拒绝覆盖**，写出采用同目录临时文件 + 硬链接 + 目录 `fsync`，中断不会把半份包暴露给下载方，也不会撤销已登记材料。

## 2. 初始化 `bench`

### 2.1 配置

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-benchmark-config",
  "project_id": "bench-196",
  "datasets": [
    {"dataset_id": "survey-a", "version": 0, "fingerprint": "…"},
    {"dataset_id": "survey-b", "version": 0, "fingerprint": "…"}
  ],
  "relations": [
    {"points": [
      {"dataset_id": "survey-a", "sequence": 10, "track_id": "rep"},
      {"dataset_id": "survey-b", "sequence": 40, "track_id": "rep"}
    ]}
  ]
}
```

- `project_id`：1..64 个 `[A-Za-z0-9_:-]`。
- `datasets`：1..8 份**现有** `view` 导出，每份 `dataset_id` 不同，且必须给出完整的 `dataset_id`、`version`、`fingerprint`。每条都必须有一个对应的 `--view DATASET_ID=PATH`，不多也不少。
- `relations`：0..120,000 条复核记录，每条 2 个已知测点。可为空。

初始化一次性创建项目（`revision = 0`），从不覆盖已存在的项目。此后所有操作只读项目状态；原 `view` 文件可以删除。

### 2.2 view 校验与测点冻结

每份 view 按交付契约重新校验结构、点身份 `(sequence, track_id)` 与精确有理数坐标，并**按原始点行重算** `camera-tracks-points-v1` 指纹。重算指纹必须等于 view 自带指纹，`dataset_id`、`version`、`fingerprint` 三个字段又必须逐一匹配配置——指纹相同也不能替代另一个版本。任何一份不符都是输入错误，项目不会创建。

初始化把每份 view 的点表按采集批（`sequence`）冻结进项目状态：坐标与来源端点（`dataset_id`、`version`、`fingerprint`）此后不可改写。原 `view` 文件被删除、移走或篡改都不影响后续操作。初始化不改写任何原生点表、`view` 或交付状态库。

### 2.3 测点身份与泄漏边界

项目内以 `(dataset_id, sequence, track_id)` 引用一个测点。`dataset_id` 用来区分不同 view；同一 `dataset_id` 下 `(sequence, track_id)` 唯一。

- **同批规则**：相同 `dataset_id` 与 `sequence` 的测点来自同一采集批，无条件归入同一关系组。发布其中一个点，整批都视为已暴露。
- **复核记录**：一条记录连接一对已知测点，表示二者的采集来源发生关联。关系**无向、可跨批、可跨数据集、可传递**。不同批不会因坐标相同或 track 同名而自动相连。
- 关系组是上述同批节点与复核记录并查集的连通分量。

## 3. 提交边界

初始化修订号为 0。发布训练材料、签发题本、追加关系、登记预测各用一个**项目内全局唯一**的操作 ID（`op_id`）；每次**首次**成功恰好推进一个修订号。

- 已成功过的 `op_id` 优先按历史处理：同 ID 同请求重投，原样返回首次回执，**不推进修订**——哪怕题本此时已撤销、材料此后已泄漏。同 ID 不同内容拒绝。
- 未提交的请求不占 ID，不留下半个包、半份可见成绩或半次撤销。
- 请求相等按**解析后的值**判断：忽略 JSON 对象键顺序、各清单排列顺序、无向关系端点顺序；数字按解析后的精确值比较（`"2"`、`"2.0"`、`"4/2"` 等价）。训练发布 ID 与题本 ID 分别在项目内唯一，预测 ID 在对应题本内唯一；除原操作重投外，不得用新操作重复占用这些已登记身份。
- 训练发布/关系追加与它引发的撤销**一次生效**：一次原子替换同时落盘新材料（或新关系）、撤销记录与推进后的修订号。不存在“能下发训练材料却还有可用泄漏题本”或“训练材料尚未提交却先撤销了题本”的中间状态。允许只支持同一项目串行写入。
- 进程退出重开后按持久状态恢复；成功提交但回执未送达时，重投按上述规则取回原回执。

## 4. 训练发布 `bench-release`

请求：

```json
{
  "op_id": "rel-1",
  "release_id": "train-A0",
  "points": [{"dataset_id": "survey-a", "sequence": 1, "track_id": "A0"}]
}
```

- `release_id` 项目内唯一；`points` 非空、不重复，全部必须是项目内已知测点。
- 提交成功即把这份材料开放给训练端；是否有人运行下载不影响暴露判定。后续发布会**追加**到既有材料，不替换旧材料。
- 材料与任何一次已发布训练点相连的**整个关系组**都视为已暴露。

回执：

```json
{
  "project_id": "bench-196", "status": "released", "op_id": "rel-1",
  "revision": 5, "release_id": "train-A0", "points": 1,
  "sha256": "…", "withdrawn": ["booklet-H"]
}
```

`withdrawn` 列出本次首次撤销的全部题本 ID。后续重投原样返回同一清单，绝不换成当时未受影响的题本。

## 5. 签发题本 `bench-issue`

请求：

```json
{
  "op_id": "issue-1",
  "booklet_id": "book-H",
  "elevation_limit": "1.5",
  "points": [{"dataset_id": "survey-a", "sequence": 2, "track_id": "H"}]
}
```

- `booklet_id` 项目内唯一；`points` 1..4096 个、不重复、全部已知；`elevation_limit` 非负、有界十进制字符串（按精确值保存）。
- 只要其中**任何**一个点已暴露，就拒绝签发整个题本（退出 2，不占 `op_id`、不推进修订）。

回执给出 `booklet_id`、`points`、`elevation_limit`、`sha256` 与 `revision`。

## 6. 材料格式

训练包与题本都用**独立于原生 `view` 的格式**，紧凑、键排序、UTF-8、末尾一个换行，以文件字节的 SHA-256 标识身份。二者点行按 `(dataset_id, sequence, track_id)` 稳定排序。已签发材料的**历史导出一律按原内容原样重领**，即使题本后来已撤销；字节与首次导出完全一致。

> 这里的 `source` 端点就是该点冻结时的 `dataset_id`、`version`、`fingerprint` 三元组。

### 6.1 训练包 `camera-tracks-training-package`

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-training-package",
  "package_format": "camera-tracks-training-v1",
  "project_id": "bench-196",
  "release_id": "train-A0",
  "release_revision": 5,
  "count": 1,
  "points": [
    {"dataset_id": "survey-a", "sequence": 1, "track_id": "A0",
     "xyz": ["2","1","4"],
     "source": {"dataset_id": "survey-a", "version": 0, "fingerprint": "…"}}
  ]
}
```

只按清单交付这些点的完整 XYZ 与来源端点；**不额外下发同组点**（尽管它们在选型上已视为暴露）。

### 6.2 题本 `camera-tracks-benchmark-booklet`

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-benchmark-booklet",
  "package_format": "camera-tracks-booklet-v1",
  "project_id": "bench-196",
  "booklet_id": "book-H",
  "booklet_revision": 7,
  "elevation_limit": "3/2",
  "count": 1,
  "points": [
    {"dataset_id": "survey-a", "sequence": 2, "track_id": "H",
     "xy": ["5","5"],
     "source": {"dataset_id": "survey-a", "version": 0, "fingerprint": "…"}}
  ]
}
```

只给待测点身份、XY、来源端点与误差限，**不下发真实 Z**。

## 7. 评分 `bench-score`

请求：

```json
{
  "op_id": "score-1",
  "booklet_id": "book-H",
  "booklet_sha256": "…",
  "prediction_id": "pred-H-1",
  "predictions": [
    {"dataset_id": "survey-a", "sequence": 2, "track_id": "H", "z": "21.5"}
  ]
}
```

- `booklet_sha256` 必须等于该题本**首次签发文件的字节摘要**（`bench-export-booklet` 早年导出的哈希）。错误指纹、缺项、多项、重复、非法数字都拒绝，且**不占用** `prediction_id`。
- `predictions` 每个点恰一项，覆盖题本全部测点；`z` 为有界十进制字符串，按精确值解析。
- 逐点比较 `|预测 Z − 真实 Z| ≤ elevation_limit` 判通过，**等于误差限算通过**。

回执只给通过点数与总点数、题本与预测身份、登记修订号，**不下发逐点真值**：

```json
{"project_id":"bench-196","status":"scored","op_id":"score-1","revision":8,
 "booklet_id":"book-H","prediction_id":"pred-H-1","passed":1,"total":1}
```

## 8. 迟到关系与选型

追加关系或发布训练材料后，任何现有题本只要有一个待测点进入已暴露组，就**撤销整个题本**，且在同一次提交中：

1. 冻结该题本**首次**撤销的修订号与导致撤销的操作 ID；
2. 冻结至少一组当时已连通的证据：一个待测点、一个已发布训练点、以及发布该训练点的**训练发布 ID**（优先选取与待测点不同的训练点；只有当整个连通组只发布过该待测点本身时才回落到它）。证据在后续关系增加时绝不改写；
3. 该题本的全部旧成绩从**当前可比较成绩**中移除；
4. 回执的 `withdrawn` 列出全部首次撤销的题本。

### 8.1 当前成绩 `bench-scores`

只列仍有效的题本成绩；已撤销题本整本不出现（无关题本不受影响，仍可取用、仍可登记新预测）。`--booklet` 给出该题本当前的 `comparable` 状态。

### 8.2 审计 `bench-audit`

独立取回历史预测的原成绩与题本当前是否已撤销、首次撤销依据。原成绩**不改写成零**，也**不混入**当前可比较成绩：

```json
{"project_id":"bench-196","booklet":{"booklet_id":"book-H","revision":7,"points":1,
 "withdrawn":true,
 "withdrawal":{"revision":9,"op_id":"rel-BH","release_id":"train-A0",
   "test_point":{"dataset_id":"survey-a","sequence":2,"track_id":"H"},
   "training_point":{"dataset_id":"survey-a","sequence":1,"track_id":"A0"}},
 "predictions":[{"prediction_id":"pred-H-1","passed":1,"total":1,"revision":8,"op_id":"score-1"}]}}
```

撤销之后，当前查询把该题本标记为已撤销，**拒绝新的预测登记**（`bench-score` 退出 2），但审计仍能看到其历史成绩。历史下载不能恢复题本有效性。

## 9. 失败与中断约定

- 错误指纹、未知端点、批内任一条非法（未知/重复端点、同点自连）：整批关系拒绝，项目不变。
- 环境变量 `CAMERA_TRACKS_FAULT` 提供两处真实事务边界：
  - `benchmark-before-commit`：全部校验与变更计算完成、写状态库之前中断 → 重开后仍是旧材料开放/选型状态；同请求重投可成功。
  - `benchmark-after-commit`：状态库替换完成后、返回回执前中断 → 重开会看到已提交结果（不允许新的泄漏预测，重投只取原回执），再次操作也不能改变已提交的历史包。
  - `benchmark-export-before-write`：导出包体计算完成、写临时文件之前中断 → 目标路径不出现文件。
- 导出拒绝覆盖任何既有路径，也不把半文件暴露给下载方；导出失败不会撤销已登记训练材料。

## 10. 容量与实测

项目累计支持 80,000 个点、120,000 条显式关系；单次训练清单最多 80,000 个点；单份题本 1..4096 个点；配置与请求文件至多 64 MiB；关系批内也按整批原子拒绝。

4 份各 100 批、每批 200 个点的合法 `view`（合计 80,000 点，每份 view 用复核记录把相邻批代表点串成一组）走查：初始化、给第 2/3 份 view 各签发 4096 点题本并登记预测、只发布第 1 份 view 中 1 个点、追加一条连接第 1/2 份 view 代表点的边——第 2 份题本撤销，第 3 份仍可用于选型。现场 Linux 单进程 CPython 串行命令（`python3 examples/bench_benchmark.py`）实测各命令峰值 RSS ≤ 91 MiB、总 CPU 约 7.7 s（预算 256 MiB / 45 s）；交互式小样例见 `./examples/bench.sh`。

## 11. 边界

本流程只处理**登记到本流程**的材料和关联：不推断外部训练行为、不扫描外部文件，也不阻止对导出文件的直接本机访问（评分是本地评估员操作，无账户、无网络服务）。不做坐标回写、不重建三维点、不做跨测次点对应。一个项目处理一套材料与关联；多项目由调用方分别运行。
