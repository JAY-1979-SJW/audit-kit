"""audit_kit._proc.no_window_kwargs() 시험 — Windows 콘솔 깜빡임 방지 헬퍼."""

from __future__ import annotations

from unittest import mock

from audit_kit._proc import no_window_kwargs


def test_returns_create_no_window_flag_on_windows():
    with mock.patch("audit_kit._proc.sys.platform", "win32"):
        import subprocess

        assert no_window_kwargs() == {"creationflags": subprocess.CREATE_NO_WINDOW}


def test_returns_empty_dict_on_non_windows():
    with mock.patch("audit_kit._proc.sys.platform", "linux"):
        assert no_window_kwargs() == {}


def test_returns_empty_dict_on_darwin():
    with mock.patch("audit_kit._proc.sys.platform", "darwin"):
        assert no_window_kwargs() == {}
