# data-qd-196 · 多视图观测点重建与离线交付

由已给相机投影矩阵与同名二维观测，解加权代数最小二乘三维点，输出回投影残差、视深和不可观测状态。在此基础上提供一条可纠错的离线点云交付链路：上游按标定版本重建并导出全量/增量包，下游按相邻版本原子应用、可重投、可查询。

## 安装与运行

Python >=3.11，实际验证 CPython 3.11.2（本容器）与 CPython 3.12.14 + uv 0.12.19（原基线环境）。运行依赖仅标准库；构建固定 setuptools 80.9.0、wheel 0.45.1。每个项目独立虚拟环境，公共下载缓存可以复用。

在本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/camera-tracks --source fixtures/input.json --output run/demo.json
```

以上安装命令已在本地公共缓存存在时真实执行。首次使用且缓存为空时须先准备 Python 和锁定构建包，或在允许联网的安装环境移除对应 `--offline`；运行与测试不需要网络。`.venv` 互不共享，未纳入Git。

不建虚拟环境也可以直接运行（本容器实际采用的验证方式）：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m camera_tracks.cli --source fixtures/input.json --output run/demo.json
./examples/delivery.sh            # 完整交付走查，产物在 run/example/
./examples/resurvey.sh            # 补测工单走查，产物在 run/resurvey/
./examples/earthwork.sh           # 土方包走查，产物在 run/earthwork/
```

样例重建 XYZ = [2,1,4]，两台相机回投影误差均为0。

演示标准输出摘要：

```json
{"cameras": 2, "reconstructed": 1, "tracks": 1, "tracks_with_nonpositive_depth": 0, "unobservable": 0}
```

完整证据写入 `run/demo.json`。数值输入为契约约定的十进制字符串或整数分数对，输出的精确有理数采用整数或 `n/d` 字符串，避免浮点舍入。

## 离线标定更正与交付

新增子命令，`camera-tracks --source ... --output ...` 的输入、报告字节和退出码不变：

```sh
camera-tracks build   --input fixtures/build.json    --store run/upstream.json
camera-tracks correct --store run/upstream.json      --request CHANGE.json
camera-tracks query   --store run/upstream.json      --version 1 --sequence 10 --track marker
camera-tracks export  --store run/upstream.json      --full --output run/full.json
camera-tracks export  --store run/upstream.json      --base-version 0 --output run/delta.json
camera-tracks apply   --store run/downstream.json    --package run/delta.json
camera-tracks view    --store run/downstream.json    [--sequence 10 --track marker | --output run/view.json]
```

- 上游：一个数据集建一个状态库（版本 0），之后每次有效更正产生一个不可变的新版本；可按指定版本查询每个点的标定来源、重建状态、深度、回投影诊断与可交付原因；可导出首版全量或任意相邻版本增量，同一端点重复导出字节一致。
- 下游：空库只收首版，此后只收基版本/基指纹吻合的下一包；应用前后校验记录、删除目标与目标指纹，一次原子提交；原包重投答复原成功版本，不回退不重写；损坏、跳版、串数据集、指纹不符的包不留下任何变化。
- 只有唯一重建、全部深度严格为正、全部残差平方长度不超过阈值平方时才交付；零/负深度、超差、不可观测都是逐点业务结果，按不可观测 → 非正深度 → 超差的顺序给主因。
- 交付视图只含 `(采集序号, track ID)` 与精确 XYZ；指纹是 SHA-256，定义、规范化编码与失败诊断见 [交付契约](docs/delivery.md)。

## 复测位移验收

在交付视图之上再做离线验收（[验收契约](docs/acceptance.md)）：

```sh
camera-tracks accept --baseline VIEW1.json --resurvey VIEW2.json \
                     --config ZONE.json --output REPORT.json
```

