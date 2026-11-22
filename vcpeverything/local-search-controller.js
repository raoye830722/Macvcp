const http = require('http'); // 核心变更：使用Node.js内置的http模块
const fs = require('fs');
const path = require('path');

// 仅加载插件自身的.env配置
require('dotenv').config({ path: path.join(__dirname, '.env'), quiet: true });

// --- 配置 ---
// 新增：Everything HTTP服务器的端口配置
const EVERYTHING_PORT = parseInt(process.env.EVERYTHING_PORT || '80');
const DEBUG_MODE = process.env.DEBUG_MODE === 'true';

// --- 工具函数 ---
function debugLog(message, data = null) {
    if (DEBUG_MODE) {
        const timestamp = new Date().toISOString();
        console.error(`[DEBUG ${timestamp}] ${message}`);
        if (data) {
            console.error(JSON.stringify(data, null, 2));
        }
    }
}

// --- 核心功能 ---
/**
 * 通过Everything的HTTP服务器执行搜索。
 * [最终版] 这是最稳定、最可靠的官方推荐方式。
 * @param {string} query - 搜索查询字符串。
 * @param {number} maxResults - 返回的最大结果数量。
 * @returns {Promise<object>} - 返回一个包含搜索结果的完整对象。
 */
function searchWithEverythingHTTP(query, maxResults = 100) {
    return new Promise((resolve, reject) => {
        // 构建请求URL：
        // ?s=...       - 设置搜索词
        // &json=1      - 关键！让服务器返回JSON格式的数据
        // &path_column=1 - 请求包含完整路径列
        // &n=...       - 设置最大结果数
        const encodedQuery = encodeURIComponent(query);
        const requestPath = `/?s=${encodedQuery}&json=1&path_column=1&n=${maxResults}`;
        
        const options = {
            hostname: '127.0.0.1', // 只在本地访问
            port: EVERYTHING_PORT,
            path: requestPath,
            method: 'GET'
        };

        debugLog('Making HTTP request to Everything server', options);

        const req = http.request(options, (res) => {
            if (res.statusCode !== 200) {
                return reject(new Error(`Everything HTTP server responded with status code: ${res.statusCode}`));
            }

            let rawData = '';
            res.setEncoding('utf8');
            res.on('data', (chunk) => {
                rawData += chunk;
            });
            res.on('end', () => {
                try {
                    const parsedData = JSON.parse(rawData);
                    debugLog('Successfully received and parsed JSON response from Everything');
                    resolve(parsedData); // 直接返回解析后的JSON对象
                } catch (e) {
                    reject(new Error(`Failed to parse JSON response from Everything: ${e.message}`));
                }
            });
        });

        req.on('error', (e) => {
            debugLog('HTTP request to Everything failed', { error: e.message });
            if (e.code === 'ECONNREFUSED') {
                reject(new Error(`Connection to Everything HTTP server refused on port ${EVERYTHING_PORT}. Please ensure Everything is running and the HTTP server is enabled in Tools -> Options.`));
            } else {
                reject(new Error(`HTTP request error: ${e.message}`));
            }
        });

        req.end();
    });
}
// ==================== macOS mdfind 分支 (施工单 v1.1, 2026-10-21) ====================
const { spawn } = require('child_process');
const readline = require('readline');

const MDFIND_TIMEOUT_MS = parseInt(process.env.MDFIND_TIMEOUT_MS || '10000');
const MDFIND_DEFAULT_SCOPE = process.env.MDFIND_DEFAULT_SCOPE || '';

// [Todo2] 三层判定规则: ①raw:前缀→透传 ②含kMDItem→透传 ③其余→Everything转换
// 已知边界: 混合输入(裸词+kMDItem)走规则②时裸词部分为内容搜索语义——规避: 用raw:前缀或全Everything风格
function routeQuery(query) {
    if (query.startsWith('raw:')) {
        return { mode: 'raw', raw: query.slice(4).trim() };
    }
    if (query.includes('kMDItem')) {
        return { mode: 'raw', raw: query };
    }
    const conv = convertEverythingToMdfind(query);
    if (conv.error) return { mode: 'error', error: conv.error };
    return { mode: 'convert', raw: conv.raw, onlyin: conv.onlyin };
}

