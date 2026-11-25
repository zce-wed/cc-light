import ctypes
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_light as light


def test_memory_alert_is_strictly_above_90_percent():
    assert light.memory_display(0) == ("0%", "#ededed")
    assert light.memory_display(90) == ("90%", "#ededed")
    assert light.memory_display(91) == ("91%", light.COLORS["red"]["on"])
    assert light.memory_display(100) == ("100%", light.COLORS["red"]["on"])
    assert light.memory_display(None) == ("--%", "#777777")


def test_native_memory_read_and_api_failure():
    def query(pointer):
        status = pointer._obj
        assert status.dwLength == 64
        status.dwMemoryLoad = 62
        # Large physical RAM values must retain all 64 bits.
        status.ullTotalPhys = 32 * 1024 ** 3
        return 1
    with patch.object(ctypes.windll.kernel32, "GlobalMemoryStatusEx", side_effect=query):
        assert light.read_memory_percent() == 62
    with patch.object(ctypes.windll.kernel32, "GlobalMemoryStatusEx", return_value=0):
        assert light.read_memory_percent() is None
    with patch.object(ctypes.windll.kernel32, "GlobalMemoryStatusEx", side_effect=OSError("unavailable")):
        assert light.read_memory_percent() is None