- 两轮扫描各自经交付链路导出 `view` JSON；配置指定基准/复测测次的 `dataset_id`、`version`、`fingerprint`（三者都须匹配，指纹相同也不能拿错版本）、验收区 ID、非负位移上限（精确有理数文本）、两侧各 1..4096 个必检点、至多 32768 条现场候选与可为空的人工确认。
- 不把同名 track 或最近的点当作同一测点；位移大本身是要发现的结果，不作为排除对应的条件。完整对应须覆盖全部必检点、每点恰用一次并兑现全部确认。
- 对应不完整时报告基准侧非空缺配集合及其全部可用复测邻居（邻居数严格小于集合点数）；存在完整对应时逐点给出可用对应与必定超限/必定不超限/取决于对应，并报告实际可达的最小、最大超限点数各附一份证据对应。最大值为零才可签字，最小值大于零为不合格，其余须核实标签。
- 只读输入、原子新建报告、绝不覆盖已有报告；配置数组的排列不改变报告字节。4096 点规模基准实测约 0.3 s CPU、峰值 RSS 约 54 MiB（预算 30 s / 256 MiB），复现见 `python3 examples/accept_benchmark.py` 与 `./examples/accept.sh`。

## 补测工单

在交付视图之上离线生成下一轮补测工单（[补测工单契约](docs/resurvey.md)）：

```sh
camera-tracks resurvey --view VIEW.json --plan PLAN.json --output REPORT.json
```

- 区域方案给出区域 ID、预期端点（`dataset_id`、`version`、`fingerprint` 三者都须匹配）、网格原点/边长/行列数、有效高程带、每格要求的独立采集批数，以及免测通行矩形与禁入矩形两个列表（合计至多 25,000 个）。格子归属与高程判定用精确有理数，不用浮点。
- 每格四类互斥：禁入 > 通行 > 已验收（独立批数达标，按 `sequence` 去重）> 待补。待补格与通行格沿格边连通（角接触不算），禁入与已验收格隔断工单；每个含待补格的连通分量出一张工单，仅含通行格的分量不出单。
- 报告给出四类精确格数（十进制整数字符串）与每单的待补格数、最小外接格边界、标识格和精确待补范围（若干非空、两两不重叠的半开矩形，并集恰为本单待补格）；只置换输入数组或重排对象键不改变报告字节。只读输入、原子新建报告、绝不覆盖已有报告。
- 10^9×10^9 网格、两万余障碍的容量样例实测约 0.36 s CPU、峰值 RSS 约 67 MiB（预算 20 s / 192 MiB），复现见 `python3 examples/resurvey_benchmark.py` 与 `./examples/resurvey.sh`。

## 离线土方面叠算

在交付视图之上离线结算道路整修前后的挖填方（[土方包契约](docs/earthwork.md)）：

```sh
camera-tracks earthwork --old OLD_VIEW.json --new NEW_VIEW.json \
                        --plan ZONE.json --output PACKAGE.json
```

- 方案给出作业区 ID、矩形作业范围、两份 view 各自的 `dataset_id`、`version`、`fingerprint`（三者都须匹配，同指纹不能替代同版本），以及两侧各自的地面分块清单。每层至多 20,000 块，每块指定左下、右下、左上三个点身份；点身份存在时其 XY 必须精确等于声明角，缺测的角只吊销本块，且不隐瞒同块其他角的 XY 错误。
- 每块以三点唯一决定的仿射高程面表示整块，不跨块连续、不外推。旧块与新块的正面积交叠是候选小片，只有两块锚点都完整时才结算；零线穿过小片时按精确平面差分别积分挖方与填方，从不用净高差或中心点正负。覆盖面积、挖填量与覆盖判断全用精确有理数。
- 包逐片给出范围、两来源块 ID、四角高差、挖方、填方，并给出缺测块清单、已结算/未结算面积、挖填小计与 `complete`/`partial` 状态；未结算面积是作业区面积减已结算小片面积之和，不是两侧缺测面积相加。已结算小片超过 100,000 片时明确报错且不生成包。
- 只读输入、原子新建包、拒绝已有目标与指向任一输入的目标；声明与点数组的排列、对象键顺序不改变包字节。317 条竖条铺 317×317 全部得到 100,489 片时拒绝；两条缺角剩 99,855 片发布、未结算 634 m²。逐片核对的 279² 批量样例实测约 4.2–4.7 s CPU、峰值 RSS 约 115 MiB（预算 20 s / 192 MiB），复现见 `python3 examples/earthwork_benchmark.py` 与 `./examples/earthwork.sh`。

