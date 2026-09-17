-- =====================================================================
-- AI-alarm_autoFix 数据表
-- 与 Java 版（ccd-java-system com.xizhao.service.feishu.alarm）完全同构，
-- 可直接复用 Java 版已存在的表；仅当独立部署、新建库时执行本文件。
-- =====================================================================

CREATE TABLE IF NOT EXISTS alarm_analyze_result
(
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键自增',
    trace_id        VARCHAR(128) NULL COMMENT '告警链路id',
    analyze_content MEDIUMTEXT   NULL COMMENT 'AI分析结果（AlarmAnalyseResult 结构的 JSON，键为 camelCase，与 Java 版一致）',
    service_name    VARCHAR(128) NULL COMMENT '告警服务',
    interface       VARCHAR(512) NULL COMMENT '请求接口',
    create_time     DATETIME     NULL COMMENT '创建时间',
    update_time     DATETIME     NULL COMMENT '更新时间',
    fix_status      VARCHAR(32)  NULL COMMENT '修复状态：FIXED-已修复；NOT_NEEDED-无需处理（LLM 判定或人工忽略）；FAILED-修复失败；HANDOVER-已转人工排查；MANUAL_PENDING-待人工处理；NULL-未发起修复',
    fix_summary     VARCHAR(2048) NULL COMMENT '修复说明（commit message 原文、人工操作步骤或忽略标记，应用层截断到 1000 字符）',
    fix_time        DATETIME     NULL COMMENT '修复结果时间',
    problem_type    VARCHAR(32)  NULL COMMENT '问题归因：CODE-代码问题/DEPENDENCY-上下游依赖/MANUAL_REQUIRED-需人工介入/NOT_A_PROBLEM-无需处理；NULL-未归因（历史数据）',
    fingerprint     VARCHAR(64)  NULL COMMENT '告警指纹 md5(service|requestUri|exceptionClass|首个业务帧)，同指纹视为同一处错误',
    repeat_type     VARCHAR(16)  NULL COMMENT '重复类型：NORMAL-首次/REPEATED-重复告警(压卡片)/RECURRED-修复后复发；NULL-历史数据',
    root_id         BIGINT       NULL COMMENT '首次出现记录 id（REPEATED/RECURRED 时指向根告警，NORMAL 为 NULL）',
    PRIMARY KEY (id),
    KEY idx_trace_id (trace_id),
    KEY idx_create_time (create_time),
    KEY idx_fingerprint_time (fingerprint, create_time)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4 COMMENT ='告警分析结果（飞书卡片回调修复/日报统计的数据来源）';

-- ---------------------------------------------------------------------
-- 若复用 Java 版已存在的旧表，按批次补齐新增列：
-- ---------------------------------------------------------------------
-- 2026-09-07 修复三态三列：
-- ALTER TABLE alarm_analyze_result
--     ADD COLUMN fix_status  VARCHAR(32)  NULL COMMENT 'FIXED/NOT_NEEDED/FAILED/NULL',
--     ADD COLUMN fix_summary VARCHAR(2048) NULL COMMENT '修复说明',
--     ADD COLUMN fix_time    DATETIME     NULL COMMENT '修复结果时间';
--
-- 2026-09-17 归因与重复/复发识别（对应 Java 版 ea7f3e678，须先执行再发布新代码，
--            否则指纹查询/落库会因缺列报错）：
-- ALTER TABLE alarm_analyze_result
--     ADD COLUMN problem_type VARCHAR(32) DEFAULT NULL COMMENT '问题归因：CODE/DEPENDENCY/MANUAL_REQUIRED/NOT_A_PROBLEM；NULL-未归因',
--     ADD COLUMN fingerprint  VARCHAR(64) DEFAULT NULL COMMENT '告警指纹 md5(service|requestUri|exceptionClass|首个业务帧)',
--     ADD COLUMN repeat_type  VARCHAR(16) DEFAULT NULL COMMENT '重复类型：NORMAL/REPEATED/RECURRED',
--     ADD COLUMN root_id      BIGINT      DEFAULT NULL COMMENT '首次出现记录 id（REPEATED/RECURRED 非空）',
--     ADD INDEX idx_fingerprint_time (fingerprint, create_time);