// [Red验收庭R2/R5/R7修复] 名称匹配单元构造: mdfind的c后缀=不区分大小写, 默认加c对齐Everything默认不敏感;
// caseSensitive=true时不加c(mdfind默认敏感=区分大小写, 对齐Everything case:强制区分)——语义方向修正
function buildNameUnit(value, caseSensitive) {
    const suffix = caseSensitive ? '' : 'c';
    if (/[*?]/.test(value)) {
        return `kMDItemFSName == '${value}'${suffix}`;
    }
    return `kMDItemFSName == '*${value}*'${suffix}`;
}
// [Todo3] Everything 兼容转换层: 分词(引号短语为整体token) → 规则表 → &&/|| 拼接
function convertEverythingToMdfind(query) {
    // 多 path: OR 报错 (Red Q3 裁决: 跨查询配额+去重+排序池复杂度失衡)
    if ((query.match(/path:/gi) || []).length > 1 || /\bpath:\S+\s*\|/.test(query)) {
        return { error: 'mdfind -onlyin 不支持多目录查询。请分两次调用，或使用 raw: 前缀手写 kMDItemPath 查询（注意通配包含语义与目录前缀语义的差异）。' };
    }
    // 硬缺口报错 (禁静默失败)
    const unsupported = query.match(/\b(regex|stem|child|dupe):/i);
    if (unsupported) {
        return { error: `mdfind 不支持 ${unsupported[1]}: 语法。请使用 raw: 前缀手写 kMDItem 查询，或简化查询。` };
    }
    let onlyin = null;
    const q = query.replace(/\bpath:(\S+)/i, (_, p) => { onlyin = p; return ' '; }).trim();
    // 分词: 引号短语为整体 token (Red Q1 裁决, 禁按空格切碎)
    const tokens = [];
    const re = /"([^"]*)"|(\S+)/g;
    let m;
    while ((m = re.exec(q)) !== null) {
        const v = (m[1] !== undefined) ? m[1] : m[2];
        if (v) tokens.push(v);
    }
    const parts = [];
    let orNext = false, notNext = false, caseNext = false;
    for (const v of tokens) {
        let mm;
        let unit = null;
        if (v === '|') { orNext = true; continue; }
        if (v === '!') { notNext = true; continue; }
        // [R2贴词+R7语义反转] case:双形式: 贴词case:VCP立即生效(仅此token), 独立case:作用于后续token
        // Everything case:=强制区分大小写 → mdfind不加c(默认敏感)——旧实现加c方向相反, 已修正
        if (/^case:/i.test(v)) {
            caseNext = true;
            const rest = v.slice(5).trim();
            if (rest) {
                parts.push({ op: orNext ? '||' : '&&', prefix: notNext ? '!' : '', expr: `(${buildNameUnit(rest, true)})` });
                orNext = false; notNext = false; caseNext = false;
            }
            continue;
        }
        if ((mm = v.match(/^ext:(\w+)$/i))) {
            unit = `kMDItemFSName == '*.${mm[1]}'${caseNext ? '' : 'c'}`;
        } else if ((mm = v.match(/^size:([<>])\s*(\d+(?:\.\d+)?)\s*(kb|mb|gb)$/i))) {
            const mult = { kb: 1024, mb: 1048576, gb: 1073741824 }[mm[3].toLowerCase()];
            unit = `kMDItemFSSize ${mm[1]} ${Math.round(parseFloat(mm[2]) * mult)}`;
        } else if ((mm = v.match(/^size:(\d+(?:\.\d+)?)\s*(kb|mb|gb)\s*\.\.\s*(\d+(?:\.\d+)?)\s*(kb|mb|gb)$/i))) {
            const mult = { kb: 1024, mb: 1048576, gb: 1073741824 };
            unit = `kMDItemFSSize >= ${Math.round(parseFloat(mm[1]) * mult[mm[2].toLowerCase()])} && kMDItemFSSize <= ${Math.round(parseFloat(mm[3]) * mult[mm[4].toLowerCase()])}`;
        } else if ((mm = v.match(/^date-modified:(.+)$/i))) {
            const dv = mm[1].trim().toLowerCase();
            if (dv === 'today') unit = 'kMDItemFSContentChangeDate >= $time.today';
            else if (dv === 'yesterday') unit = 'kMDItemFSContentChangeDate >= $time.today(-1)';
            else {
                const d = new Date(mm[1].trim());
                if (isNaN(d.getTime())) return { error: `无法解析 date-modified 日期: ${mm[1]}` };
                const days = Math.floor((Date.now() - d.getTime()) / 86400000);
                unit = `kMDItemFSContentChangeDate >= $time.today(-${days})`;
            }
        } else {
            unit = buildNameUnit(v, caseNext);
        }
        parts.push({ op: orNext ? '||' : '&&', prefix: notNext ? '!' : '', expr: `(${unit})` });
        orNext = false; notNext = false; caseNext = false;
    }
    if (parts.length === 0) {
        return { error: '查询转换后为空。' };
    }
    let raw = parts[0].prefix + parts[0].expr;
    for (let i = 1; i < parts.length; i++) {
        raw += ` ${parts[i].op} ${parts[i].prefix}${parts[i].expr}`;
    }
    return { raw, onlyin };
}