## 离线分片坐标归并

在各自带局部原点的 `view` 导出之上离线生成统一工程坐标点集（[坐标归并契约](docs/merge.md)）：

```sh
camera-tracks merge --plan MERGE_PLAN.json --output PACKAGE.json \
                    --view FRAGMENT_ID=VIEW.json [--view ...]
```

- 方案给出方案 ID、基准分片（T=0）、分片清单与联测记录；联测 u→v 表示 `T(v)−T(u)`，为三个整数微米分量，反向使用全部分量取反，记录分已复核（永不舍弃）与暂定。全部联测按无向图必须连通到基准，否则是输入错误。
- 绑定端点的分片必须提供一个现有 view，且 `dataset_id`、`version`、重算的点表指纹三者逐一匹配（同指纹不能替代另一个版本）；即使分片最终被扣留也照样校验，缺/多文件、篡改点表都报错。不同分片的 `(sequence, track_id)` 相互独立，重名或坐标重合都不合并。
- 闭合不一致时只考虑**单条**暂定记录的舍弃：舍弃后仍须连通且剩余联测存在唯一精确位置；不选最先候选、不拟合、不做两条联合修复。分片仅在每种可行舍弃下 T 都相同时放行，否则扣留并给两条可由方案独立复算、位置不同的舍弃证据；没有任何单条解释时输出可识别的无法解释包，连基准点在内一个归并点都不发。
- 归并点为局部 XYZ（米）加 T（米），全程整数微米与 `Fraction` 精确有理文本，身份保留为 `(fragment_id, sequence, track_id)`。包为独立 `camera-tracks-merge-package`，不改原生 view 的生成与指纹；只读输入、原子新建、拒绝已有目标与输入目标，重排声明数组、对象键或点行不改变包字节。
- 60000 分片、60002 条联测、20001 个可行舍弃的验收批量实测约 1.9 s CPU、峰值 RSS 约 100 MiB（预算 20 s / 256 MiB），复现见 `python3 examples/merge_benchmark.py` 与 `./examples/merge.sh`。

## 离线训练材料与盲测基准发布

在交付视图之上离线发布训练材料、签发盲测题本、登记预测，并在迟到的采集来源关联出现时审计旧成绩、撤销受影响题本（[基准发布契约](docs/benchmark.md)）：

```sh
camera-tracks bench --config CONFIG.json --project PROJECT.json \
                    --view DATASET_ID=VIEW.json [--view ...]
camera-tracks bench-release          --project PROJECT.json --request RELEASE.json
camera-tracks bench-issue            --project PROJECT.json --request ISSUE.json
camera-tracks bench-relate           --project PROJECT.json --request RELATE.json
camera-tracks bench-score            --project PROJECT.json --request SCORE.json
camera-tracks bench-scores           --project PROJECT.json [--booklet ID]
camera-tracks bench-audit            --project PROJECT.json [--booklet ID]
camera-tracks bench-status           --project PROJECT.json
camera-tracks bench-export-training  --project PROJECT.json --id ID --output OUT.json
camera-tracks bench-export-booklet   --project PROJECT.json --id ID --output OUT.json
```

