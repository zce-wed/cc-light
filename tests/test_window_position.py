import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cc_light as light


def test_disconnected_left_monitor_moves_light_into_current_work_area():
    assert light.visible_position(-599, 693, 52, 150, [(0, 0, 1920, 1040)]) == (0, 693)


def test_connected_left_monitor_keeps_valid_negative_coordinates():
    assert light.visible_position(-599, 693, 52, 150,
        [(-1920, 0, 0, 1040), (0, 0, 1920, 1040)]) == (-599, 693)


def test_resolution_change_keeps_entire_light_visible_above_taskbar():
    assert light.visible_position(1900, 1050, 52, 150, [(0, 0, 1280, 720)]) == (1228, 570)


def test_monitor_above_primary_and_missing_monitor_data():
    assert light.visible_position(200, -500, 52, 150,
        [(0, -1080, 1920, 0), (0, 0, 1920, 1040)]) == (200, -500)
    assert light.visible_position(100, 200, 52, 150, []) == (100, 200)
