"""Pixel artwork for the compact light. The output is a top-down premultiplied BGRA DIB."""

import math
from functools import lru_cache


WIDTH, HEIGHT = 52, 150
_CX = WIDTH / 2
_LAMPS = (("red", 31), ("yellow", 72), ("green", 108))
# Aperture in shadow, colour of the lit glass, brightest transmitted light.
_GLASS = {
    "red": ((48, 28, 34), (237, 57, 65), (255, 152, 125)),
    "yellow": ((52, 42, 27), (249, 163, 37), (255, 222, 135)),
    "green": ((28, 45, 39), (41, 196, 99), (149, 247, 174)),
}
_ALERT = "#ff4d4d"


def memory_display(percent):
    if percent is None:
        return "--%", "#777777"
    return "%d%%" % percent, _ALERT if percent > 90 else "#ededed"


def _unit(value):
    return min(1.0, max(0.0, value))


def _mix(a, b, amount):
    return tuple(x + (y - x) * amount for x, y in zip(a, b))


def _over(frame, offset, rgb, opacity):
    """Tint an existing surface, retaining its coverage at the silhouette."""
    if opacity <= 0:
        return
    alpha = frame[offset + 3] / 255
    for i, channel in enumerate(reversed(rgb)):
        value = frame[offset + i]
        frame[offset + i] = round(value + (channel * alpha - value) * opacity)


@lru_cache(maxsize=1)
def _housing():
    frame = bytearray(WIDTH * HEIGHT * 4)
    radius = 11.5
    for y in range(HEIGHT):
        for x in range(WIDTH):
            # Signed distance to the outside of the rounded metal housing.
            dx = abs(x + 0.5 - _CX) - (WIDTH / 2 - 2 - radius)
            dy = abs(y + 0.5 - HEIGHT / 2) - (HEIGHT / 2 - 2 - radius)
            distance = math.hypot(max(0, dx), max(0, dy)) + min(max(dx, dy), 0) - radius
            coverage = _unit(0.5 - distance)
            if not coverage:
                continue
            light = (1 - y / HEIGHT) * 0.82 + (1 - x / WIDTH) * 0.18
            face = _mix((24, 28, 36), (35, 40, 50), light)
            bevel = _mix((48, 54, 62), (84, 92, 104), light)
            rgb = _mix(face, bevel, _unit(distance + 1.32))
            offset = (y * WIDTH + x) * 4
            frame[offset:offset + 4] = bytes(round(c * coverage) for c in (*reversed(rgb), 255))
    return bytes(frame)


def _lamp(frame, key, cy, power):
    dark, hue, pale = _GLASS[key]
    radius = 11.9
    for y in range(cy - 20, cy + 20):
        for x in range(int(_CX) - 20, int(_CX) + 20):
            dx, dy = x + 0.5 - _CX, y + 0.5 - cy
            r = math.hypot(dx, dy)
            offset = (y * WIDTH + x) * 4
            if power:
                halo = math.exp(-((r - 14.2) / 2.7) ** 2)
                _over(frame, offset, hue, 0.16 * power * halo)
            # A recessed, shaded socket separates the thin rim from the glass.
            _over(frame, offset, (13, 17, 22), 0.92 * _unit(14.7 - r))
            rim = _unit(1.07 - abs(r - 14.4))
            rim_light = _unit(0.63 - (dx + dy) / 80)
            _over(frame, offset, _mix((68, 73, 79), hue, power), rim * (0.38 + 0.62 * power) * rim_light)
            coverage = _unit(radius + 0.5 - r)
            if not coverage:
                continue
            nx, ny = dx / radius, dy / radius
            nz = math.sqrt(max(0.0, 1 - nx * nx - ny * ny))
            diffuse = _unit(0.81 * nz - 0.39 * nx - 0.46 * ny)
            under_glass = _mix((15, 19, 23), dark, 0.28 + 0.72 * diffuse)
            luminous = _mix(dark, hue, 0.29 + 0.71 * nz)
            luminous = _mix(luminous, pale, 0.32 * diffuse ** 2)
            rgb = _mix(under_glass, luminous, power)
            # Reflection of a softbox above-left, plus the narrow glint on its lower edge.
            highlight = math.exp(-((nx + 0.31) / 0.43) ** 2 - ((ny + 0.52) / 0.22) ** 2)
            glint = math.exp(-((nx + 0.39) / 0.19) ** 2 - ((ny + 0.56) / 0.12) ** 2)
            rgb = _mix(rgb, (247, 250, 248), (0.08 + 0.28 * power) * highlight)
            rgb = _mix(rgb, (255, 255, 249), (0.06 + 0.67 * power) * glint)
            _over(frame, offset, rgb, coverage)


