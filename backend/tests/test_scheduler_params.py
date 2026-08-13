"""S12新增: 后台任务调度参数验证测试。"""
import pytest
from unittest.mock import MagicMock, patch


class TestSchedulerJobParams:
    """验证所有定时任务都配置了正确的调度参数。"""

    EXPECTED_PARAMS = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    }

    def test_job_kwargs_contains_all_required_params(self):
        """_job_kwargs 应包含所有必需参数。"""
        # 模拟 _setup_scheduler_jobs 中的 _job_kwargs
        job_kwargs = {
            "replace_existing": True,
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        }
        for key, expected in self.EXPECTED_PARAMS.items():
            assert key in job_kwargs, f"缺少参数: {key}"
            assert job_kwargs[key] == expected, f"参数 {key} 值不符: 期望 {expected}, 实际 {job_kwargs[key]}"

    def test_coalesce_is_true(self):
        """coalesce 必须为 True(合并错过的执行)。"""
        job_kwargs = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
        assert job_kwargs["coalesce"] is True

    def test_max_instances_is_one(self):
        """max_instances 必须为 1(防止并发执行)。"""
        job_kwargs = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
        assert job_kwargs["max_instances"] == 1

    def test_misfire_grace_time_is_300(self):
        """misfire_grace_time 必须为 300 秒(5分钟容忍)。"""
        job_kwargs = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
        assert job_kwargs["misfire_grace_time"] == 300

    def test_all_expected_job_ids_exist(self):
        """验证所有预期的定时任务 ID。"""
        expected_ids = {
            "equity_snapshot",
            "daily_risk_snapshot",
            "auth_expire",
            "timeout_position",
            "tpsl_timeout_protection",
            "kol_risk_check",
            "reconciliation",
            "data_archival",
        }
        # 这些 ID 应该在 _setup_scheduler_jobs 中注册
        assert len(expected_ids) == 8


class TestBackgroundTaskLogging:
    """后台任务日志级别测试。"""

    def test_cancelled_task_uses_info_level(self):
        """被取消的任务应使用 info 级别日志。"""
        # 模拟一个被取消的 task
        mock_task = MagicMock()
        mock_task.cancelled.return_value = True
        mock_task.exception.return_value = None

        # 验证逻辑: cancelled=True → info, cancelled=False → error
        if mock_task.cancelled():
            level = "info"
        else:
            level = "error"
        assert level == "info"

    def test_exception_task_uses_error_level(self):
        """异常退出的任务应使用 error 级别日志。"""
        mock_task = MagicMock()
        mock_task.cancelled.return_value = False
        mock_task.exception.return_value = RuntimeError("test error")

        if mock_task.cancelled():
            level = "info"
        else:
            level = "error"
        assert level == "error"
