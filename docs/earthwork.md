# 土方包 v1

本文档是 [多视图点重建契约](contract.md) 与 [离线标定更正与点云交付](delivery.md) 的增量：在交付视图之上增加离线土方面叠算。原有 `camera-tracks --source ... --output ...` 与 `build/correct/query/export/apply/view/accept/resurvey` 的输入、输出和退出码完全不变。

```
camera-tracks earthwork --old OLD_VIEW.json --new NEW_VIEW.json \
                        --plan ZONE.json --output PACKAGE.json
```

道路整修前后测量队各交付一份 `view` 点云。两份点表已在同一米制工程坐标系内，但地面分块不同。命令按方案给出的一份矩形作业区与两侧各自的地面分块，把正面积交叠逐片结算成挖方与填方，交付可由施工软件逐片复算的土方包，而不是只给一个区域总数。

命令只读输入：不回写点表、方案或任何状态库。成功时向标准输出打印一行 JSON 摘要并退出 0；输入无效、超出小片上限或发布失败时打印 `error: <可读诊断>` 到标准错误、退出 2，且目标路径上不出现包。包始终以“同目录临时文件 + fsync + 原子硬链接”一次新建；**已存在的目标与指向任一输入的目标都被拒绝**，任何失败都不会留下半份包。运行依赖仅 Python 3.11+ 标准库，无网络。

## 1. 输入

### 1.1 两份 view 导出

两侧必须是现有 `view` 命令导出的完整 JSON（`kind: "camera-tracks-view"`）。命令重新校验结构、点身份 `(sequence, track_id)` 与精确有理数坐标，并按交付契约规范化编码重算点表指纹——篡改点值而保留旧指纹会被拒绝。随后逐项核对方案指定端点的 `dataset_id`、`version`、`fingerprint`：**三者都须匹配**；即便两个版本的点表指纹相同，拿错版本也会被拒绝。

两份点表的身份空间相互独立：旧侧 `(sequence, track_id)` 只到旧视图里取点，新侧只到新视图里取点，从不按名称、顺序或距离跨表猜同一个点，也不要求两侧使用相同的身份。