// [Todo5辅助] 从查询提取用于评分的名称词(跳过操作符与限定符; raw/透传查询不评分)
function extractNameTokens(query) {
    if (query.startsWith('raw:') || query.includes('kMDItem')) return [];
    const tokens = [];
    const re = /"([^"]*)"|(\S+)/g;
    let m;
    while ((m = re.exec(query)) !== null) {
        const v = (m[1] !== undefined) ? m[1] : m[2];
        if (!v) continue;
        if (/^(ext|size|date-modified|path|case|regex|stem|child|dupe):/i.test(v)) continue;
        if (v === '|' || v === '!' || v === '&&') continue;
        const cleaned = v.replace(/[*?]/g, '');
        if (cleaned) tokens.push(cleaned);
    }
    return tokens;
}

// [Todo5] 文件名评分: 前缀匹配(3) > 词边界匹配(2) > 子串匹配(1) —— 借鉴思想不借鉴代码(Red OilFind裁决层③)
function scoreFilename(filePath, tokens) {
    const name = path.basename(filePath).toLowerCase();
    let score = 0;
    for (const t of tokens) {
        const tv = t.toLowerCase();
        if (name.startsWith(tv)) score += 3;
        else if (new RegExp(`[\\s._\\-]${tv.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`).test(name)) score += 2;
        else if (name.includes(tv)) score += 1;
    }
    return score;
}

// [Todo4] 流式截断 + [Todo6] 错误处理: 评分池阈值 max(100, maxResults*10) 行(方案A, 红队主攻一修正)
function searchWithMdfind(rawQuery, scope, maxResults) {
    return new Promise((resolve, reject) => {
        // [Todo6] -onlyin 路径前置校验(mdfind 对不存在路径静默空, EXIT=0)
        if (scope && !fs.existsSync(scope)) {
            return reject(new Error(`搜索目录不存在: ${scope}（mdfind 对不存在路径会静默返回空结果，此处已前置校验）`));
        }
        const args = [];
        if (scope) args.push('-onlyin', scope);
        args.push(rawQuery);
        debugLog('Executing mdfind', { args });
        const child = spawn('mdfind', args);
        const poolSize = Math.max(100, maxResults * 10);
        const lines = [];
        let timedOut = false;
        const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, MDFIND_TIMEOUT_MS);
        const rl = readline.createInterface({ input: child.stdout });
        rl.on('line', (line) => {
            const t = line.trim();
            if (t) lines.push(t);
            if (lines.length >= poolSize) { rl.close(); child.kill('SIGKILL'); }
        });
        child.on('error', (e) => {
            clearTimeout(timer);
            reject(new Error(`mdfind 执行失败: ${e.message}`));
        });
        child.on('close', () => {
            clearTimeout(timer);
            if (timedOut) {
                return reject(new Error(`mdfind 查询超时 (${MDFIND_TIMEOUT_MS}ms)，已终止。可尝试缩小查询范围。`));
            }
            // [Todo2兜底] stdout 错误文本检测——mdfind 失败反馈三形态退出码全 0, 禁只看 exit code (Red 实测)
            const errLine = lines.find(l => l.includes('Failed to create query'));
            if (errLine) {
                return reject(new Error(`mdfind 查询语法错误: ${errLine}`));
            }
            if (lines.length === 0) {
                return resolve({ paths: [], total: 0 });
            }
            resolve({ paths: lines, total: lines.length });
        });
    });
}

