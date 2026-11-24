#!/usr/bin/env python3
"""Read a hyperspectral scan recorded by the ESP32 scanner (SCAN_nnnnn.hsi).

    python3 read_scan.py SCAN_00003.hsi                # summary and RGB preview PNG
    python3 read_scan.py SCAN_00003.hsi --pickle       # also write SCAN_00003.pkl in the Pi format
    python3 read_scan.py SCAN_00003.hsi --cube         # also write the raw window stack as .npy

The --pickle output has the same layout as the Pi's scan files (info dict, one dict
per frame with 'frameLine', 'picam', 'metadata', 'angle', 'frame', 'time', then the
footer with 'success', 'nframes', 'overviewFrames', 'collectTime'), so
databaseUtilities.DataLook and viewer.py read it unchanged.

As a library:
    from read_scan import ScanFile
    scan = ScanFile('SCAN_00003.hsi')
    for frame in scan:            # dicts like the Pi's per-frame records
        frame['picam']            # uint16 (h, w) raw window, 12-bit values
        frame['frameLine']        # uint8 (pixels, 3) webcam centre column, RGB
"""
import argparse
import glob
import io
import json
import os
import pickle
import struct

import numpy as np

FRAME_HEADER = struct.Struct('<4sIfIIHHHH')


class ScanFile:
    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            data = f.read()
        if data[:8] != b'HSISCAN1':
            raise ValueError('%s is not an ESP32 scan file' % path)
        (n,) = struct.unpack_from('<I', data, 8)
        self.header = json.loads(data[12:12 + n])
        pos = 12 + n
        self.offsets = []
        while pos + FRAME_HEADER.size <= len(data) and data[pos:pos + 4] == b'FRAM':
            magic, index, angle, t_ms, exp_us, ww, wh, pixels, _ = FRAME_HEADER.unpack_from(data, pos)
            size = FRAME_HEADER.size + pixels * 3 + ww * wh * 2
            if pos + size > len(data):
                break   # truncated last frame (power loss or full card)
            self.offsets.append(pos)
            pos += size
        self.footer = {'success': False, 'nframes': len(self.offsets), 'truncated': True}
        if data[pos:pos + 8] == b'HSIEND01':
            (n,) = struct.unpack_from('<I', data, pos + 8)
            try:
                self.footer = json.loads(data[pos + 12:pos + 12 + n])
            except ValueError:
                pass   # footer cut short; keep the frames

        self._data = data

    def __len__(self):
        return len(self.offsets)

    def __getitem__(self, i):
        pos = self.offsets[i]
        _, index, angle, t_ms, exp_us, ww, wh, pixels, _ = FRAME_HEADER.unpack_from(self._data, pos)
        pos += FRAME_HEADER.size
        line = np.frombuffer(self._data, np.uint8, pixels * 3, pos).reshape(pixels, 3)
        pos += pixels * 3
        window = np.frombuffer(self._data, '<u2', ww * wh, pos).reshape(wh, ww)
        return {
            'frameLine': line,
            'picam': window,
            'metadata': {'windowed': True, 'ExposureTime': exp_us,
                         'AnalogueGain': self.header.get('analog_gain', 1.0)},
            'angle': angle,
            'frame': index,
            'time': t_ms / 1000.0,
        }

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def overview_files(self):
        base = os.path.splitext(self.path)[0]
        return sorted(glob.glob(base + '_ov*.jpg'))

    def rgb_preview(self):
        """Webcam centre columns side by side: the RGB picture of what was scanned."""
        return np.stack([self[i]['frameLine'] for i in range(len(self))], axis=1)

    def cube(self):
        """All raw windows stacked: (frames, h, w) uint16."""
        return np.stack([self[i]['picam'] for i in range(len(self))])

    def write_pi_pickle(self, out_path):
        overview = []
        try:
            from PIL import Image
            for jpg in self.overview_files():
                overview.append(np.array(Image.open(jpg).convert('RGB')))
        except ImportError:
            print('install Pillow to include overview frames')
        with open(out_path, 'wb') as f:
            info = {'angle': self.header['angle'], 'hsiscan': self.header.get('hsiscan', 'esp32-1'),
                    'exposure': self.header['exposure_us'], 'esp32': self.header}
            pickle.dump(info, f, pickle.HIGHEST_PROTOCOL)
            for d in self:
                d = dict(d, picam=np.array(d['picam']), frameLine=np.array(d['frameLine']))
                pickle.dump(d, f, pickle.HIGHEST_PROTOCOL)
            pickle.dump({'success': self.footer.get('success', False), 'nframes': len(self),
                         'overviewFrames': overview,
                         'collectTime': self.footer.get('collect_ms', 0) / 1000.0}, f, pickle.HIGHEST_PROTOCOL)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('scan')
    ap.add_argument('--pickle', action='store_true', help='write a Pi-format .pkl next to the scan')
    ap.add_argument('--cube', action='store_true', help='write the raw window stack as .npy')
    args = ap.parse_args()

    scan = ScanFile(args.scan)
    base = os.path.splitext(args.scan)[0]
    h = scan.header
    print('%s: %d frames, sweep %s deg, exposure %s us, window %s-%s, %s' % (
        os.path.basename(args.scan), len(scan), h['angle'], h['exposure_us'],
        h['window']['ul'], h['window']['lr'], 'complete' if scan.footer.get('success') else 'INCOMPLETE'))
    if len(scan):
        print('angles %.3f to %.3f deg, %.1f s' % (scan[0]['angle'], scan[len(scan) - 1]['angle'],
                                                  scan[len(scan) - 1]['time']))
        try:
            from PIL import Image
            Image.fromarray(scan.rgb_preview()).save(base + '_rgb.png')
            print('wrote', base + '_rgb.png')
        except ImportError:
            print('install Pillow for the RGB preview')
    if args.cube:
        np.save(base + '_cube.npy', scan.cube())
        print('wrote', base + '_cube.npy')
    if args.pickle:
        scan.write_pi_pickle(base + '.pkl')
        print('wrote', base + '.pkl')


if __name__ == '__main__':
    main()