- 初始化绑定 1..8 份现有 `view`，各自 `dataset_id` 不同，各声明完整 `dataset_id`/`version`/`fingerprint`；逐份重算点表指纹，三字段都须匹配。坐标与来源端点冻结进项目，原文件可删。同 `dataset_id` 同名 `sequence` 的点无条件同组；复核记录无向、跨批、跨数据集、可传递；不同批不会因坐标或 track 同名自动相连。项目累计 80,000 点、120,000 条显式关系；一次关系追加整批原子（未知/重复/自连/批内任一非法都整批拒绝）。
- 训练发布按清单交付完整 XYZ 与来源端点，不额外下发同组点，但整组视为已暴露；发布即生效，与是否有人下载无关。题本只给身份、XY、来源端点与误差限，不下发真实 Z。任一点已暴露即拒绝签发整个题本。两者为独立于 `view` 的稳定 UTF-8 JSON，以字节 SHA-256 标识，历史导出永远按原字节重领。
- 评分绑定题本 ID、题本文件 SHA-256 与预测 ID，覆盖题本全部点、每点恰一项；逐点 `|预测−真值| ≤ 误差限` 判通过（等于算通过），只回通过点数/总数与身份，不下发真值。错误指纹、缺项、多项、重复、非法数字都不占预测 ID。
- 每次首次成功推进一个修订号，`op_id` 全局幂等：同 ID 同请求重投原样返回首次回执、不推进修订（哪怕题本已撤销），同 ID 不同内容拒绝。请求相等忽略键序、清单排列与无向端点顺序，数字按精确值比较。
- 追加关系或发布材料后，任一点进入已暴露组的题本整本撤销：冻结首次撤销的修订号、导致操作 ID 与一组当时已连通的待测点/已发布训练点/训练发布 ID（后续关系绝不改写），其旧成绩退出当前可比较表；无关题本成绩照常可取、仍可登记。审计独立取回原成绩与撤销依据、绝不清零、也不混入可比较成绩。训练发布/关系追加与引发的撤销一次原子生效。
- 只读导出拒绝覆盖既有路径、不暴露半文件、失败不撤销已登记材料；程序重开后据持久状态恢复；`CAMERA_TRACKS_FAULT=benchmark-before-commit|benchmark-after-commit|benchmark-export-before-write` 用于验证事务边界。

4 份各 100 批、每批 200 点（合计 80,000 点）的验收批量走查：初始化 → 为第 2、3 份 view 各签发 4096 点题本并登记预测 → 只发布第 1 份 view 的 1 个点 → 追加连接第 1、2 份 view 代表点的边，第 2 份题本撤销、第 3 份仍可取用；件内不含真实 Z，重导字节一致。单进程 CPython 串行命令实测总 CPU 约 7.7 s、各命令峰值 RSS ≤ 91 MiB（预算 45 s / 256 MiB），复现见 `python3 examples/bench_benchmark.py` 与 `./examples/bench.sh`。

## 已执行验证

在本容器以 `PYTHONPATH=src python3 -m unittest discover -s tests -v` 实际运行 **180 项测试全部通过**（原 150 项 + 基准发布 30 项），并运行 `examples/delivery.sh` 完成端到端交付走查（三条 `[2,1,4]` → 撤回 10、20 → 补回 20）、`examples/accept.sh` 完成验收走查（仅候选判 `fail` 且无法指认 → 确认 `(30,marker)` 后锁定超限点）、`examples/resurvey.sh` 完成补测工单走查（三点使 `(2,1)` 格达标，开出覆盖 13 个待补格的一单）、`examples/earthwork.sh` 完成土方走查（旧块高 0、新块 `z = x − 3/2`，1 片、挖填各 `1/8`、未结算 2）、`examples/merge.sh` 完成坐标归并走查（三角网全暂定扣留 A/B → 锁定 R→A 后放行 A → R→B 改 2 μm 后全网一致）、`examples/bench.sh` 完成训练/盲测走查（H 恰在限内通过、发布 A0 后 A1 题本被拒、B1—H 迟到关联使 H 退出选型而 U 照常评分、撤销后题本按原字节重领且不含真实 Z）。原基线的 5 项业务测试保持通过；`--source/--output` 报告与基线 HEAD 版本**字节一致**（已用 `git archive` 导出旧版对比）。

覆盖重点：

