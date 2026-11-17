-- ============================================================
-- 05 17track 轨迹接入层
--
-- 按 17track Tracking API v2.2 的真实机制设计(已核对官方文档):
--   · 计费按"注册单号数", 不按查询次数。注册成功后自动跟踪不再扣配额。
--   · 注册后几秒内返回首次结果; 后续按上次状态每 6~12 小时自动跟踪一次,
--     已签收/异常状态的每 24 小时一次 —— 所以【不需要自己写轮询调度】。
--   · 连续 30 天无事件更新 → 系统自动停止跟踪; 已签收保持 15 天 → 停止。
--     停止后可重新激活 1 次, 停止的号保留 90 天后删除。
--   · 单次请求最多 40 个号, 限流 3 req/s, 约 40 万号/小时。
--   · 异步: 首次 gettrackinfo 常返回空, 靠 webhook 推 TRACKING_UPDATED。
--   · 传 lang 做翻译会额外多扣 1 份配额 —— 所以不传, 自己在入库时映射中文。
--
-- 成本主变量 = 每月注册的单号数, 因此: 一单只注册 1 条腿(不注册 2~3 条)。
-- ============================================================
CREATE SCHEMA IF NOT EXISTS trk;

-- ---------- 承运商编码映射 (物流服务商 -> 17track carrier code) ----------
-- 编码取自 https://res.17track.net/asset/carrier/info/apicarrier.all.json (3598 家在册)
-- '0' = 不指定承运商, 交给系统自动识别(返回 origin=1 为置信结果, 3 为猜测)
-- 我方 10 家中方承运商在 17track 均有对应编码, 且附加参数均为可选
CREATE OR REPLACE TABLE trk.dim_carrier_map (
    物流服务商      VARCHAR PRIMARY KEY,
    十七track编码   VARCHAR,
    承运商名称      VARCHAR,
    主查腿          VARCHAR,
    备注            VARCHAR
);

INSERT INTO trk.dim_carrier_map VALUES
 ('WYT-USPS',      '190284', 'WINIT 万邑通',     '主单号', 'GFUS 开头为万邑通分段号, 可直接查; 末端 USPS 可配 final_carrier=21051'),
 ('Hete-Logistics','191376', '3PE EXPRESS',      '主单号', '3PE 货代单可直查; 有转单号(887/889 段 USPS)时优先查转单号'),
 ('postnet',       '100644', 'PostNet 南非',     '主单号', 'WMS 侧签收率 0%, 本项目最大盲区; 单号复用率高需按号去重注册'),
 ('Yun Express',   '190008', 'YunExpress 云途',  '主单号', 'YT 格式规范'),
 ('SF Express',    '100012', 'SF Express 顺丰',  '主单号', '部分单号带 "+1" 多包裹后缀, 注册前需拆分; WMS 签收率仅 28%'),
 ('DHL',           '101266', 'DHL',              '主单号', 'postal_code 为可选附加参数, 我们邮编覆盖率 100%'),
 ('UPS',           '100002', 'UPS',              '主单号', '1Z 开头'),
 ('YFH-Logistics', '190389', 'YFH',              '转单号', '一飞; 混合 UPS/UUS 段'),
 ('XS_Express',    '190477', 'XS Express',       '主单号', 'XSSY 前缀'),
 ('pep',           '0',      '(自动识别)',        '主单号', '17track 无同名承运商, 走自动识别'),
 ('zx express',    '0',      '(自动识别)',        '主单号', '仅 3 单, 可忽略'),
 ('zxin express',  '0',      '(自动识别)',        '主单号', '仅 14 单, 可忽略'),
 ('BR-JZlogistic', '0',      '(自动识别)',        '主单号', '仅 10 单, 可忽略'),
 ('Frodio-JYOFFline','0',    '(自动识别)',        '主单号', '仅 1 单, 可忽略'),
 ('',              '0',      '(未分配)',          '主单号', '未分配承运商的单无法注册, 需先在 WMS 补齐');

-- ---------- 注册台账 (整个集成的心脏) ----------
CREATE OR REPLACE TABLE trk.subscription (
    单号            VARCHAR,
    承运商编码      VARCHAR,
    注册承运商      INTEGER,          -- 17track 回填的 carrier code
    origin          INTEGER,          -- 1=系统置信 / 2=传入正确 / 3=系统猜测
    履约单号        VARCHAR,          -- 通过 tag 字段回传, 不另建映射表
    物流服务商      VARCHAR,
    仓库代码        VARCHAR,
    目的国          VARCHAR,
    注册层级        VARCHAR,          -- T1/T2/T3, 用于分层灰度与成本归集
    状态            VARCHAR,          -- pending/accepted/tracking/delivered/stopped/rejected/exhausted
    注册时间        TIMESTAMP,
    首次有数据时间  TIMESTAMP,
    末次事件时间    TIMESTAMP,
    已重激活        BOOLEAN DEFAULT FALSE,
    拒因代码        INTEGER,          -- -18019903 无法识别 / -18019907 日限额 / -18019908 配额用尽
    拒因说明        VARCHAR,
    PRIMARY KEY (单号, 承运商编码)
);

