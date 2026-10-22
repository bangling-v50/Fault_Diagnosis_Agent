-- 首次初始化数据目录时自动启用 pgvector 扩展。
-- 放在 /docker-entrypoint-initdb.d/ 下的 .sql 只在**空数据目录**上执行一次；
-- 已经初始化过的库需要手动执行本文件的内容。
CREATE EXTENSION IF NOT EXISTS vector;
