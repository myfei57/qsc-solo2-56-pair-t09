# 班次交接（shift handover）设计说明

## 1. 要解决的问题

系统原来没有班次概念：上一个值班员开了一半的批量开门、挂起还没处理的门区
故障，接班人完全无从知晓；跨班下达的旧命令也不会被识别。本子系统在既有
门控 / 联锁 / 审计之上加了一个**值班台唯一控制点**，保证：

1. 在办事项必须**逐项**交到接班人手上，没交的**自动挂起**；
2. 挂起项在**发车放行**上一票否决，恢复必须重新过联锁；
3. 跨班迟到命令被**权威代际**挡掉；
4. 谁能交接、谁能会签由**角色 + 会话**控制；
5. 班次与交接历史**只追加、不可变、可自校验**，掉电可重放恢复。

## 2. 关键取舍（安全边界）

### 2.1 未交接完的动作：硬冻结，确认后重新过联锁再续办

选择"**硬冻结**"而不是"接班人确认后原样继续"：

* `begin_handover` 一旦执行，值班台进入 `frozen`，除交接本身外的**所有安全
  动作**（到站开门、批量开门、故障置位/清除、对位、间隙确认、清扫、指令入队
  等）都被 `ShiftService.guard()` 统一拒绝，直到交接完成、代际切换；
* 接班人对每件事项**逐项确认**（`acknowledge_item`）。确认只表示"我知道并
  接收这件事"，**不代表动作可以直接继续**；
* 确认事项要真正续办时（`resume_item`）：
  * 安全类事项（批量开门 / 故障 / 门控指令）必须经**班组长会签**；
  * 恢复时由平台回调按**当前**现场重新过一遍联锁（门是否还锁、布局是否还在），
    不盲信交接时的快照；联锁不通过就保持挂起。

对比"直接冻结到死"的方案，这样接班人不用在班前把整列操作重开一遍；对比
"确认即继续"的方案，冻结窗口杜绝了交班方在交接期间继续改动现场。

### 2.2 班组长复核分级，而不是所有交接都会签

* **零挂起、全部逐项确认**：交班人发起、接班人逐项签收即可完成，不强制组长
  在场（高频常态交接不能被流程拖死）；
* **只要有挂起项**：交接进入 `review`，必须由**独立的第三方班组长**会签
  （会签组长不能属于交班班次、不能是发起人，也不能同时是签收人）；
* **接班人始终缺位**：班组长可 `force_close` 强制收口（必须填原因），当班
  结束、未办结事项全部变成无主**孤儿**挂起；新班开班后由班组长 `adopt_orphans`
  认领，认领后状态为"已确认"，恢复安全动作仍需会签。

### 2.3 挂起即扣发放行

`Platform.depart()` 在既有"间隙确认 + 关门落盘"之后，再过一道班次闸门
`ShiftService.check_release()`：

* 任何处于 `open / presented / suspended` 的安全事项都会**全局**一票否决
  发车放行（不区分列车、站台——挂起事项未闭环前不放行任何车）；
* `acknowledged / resumed / done / aborted` 不再拦截；
* 每次拒绝与放行都写哈希链台账（`release_denied` / `release_granted`）。

## 3. 状态机

### 3.1 班次

```
planned ──open_shift──▶ active ──begin_handover──▶ frozen ──complete──▶ handed
   │                      ▲                           │
   │                      └──── (接班人签收后) ────────┘
   └────────────────────────── force_close ──────────▶ forced
```

* 同一时刻全台至多一个 `active/frozen` 班次（值班台单一控制点）；
* 开班、交接完成、强制收口都会推进**权威代际 epoch**。

### 3.2 交接

```
begin ─▶ open（逐项确认中）
           ├─ 全部确认 ─▶（零挂起：直接完成 / 有挂起：先会签）─▶ done
           └─ 到点仍有未确认 ─▶ review（自动挂起）─ 组长会签 ─▶ done
无接班人 + 组长强制收口 ─▶ forced
```

* 确认时限 `handover_deadline_s`（默认 300 秒，可在 `begin` 时覆盖）；
* `cycle()` 每个控制周期调用 `tick()`，到点以 **system** 身份自动挂起，不依赖
  人是否记得点按钮；班组长也可在时限前人工提前挂起。

### 3.3 在办事项

```
open ──begin──▶ presented ──ack──▶ acknowledged ──resume(会签)──▶ resumed ──▶ done
                    │                                                       ▲
                    └──────────── 到点/强制 ──▶ suspended ─resume(会签)──────┘
                                                    └──────── abort（安全收口）
```

* 故障事项不能 abort，只能走故障清除（`clear_fault` 自动办结）；
* 批量事项 abort 时平台执行**安全收口**：把该站已开的门全部关回。

## 4. 权限模型（谁能交接）

| 动作 | 谁可以执行 |
|---|---|
| 登记人员 / 计划班次 / 编排名册 | 班组长（在册身份自证；冷启动自举放开） |
| 开班 | 本班名册中的值班员或班组长 |
| 发起交接 | 当前在值班次的值班员/班组长 |
| 逐项确认 | **接班班次**名册中的值班员/班组长 |
| 提前挂起未交项 | 班组长 |
| 会签 | **独立第三方**班组长（不属于交班班次） |
| 签收完成交接 | 接班班次名册成员（不能同时是会签组长） |
| 强制收口 | 班组长（必须填原因） |
| 认领孤儿 | 在值班次的班组长 |
| 恢复安全类事项 | 在值值班员操作 + **另一名**班组长会签 |

身份有两种载体：

