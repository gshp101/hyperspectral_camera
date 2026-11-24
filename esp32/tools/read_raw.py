#!/usr/bin/env python3
"""Read a raw frame captured by the ESP32 scanner (IMG_nnnnn.r12 + IMG_nnnnn.json).

The .r12 file holds the frame exactly as the camera sent it over MIPI CSI-2:
RAW12 packed, 2 pixels in 3 bytes (P0[11:4], P1[11:4], P1[3:0]<<4 | P0[3:0]).

    python3 read_raw.py IMG_00001.json                 # print stats, write IMG_00001.png preview
    python3 read_raw.py IMG_00001.json --npy           # also save the unpacked uint16 array
    python3 read_raw.py IMG_00001.json --window 466 198 738 786   # crop like the Pi scanner

As a library:
    from read_raw import load
    img, meta = load('IMG_00001.json')   # img: uint16 array (height, width), values 0..4095

The unpacked array matches what the Pi code builds with convertRawTo12Bits().
"""
import argparse
import json
import os

import numpy as np


def unpack_raw12(data, width, height, swap_words=False):
    """Unpack CSI-2 RAW12 bytes into a (height, width) uint16 array."""
    raw = np.frombuffer(data, dtype=np.uint8)
    if swap_words:
        # Try this if the colour-bar test pattern looks scrambled: undo a 32-bit byte swap.
        raw = raw[: len(raw) // 4 * 4].reshape(-1, 4)[:, ::-1].reshape(-1)
    need = width * height * 3 // 2
    if raw.size < need:
        raise ValueError('file has %d bytes, expected %d' % (raw.size, need))
    b = raw[:need].reshape(-1, 3).astype(np.uint16)
    p0 = (b[:, 0] << 4) | (b[:, 2] & 0x0F)
    p1 = (b[:, 1] << 4) | (b[:, 2] >> 4)
    out = np.empty(b.shape[0] * 2, dtype=np.uint16)
    out[0::2] = p0
    out[1::2] = p1
    return out.reshape(height, width)


def load(json_path, swap_words=False):
    with open(json_path) as f:
        meta = json.load(f)
    raw_path = os.path.join(os.path.dirname(json_path), meta['file'])
    with open(raw_path, 'rb') as f:
        data = f.read()
    return unpack_raw12(data, meta['width'], meta['height'], swap_words), meta


def preview_rgb(img, bayer):
    """Half-resolution RGB preview: one RGB pixel per 2x2 Bayer cell, gamma 2.2, 8 bit."""
    cells = {(0, 0): bayer[0], (0, 1): bayer[1], (1, 0): bayer[2], (1, 1): bayer[3]}
    planes = {'R': [], 'G': [], 'B': []}
    for (r, c), colour in cells.items():
        planes[colour].append(img[r::2, c::2].astype(np.float32))
    rgb = np.stack([np.mean(planes[k], axis=0) for k in 'RGB'], axis=-1)
    rgb = np.clip((rgb - 256) / (4095 - 256), 0, 1)   # IMX477 black level is about 256
    return (rgb ** (1 / 2.2) * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('json')
    ap.add_argument('--npy', action='store_true', help='save the unpacked frame as .npy')
    ap.add_argument('--swap-words', action='store_true', help='undo a 32-bit byte swap')
    ap.add_argument('--window', nargs=4, type=int, metavar=('X0', 'Y0', 'X1', 'Y1'),
                    help='crop to this window (Pi calibration hsiInfo ul/lr, padded by 2)')
    args = ap.parse_args()

    img, meta = load(args.json, args.swap_words)
    if args.window:
        x0, y0, x1, y1 = args.window
        img = img[y0:y1, x0:x1]
    base = os.path.splitext(args.json)[0]
    print('%s: %dx%d, bayer %s, exposure %s us, gain %s' % (
        meta['file'], img.shape[1], img.shape[0], meta['bayer'], meta['exposure_us'], meta['analog_gain']))
    print('min %d  max %d  mean %.1f  (12-bit values; >4095 would mean a wrong unpack)' % (
        img.min(), img.max(), img.mean()))
    if args.npy:
        np.save(base + '.npy', img)
        print('wrote', base + '.npy')
    try:
        from PIL import Image
        Image.fromarray(preview_rgb(img, meta['bayer'])).save(base + '.png')
        print('wrote', base + '.png')
    except ImportError:
        print('install Pillow for a PNG preview')


if __name__ == '__main__':
    main()
