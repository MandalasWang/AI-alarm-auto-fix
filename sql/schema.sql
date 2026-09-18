-- =====================================================================
-- AI-alarm_autoFix 数据表
-- 与 Java 版（ccd-java-system com.xizhao.service.feishu.alarm）共用一张表；
-- 独立部署/新建库时执行下方 CREATE，旧库对齐新结构见文件尾部迁移注释。
-- =====================================================================

CREATE TABLE IF NOT EXISTS alarm_analyze_result
(
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键自增',
    trace_id        VARCHAR(255) NOT NULL COMMENT '告警链路id',
    analyze_content JSON         NULL COMMENT 'AI分析结果',
    service_name    VARCHAR(50)  NULL COMMENT '告警服务',
    system_type     VARCHAR(30)  NULL COMMENT '系统分类 ccd:归口系统',
    `interface`     VARCHAR(100) NULL COMMENT '接口',
    create_time     DATETIME     NOT NULL COMMENT '创建时间',
    update_time     TIMESTAMP    NULL COMMENT '更新时间',
    fix_status      VARCHAR(20)  NULL COMMENT '修复状态：FIXED-已修复；NOT_NEEDED-无需修复(非代码问题)；FAILED-修复失败；HANDOVER-已转人工；MANUAL_PENDING-待人工；MANUAL_RESOLVED-人工已解决；NULL-未发起',
    fix_summary     VARCHAR(1024) NULL COMMENT '修复说明（commit message 原文或无需修复原因）',
    fix_time        DATETIME     NULL COMMENT '修复结果时间',
    problem_type    VARCHAR(32)  NULL COMMENT '问题归因：CODE/DEPENDENCY/MANUAL_REQUIRED/NOT_A_PROBLEM；NULL-未归因',
    fingerprint     VARCHAR(64)  NULL COMMENT '告警指纹 md5(service|requestUri|exceptionClass|首个业务帧)',
    repeat_type     VARCHAR(16)  NULL COMMENT '重复类型：NORMAL/REPEATED/RECURRED',
    root_id         BIGINT       NULL COMMENT '首次出现记录 id（REPEATED/RECURRED 非空）',
    PRIMARY KEY (id),
    KEY idx_fingerprint_time (fingerprint, create_time),
    KEY idx_trace_id_create_time (trace_id, create_time DESC)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4 COMMENT ='告警';

-- ---------------------------------------------------------------------
-- 操作审计表（独立于告警表，Java 版不受影响；写入为尽力而为，失败不影响主流程）
-- 记录 修复/忽略/结案 的受理与终态：谁在何时对哪条告警做了什么（操作者身份依赖
-- 接入层鉴权传递，跳转链接场景记录来源 IP）
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alarm_operation_audit
(
    id           BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键自增',
    create_time  DATETIME     NOT NULL COMMENT '操作时间',
    action       VARCHAR(20)  NOT NULL COMMENT '动作：fix/ignore/resolve/fix_result',
    trace_id     VARCHAR(255) NOT NULL COMMENT '告警链路id',
    service_name VARCHAR(50)  NULL COMMENT '告警服务',
    from_status  VARCHAR(20)  NULL COMMENT '操作前 fix_status',
    to_status    VARCHAR(20)  NULL COMMENT '操作后 fix_status（受理类动作为空）',
    outcome      VARCHAR(20)  NOT NULL COMMENT 'accepted=受理 / rejected=拒绝 / 具体终态',
    detail       VARCHAR(500) NULL COMMENT '拒绝原因/修复说明摘要',
    source_ip    VARCHAR(50)  NULL COMMENT '来源 IP',
    PRIMARY KEY (id),
    KEY idx_trace_id_time (trace_id, create_time),
    KEY idx_create_time (create_time)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4 COMMENT ='告警操作审计';

-- ---------------------------------------------------------------------
-- 旧表对齐新结构（2026-09-18 批次）。注意：
-- 1) trace_id 转 NOT NULL 前先回填：UPDATE alarm_analyze_result SET trace_id = '' WHERE trace_id IS NULL;
-- 2) analyze_content 转 JSON 需 MySQL ≥ 5.7，且现有内容均为合法 JSON（应用层一直写合法 JSON）；
-- 3) 索引 DESC 顺序需 MySQL ≥ 8 才真正生效，5.7 会忽略降序（查询语义不受影响）。
-- ---------------------------------------------------------------------
-- ALTER TABLE alarm_analyze_result
--     MODIFY COLUMN trace_id VARCHAR(255) NOT NULL COMMENT '告警链路id',
--     MODIFY COLUMN analyze_content JSON NULL COMMENT 'AI分析结果',
--     MODIFY COLUMN service_name VARCHAR(50) NULL COMMENT '告警服务',
--     ADD COLUMN system_type VARCHAR(30) NULL COMMENT '系统分类 ccd:归口系统' AFTER service_name,
--     MODIFY COLUMN `interface` VARCHAR(100) NULL COMMENT '接口',
--     MODIFY COLUMN update_time TIMESTAMP NULL COMMENT '更新时间',
--     MODIFY COLUMN fix_status VARCHAR(20) NULL COMMENT '修复状态：FIXED-已修复；NOT_NEEDED-无需修复(非代码问题)；FAILED-修复失败；NULL-未发起',
--     MODIFY COLUMN fix_summary VARCHAR(1024) NULL COMMENT '修复说明（commit message 原文或无需修复原因）',
--     DROP INDEX idx_trace_id,
--     DROP INDEX idx_create_time,
--     ADD INDEX idx_trace_id_create_time (trace_id, create_time DESC);
