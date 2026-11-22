# 离线标定更正与点云交付 v1

本文档是 [多视图点重建契约](contract.md) 的增量部分：在上游增加一个可重建、可更正、可导出差量的状态库，在下游增加一个只接受单调相邻版本的本地应用库。原 `camera-tracks --source ... --output ...` 的输入、报告字节和退出码完全不变。

运行依赖仅 Python 3.11+ 标准库，无网络、无外部服务、无共享数据库。两侧各是一个本地 UTF-8 JSON 文件，由命令行独占读写；不实现多写者调度或网络同步。

## 1. 命令

```
camera-tracks --source INPUT --output REPORT              # 原有单次重建
camera-tracks build   --input BUILD.json --store UP.json
camera-tracks correct --store UP.json --request CHANGE.json
camera-tracks query   --store UP.json --version N --sequence S --track TRACK
camera-tracks export  --store UP.json --output PKG.json (--full | --base-version N)
camera-tracks apply   --store DOWN.json --package PKG.json
camera-tracks view    --store DOWN.json [--output VIEW.json]
camera-tracks view    --store DOWN.json --sequence S --track TRACK
```

成功时向标准输出打印一行 JSON 摘要，退出 0；任何错误打印 `error: <可读诊断>` 到标准错误，退出 2，且不产生可被误认为成功的半份数据。`build` 不覆盖已存在的状态库；`export` 与 `view --output` 以“同目录临时文件 + 原子替换”写出，中断不会留下部分包。

数据集合身份、相机 ID、track ID、观测、投影矩阵、权重与十进制数值格式沿用 `contract.md`：ID 为 1..64 个 `[A-Za-z0-9_:-]`；矩阵 3×4 且行秩为 3；每 track 2..20 个互不相同的相机观测；权重为正；数值为有界十进制字符串。

## 2. 建库 `build`

输入对象字段：`dataset_id`、`max_reprojection_error`（十进制字符串，非负）、`cameras`（2..20 个 `{id, projection}`）、`batches`（1..100 个 `{sequence, tracks}`）。

- `sequence` 是 0..10^9 的唯一整数；`tracks` 为 1..200 个当前格式 track。不同批可复用同名 track。
- 点身份是 `(采集序号, track ID)`；`track ID` 只在批内唯一。
- 初始标定适用于全部序号，建库后版本号为 0。
- 状态库自包含：建库输入文件删除后，查询、更正、导出仍然可用（测试 `test_store_is_self_contained_after_input_removal`）。
- 此后不追加观测、不修改阈值。

## 3. 每点诊断与可交付判定

`query` 返回指定版本、指定批次、指定点的记录：

```json
{
  "dataset_id": "...", "version": 1, "sequence": 10, "track_id": "marker",
  "calibration_sources": {"left": {"source": "initial", "change_id": null, "version": null},
                          "right": {"source": "change", "change_id": "c1", "version": 1}},
  "status": "reconstructed", "xyz": ["-2","-1","-4"],
  "observations": [{"camera_id":"left","source":{...},"depth":"-4","positive_depth":false,
                    "projected_xy":["1/2","1/4"],"residual_xy":["0","0"],
                    "squared_residual":"0"}, ...],
  "algebraic_squared_error": "0",
  "deliverable": false, "reason": "nonpositive_depth", "reasons": ["nonpositive_depth"]
}
```

- `calibration_sources` 与每观测的 `source` 标明该点选用的标定来源：`initial`，或覆盖该序号的最新一次更正（`change_id` + 版本号）。查询旧版本时只看到该版本当时已存在的更正，不使用最新标定。
- `status` 沿用现有算法：满秩为 `reconstructed` 并给精确 XYZ；秩亏为 `unobservable` 且 `xyz=null`，不任意固定深度。
- `depth = P3·[XYZ,1]`；深度为 0 时 `projected_xy`、`residual_xy`、`squared_residual` 为 `null`。
- 可交付当且仅当：唯一重建、所有参与观测深度**严格为正**、所有回投影残差的平方欧氏长度 `≤` 阈值平方。全部相机参与，绝不丢掉失败相机重算；矩阵正负号按输入处理。
- `reason` 是主因，`reasons` 列出全部：优先级依次为 `unobservable`、`nonpositive_depth`、`excessive_reprojection_error`。零深度、负深度、超差与不可观测都是**业务结果**（命令退出 0），不是整批失败。
- 数值输出复用精确有理数：整数或最简 `n/d` 字符串，不转浮点。