- 训练材料与盲测基准：同批 A0/A1 与 B0/B1、独立批 H/U 的验收走查（H 恰在误差限通过、发布 A0 后无下载两份成绩仍可用、A1 单独题本被拒、删源文件后追加 A1—B0 再 B1—H 只使 H 退出选型、U 照常评分）；题本撤销后按原字节重领且不含真实 Z、历史成绩与回执不变；新训练发布触发 U 撤销、两次旧操作重放取回各自首次撤销清单互不串改、证据在后续关系下不改写；同指纹错版本、篡改点表、错误题本指纹、非法关系整批无效、请求重排幂等；`benchmark-before-commit`/`benchmark-after-commit` 两条真实中断边界；4×100×200 容量批量的第 2 份题本撤销、第 3 份仍可选型并完整核对两件题本与历史回执（`test_benchmark.py`）。

- 负深度更正：受影响两点仍有唯一零残差解但深度为负，增量只撤回 `(10,marker)`、`(20,marker)`；随后只恢复 `[20,30]`，增量只补回 `(20,marker)`，不为 30 发伪更新（`test_negative_depth_correction_retracts_two_points_then_restores_one`）。
- 真正改变 XYZ 的合格更新，以及无记录的增量仍推进版本（`test_xyz_update_and_empty_delta_both_advance_versions`）。
- 下游两次中断边界：撤回未生效时中断后仍见三条旧点/旧版本/原指纹，重投恰好撤回两条；撤回已生效但未答复时中断，重开后先应用恢复包，再重投首次撤回原包返回原成功版本并保留新点与新指纹（`test_interruption.py`）。
- 上游更正版本、change_id、查询与导出可见性一同生效或一同失败（`test_interrupt_before_correct_commit_keeps_version_and_export`、`test_interrupt_after_correct_commit_leaves_consistent_version`）。
- 更正旧请求重试、未知/冲突更正、失败请求不占 ID、旧版重导字节一致、损坏/跳版/串数据集/基指纹不符不改变下游、原单次重建入口行为（`test_delivery.py`、`test_downstream.py`、`test_validation.py`、`test_legacy_cli.py`）。
- 复测位移验收：同轴两点的四种对应恰有一个超限（各自不可判但整体 `fail`）；确认把 `[0,2]` 区间分别收敛到 `[0,0]`（签字）与 `[2,2]`（不合格）；候选中进不了任何完整对应的边不出现在逐点对应里；位移恰好等于上限不算超限；配置数组置换后报告字节一致；共用同一复测点的缺配集合与邻居可由输入复算；仅因确认占用端点产生的缺配；错误版本（指纹不变）、篡改点值（保留旧指纹）、候选越界、已有报告不被覆盖；真实交付链路导出的视图端到端验收；4096 点规模两个用例的完整逐点结果与证据（`test_acceptance.py`）。
- 土方包：主样例 `[0,0,3,1]` 旧块 `[0,0,2,1]` 高 0、新块 `[1,0,3,1]` 在 `z = x − 3/2` 上，只出 1 片、已结算 1、未结算 2、挖填各 `1/8`；删去新块一角后包空、未结算升到 3 并点名缺角，另一组验证不受影响的小片仍输出；同高整片（`complete`、挖填均 0）、恰好共边不出片、零线过角点分成 1/6 与 1/6、同层重叠（含缺测块）被拒、缺一角时另一角 XY 错误仍被拒、同指纹不同版本被拒、输入重排后字节一致、已有目标与输入目标被拒、发布中途失败不留目标或临时文件；317×317 竖横条全齐 100,489 片与只缺一个孤锚都拒绝，两处缺角则 99,855 片发布且未结算 634；140×140 批量逐片核对 77,841 个小片的范围与两来源块、已结算 279²L²、未结算 (281²−279²)L²、填方 2×279²L²，打乱声明后包字节一致；随机切分的双侧分块与稠密采样独立对照，累计挖填在 1% 尺度内一致且面积逐片核尽；真实交付链路导出的两份视图端到端结算并保留原单次重建与点表消费回归（`test_earthwork.py`）。
- 补测工单：5×2 混合分类场景（禁入压过通行、同批多 track 只算一批、z 恰为上界与网格外点不计入）开一单并在通道被禁入后拆成两单，逐单核对矩形实际覆盖的格子；角相接不并单、仅通行分量不出单、零工单正常；同指纹不同版本、保留指纹篡改点表、方案矩形越界、矩形超量、非法网格/高程带/批数均被拒；输入重排后报告字节一致；已有报告不被覆盖、输出不得指向输入；真实交付链路导出的视图端到端派单；10^9×10^9 容量两用的完整矩形核对（在网格内、不相交、不覆盖禁入或通道、面积与精确格数一致）（`test_resurvey.py`）。
- 坐标归并：验收三角网三阶段（全暂定三条候选且 A/B 扣留、R→A 复核后只剩两条候选且 A 放行、R→B 改 2 μm 后全网一致零候选），每条扣留证据用独立 BFS 重解复算；平行矛盾联测、反向输入取负、已复核记录绝不可舍弃、单桥不可弃、两个不共边失闭环无单条修复且全包不发点；错误端点（含同指纹不同版本）、篡改被扣留分片点表、缺/多 view、未连通、重复 ID 与非法结构均按输入错误拒绝；重排声明/键/点行包字节一致、已有目标与输入目标拒绝、`merge-before-write` 注入失败不留半包、同名点跨分片不合并、真实 build/export/apply/view 链路端到端归并；800 个随机小图对全量逐舍弃 BFS 暴力核对候选集合、平移量与逐分片决定（`test_merge.py`，另含 60000 分片批量的完整候选/证据核对与 CPU、RSS 预算）。

