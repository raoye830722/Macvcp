// gsv_adapter.js — VCPChat Sovits5客户端 ↔ gsv_server 协议适配层 (Codex 2026-11-07)
// 2026-11-12 v2: ①STABLE_SEED 555→42（娜塔莎参考下seed扫描最优:42 tail_ratio=0.327且ASR逐字正确;555在其上尾部伪影+开头幻觉"是"）
//               ②/tts响应加20ms尾部micro-fade（机械压平AR采样尾部伪影，主人反馈"每句话结尾滋一下"）
const http = require('http');
const fs = require('fs');
const path = require('path');
const REF_DIR = '/Users/raoye/Desktop/vcp/gsv_client_refs'; // 纯ASCII,已在server白名单
const UPSTREAM = { host: '127.0.0.1', port: 9880 };
const STABLE_SEED = 42; // 2026-11-12 seed扫描实测（/tmp/seed_scan.json留档）

// 尾部micro-fade：对wav PCM最后fadeMs毫秒做线性淡出，机械压平AR尾部伪影（滋声）
// 只处理RIFF/16bit标准wav，异常时原样返回不阻断音频流
function tailFade(buf, fadeMs) {
    try {
        if (buf.length < 44 || buf.toString('ascii', 0, 4) !== 'RIFF') return buf;
        let pos = 12, fmt = null, dataOff = -1, dataLen = 0;
        while (pos + 8 <= buf.length) {
            const id = buf.toString('ascii', pos, pos + 4);
            const sz = buf.readUInt32LE(pos + 4);
            if (id === 'fmt ') fmt = { channels: buf.readUInt16LE(pos + 10), sampleRate: buf.readUInt32LE(pos + 12), bits: buf.readUInt16LE(pos + 22) };
            if (id === 'data') { dataOff = pos + 8; dataLen = Math.min(sz, buf.length - dataOff); break; }
            pos += 8 + sz + (sz % 2);
        }
        if (dataOff < 0 || !fmt || fmt.bits !== 16) return buf;
        const bpf = (fmt.bits / 8) * fmt.channels;
        const totalFrames = Math.floor(dataLen / bpf);
        const fadeFrames = Math.min(Math.floor(fmt.sampleRate * fadeMs / 1000), totalFrames);
        const startFrame = totalFrames - fadeFrames;
        for (let f = 0; f < fadeFrames; f++) {
            const g = f / fadeFrames; // 0→1线性增益
            for (let c = 0; c < fmt.channels; c++) {
                const off = dataOff + (startFrame + f) * bpf + c * 2;
                buf.writeInt16LE(Math.round(buf.readInt16LE(off) * g), off);
            }
        }
        return buf;
    } catch (e) { return buf; } // fade失败不阻断音频
}
// 2026-11-12 v3: 谱减降噪+响度补偿（主人拍板"降噪C响度补偿版"听感）
// 调用同目录denoise_worker.py（stdin/stdout传wav字节），8秒超时熔断，任何失败fallback原音频不阻断链路
function denoise(buf) {
    return new Promise((resolve) => {
        try {
            const { spawn } = require('child_process');
            const py = spawn('python3', [path.join(__dirname, 'denoise_worker.py')]);
            const out = [];
            let done = false;
            const finish = (r) => { if (!done) { done = true; resolve(r); } };
            py.stdout.on('data', (c) => out.push(c));
            py.on('close', (code) => { finish(code === 0 && out.length > 0 ? Buffer.concat(out) : buf); });
            py.on('error', () => finish(buf));
            py.stdin.on('error', () => finish(buf));
            py.stdin.end(buf);
            setTimeout(() => { try { py.kill(); } catch (e2) {} finish(buf); }, 8000);
        } catch (e) { resolve(buf); }
    });
}

function proxy(req, res, bodyOverride) {
    const headers = { ...req.headers, host: `${UPSTREAM.host}:${UPSTREAM.port}` };
    delete headers['content-length'];
    const opts = { ...UPSTREAM, path: req.url, method: req.method, headers };
    const up = http.request(opts, (ur) => {
        res.writeHead(ur.statusCode, ur.headers);
        ur.pipe(res);
    });
    up.on('error', (e) => { res.writeHead(502, {'Content-Type':'application/json'}); res.end(JSON.stringify({error:`upstream: ${e.message}`})); });
    if (bodyOverride !== undefined) up.end(bodyOverride); else req.pipe(up);
}

http.createServer((req, res) => {
    console.log('[' + new Date().toISOString().slice(11,19) + '] ' + req.method + ' ' + req.url);
    if (req.method === 'POST' && req.url.startsWith('/models')) {
        let body = '';
        req.on('data', c => body += c);
        req.on('end', () => {
            res.writeHead(200, {'Content-Type': 'application/json'});
            res.end(JSON.stringify({
                msg: '获取成功',
                models: [{
                    name: 'gsv-local',
                    defaults: [
                        { id: 'nova', name: 'Nova', ref_audio_path: '/Users/raoye/Desktop/vcp/gsv_client_refs/client_nova_16k.wav', prompt_text: '让我看看嗯，外形不太一样，但功能倒是差不多，也许能通过共鸣嗯。', prompt_lang: 'zh' }
                    ],
                    remoteVoices: []
                }]
            }));
        });
        return;
    }
    if (req.method === 'POST' && req.url.startsWith('/reference')) {
        const name = (new URL(req.url, 'http://x').searchParams.get('name') || 'ref').replace(/[^a-zA-Z0-9_.-]/g, '_');
        const chunks = [];
        req.on('data', c => chunks.push(c));
        req.on('end', () => {
            const file = path.join(REF_DIR, `client_${name.replace(/\.wav$/i,'')}.wav`);
            fs.writeFileSync(file, Buffer.concat(chunks));
            res.writeHead(200, {'Content-Type':'application/json'});
            res.end(JSON.stringify({ path: file }));
        });
        return;
    }
    if (req.method === 'POST' && req.url.startsWith('/tts')) {
        const chunks = [];
        req.on('data', c => chunks.push(c));
        req.on('end', () => {
            try {
                const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
                if (Number(body.seed) === -1) body.seed = STABLE_SEED; // 稳定性改写(v2: 555→42)
                const headers = { ...req.headers, host: `${UPSTREAM.host}:${UPSTREAM.port}` };
                delete headers['content-length'];
                const opts = { ...UPSTREAM, path: req.url, method: req.method, headers };
                const up = http.request(opts, (ur) => {
                    const bufs = [];
                    ur.on('data', c => bufs.push(c));
                    ur.on('end', async () => {
                        let out = Buffer.concat(bufs);
                        if (ur.statusCode === 200) {
                            out = tailFade(out, 20); // v2: 尾部20ms淡出
                            out = await denoise(out); // v3: 谱减降噪+响度补偿
                        }
                        const h = { ...ur.headers };
                        h['content-length'] = out.length; // fade不改长度但显式重算防漂
                        res.writeHead(ur.statusCode, h);
                        res.end(out);
                    });
                });
                up.on('error', (e) => { res.writeHead(502, {'Content-Type':'application/json'}); res.end(JSON.stringify({error:`upstream: ${e.message}`})); });
                up.end(Buffer.from(JSON.stringify(body)));
            } catch (e) { res.writeHead(400); res.end(JSON.stringify({error:'bad json'})); }
        });
        return;
    }
    proxy(req, res); // 其余全透传
}).listen(9881, () => console.log('gsv_adapter :9881 → :9880 ready (v3 seed42+tailfade20ms+denoise)'));
