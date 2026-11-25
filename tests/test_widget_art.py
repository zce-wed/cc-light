import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from widget_art import HEIGHT, WIDTH, render_widget


def test_widget_has_original_footprint_and_translucent_corners():
    frame = render_widget("green", False, True, 77, ("green",))
    assert (WIDTH, HEIGHT) == (52, 150)
    assert len(frame) == WIDTH * HEIGHT * 4
    alpha = frame[3::4]
    assert alpha[0] == 0 and alpha[WIDTH * (HEIGHT // 2) + WIDTH // 2] == 255
    for x0, y0 in ((0, 0), (WIDTH - 14, 0), (0, HEIGHT - 14), (WIDTH - 14, HEIGHT - 14)):
        corner = {alpha[y * WIDTH + x] for y in range(y0, y0 + 14) for x in range(x0, x0 + 14)}
        assert len(corner - {0, 255}) >= 4
    # Windows blends premultiplied RGB. Straight RGB produces fringes on light backgrounds.
    for start in range(0, len(frame), 4):
        b, g, r, a = frame[start:start + 4]
        assert max(b, g, r) <= a


def test_waiting_lamp_replaces_memory_dial_and_running_lamp_pulses():
    waiting = render_widget("red", False, False, None, ("red",))
    assert render_widget("red", False, True, 100, ("red",)) == waiting
    normal = render_widget("yellow", True, True, 90, ("yellow",))
    assert render_widget("yellow", True, True, 91, ("yellow",)) != normal
    assert render_widget("yellow", False, True, 90, ("yellow",)) != normal