源码入口为 `src/camera_tracks/service.py`（单次重建）、`src/camera_tracks/upstream.py`、`downstream.py`（交付链路）、`src/camera_tracks/accept.py`、`matching.py`（验收）、`src/camera_tracks/resurvey.py`（补测工单）、`src/camera_tracks/earthwork.py`（土方包）、`src/camera_tracks/merge.py`（坐标归并）与 `src/camera_tracks/benchmark.py`（训练材料与盲测基准发布）；测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)、[交付契约](docs/delivery.md)、[验收契约](docs/acceptance.md)、[补测工单契约](docs/resurvey.md)、[土方包契约](docs/earthwork.md) 、[坐标归并契约](docs/merge.md) 与 [基准发布契约](docs/benchmark.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。

## 当前边界

不读取图像，不做相机标定、非线性像素误差优化或束调整。投影矩阵缩放会改变代数权重；非正视深保留并标记。

交付链路不实现网络同步、多写者调度或下游跨多版本跳跃；落后多轮时须按序补齐相邻包。两侧各为一个本地 JSON 状态文件，由命令行独占读写。

验收命令只读输入、不做坐标拟合与点表回写，不自动匹配标签；一个配置处理一个验收区，候选与确认完全来自现场输入。

补测工单命令只读输入、不重建三维点、不推断不同测次的点对应；一个方案处理一个区域，矩形列表完全来自现场输入，工单只描述本轮补测范围，不调度人员或设备。

土方命令只读输入、不重建三维点、不做跨测次的点对应；块高程面完全由各块三个锚点决定，缺锚点不外推、不插值、不借相邻块补齐；一个方案处理一个矩形作业区，多区结算由调用方逐区运行。

坐标归并命令只读方案与 view、不回写任何原生视图或状态库；一条暂定联测要么整体成立要么整体舍弃，不做平差、部分分量修复或两条联合修复；一个方案处理一个基准与一张联测网，多基准工程由调用方先统一基准后逐网运行。

基准发布命令只处理登记到本流程的材料和关联，不推断外部训练行为、不扫描外部文件，也不阻止对导出文件的本机直接访问（评分为本地评估员操作，无账户、无网络服务）；不回写任何 view、交付库或点表，不做坐标回写与跨测次点对应；一个项目处理一套材料与关联。
