"""
tests/test_adbtools.py - adb 设备枚举相关测试
=============================================
不依赖真实 adb/设备:
    - parse_devices_output:纯文本解析(标题行/空行/daemon 提示/多状态/附加描述列)
    - list_devices:通过替换 _run_raw 验证命令拼装与解析接线
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adbtools import AdbClient, AdbError


class TestParseDevicesOutput:
    def test_mixed_states_kept_in_order(self):
        output = (
            "List of devices attached\n"
            "emulator-5554\tdevice\n"
            "192.168.1.10:5555\toffline\n"
            "ABC123XYZ\tunauthorized\n"
        )
        assert AdbClient.parse_devices_output(output) == [
            ("emulator-5554", "device"),
            ("192.168.1.10:5555", "offline"),
            ("ABC123XYZ", "unauthorized"),
        ]

    def test_header_only_means_empty(self):
        assert AdbClient.parse_devices_output("List of devices attached\n") == []
        assert AdbClient.parse_devices_output("") == []
        assert AdbClient.parse_devices_output(None) == []  # type: ignore[arg-type]

    def test_daemon_notice_and_blank_lines_skipped(self):
        output = (
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            "\n"
            "List of devices attached\n"
            "   \n"
            "emulator-5554   device\n"
        )
        assert AdbClient.parse_devices_output(output) == [("emulator-5554", "device")]

    def test_extra_description_columns_ignored(self):
        # 部分 adb 版本在序列号与状态后附加 product/model/device 等信息
        output = (
            "List of devices attached\n"
            "ABC123XYZ       device product:p30 model:XXX device:HWXXX transport_id:1\n"
        )
        assert AdbClient.parse_devices_output(output) == [("ABC123XYZ", "device")]

    def test_malformed_single_token_line_skipped(self):
        output = "List of devices attached\nGARBAGE_LINE_WITHOUT_STATE\n"
        assert AdbClient.parse_devices_output(output) == []


class TestListDevices:
    def _client_with_fake_adb(self, stdout: str = "", returncode: int = 0):
        client = AdbClient()

        def fake_run_raw(args, timeout=10.0):
            assert args == ["devices"]
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        client._run_raw = fake_run_raw  # type: ignore[method-assign]
        return client

    def test_lists_parsed_devices(self):
        client = self._client_with_fake_adb(
            "List of devices attached\n"
            "emulator-5554\tdevice\nserial-b\tunauthorized\n"
        )
        assert client.list_devices() == [
            ("emulator-5554", "device"),
            ("serial-b", "unauthorized"),
        ]

    def test_empty_when_no_device_attached(self):
        client = self._client_with_fake_adb("List of devices attached\n")
        assert client.list_devices() == []

    def test_adb_failure_raises_adb_error(self):
        client = AdbClient()

        def fake_run_raw(args, timeout=10.0):
            raise AdbError("adb 可执行文件未找到: 'adb'")

        client._run_raw = fake_run_raw  # type: ignore[method-assign]
        with pytest.raises(AdbError):
            client.list_devices()