* `token`：`check_in` 领取的内存会话，绑定 `operator_id / shift_id / epoch`；
* `operator_id`：开班前还没有会话的引导类动作（计划、名册、会签、强制收口）
  用在册班组长身份自证。

## 5. 权威代际与跨班时序

* 会话只存在内存里，不持久化（重启后所有人重新签到）；
* 交接完成：epoch +1，交班班次会话全部作废，接班班次会话**提升到新代际**
  （接班人无需重签）；开班/强制收口：epoch +1，旧会话清空或失效；
* `guard()` 对每个安全动作依次检查：会话存在 → 操作员属于当前班次 →
  会话 epoch == 当前 epoch → 班次非 frozen。拒绝原因可区分：
  `bad_session / not_on_duty / stale_epoch / handover_frozen / desk_inactive`；
* 冻结窗口在门控动作入口和发车放行入口**各挡一次**：前者挡"现场动作"，后者
  挡"既成事实后的放行结论"。

## 6. 与门控 / 联锁 / 审计的一致性

* **门控**：批量开门若有延后（超单批上限）或跳过（门区锁定），自动登记/更新
  一件 `batch_open` 事项，快照写入事项 payload；一次开完则自动办结；
* **联锁**：置位故障自动登记 `fault` 事项（按门区去重），清除故障自动办结；
  恢复批量时逐门区读取**当前**锁定状态，锁定门区不得开；
* **审计**：每个班次动作同时写既有审计流水（`handover_began` /
  `handover_ack` / `handover_suspended` / `handover_countersign` /
  `handover_completed` / `departure_release_denied` …）与哈希链台账；
* **发车放行**：见 2.3，是门控结论与班次结论的合流点；
* **控制周期**：`cycle()` 驱动 `tick()` 处理确认超时。

## 7. 历史不可变与部分失败恢复

落盘分两层：

| 层 | 文件 | 性质 |
|---|---|---|
| 台账（事实） | `shift-ledger-00000.jsonl`（分段追加） | 只追加，SHA-256 哈希链 |
| 投影（当前态） | `shift/operator/*`、`shift/shift/*`、`shift/handover/*`、`shift/item/*` | 原子 JSON，可随时由台账重放重建 |

台账每条记录含 `seq / kind / at / actor / payload / prev_hash / hash`，
对规范 JSON（排序键、无空白）做 SHA-256。任何改内容、删行、插行、乱序都会让
`ShiftLedger.verify()` 在第一条失配处失败；服务启动重放时校验不过**直接拒
启动**，绝不带篡改历史运行。

写序固定为**台账在先**：`append 台账（fsync）→ 更新投影 → 写审计`。

* 台账写完、投影没写完就掉电：重启 `recover()` 重放台账补齐；
* 投影写一半：投影文档是 `os.replace` 原子替换，不存在半截 JSON；
* 会话丢失：按设计重签即可，不影响事实；
* 重启后：epoch、班次状态、事项归属/状态、交接结论全部从台账重建。

## 8. HTTP 接口

只读：`GET /api/shift/desk`、`/api/shift/items`、`/api/shift/handovers`、
`/api/shift/handovers/{id}`、`/api/shift/ledger`。

动作（请求体带 `token`，开班前引导动作可带 `operator_id`）：

```
POST /api/shift/operator           登记值班人员（operator_id, name, role）
POST /api/shift/plan               计划班次（shift_id）
POST /api/shift/roster             编排名册（shift_id, roster[], leader_id）
POST /api/shift/checkin            签到（operator_id, shift_id）→ token
POST /api/shift/open               开班
POST /api/shift/begin-handover     发起交接（incoming_shift_id?, deadline_s?）
POST /api/shift/acknowledge        逐项确认（handover_id, item_id）
POST /api/shift/suspend            提前挂起（仅班组长）
POST /api/shift/countersign        班组长会签
POST /api/shift/complete-handover  签收完成
POST /api/shift/force-close        强制收口（handover_id, reason）
POST /api/shift/adopt-orphans      认领孤儿（item_ids? 省略=全部）
POST /api/shift/items              登记通用在办事项
POST /api/shift/items/{id}/resume    恢复（leader_id 会签）
POST /api/shift/items/{id}/complete  办结
POST /api/shift/items/{id}/abort     终止（reason，批量项自动安全收口）
```

既有安全动作接口（`/api/arrive`、`/api/depart`、`/api/batch-open`、
`/api/fault`、`/api/clear-fault` …）在请求体里多带一个 `token`。**未启用
班次管理时不传 token 行为完全不变**（冷启动、旧部署无感）。

## 9. 命令行

```
make shift-demo     # python -m psdline shift-demo --deadline 10
```

该演示覆盖：开班 → G02 挂故障 + 半截批量 → 发起交接即冻结 → 逐项确认故障、
批量项到点自动挂起 → 第三方班组长会签 → 代际切换 → 发车被扣 → 清故障、
会签续开批量 → 放车成功 → 哈希链校验。

## 10. 配置

`config.shift`：

```json
{ "handover_deadline_s": 300.0, "journal_segment": 512 }
```

## 11. 模块清单

```
psdline/shift/model.py        数据模型与状态常量
psdline/shift/registries.py   当前态投影（人员/班次/交接/事项四张表）
psdline/shift/ledger.py       只追加 SHA-256 哈希链台账与校验
psdline/shift/service.py      状态机、闸门、放行联动、会话、重放恢复
psdline/app.py                钩子装配、安全动作 guard、depart 放行合流
psdline/console/api.py        /api/shift/* 接口
psdline/cli.py                shift-demo
tests/test_shift.py           20 项用例（含篡改检测与重启重放）
```
