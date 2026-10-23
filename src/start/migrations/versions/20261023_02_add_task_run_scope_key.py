"""给后台任务行增加执行作用域键，支持同作用域 FIFO 排队。"""

from __future__ import annotations

name = "20261023_02_add_task_run_scope_key"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE background_task_run ADD COLUMN IF NOT EXISTS scope_key VARCHAR(128) NULL"
    )
    # 旧版本在入队时就占 mutex；回填活动行的 scope，避免升级瞬间同库任务失去串行保护。
    database.execute_sql(
        """
        UPDATE background_task_run
        SET scope_key = 'library_import:' || (params::jsonb ->> 'library_id')
        WHERE task_key = 'library_import'
          AND scope_key IS NULL
          AND state IN ('pending', 'running')
          AND NULLIF(params::jsonb ->> 'library_id', '') IS NOT NULL
        """
    )
    database.execute_sql(
        """
        UPDATE background_task_run
        SET scope_key = 'library_import:' || (
            params::jsonb #>> '{failure_item,library_id}'
        )
        WHERE task_key = 'library_import'
          AND scope_key IS NULL
          AND state IN ('pending', 'running')
          AND NULLIF(params::jsonb #>> '{failure_item,library_id}', '') IS NOT NULL
        """
    )
    database.execute_sql(
        """
        UPDATE background_task_run
        SET scope_key = 'library_import:' || (params::jsonb ->> 'target_library_id')
        WHERE task_key = 'media_storage_transfer'
          AND scope_key IS NULL
          AND state IN ('pending', 'running')
          AND NULLIF(params::jsonb ->> 'target_library_id', '') IS NOT NULL
        """
    )
    database.execute_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS backgroundtaskrun_running_scope_key "
        "ON background_task_run (scope_key) "
        "WHERE state = 'running' AND scope_key IS NOT NULL"
    )