### 1.2 作业区方案

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-earthwork-plan",
  "zone_id": "zone-196-yard",
  "zone": [0, 0, 3, 1],
  "old": {
    "dataset_id": "earthwork-old",
    "version": 0,
    "fingerprint": "…",
    "blocks": [
      {"block_id": "old-a", "area": [0, 0, 2, 1],
       "lower_left": {"sequence": 10, "track_id": "ground"},
       "lower_right": {"sequence": 20, "track_id": "ground"},
       "upper_left": {"sequence": 30, "track_id": "ground"}}
    ]
  },
  "new": {
    "dataset_id": "earthwork-new",
    "version": 0,
    "fingerprint": "…",
    "blocks": [
      {"block_id": "new-b", "area": [1, 0, 3, 1],
       "lower_left": {"sequence": 10, "track_id": "ground"},
       "lower_right": {"sequence": 20, "track_id": "ground"},
       "upper_left": {"sequence": 30, "track_id": "ground"}}
    ]
  }
}
```

- `zone_id`：作业区 ID（1..64 个 `[A-Za-z0-9_:-]`）。
- `zone`：矩形作业范围 `[左,下,右,上]`，整数米边界，坐标在 `-10^9..10^9`；右、上边界不计入覆盖面积，必须 `左 < 右` 且 `下 < 上`。
- `old` / `new`：两侧端点与地面分块清单。端点为 `dataset_id`、`version`、`fingerprint`（三者在 1.1 中逐项核对）。
- `blocks`：每侧 0..20,000 块，允许为空。每块是：
  - `block_id`：本层内唯一的 ID；
  - `area`：整数米矩形 `[左,下,右,上]`，必须非空且完全在 `zone` 内；
  - `lower_left`、`lower_right`、`upper_left`：三个点身份，分别锚定本块左下、右下、左上角。

同一层的所有块**内部两两不得重叠**（即使其中某块缺测也照样非法）；共边或共角不算重叠。相邻块可以共用点身份。不同层的分块方式互不相干。

## 2. 锚点与高程面

对每个块、每个角：

- 点身份合法但不在对应 view 中，表示该角没有取得合格测点。该块整体不可用于算量；凭证列出是哪一层、哪一块、缺哪些角以及对应的点身份。
- 点在对应 view 中存在时，其 XY 必须**精确等于**声明的角坐标；不符则整份方案非法并指出该层、该块、该角与实际 XY。**即使同块还有别的角缺测，已存在的角也照样校验**，缺测不会隐藏坐标错误。
- 三个角都存在且 XY 全部相符时，块以三点唯一决定的仿射高程面 `z = a·x + b·y + c` 表示整块（左上、右下、左下三个角定平面，右上角由平面取值）。不要求相邻块在共边上高程连续，也绝不把平面向块外推；其他点不参与计算。

## 3. 候选小片与结算

每块旧地面的块与每块新地面的块的正面积交叠是一个候选小片。仅当两块都有完整有效锚点时，该小片才进入结算。缺锚点的块不产生任何小片——哪怕交叠面积上的高差恰好为零，也不得以零体积冒充已结算。

令 `h(x, y) = z_new(x, y) − z_old(x, y)`，在整块小片上它是仿射函数。挖方为 `∫ max(−h, 0)`，填方为 `∫ max(h, 0)`，面积与体积都是精确有理数：

- `h ≥ 0` 的区域只计填方，`h ≤ 0` 的区域只计挖方；
- 零线穿过小片时，沿零线把小片切成两块分别精确积分，**不使用净高差、中心点正负或整片平均值**；
- 只在边或角上取零不单独产生体积；同高小片仍是已结算小片，挖方与填方都为 0。

作业范围总面积减去所有已结算小片面积之和，就是未能结算的面积：它包括一侧或两侧都没有块、以及有块但缺锚点的地方，按“作业区减已结算小片”一次算出，**不是**把两侧各自的缺测面积相加。

## 4. 土方包

包是 UTF-8、紧凑、单行、键顺序固定的 JSON（末尾一个换行），由命令流式写出：

```json
{
  "schema_version": 1,
  "kind": "camera-tracks-earthwork",
  "zone_id": "zone-196-yard",
  "zone": [0, 0, 3, 1],
  "zone_area": "3",
  "old": {"dataset_id": "earthwork-old", "version": 0, "fingerprint": "…"},
  "new": {"dataset_id": "earthwork-new", "version": 0, "fingerprint": "…"},
  "slice_limit": 100000,
  "slice_count": 1,
  "package_digest": "camera-tracks-earthwork-v1:…",
  "slices": [
    {"range": [1, 0, 2, 1],
     "old_block": "old-a",
     "new_block": "new-b",
     "corner_differences": {"lower_left": "-1/2", "lower_right": "1/2",
                            "upper_right": "1/2", "upper_left": "-1/2"},
     "cut": "1/8",
     "fill": "1/8"}
  ],
  "missing_blocks": [],
  "settled_area": "1",
  "unsettled_area": "2",
  "settled_cut": "1/8",
  "settled_fill": "1/8",
  "status": "partial"
}
```

- `zone_area`：作业范围面积，十进制整数字符串。
- `slice_count`：`slices` 长度，与实际可结算小片数一致；`slice_limit` 固定为 100,000。
- `package_digest`：`camera-tracks-earthwork-v1:<SHA-256>`，对本包结算内容（作业区与两端点、按序小片的范围/来源/高差/挖填、缺测块清单）的规范编码摘要，与输入数组排列和对象键顺序无关，便于接收端核对整包一致。
- `slices`：每个可结算小片一项，按 `(range, old_block, new_block)` 的字典序（先左、再下、再右、再上，然后两块 ID）排列，同一输入排列下字节稳定。
  - `range`：小片矩形 `[左,下,右,上]`（整数米、半开）；
  - `old_block`、`new_block`：来源块 ID；来源不同的小片绝不合并、绝不截断；
  - `corner_differences`：新地面减旧地面在左下、右下、右上、左上四角的高差，精确最简分数；
  - `cut`、`fill`：本小片的挖方与填方体积，精确最简分数；接受端可仅凭 `range` 与四角高差复算。
- `missing_blocks`：缺锚点块清单，按“层（old 先于 new）再块 ID”排列；每项给出 `layer`、`block_id`、`area`、`missing_anchors`（每项 `corner`、`sequence`、`track_id`，按左下、右下、左上顺序）。
- `settled_area`、`unsettled_area`：已结算与未能结算面积，十进制整数字符串，二者之和恰为 `zone_area`。
- `settled_cut`、`settled_fill`：已结算小片的挖填小计，精确最简分数。
- `status`：`unsettled_area` 为正时是 `partial`，只报告已知挖填小计与部分覆盖状态，**不发布全区土方量**；为零时是 `complete`，此时小计即全区量。包里没有任何隐藏的“全区总数”字段。

精确值一律用最简分数文本：整数写整数（`"0"`、`"2"`），非整数写 `n/d`（`"1/8"`）。声明块与点的数组排列、对象键顺序都不改变包字节。

## 5. 小片上限与拒绝

结算系统单包最多接收 100,000 个实际可结算小片。超过上限时命令以退出码 2 报错并**不生成包**：不截断、不合并来源不同的小片、不给出看似成功的摘要；缺锚点的块与它们相交的候选小片不计入上限，因为那些小片根本不会输出。

## 6. 可运行样例

`./examples/earthwork.sh` 完整走查：`fixtures/earthwork-old-build.json` 与 `fixtures/earthwork-new-build.json` 分别经 build → export → apply → view 导出两份点表，再以 `fixtures/earthwork-plan.json` 结算作业区 `[0,0,3,1]`。旧块 `[0,0,2,1]` 高 0，新块 `[1,0,3,1]` 在平面 `z = x − 3/2` 上；两份点表的端点指纹就是这条交付链路实际导出的结果。输出即上节示例：1 个已结算小片，已结算面积 1，未结算面积 2，挖方与填方各 `1/8`，状态 `partial`。

去掉新块任一角后（例如从新视图删去 `(20, ground)`），该块缺 `lower_right`，整块不可用：包中 `slices` 为空，未结算面积升到 3，`missing_blocks` 指出 `new` / `new-b` / `lower_right` / `20` / `ground`。`tests/test_earthwork.py` 另构造了有一条不受影响小片的输入，验证缺角只丢受影响的片。

## 7. 容量与性能

`python3 examples/earthwork_benchmark.py` 在 Linux 单进程 CPython 3.11.2 下测量每次命令从读输入到写完包的 CPU 时间与峰值 RSS（`os.wait4`；输入生成与独立验算不计时）：

- **竖横条样例**：317×317 作业区，旧面 317 条宽 1 米竖条、新面 317 条宽 1 米横条。锚点全齐时 100,489 片，拒绝；只让一个不被其他块引用的锚点缺失（旧条 `v0` 的 `g0_0`），仍有 100,172 片，拒绝；让两条各缺一角（`v0` 与 `v316`），剩 99,855 片，发布，未结算面积 634。
- **批量验收样例**：每层 140×140 块，边长 2L、`L = 1,000,000` 米；旧层铺 `[0,0,280L,280L]`、高程 0，新层整体向正 X、正 Y 各移 L、高程 2，作业区 `[0,0,281L,281L]`。完整输出 279² = 77,841 片，已结算面积 279²L²，未结算面积 (281²−279²)L²，填方 2×279²L²，挖方 0。把每一侧块与点数组随机打乱后重跑，包字节完全一致。

| 运行 | 结果 | CPU 时间 | 峰值 RSS |
| --- | --- | --- | --- |
| 竖横条全齐 | 拒绝，退出 2 | 0.09 s | 29 MiB |
| 竖横条一个孤锚缺失 | 拒绝，退出 2 | 0.09 s | 29 MiB |
| 竖横条两条缺角 | 99,855 片，退出 0 | 3.4–4.2 s | 38 MiB |
| 140×140 批量 | 77,841 片，退出 0 | 4.2–4.7 s | 115 MiB |
| 140×140 批量（声明打乱） | 字节一致，退出 0 | 4.3–5.3 s | 115 MiB |

实测为本容器 CPython 3.11.2、Linux x86-64、单进程、无第三方运行依赖；同一命令重复运行会有数个百分点浮动。

预算为每次命令 20 秒 CPU 时间、192 MiB 峰值 RSS。`tests/test_earthwork.py` 的 `StripCapacityTests` 与 `BatchCapacityTests` 复现同样的输入，逐片核对小片范围与来源，而不只核对累计值，并断言各自在上限内。

## 8. 边界

不重建三维点、不改写点表或方案、不做跨测次的点对应。一个方案只处理一个矩形作业区；多区结算由调用方逐区运行。缺锚点的块不会被插值、外推或借用相邻块高程补齐；方案不声明的地面区域一律记为未结算。