# Coverage atlas for 0123456789-% (Segoe UI Bold 15px, regular 9px unit).
# Rasterized at 8x and downsampled; four bits of grayscale per pixel.
_GLYPH_ROWS = (
    "002798200 000069200 015897200 027996100 000017710 028777720 000279930 277777871 003897200 003897100 000000000 0000000",
    "03efffe30 015cff400 0affffe30 0affffc10 00008ff20 05fffff50 006ffff90 4fffffff3 04ffffe40 05ffffd10 000000000 0000000",
    "0bfa3bfb0 05ffff300 0a845df90 06537ff50 0003fff20 06fa66620 03ff83440 16665afb0 0cf829fb0 1ef83bf80 000000000 0000000",
    "2ff305fe2 04a5ef300 010009fb0 00000ef50 000cfef20 07f500000 0af800000 00000cf40 0df304fc0 4fe104fd0 000000000 0000000",
    "5fe203ff3 0001ff300 00001cf80 0044afc10 008f7df20 08fb97200 1df59b810 00007fb00 06fb5ce50 4fe205fe2 000000000 0000000",
    "6fe102ff4 0002ff300 0001bfe20 01fffc300 04fa0ef20 0affffe20 2ffffffa0 0001ef400 00bfff900 1efc8efe2 000000000 1520020",
    "6fe102ff4 0002ff300 004dfd300 0078cfe30 3ed11ef20 03336ef90 3ff916ff3 0006fc000 0afb7cf80 04dffafe1 008ddd600 9490650",
    "4ff204fe2 0002ff300 03ff91000 00000bf90 afeddffd5 000009fb0 2ef301ef4 000cf7000 3fe102ff2 001414fc0 007cbc500 a084800",
    "2ef608fc0 0002ff300 0cf800000 12000cf90 8ddddffd5 01001bf90 0cf703ff2 002ff3000 4fe102ef3 01001cf70 000000000 6858330",
    "09febff50 0002ff400 4ffdddd90 2ebadff40 00001ef20 0ccadff30 05ffaef90 007fe1000 1dfc8dfb0 0abaefd10 000000000 0056777",
    "019efe700 0002ee300 4eeeeeea0 1bfffc500 00001de20 0afffc400 007dfe910 009ea0000 02bffea10 09ffea200 000000000 028370a",
    "000121000 000011000 011111110 001220000 000001100 001220000 000121000 001110000 000121000 001210000 000000000 0721976",
    "000000000 000000000 000000000 000000000 000000000 000000000 000000000 000000000 000000000 000000000 000000000 0100130",
)
_GLYPHS = dict(zip("0123456789-%", zip(*(row.split() for row in _GLYPH_ROWS))))


def _readout(frame, percent, cy):
    text, foreground = memory_display(percent)
    color = tuple(bytes.fromhex(foreground[1:]))
    left = int(_CX - ((len(text) - 1) * 9 + 7) / 2)
    for char in text:
        ink = (161, 166, 174) if char == "%" and foreground == "#ededed" else color
        glyph = _GLYPHS[char]
        for y, row in enumerate(glyph, cy - 5):
            for x, opacity in enumerate(row, left):
                if opacity != "0":
                    _over(frame, (y * WIDTH + x) * 4, ink, int(opacity, 16) / 15)
        left += len(glyph[0])


@lru_cache(maxsize=32)
def _dial(percent):
    """Cache just the band containing the dial, independent of session activity."""
    frame = bytearray(_housing())
    cy, radius = _LAMPS[0][1], 18.2
    angle = math.pi / 4 + math.tau * (percent or 0) / 100
    mx, my = radius * math.cos(angle), radius * math.sin(angle)
    alert = percent is not None and percent > 90
    accent = (255, 77, 77) if alert else (255, 161, 126)
    for y in range(cy - 23, cy + 23):
        for x in range(WIDTH):
            dx, dy = x + 0.5 - _CX, y + 0.5 - cy
            r = math.hypot(dx, dy)
            offset = (y * WIDTH + x) * 4
            _over(frame, offset, (21, 25, 32), _unit(radius - r))
            track = _unit(1.0 - abs(r - radius))
            _over(frame, offset, (79, 84, 94), track * 0.86)
            if percent is not None:
                tail = (angle - math.atan2(dy, dx)) % math.tau
                _over(frame, offset, accent, track * 0.53 * _unit(1 - tail / 1.06))
                distance = math.hypot(dx - mx, dy - my)
                _over(frame, offset, accent, 0.4 * math.exp(-(distance / 3.25) ** 2))
                _over(frame, offset, accent, _unit(2.65 - distance))
                _over(frame, offset, (255, 179, 168) if alert else (255, 231, 207),
                      _unit(1.65 - distance))
    _readout(frame, percent, cy)
    return bytes(frame[(cy - 23) * WIDTH * 4:(cy + 23) * WIDTH * 4])


@lru_cache(maxsize=12)
def _body(state, pulse_bright, dial_visible):
    frame = bytearray(_housing())
    for key, cy in _LAMPS:
        if key == "red" and dial_visible:
            continue
        power = 1.0 if key == state else 0.0
        if key == state == "yellow" and not pulse_bright:
            power = 0.70
        _lamp(frame, key, cy, power)
    return bytes(frame)


def render_widget(state, pulse_bright, show_memory, percent, dot_states):
    """Render session lights and system RAM. dot_states is in display order."""
    dial_visible = show_memory and state != "red"
    frame = bytearray(_body(state, state == "yellow" and pulse_bright, dial_visible))
    if dial_visible:
        cy = _LAMPS[0][1]
        frame[(cy - 23) * WIDTH * 4:(cy + 23) * WIDTH * 4] = _dial(percent)
    count = len(dot_states)
    if count:
        spacing = min(8.4, (WIDTH - 12) / count)
        radius = min(2.8, spacing * 0.37)
        for i, state in enumerate(dot_states):
            center = _CX + (i - (count - 1) / 2) * spacing
            color = _GLASS[state][1] if state in _GLASS else (108, 113, 121)
            for y in range(130, 137):
                for x in range(math.floor(center - radius - 1), math.ceil(center + radius + 1)):
                    distance = math.hypot(x + 0.5 - center, y + 0.5 - 133.5)
                    _over(frame, (y * WIDTH + x) * 4, color, _unit(radius + 0.5 - distance))
    return bytes(frame)
