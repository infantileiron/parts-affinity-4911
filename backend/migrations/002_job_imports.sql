-- 增量：作业版本化 + 批量导入批次与逐行回执
-- 适用于已用 001_initial.sql 建库、需保留既有作业数据的实例。
ALTER TABLE `jobs`
  ADD COLUMN `version` int NOT NULL DEFAULT '1' AFTER `note`,
  ADD COLUMN `source` varchar(20) DEFAULT NULL AFTER `version`,
  ADD COLUMN `batch_no` varchar(64) DEFAULT NULL AFTER `source`,
  ADD COLUMN `updated_at` varchar(40) DEFAULT NULL AFTER `batch_no`;

UPDATE `jobs` SET `version`=1,`source`='seed' WHERE `version` IS NULL OR `version`<1;

CREATE TABLE IF NOT EXISTS `job_import_batches` (
  `id` int NOT NULL AUTO_INCREMENT,
  `batch_no` varchar(64) NOT NULL,
  `content_hash` varchar(64) NOT NULL,
  `state` varchar(20) NOT NULL,
  `actor` varchar(100) DEFAULT NULL,
  `total` int DEFAULT NULL,
  `counts` json DEFAULT NULL,
  `receipt` json DEFAULT NULL,
  `error` text,
  `created` varchar(40) DEFAULT NULL,
  `updated` varchar(40) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `batch_no` (`batch_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `job_import_rows` (
  `id` int NOT NULL AUTO_INCREMENT,
  `batch_id` int NOT NULL,
  `line` int NOT NULL,
  `code` varchar(60) DEFAULT NULL,
  `version` int DEFAULT NULL,
  `payload` json DEFAULT NULL,
  `content_hash` varchar(64) DEFAULT NULL,
  `outcome` varchar(20) NOT NULL,
  `reason_code` varchar(40) DEFAULT NULL,
  `reason` text,
  `job_id` int DEFAULT NULL,
  `accepted_version` int DEFAULT NULL,
  `current_version` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `batch_id` (`batch_id`,`line`),
  KEY `ix_job_import_rows_batch_outcome` (`batch_id`,`outcome`),
  CONSTRAINT `job_import_rows_ibfk_1` FOREIGN KEY (`batch_id`) REFERENCES `job_import_batches` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
