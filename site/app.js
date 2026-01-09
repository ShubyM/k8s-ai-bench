const DATA_URL = "combined_results.jsonl";
const MODEL_KEYWORDS = {
    "gemini": "Proprietary"
};

// Lab/Company identification and styling
const LAB_CONFIG = {
    google: {
        keywords: ["gemini"],
        name: "Google",
        color: "#4285F4",
        icon: '<svg class="lab-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>'
    },
    qwen: {
        keywords: ["qwen"],
        name: "Qwen",
        color: "#6366F1",
        icon: '<svg class="lab-icon" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8z"/><path d="M12 6c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm0 10c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4z"/><circle cx="12" cy="12" r="2"/></svg>'
    },
    openai: {
        keywords: ["openai", "gpt"],
        name: "OpenAI",
        color: "#10A37F",
        icon: '<svg class="lab-icon" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.896zm16.597 3.855l-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.791a4.494 4.494 0 0 1-.676 8.105v-5.678a.79.79 0 0 0-.407-.667zm2.01-3.023l-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.5 4.5 0 0 1 6.68 4.66zm-12.64 4.135l-2.02-1.164a.08.08 0 0 1-.038-.057V6.075a4.5 4.5 0 0 1 7.375-3.453l-.142.08L8.704 5.46a.795.795 0 0 0-.393.681zm1.097-2.365l2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z"/></svg>'
    },
    zhipu: {
        keywords: ["zai", "glm"],
        name: "Zhipu AI",
        color: "#FF6B35",
        icon: '<svg class="lab-icon" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>'
    },
    default: {
        name: "Other",
        color: "#6B7280",
        icon: '<svg class="lab-icon" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>'
    }
};

function getLabInfo(modelName) {
    const n = modelName.toLowerCase();
    for (let labKey in LAB_CONFIG) {
        if (labKey === 'default') continue;
        const lab = LAB_CONFIG[labKey];
        for (let keyword of lab.keywords) {
            if (n.includes(keyword)) {
                return { key: labKey, ...lab };
            }
        }
    }
    return { key: 'default', ...LAB_CONFIG.default };
}

function getModelDisplayName(modelName) {
    // Extract just the model name without org prefix if present
    if (modelName.includes('/')) {
        return modelName.split('/').pop();
    }
    return modelName;
}

function getModelWithIcon(modelName, linkUrl) {
    const lab = getLabInfo(modelName);
    const displayName = getModelDisplayName(modelName);
    const iconHtml = '<span class="lab-icon-wrapper" style="color: ' + lab.color + ';" title="' + lab.name + '">' + lab.icon + '</span>';
    if (linkUrl) {
        return iconHtml + '<a href="' + linkUrl + '" class="model-link">' + displayName + '</a>';
    }
    return iconHtml + '<span class="model-name">' + displayName + '</span>';
}

async function loadData() {
    try {
        const response = await fetch(DATA_URL);
        if (!response.ok) throw new Error("Failed to load data file: " + DATA_URL);
        const text = await response.text();
        const rawData = text.trim().split('\n').map(line => {
            try { return JSON.parse(line); } catch (e) { return null; }
        }).filter(x => x); 
        processData(rawData);
        if (window.renderPage) window.renderPage();
    } catch (err) {
        console.error(err);
        const container = document.querySelector('.container');
        if(container) {
            container.innerHTML = 
                '<div style="color:#cf222e; text-align:center; margin-top:50px; background:#fff; padding:2rem; border-radius:6px; border:1px solid #e1e4e8;">' +
                '<h2>Error Loading Data</h2>' +
                '<p>Could not fetch <code>'+DATA_URL+'</code>.</p>' +
                '<p style="color:#57606a;"><strong>Note:</strong> If opening locally, you must run a local server (browsers block file:// access).</p>' +
                '<code style="background:#f6f8fa; padding:5px; border-radius:4px;">python3 -m http.server</code>' +
                '</div>';
        }
    }
}

function getModelType(name) {
    const n = name.toLowerCase();
    for (let k in MODEL_KEYWORDS) {
        if (n.includes(k)) return MODEL_KEYWORDS[k];
    }
    return 'Open Source';
}

function passAtK(n, c, k) {
    if (n === 0) return 0.0;
    const p = c / n;
    return (1.0 - Math.pow(1.0 - p, k)) * 100;
}

