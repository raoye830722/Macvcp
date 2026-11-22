#!/usr/bin/env python3
# denoise_worker.py — GSV adapter v3 降噪worker
# 协议: stdin=完整wav字节(RIFF/PCM16) → stdout=降噪+响度补偿后wav字节
# 失败exit非0, adapter侧fallback原音频不阻断语音链路
import sys, io, wave
import numpy as np

def voice_rms(x, sr):
    # 50ms窗RMS取top50%均值 = 语音本体响度参考(抗间隙噪声污染)
    w2 = max(int(sr*0.05), 1)
    nw = len(x)//w2
    if nw == 0:
        return float(np.sqrt((x**2).mean())) if len(x) > 0 else 0.0
    r = np.array([np.sqrt((x[i*w2:(i+1)*w2]**2).mean()) for i in range(nw)])
    r.sort()
    top = r[int(nw*0.5):]
    return float(top.mean()) if len(top) > 0 else 0.0

def main():
    raw = sys.stdin.buffer.read()
    with wave.open(io.BytesIO(raw), 'rb') as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(n)
    if sw != 2:
        print('unsupported sample width', file=sys.stderr); sys.exit(2)
    samples = np.frombuffer(frames, dtype='<i2').astype(np.float64)
    if ch > 1:
        samples = samples.reshape(-1, ch).mean(axis=1)
    # 噪声模板自适应: 开头连续低能量窗(<中位RMS30%), 上限0.5s; 不足则取全曲最安静0.15s
    win = max(int(sr*0.05), 1)
    nwin = len(samples)//win
    rms = np.array([np.sqrt((samples[i*win:(i+1)*win]**2).mean()) for i in range(nwin)])
    pos = rms[rms > 0]
    med = float(np.median(pos)) if len(pos) > 0 else 0.0
    lead = 0
    while lead < nwin and lead < 10 and rms[lead] < med*0.3:
        lead += 1
    if lead >= 2:
        noise = samples[:lead*win]
    else:
        qi = int(np.argmin(rms)) if nwin > 0 else 0
        s0 = max(qi*win - win, 0)
        noise = samples[s0:s0+int(sr*0.15)]
    import noisereduce as nr
    reduced = nr.reduce_noise(y=samples, y_noise=noise, sr=sr, prop_decrease=0.9, stationary=False)
    target = voice_rms(samples, sr)
    cur = voice_rms(reduced, sr)
    if cur > 100:
        gain = min(max(target/cur, 1.0), 2.5)
        reduced = reduced*gain
    out = np.clip(reduced, -32000, 32000).astype('<i2')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(out.tobytes())
    sys.stdout.buffer.write(buf.getvalue())

if __name__ == '__main__':
    main()