/**
 * 主处理函数 - 插件的“大脑”
 * @param {object} request - VCP框架传递过来的、已经解析好的JSON对象
 */
async function processRequest(request) {
    const { query, maxResults } = request;

    if (!query) {
        return {
            status: 'error',
            error: 'Missing required parameter: query',
        };
    }

    // [Todo1] 平台分支: Windows 走原 Everything HTTP 逻辑(一行未动), macOS 走 mdfind
    if (process.platform === 'win32') {
        try {
            const everythingResponse = await searchWithEverythingHTTP(query, maxResults);
            const filePaths = everythingResponse.results.map(item => path.join(item.path, item.name));
            return {
                status: 'success',
                result: {
                    searchQuery: query,
                    resultCount: everythingResponse.totalResults,
                    results: filePaths,
                },
            };
        } catch (error) {
            return { status: 'error', error: error.message };
        }
    }
    if (process.platform !== 'darwin') {
        return {
            status: 'error',
            error: `不支持的平台: ${process.platform}。本插件支持 Windows (Everything HTTP) 与 macOS (mdfind)。`,
        };
    }

    // [macOS mdfind 分支]
    try {
        const route = routeQuery(query);
        if (route.mode === 'error') {
            return { status: 'error', error: route.error };
        }
        const max = parseInt(maxResults) > 0 ? parseInt(maxResults) : 100;
        const result = await searchWithMdfind(route.raw, route.onlyin || MDFIND_DEFAULT_SCOPE, max);

        // [Todo5] 评分排序: 对评分池(阈值行)排序取前 maxResults —— 非对截断后排序(红队主攻一修正, 方案A)
        const nameTokens = extractNameTokens(query);
        let finalPaths;
        if (result.paths.length > max && nameTokens.length > 0) {
            const scored = result.paths.map(p => ({ p, s: scoreFilename(p, nameTokens) }));
            scored.sort((a, b) => b.s - a.s || a.p.localeCompare(b.p));
            finalPaths = scored.slice(0, max).map(x => x.p);
        } else {
            finalPaths = result.paths.slice(0, max);
        }

        // [Todo6] 盲区提示(三盲区文档化, Worker不得删除)
        const notes = [];
        const qTokens = query.split(/\s+/);
        if (qTokens.some(t => t.startsWith('.') && t.length > 1)) {
            notes.push('提示: Spotlight 默认不索引点文件(隐藏文件), 此类查询建议用 find 补搜。');
        }
        if ((route.onlyin || MDFIND_DEFAULT_SCOPE || '').startsWith('/Volumes/')) {
            notes.push('提示: 目标为外置卷, 若结果不全请检查该卷 Spotlight 索引状态 (mdutil -s <卷路径> 查看, mdutil -i on 启用)。');
        }
        if (MDFIND_DEFAULT_SCOPE) {
            notes.push(`提示: 搜索域限定为 ${MDFIND_DEFAULT_SCOPE} (MDFIND_DEFAULT_SCOPE)。`);
        }

        return {
            status: 'success',
            result: {
                searchQuery: query,
                resultCount: finalPaths.length,
                results: finalPaths,
                ...(notes.length > 0 ? { notes } : {}),
            },
        };
    } catch (error) {
        return { status: 'error', error: error.message };
    }
}


// --- stdio 通信 ---
let inputBuffer = '';
process.stdin.setEncoding('utf8');

process.stdin.on('data', async (chunk) => {
    inputBuffer += chunk;
});

process.stdin.on('end', async () => {
    if (!inputBuffer.trim()) {
        console.log(JSON.stringify({ status: 'error', error: 'No input received.' }));
        return;
    }
    debugLog('Received raw input from VCP', inputBuffer);
    try {
        const request = JSON.parse(inputBuffer);
        const response = await processRequest(request);
        console.log(JSON.stringify(response));
    } catch (error) {
        console.log(JSON.stringify({ status: 'error', error: `Invalid JSON input: ${error.message}` }));
    }
});

debugLog('VCPEverything (HTTP Mode) plugin started and ready.');
