#!/usr/bin/env python3
"""Convert the Pi calibration pickle (pi2web_calibration.pkl) into calibration.json
for the ESP32 scanner. Copy the result to the SD card as hsi/calibration.json.

    python3 convert_calibration.py ../../code/CameraPi/pi2web_calibration2.pkl calibration.json

Carries over what captureServer.py used:
  - hsiInfo ul/lr  -> raw window, padded by 2 pixels as captureServer.py did
  - scan           -> scan band rows drawn on the webcam viewfinder
  - singlePixelInformation center/radius -> viewfinder target circle
  - webcamOffset   -> offset added to the target
  - the pi->webcam polynomial (coeffs, degree, sourceDimensions) and the
    wavelength list, kept for reference and for later tools
The pickle is read with a restricted unpickler (numpy arrays only), so the Pi
code does not need to be importable.
"""
import json
import pickle
import sys

import numpy as np


class _Calibration:
    """Stand-in for calibration.DistortionCorrection: just keeps its attributes."""


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if name == 'DistortionCorrection':
            return _Calibration
        if module.startswith('numpy') and name in ('_reconstruct', 'ndarray', 'dtype', 'scalar'):
            import importlib
            return getattr(importlib.import_module(module), name)
        raise pickle.UnpicklingError('refusing to load %s.%s' % (module, name))


def plain(v):
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, np.generic):
        return v.item()
    return v


def convert(pkl_path):
    with open(pkl_path, 'rb') as f:
        data = _Unpickler(f).load()
    cal = data['calibration']
    extra = data.get('additional', {}) or {}
    hsi = data.get('hsiInfo')
    out = {'source': pkl_path.split('/')[-1], 'version': 1}

    if hsi is not None:
        ul = np.asarray(hsi['ul']).astype(int)
        lr = np.asarray(hsi['lr']).astype(int)
        if np.all(ul % 2 == 0) and np.all(lr % 2 == 0):
            ul, lr = ul - 2, lr + 2          # same padding as captureServer.py
            out['window'] = {'ul': plain(ul), 'lr': plain(lr)}
        else:
            print('warning: hsiInfo window is not even; window not set')
        if 'wavelengths' in hsi:
            out['wavelengths_nm'] = plain(np.asarray(hsi['wavelengths']))
        for k in ('fov', 'centerFov', 'nWavelengths'):
            if k in hsi:
                out[k] = plain(hsi[k])

    spi = extra.get('singlePixelInformation')
    if spi:
        out['target'] = {'center': plain(np.asarray(spi['center']).astype(int)), 'radius': plain(spi['radius'])}
    if 'scan' in extra:
        out['scan_band'] = plain(extra['scan'])
    out['webcam_offset'] = plain(extra.get('webcamOffset', [0, 0]))

    out['pi2web'] = {
        'degree': plain(getattr(cal, 'degree', 1)),
        'source_dimensions': plain(getattr(cal, 'sourceDimensions', None)),
        'coeffs': plain(getattr(cal, 'coeffs', None)),
    }
    return out


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    out = convert(sys.argv[1])
    with open(sys.argv[2], 'w') as f:
        json.dump(out, f, indent=1)
    print('wrote %s: window %s, scan band %s, target %s, webcam offset %s' % (
        sys.argv[2], out.get('window'), out.get('scan_band'), out.get('target'), out.get('webcam_offset')))


if __name__ == '__main__':
    main()