## 4. 更正 `correct`

请求字段：`dataset_id`、`change_id`、`base_version`、`replacements`（1..20 项 `{camera_id, start, end, projection}`）。

- 每项指定已有相机、闭区间 `[start,end]`（0..10^9）与合法新投影矩阵。
- 同一请求内同一相机的区间不得相交；跨请求允许重叠。对每个相机、每个序号，取该版本下最新且覆盖它的替换，未覆盖部分保持原状。
- 请求全部有效且 `base_version` 等于当前版本时，生成恰好一个下一版本；即使没有匹配观测、交付内容不变也记录版本（`test_xyz_update_and_empty_delta_both_advance_versions`）。
- `change_id` 幂等：已成功应用的同 ID 同内容请求返回原版本号（`status: "already_applied"`，不前进）；同 ID 不同内容拒绝。已成功的 ID 优先于“基版本过期”检查，因此晚到的重试仍成功。
- “请求相等”按解析后的字段值判断：忽略对象键顺序、替换项排列顺序与 JSON 空白；矩阵数字按其**原输入字符串**比较（`"0"` 与 `"0.0"` 不等价）。印证：`test_change_equality_ignores_key_and_replacement_order`。
- 失败请求不占用 ID，也不分配版本。`test_change_retry_and_conflicts` 验证：过期基版本、未知相机、同相机区间相交、秩亏矩阵都只返回错误，状态库版本和已用 ID 不变。
- 版本是提交单位：状态库用一个原子替换同时落盘新版本、变更记录、视图指纹与历史表。不存在“版本已分配但不可导出”的中间状态（`test_interrupt_before_correct_commit_keeps_version_and_export`、`test_interrupt_after_correct_commit_leaves_consistent_version`）。

## 5. 导出 `export`

`--full` 只能导出版本 0 的全量；`--base-version N` 导出 `N → N+1`，`N+1` 必须已经存在。同一数据集同一端点重复导出的文件**字节一致**，包括更高版本已存在、下游落后多轮的情形（测试 `test_historical_export_bytes_are_stable`）。

`export --output` 与 `view --output` 允许覆盖同名输出（便于按固定路径重复导出），但写入是同目录临时文件 + 原子替换：中断只会留下旧的完整文件或新文件，不会留下半份包；也拒绝把输出路径指向状态库本身。