-- ---------- 轨迹事件 (append-only, webhook 与补拉共用) ----------
CREATE OR REPLACE TABLE trk.event (
    单号          VARCHAR,
    注册承运商    INTEGER,
    event_time    TIMESTAMP,
    event_time_raw VARCHAR,
    主状态        VARCHAR,   -- NotFound/InfoReceived/InTransit/Expired/AvailableForPickup/
                             -- OutForDelivery/DeliveryFailure/Delivered/Exception
    子状态        VARCHAR,   -- 30 种, 如 Exception_Lost / Exception_Returning / Delivered_Other
    标准状态      VARCHAR,   -- 归一后的中文
    事件描述      VARCHAR,
    地点          VARCHAR,
    国家          VARCHAR,
    邮编          VARCHAR,
    是否签收      BOOLEAN,
    是否异常      BOOLEAN,
    来源          VARCHAR,   -- webhook / gettrackinfo
    入库时间      TIMESTAMP DEFAULT current_timestamp,
    事件指纹      VARCHAR    -- md5(单号|event_time|主状态|事件描述), 幂等去重
);

-- ---------- 主状态 -> 中文标准状态 ----------
CREATE OR REPLACE TABLE trk.dim_status_map (
    主状态      VARCHAR PRIMARY KEY,
    标准状态    VARCHAR,
    是否终结    BOOLEAN,   -- 终结状态不再计入在途
    是否异常    BOOLEAN
);
INSERT INTO trk.dim_status_map VALUES
 ('InfoReceived',    '已揽收信息',   FALSE, FALSE),
 ('InTransit',       '运输中',       FALSE, FALSE),
 ('Expired',         '长时间未更新', TRUE,  TRUE),
 ('AvailableForPickup','待自提',     FALSE, FALSE),
 ('OutForDelivery',  '派送中',       FALSE, FALSE),
 ('DeliveryFailure', '投递失败',     FALSE, TRUE),
 ('Delivered',       '已签收',       TRUE,  FALSE),
 ('Exception',       '异常',         TRUE,  TRUE),
 ('NotFound',        '查无此单',     FALSE, TRUE);

-- ---------- 单号级滚动状态 ----------
CREATE OR REPLACE VIEW trk.v_track_state AS
SELECT 单号, 注册承运商,
       arg_max(标准状态, event_time)            AS 最新状态,
       max(event_time)                          AS 最新事件时间,
       max(event_time) FILTER (WHERE 是否签收)   AS 承运商签收时间,
       count(*)                                 AS 事件数,
       bool_or(是否异常)                         AS 有过异常,
       count(*) FILTER (WHERE 子状态 = 'DeliveryFailure_NoBody')  AS 投递无人次数,
       max(子状态) FILTER (WHERE 是否异常)        AS 最近异常子状态
FROM trk.event
GROUP BY 1, 2;

-- ---------- 履约单级轨迹汇总 + WMS 对账 ----------
CREATE OR REPLACE VIEW trk.v_shipment_track AS
WITH base AS (
    SELECT f.履约单号, f.仓库代码, f.物流服务商, f.目的国,
           f.签收时间_ts AS WMS签收时间, f.签出时间_ts, f.创建日期, f.申报金额,
           s.注册层级, s.状态 AS 订阅状态,
           st.最新状态, st.最新事件时间, st.承运商签收时间,
           st.事件数, st.有过异常, st.最近异常子状态
    FROM dw.fct_shipment f
    LEFT JOIN trk.subscription s ON s.履约单号 = f.履约单号
    LEFT JOIN trk.v_track_state st
           ON st.单号 = s.单号 AND st.注册承运商 = s.注册承运商
)
SELECT 履约单号, 仓库代码, 物流服务商, 目的国, 申报金额,
       注册层级, 订阅状态,
       WMS签收时间, 承运商签收时间, 签出时间_ts, 创建日期,
       最新状态 AS 轨迹最新状态, 最新事件时间, 最近异常子状态,
       事件数 AS 轨迹事件数, 有过异常 AS 轨迹有过异常,
       -- 承运商侧尾程天数(真实时效)
       CASE WHEN 承运商签收时间 IS NOT NULL
            THEN round(date_diff('minute', 签出时间_ts, 承运商签收时间) / 1440.0, 3) END AS 承运商尾程_天,
       -- 签收对账
       CASE
         WHEN 承运商签收时间 IS NOT NULL AND WMS签收时间 IS NOT NULL
              AND abs(date_diff('hour', 承运商签收时间, WMS签收时间)) <= 24 THEN '一致'
         WHEN 承运商签收时间 IS NOT NULL AND WMS签收时间 IS NOT NULL                THEN '签收时间偏差'
         WHEN 承运商签收时间 IS NOT NULL AND WMS签收时间 IS NULL                    THEN '承运商已签_WMS未签'
         WHEN 承运商签收时间 IS NULL AND WMS签收时间 IS NOT NULL                    THEN 'WMS已签_承运商未见'
         ELSE '双方均未签收'
       END AS 签收对账结论,
       CASE WHEN 最新事件时间 IS NULL THEN NULL
            ELSE round(date_diff('hour', 最新事件时间, current_timestamp) / 24.0, 1) END AS 轨迹停滞天
