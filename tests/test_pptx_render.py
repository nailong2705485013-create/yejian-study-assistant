"""Office process ownership and dispatch compatibility without launching Office."""
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pptx_render import _new_powerpoint_process, _stop_owned, _worker


class RenderProcessTests(unittest.TestCase):
    def setUp(self):
        self.process = types.ModuleType("win32process")
        self.process.GetProcessTimes = Mock(return_value={
            "CreationTime": datetime.fromtimestamp(1001, timezone.utc)})
        self.process.GetModuleFileNameEx = Mock(return_value=r"C:\Office\POWERPNT.EXE")
        self.api = types.ModuleType("win32api")
        self.handle = Mock()
        self.api.OpenProcess = Mock(return_value=self.handle)
        self.api.TerminateProcess = Mock()
        self.pythoncom = types.ModuleType("pythoncom")
        self.pythoncom.CoInitialize = Mock()
        self.pythoncom.CoUninitialize = Mock()
        self.presentation = types.SimpleNamespace(
            PageSetup=types.SimpleNamespace(SlideHeight=9, SlideWidth=16),
            Slides=types.SimpleNamespace(Item=Mock(return_value=types.SimpleNamespace(Export=Mock()))),
            Close=Mock())
        # Deliberately has no HWND member, just like the failing COM interface.
        self.app = types.SimpleNamespace(Quit=Mock(),
            Presentations=types.SimpleNamespace(Count=0, Open=Mock(return_value=self.presentation)))
        self.client = types.ModuleType("win32com.client")
        self.client.DispatchEx = Mock(return_value=self.app)
        self.com = types.ModuleType("win32com")
        self.com.client = self.client
        self.modules = {"win32process": self.process,
            "win32api": self.api, "pythoncom": self.pythoncom,
            "win32com": self.com, "win32com.client": self.client}
        self.patcher = patch.dict(sys.modules, self.modules)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_worker(self, *, existing=(), present_now=(), candidates=(101,)):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name)
        request = output / "render-request.json"
        request.write_text(json.dumps({"source": str(output / "synthetic.pptx"),
            "output": str(output), "pages": [1], "existing": list(existing), "started_at": 1000}), encoding="utf-8")
        with patch("pptx_render._powerpoint_pids", side_effect=[set(present_now), set(candidates)]), \
             patch("pptx_render.time.time", return_value=1000):
            _worker(request)
        return output, json.loads((output / "render-status.json").read_text(encoding="utf-8"))

    def test_unique_new_process_is_verified_without_a_window_handle(self):
        with patch("pptx_render._powerpoint_pids", return_value={101}):
            self.assertEqual(_new_powerpoint_process(1000), (101, 1001))
        self.handle.Close.assert_called_once()

    def test_missing_or_multiple_candidates_are_rejected(self):
        for candidates in (set(), {101, 102}):
            with self.subTest(candidates=candidates), \
                 patch("pptx_render._powerpoint_pids", return_value=candidates), \
                 self.assertRaisesRegex(ValueError, "唯一"):
                _new_powerpoint_process(1000)
        self.api.OpenProcess.assert_not_called()

    def test_worker_without_hwnd_reaches_export_and_cleans_only_owned_instance(self):
        output, status = self.run_worker()
        self.assertTrue(status["ok"])
        self.assertEqual(json.loads((output / "owned.json").read_text()), {"pid": 101, "created_at": 1001})
        self.presentation.Slides.Item.assert_called_once_with(1)
        self.presentation.Close.assert_called_once()
        self.app.Quit.assert_called_once()
        self.pythoncom.CoUninitialize.assert_called_once()

    def test_existing_process_stops_before_com_launch(self):
        for existing, present_now in (([101], []), ([], [101])):
            with self.subTest(existing=existing, present_now=present_now):
                output, status = self.run_worker(existing=existing, present_now=present_now)
                self.assertFalse(status["ok"])
                self.assertIn("PowerPoint正在运行", status["error"])
                self.assertFalse((output / "owned.json").exists())
                self.client.DispatchEx.assert_not_called()
                self.app.Presentations.Open.assert_not_called()
                self.app.Quit.assert_not_called()

    def test_older_or_wrong_process_is_not_owned_or_closed(self):
        for name, created in ((r"C:\Office\POWERPNT.EXE", 900), (r"C:\Other\unrelated.exe", 1001)):
            with self.subTest(name=name, created=created):
                self.process.GetModuleFileNameEx.return_value = name
                self.process.GetProcessTimes.return_value = {
                    "CreationTime": datetime.fromtimestamp(created, timezone.utc)}
                output, status = self.run_worker()
                self.assertFalse(status["ok"])
                self.assertIn("身份或创建时间不符", status["error"])
                self.assertFalse((output / "owned.json").exists())
                self.app.Presentations.Open.assert_not_called()
                self.app.Quit.assert_not_called()

    def test_instance_with_open_documents_is_not_owned_or_closed(self):
        self.app.Presentations.Count = 1
        output, status = self.run_worker()
        self.assertFalse(status["ok"])
        self.assertIn("已有打开的文件", status["error"])
        self.assertFalse((output / "owned.json").exists())
        self.app.Presentations.Open.assert_not_called()
        self.app.Quit.assert_not_called()

    def test_type_error_keeps_exact_stage_and_native_message_local(self):
        self.process.GetProcessTimes.side_effect = TypeError("Synthetic native handle mismatch")
        _, status = self.run_worker()
        self.assertFalse(status["ok"])
        self.assertEqual(status["diagnostic"]["stage"], "核对新PowerPoint进程及创建时间")
        self.assertEqual(status["diagnostic"]["message"], "Synthetic native handle mismatch")
        self.assertIn("接口类型不兼容", status["error"])
        self.handle.Close.assert_called_once()
        self.app.Quit.assert_not_called()
        self.pythoncom.CoUninitialize.assert_called_once()

    def test_timeout_cleanup_requires_matching_name_and_creation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "owned.json"
            marker.write_text('{"pid": 101, "created_at": 1001}', encoding="utf-8")
            _stop_owned(marker, {101}, 1000)
            self.api.OpenProcess.assert_not_called()
            self.process.GetModuleFileNameEx.return_value = r"C:\Other\unrelated.exe"
            _stop_owned(marker, set(), 1000)
            self.api.TerminateProcess.assert_not_called()
            self.process.GetModuleFileNameEx.return_value = r"C:\Office\POWERPNT.EXE"
            self.process.GetProcessTimes.return_value = {
                "CreationTime": datetime.fromtimestamp(900, timezone.utc)}
            _stop_owned(marker, set(), 1000)
            self.api.TerminateProcess.assert_not_called()
            self.process.GetProcessTimes.return_value = {
                "CreationTime": datetime.fromtimestamp(1001, timezone.utc)}
            _stop_owned(marker, set(), 1000)
            self.api.TerminateProcess.assert_called_once_with(self.handle, 1)
            self.api.TerminateProcess.reset_mock()
            self.process.GetProcessTimes.return_value = {
                "CreationTime": datetime.fromtimestamp(1002, timezone.utc)}
            _stop_owned(marker, set(), 1000)
            self.api.TerminateProcess.assert_not_called()
            marker.write_text('{"pid": 101}', encoding="utf-8")
            _stop_owned(marker, set(), 1000)
            self.api.TerminateProcess.assert_not_called()