function processData(rawData) {
    const cleanedData = rawData.map(item => {
        let res = (item.result || 'fail').toString().toLowerCase();
        let msg = null;
        if (res !== 'success' && item.failures && item.failures.length > 0) {
            msg = item.failures[0].message ? item.failures[0].message.trim() : '';
        }
        return {
            model: item.llmConfig?.model || 'Unknown',
            task: item.name || 'Unknown',
            result: res,
            message: msg
        };
    });

    const grouped = {}; 
    const allTasks = new Set();
    
    cleanedData.forEach(item => {
        const m = item.model;
        const t = item.task;
        allTasks.add(t);
        if (!grouped[m]) grouped[m] = {};
        if (!grouped[m][t]) grouped[m][t] = [];
        grouped[m][t].push(item);
    });

    const leaderboard = [];
    const model_details = {};

    for (const model in grouped) {
        const tasksMap = grouped[model];
        const p1s = [];
        const p5s = [];
        let passAllCount = 0;
        let totalRuns = 0;
        const mRows = [];

        for (const tName in tasksMap) {
            const items = tasksMap[tName];
            const n = items.length;
            const c = items.filter(i => i.result === 'success').length;
            totalRuns += n;
            p1s.push(passAtK(n, c, 1));
            p5s.push(passAtK(n, c, 5));
            if (n > 0 && c === n) passAllCount++;

            items.forEach((item, idx) => {
                mRows.push({
                    task: tName,
                    result: item.result,
                    run: idx + 1,
                    message: item.message
                });
            });
        }

        const avgP1 = p1s.length ? p1s.reduce((a,b)=>a+b,0)/p1s.length : 0;
        const avgP5 = p5s.length ? p5s.reduce((a,b)=>a+b,0)/p5s.length : 0;
        const taskCount = Object.keys(tasksMap).length;
        const pAll = taskCount ? (passAllCount / taskCount) * 100 : 0;

        leaderboard.push({
            id: model,
            type: getModelType(model),
            p1: parseFloat(avgP1.toFixed(1)),
            p5: parseFloat(avgP5.toFixed(1)),
            pAll: parseFloat(pAll.toFixed(1)),
            runs: totalRuns,
            tasks: taskCount
        });
        
        mRows.sort((a,b) => (a.task > b.task) ? 1 : (a.task === b.task) ? a.run - b.run : -1);
        model_details[model] = mRows;
    }

    const tasks = [];
    const task_details = {};

    allTasks.forEach(tName => {
        let allRes = [];
        for (const m in grouped) {
            if (grouped[m][tName]) {
                allRes = allRes.concat(grouped[m][tName].map(i => i.result));
            }
        }
        const nTotal = allRes.length;
        const cTotal = allRes.filter(r => r === 'success').length;
        
        tasks.push({
            name: tName,
            p1: parseFloat(passAtK(nTotal, cTotal, 1).toFixed(1)),
            count: nTotal
        });

        const breakdown = [];
        for (const m in grouped) {
            if (grouped[m][tName]) {
                const items = grouped[m][tName];
                const n = items.length;
                const c = items.filter(i => i.result === 'success').length;
                const p1 = passAtK(n, c, 1);
                const runs = items.map((i, idx) => ({ r: idx+1, val: i.result === 'success' ? 'S' : 'F' }));
                breakdown.push({ model: m, p1: parseFloat(p1.toFixed(1)), runs: runs });
            }
        }
        breakdown.sort((a,b) => b.p1 - a.p1);
        task_details[tName] = breakdown;
    });

    leaderboard.sort((a,b) => b.p5 - a.p5);
    tasks.sort((a,b) => a.p1 - b.p1);

    window.PROCESSED_DATA = { leaderboard, tasks, details: model_details, task_details };
}

function getHue(percentage) { return (percentage / 100) * 120; }

function createMiniBar(val, hue) {
    return '<div style="height: 6px; width: 100%; background: #eee; border-radius: 3px; margin-top: 5px; overflow: hidden;"><div style="height: 100%; width: '+val+'%; background-color: hsla('+hue+', 85%, 40%, 1.0);"></div></div>';
}

function createBar(val, hue) {
    return '<div class="score-bar-wrapper"><div class="bar-segment" style="width: '+val+'%; background-color: hsla('+hue+', 85%, 40%, 1.0);"></div></div>';
}

// New bar functions with lab-specific colors
function createLabBar(val, modelName) {
    const lab = getLabInfo(modelName);
    return '<div class="score-bar-wrapper"><div class="bar-segment" style="width: '+val+'%; background-color: '+lab.color+';"></div></div>';
}

function createLabMiniBar(val, modelName) {
    const lab = getLabInfo(modelName);
    return '<div style="height: 6px; width: 100%; background: #eee; border-radius: 3px; margin-top: 5px; overflow: hidden;"><div style="height: 100%; width: '+val+'%; background-color: '+lab.color+';"></div></div>';
}

function sortTable(table, colIndex) {
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const header = table.querySelector('th[data-idx=\"'+colIndex+'\"]');
    const isAsc = header.classList.contains('asc');
    const dir = isAsc ? -1 : 1;
    rows.sort((a, b) => {
        const aTxt = a.children[colIndex].innerText.trim();
        const bTxt = b.children[colIndex].innerText.trim();
        const aNum = parseFloat(aTxt.replace(/[^0-9.-]+/g,""));
        const bNum = parseFloat(bTxt.replace(/[^0-9.-]+/g,""));
        if (!isNaN(aNum) && !isNaN(bNum) && (aTxt.includes('%') || aTxt.match(/^\\d/))) return (aNum - bNum) * dir;
        return aTxt.localeCompare(bTxt, undefined, {numeric: true}) * dir;
    });
    tbody.innerHTML = '';
    tbody.append(...rows);
    table.querySelectorAll('th').forEach(th => th.classList.remove('asc', 'desc'));
    header.classList.toggle('asc', !isAsc);
    header.classList.toggle('desc', isAsc);
}

document.addEventListener('DOMContentLoaded', () => {
    loadData();
    document.querySelectorAll('th[data-idx]').forEach(th => {
        th.addEventListener('click', () => sortTable(th.closest('table'), th.dataset.idx));
    });
});
