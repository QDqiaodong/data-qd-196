# 样例来源

全部样例为作者自制合成数据，采用本仓MIT许可。遵循本仓docs/contract.md明确的有界模型，不来自客户、真实样本或生产作业。

`fixtures/build.json` 与 `fixtures/correction.json` 同为作者自制合成数据：前者是离线交付链路的三批（采集序号 10、20、30）样例数据集，后者把右相机第一行末项由 -1 改为 +1 以演示负深度撤回。遵循本仓 MIT 许可。

`fixtures/accept-resurvey-build.json`、`fixtures/accept-zone.json` 与 `fixtures/accept-zone-confirmed.json` 同为作者自制合成数据：复测数据集把三批点移到 `[3,1,4]`、`[2,1,4]`、`[9,1,4]`，两份验收区配置给出九个候选对（确认版追加一条 `(30, marker)` 人工确认），端点指纹由本仓交付链路实际导出。遵循本仓 MIT 许可。

`fixtures/resurvey-plan.json` 同为作者自制合成数据：4×4 单位网格、高程带 `[0,10)`、每格 3 批、一格免测通道与一格禁入的补测区域方案，端点指纹由本仓交付链路对 `fixtures/build.json` 实际导出。遵循本仓 MIT 许可。


`fixtures/earthwork-old-build.json` 与 `fixtures/earthwork-new-build.json` 同为作者自制合成数据：两批地面测点（采集序号 10、20、30，track ID `ground`）分别落在 `[0,0,0]`/`[2,0,0]`/`[0,1,0]` 与 `[1,0,-1/2]`/`[3,0,3/2]`/`[1,1,-1/2]`（即平面 `z = x − 3/2`），经本仓交付链路导出为 `fixtures/earthwork-old-view.json` 与 `fixtures/earthwork-new-view.json`。`fixtures/earthwork-plan.json` 用这两个导出的端点结算作业区 `[0,0,3,1]`：旧块 `[0,0,2,1]` 高 0、新块 `[1,0,3,1]` 在斜面上，故 1 片结算、挖填各 `1/8`、未结算 2。遵循本仓 MIT 许可。
