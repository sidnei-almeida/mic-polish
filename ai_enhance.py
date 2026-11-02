"""Speech restoration with Resemble Enhance (github.com/resemble-ai/resemble-enhance), on the GPU when
available (Intel Arc through PyTorch XPU, or NVIDIA through CUDA), otherwise on the CPU.

  denoise  removes noise only (fast)
  enhance  denoises AND regenerates the voice: restores the lows and highs a cheap mic cuts off,
           at 44.1 kHz. This is the step that can make a bad mic sound like a better one.

    .venv/bin/python ai_enhance.py <in.wav> <out.wav> [--mode enhance] [--device xpu|cuda|cpu]

Weights live in models/resemble-enhance (downloaded by setup.sh from Hugging Face, ResembleAI/resemble-enhance).
"""
import argparse
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from resemble_enhance.enhancer.inference import denoise, enhance

# resemble-enhance predates NumPy 2: it does float(fsolve(...)) on a one-element array, which NumPy 2
# rejects. It is the only fsolve caller here, so return the scalar.
import scipy.optimize  # noqa: E402

_fsolve = scipy.optimize.fsolve
scipy.optimize.fsolve = lambda *args, **kwargs: _fsolve(*args, **kwargs)[0]

RUN_DIR = Path(__file__).parent / 'models' / 'resemble-enhance' / 'enhancer_stage2'


def default_device():
    if hasattr(torch, 'xpu') and torch.xpu.is_available():
        return 'xpu'
    if torch.cuda.is_available():
        return 'cuda'
    return 'cpu'


p = argparse.ArgumentParser(description='Denoise or enhance a speech recording with Resemble Enhance.')
p.add_argument('inp')
p.add_argument('out')
p.add_argument('--mode', choices=['denoise', 'enhance'], default='enhance')
p.add_argument('--device', default=default_device())
p.add_argument('--nfe', type=int, default=64, help='enhance: solver steps (more = cleaner, slower)')
p.add_argument('--lambd', type=float, default=0.9, help='enhance: how much to denoise first (0-1)')
p.add_argument('--tau', type=float, default=0.5, help='enhance: prior temperature (lower = more conservative)')
a = p.parse_args()

if not RUN_DIR.is_dir():
    raise SystemExit(f'model weights not found in {RUN_DIR}: run ./setup.sh first')

wav, sr = sf.read(a.inp, dtype='float32', always_2d=True)
dwav = torch.from_numpy(wav.mean(axis=1))
t0 = time.time()
if a.mode == 'denoise':
    out, out_sr = denoise(dwav, sr, a.device, run_dir=RUN_DIR)
else:
    out, out_sr = enhance(dwav, sr, a.device, nfe=a.nfe, solver='midpoint', lambd=a.lambd, tau=a.tau, run_dir=RUN_DIR)
out = out.cpu().numpy().astype(np.float32)
sf.write(a.out, out, out_sr, subtype='PCM_24')
print(f'{a.mode} on {a.device}: {len(wav) / sr:.1f} s of audio in {time.time() - t0:.1f} s -> {a.out} ({out_sr} Hz)')