包对象为紧凑、键排序的 UTF-8 JSON（末尾一个换行）：

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-delivery",
  "dataset_id": "site-a",
  "base_version": 0,              // 全量为 null
  "target_version": 1,
  "base_fingerprint": "…",        // 全量为 null
  "target_fingerprint": "…",
  "records": [
    {"op":"delete","sequence":10,"track_id":"marker"},
    {"op":"upsert","sequence":20,"track_id":"marker","xyz":["2","1","4"]}
  ]
}
```

- 交付视图只包含可交付点的身份与精确 XYZ，不含诊断。
- 全量对所有合格点发 `upsert`；增量对新增或 XYZ 实际变化的点发 `upsert`，对曾可交付而现在不合格的点发 `delete`（无 XYZ），其余不发。
- 记录按采集序号升序、同序号按 track ID 的字符串（ASCII 码位）顺序；每个身份最多一条。
- 即使记录为空也可交付该版本（`records: []`）。

### 交付视图指纹

SHA-256，作用在下面这种独立于 JSON 空白与对象字段排列的规范化点表编码上（UTF-8、LF）：

```
camera-tracks-points-v1
count=<点数>
<采集序号>\t<track ID>\t<x>\t<y>\t<z>
```

每点一行，按采集序号数值升序、再按 track ID 码位升序；坐标为精确有理数字符串（整数或最简 `n/d`）。`count` 行使空视图也有稳定且互不相同的指纹，并避免前缀歧义。JSON 语法不参与摘要，因此重排对象键或改变空白不改变指纹。版本 0 的指纹记录在状态库历史表中，后续版本无法改写它。

## 6. 下游应用 `apply`

下游空库只接收首版（`base_version` 为 `null`、`target_version` 为 0 的全量包）。此后只接收同数据集、基版本等于当前版本、基指纹等于当前视图指纹、目标版本恰好为当前版本 +1 的包。

应用前校验：包结构与字段合法、记录有序且身份唯一、`delete` 的目标确实存在；应用后重新计算点表指纹，必须等于包内 `target_fingerprint`。全部满足后以一次原子文件替换提交，新版本才对 `view`/查询可见；任何失败不改变点表、不写成功记录。

- 已成功应用的原包（以**完整文件字节**的 SHA-256 判同）再次收到时，答复 `status: "already_applied"` 与它生效时的版本号，不回退、不重删重写，即使之后已应用更晚版本。
- 同数据集、同目标版本但字节不同的包不能冒充旧包：目标版本不晚于当前版本时拒绝。
- 对非原包先按版本和格式契约校验，不要求接受不同字节的等价重编码。
- 拒绝损坏、跳版、串数据集、基指纹不符、删除不存在点、目标指纹不符的包（`test_downstream.py` 逐项覆盖），且都验证下游视图前后完全一致。

`view` 打印当前版本、指纹与完整点表（或 `--output` 写文件）；`view --sequence S --track T` 按当前点身份查询，未命中返回 `found: false`。

## 7. 中断一致性

写入采用同目录临时文件 + `fsync` + 原子替换，状态库只有一个提交点。为可重复测试，进程读取环境变量 `CAMERA_TRACKS_FAULT`，在指定位置抛出 `injected interruption` 并以非零退出：

| 取值 | 失败位置 |
| --- | --- |
| `build-before-commit` | 校验完成、发布状态库之前 |
| `correct-before-commit` | 新版本已计算、写状态库之前 |
| `correct-after-commit` | 状态库已原子提交、答复之前 |
| `export-before-write` | 包已生成、写文件之前 |
| `apply-before-commit` | 下游校验完成、写下游库之前 |
| `apply-after-commit` | 下游库已原子提交、答复之前 |

这些注入点都位于写入路径内部而非读输入之前，用于证明真实的原子性边界：

- `test_interrupt_before_apply_commit_keeps_old_view`：撤回开始但未生效时中断，重开后仍能查到三条旧点、旧版本与原指纹，重投恰好撤回两条。
- `test_interrupt_after_apply_commit_then_restore_then_retry`：撤回已生效但未答复时中断；重开先应用恢复包，再重投首次撤回原包，返回原成功版本且保留恢复后的点与新指纹。
- 上游更正与导出、查询可见性一同生效或一同失败。

样例 `fixtures/build.json` 在采集序号 10、20、30 各放一份相同观测，阈值 0，三条点均为 `[2,1,4]`。`fixtures/correction.json` 将右相机第一行末项由 `-1` 改为 `+1`，覆盖 `[10,20]`：两条受影响记录仍有唯一零残差解，但深度为负；增量撤回 `(10,marker)`、`(20,marker)`，`(30,marker)` 不动。随后只对 `[20,30]` 恢复 `-1`，下一增量只补回 `(20,marker)`。完整可运行走查见 `examples/delivery.sh`。

## 8. 边界

不读取图像、不做标定求解或非线性优化、不做网络同步、不实现多写者并发。下游只接受单调相邻版本；落后多轮时须按序补齐，或由上游导出相应相邻增量（任意相邻端点均可导出）。投影矩阵缩放会改变代数权重，调用方需统一尺度约定。
