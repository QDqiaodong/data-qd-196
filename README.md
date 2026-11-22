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

## 已执行验证

在本容器以 `PYTHONPATH=src python3 -m unittest discover -s tests -v` 实际运行 **77 项测试全部通过**，并运行 `examples/delivery.sh` 完成端到端交付走查（三条 `[2,1,4]` → 撤回 10、20 → 补回 20）、`examples/accept.sh` 完成验收走查（仅候选判 `fail` 且无法指认 → 确认 `(30,marker)` 后锁定超限点）。原基线的 5 项业务测试保持通过；`--source/--output` 报告与基线 HEAD 版本**字节一致**（已用 `git archive` 导出旧版对比）。

覆盖重点：

- 负深度更正：受影响两点仍有唯一零残差解但深度为负，增量只撤回 `(10,marker)`、`(20,marker)`；随后只恢复 `[20,30]`，增量只补回 `(20,marker)`，不为 30 发伪更新（`test_negative_depth_correction_retracts_two_points_then_restores_one`）。
- 真正改变 XYZ 的合格更新，以及无记录的增量仍推进版本（`test_xyz_update_and_empty_delta_both_advance_versions`）。
- 下游两次中断边界：撤回未生效时中断后仍见三条旧点/旧版本/原指纹，重投恰好撤回两条；撤回已生效但未答复时中断，重开后先应用恢复包，再重投首次撤回原包返回原成功版本并保留新点与新指纹（`test_interruption.py`）。
- 上游更正版本、change_id、查询与导出可见性一同生效或一同失败（`test_interrupt_before_correct_commit_keeps_version_and_export`、`test_interrupt_after_correct_commit_leaves_consistent_version`）。
- 更正旧请求重试、未知/冲突更正、失败请求不占 ID、旧版重导字节一致、损坏/跳版/串数据集/基指纹不符不改变下游、原单次重建入口行为（`test_delivery.py`、`test_downstream.py`、`test_validation.py`、`test_legacy_cli.py`）。
- 复测位移验收：同轴两点的四种对应恰有一个超限（各自不可判但整体 `fail`）；确认把 `[0,2]` 区间分别收敛到 `[0,0]`（签字）与 `[2,2]`（不合格）；候选中进不了任何完整对应的边不出现在逐点对应里；位移恰好等于上限不算超限；配置数组置换后报告字节一致；共用同一复测点的缺配集合与邻居可由输入复算；仅因确认占用端点产生的缺配；错误版本（指纹不变）、篡改点值（保留旧指纹）、候选越界、已有报告不被覆盖；真实交付链路导出的视图端到端验收；4096 点规模两个用例的完整逐点结果与证据（`test_acceptance.py`）。

源码入口为 `src/camera_tracks/service.py`（单次重建）、`src/camera_tracks/upstream.py`、`downstream.py`（交付链路）与 `src/camera_tracks/accept.py`、`matching.py`（验收）；测试在 `tests/`。业务字段、身份、单位、界限和失败约定见 [数据契约](docs/contract.md)、[交付契约](docs/delivery.md) 与 [验收契约](docs/acceptance.md)。样例全部为原创合成数据，见 [样例来源](fixtures/SOURCES.md)，源码和样例采用MIT许可。

## 当前边界

不读取图像，不做相机标定、非线性像素误差优化或束调整。投影矩阵缩放会改变代数权重；非正视深保留并标记。

交付链路不实现网络同步、多写者调度或下游跨多版本跳跃；落后多轮时须按序补齐相邻包。两侧各为一个本地 JSON 状态文件，由命令行独占读写。

验收命令只读输入、不做坐标拟合与点表回写，不自动匹配标签；一个配置处理一个验收区，候选与确认完全来自现场输入。