FROM base;

-- ---------- 待注册队列 ----------
-- 一单只注册 1 条腿; 排除: 已注册 / 取消单 / 已签收 / 无号 / 库龄超 30 天(注册也查不到)
CREATE OR REPLACE VIEW trk.v_register_queue AS
WITH cand AS (
    SELECT f.履约单号, f.物流服务商, f.仓库代码, f.目的国, f.申报金额,
           f.签出时间_ts, f.数据截止日,
           date_diff('day', f.签出时间_ts::DATE, f.数据截止日) AS 库龄天,
           m.十七track编码 AS 承运商编码, m.主查腿,
           -- 优先末端可查号, 其次主单号; 只取一条
           coalesce(nullif(trim(f.转单号_raw), ''), nullif(trim(f.快递单号_raw), '')) AS 待注册号,
           CASE WHEN f.物流服务商 IN ('postnet','SF Express') THEN 'T1'
                WHEN f.状态 = '物流异常' OR f.时效状态 = '在途超期未达' THEN 'T1'
                WHEN f.物流服务商 IN ('Hete-Logistics','YFH-Logistics','XS_Express','DHL','UPS','pep') THEN 'T2'
                ELSE 'T3' END AS 注册层级
    FROM dw.fct_shipment f
    LEFT JOIN trk.dim_carrier_map m ON m.物流服务商 = f.物流服务商
    WHERE f.状态 <> '已取消'
      AND f.签收时间_ts IS NULL                       -- 已签收的不必再查
      AND f.签出时间_ts IS NOT NULL                    -- 未发出的还没号
)
SELECT c.*
FROM cand c
WHERE c.待注册号 IS NOT NULL
  AND length(dw_norm(c.待注册号)) BETWEEN 5 AND 50     -- 17track 单号格式要求
  AND c.库龄天 <= 30
  AND NOT EXISTS (
      SELECT 1 FROM trk.subscription s
      WHERE s.单号 = dw_norm(c.待注册号) AND s.承运商编码 = c.承运商编码
  )
ORDER BY CASE c.注册层级 WHEN 'T1' THEN 1 WHEN 'T2' THEN 2 ELSE 3 END, c.库龄天 DESC;

-- ---------- 补拉队列 (只处理 webhook 没覆盖到的情况) ----------
CREATE OR REPLACE VIEW trk.v_pull_queue AS
SELECT s.单号, s.注册承运商, s.履约单号, s.物流服务商, s.注册层级, s.状态,
       s.注册时间, s.首次有数据时间, s.末次事件时间
FROM trk.subscription s
WHERE s.状态 IN ('accepted', 'tracking')
  AND (
       -- 注册后 24h 仍无任何数据
       (s.首次有数据时间 IS NULL AND s.注册时间 < current_timestamp - INTERVAL 24 HOUR)
       -- 或超过 48h 没有新事件(自动跟踪 6~12h, 说明该催了)
    OR (s.末次事件时间 IS NOT NULL AND s.末次事件时间 < current_timestamp - INTERVAL 48 HOUR)
  )
ORDER BY s.注册时间;

-- ---------- 成本测算 (按去重后的注册单号数计费) ----------
CREATE OR REPLACE VIEW trk.v_cost_estimate AS
WITH m AS (
    SELECT CASE WHEN f.物流服务商 IN ('postnet','SF Express') THEN 'T1'
                WHEN f.状态 = '物流异常' OR f.时效状态 = '在途超期未达' THEN 'T1'
                WHEN f.物流服务商 IN ('Hete-Logistics','YFH-Logistics','XS_Express','DHL','UPS','pep') THEN 'T2'
                ELSE 'T3' END AS 注册层级,
           count(DISTINCT dw_norm(coalesce(nullif(trim(f.转单号_raw),''),
                                          nullif(trim(f.快递单号_raw),'')))) AS 近90天去重单号
    FROM dw.fct_shipment f
    WHERE f.创建日期 >= (SELECT max(创建日期) FROM dw.fct_shipment) - INTERVAL 89 DAY
      AND f.状态 <> '已取消'
    GROUP BY 1
)
SELECT 注册层级, 近90天去重单号,
       round(近90天去重单号 / 3.0, 0) AS 月均注册量,
       round(100.0 * 近90天去重单号 / sum(近90天去重单号) OVER (), 1) AS 占比Pct,
       round(sum(近90天去重单号 / 3.0) OVER (), 0) AS 全量月均
FROM m ORDER BY 1;
