# 换桩对账联调站

本地服务提供两种运营商日账、施工分表底数和会计交接台。全部站日、公司、读数及票据为合成资料。运行依赖为 Python 3.11 标准库。

```sh
python3 source_service.py --port 8040
python3 -m unittest discover -s tests -v
```

`GET /` 返回当前 shift。联调人员用 `POST /time` 发送 shift 整数，把轮次向前拨。重启收件端不改变来源时钟、档案任务或会计回条。启动全新联调才重置这些状态。资料中的 known 表示该信息何时可以参与决定。

## 两家运营商

旧站可取 `GET /legacy/day?day=2026-11-20`，使用返回的 edition 与 next 依次查询。next 为 null 表示该修订已到末页。同一版的游标可以重复读取。409 表示原游标已失效，410 表示已从实时库转入归档。不带 edition 的调用会读到当前修订。

新站 `GET /new/feed?after=0` 返回大于已处理序号的事件，more 指示还有分页。record 附带一条账片，retract 给出站日和账片标识；seal 附带站日、版名和它覆盖的最大事实序号。封账以后到达该日的修改或撤回会使旧封账不能证明当前完整。410 返回当前可续接的 floor 和可能受缺段影响的营业日。恢复时可从 floor 后读到仍保留的事件，缺失段涉及的日期需要相应日包。账片的日、时间和变动序号各自独立。

`GET /archive/jobs` 可查任务及本轮剩余申请名额。`POST /archive/jobs` 接受 operator（legacy 或 new）、day 和调用方给出的稳定 token。重复提交相同参数返回同一任务，不再计入名额；新申请本轮最多一个。任务号可用于 `GET /archive/job/任务号`，forming 时已列出的分片可能可以领取，但当前包仍不完整；ready 返回全包清单。清单的 includes 给出包里可能出现的日期，分片逐日标明 day、rows 和 sha256。`GET /archive/content/文件名?job=任务号` 返回 UTF-8 NDJSON，行数和整片摘要用于核对。有相邻日时可只领取关联分片。每份清单的版名按运营商和申领站日识别，缺分片的营业日维持未确认，已有完整来源的日期不受别日影响。

## 智能表与施工记录

时间采用北京时间，账片用同一站日内的时分，区间左闭右开。kwh 和表底为十进制定点千瓦时。账片号只在本运营商内有意义。桥接文件 `/data/bridges.json` 的 links 含运营商、账片标识。按桥接记录的已知版和状态解释，撤回使以前的跨网合并不再有效。`/data/switch.json` 给每个枪位的接入表号、装表世代、有效时间和替换旧单关系。interval_uncertain 给出一个接线时间只能落在其间的闭区间；覆盖这一段的交易在无独立表端明细时不归给任何一侧。split 的项按表号、世代、起点、终点与 kWh 排列。

申请 `GET /meter?meter=NEW&epoch=g19&window=2026-11-20` 领取分表读数，202 表示读数还没有经施工确认。读数版别在 revision。首末时间按目标窗口解释。独立更正的表底和同世代身份同时有效，旧版仍可回看已冻结的交接。可确认的共表电量为同一装表世代的首末累积之差，归枪用量只在有相应真实分段证明后扣减。未归枪的余量留在共表窗口。若累计口径或总数不匹配，先标待核。

## 会计交接

`GET /data/close.json` 给分表世代和站日的截账轮 cutoff（含本轮）、所需运营商以及已签发的差额审批。各表独立核对，不能用部分资料证明整窗日账已经完整。`POST /account/handoff` 接收 request、meter、window、type、energy 和 documents，type 为 initial 或 adjustment。送调账另有 previous（旧 request）和 approval（审批号）。会计记录可为 returned（带明确时段 check）、received、posted。503 时会计台可能已收件，用 `GET /account/result/请求号` 读回。404 表示目前看不到，返回原请求、业务状态和回条。相同请求号要用相同内容；已接收的原交接可以继续变成 posted，但不能用后到的计算改写，调账另留记录。
