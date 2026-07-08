#!/usr/bin/env python3
"""Genie 影片處理面板 — 單檔小工具。

雙擊「影片處理面板.command」啟動;瀏覽器顯示 ~/Movies 每部影片的
處理狀態(筆記 / 圖文 PDF),可單部或整批排程,背景依序處理
(whisper/LLM 不可並行)。完成的筆記自動更新 INDEX。
"""
from __future__ import annotations

import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

HOME = Path.home()
MOVIES = HOME / "Movies"
NOTES = MOVIES / "genie-notes"
GENIE = HOME / "proj_genie"
PY = str(GENIE / ".venv" / "bin" / "python3")
FFPROBE = "/opt/homebrew/bin/ffprobe"
PORT = 5250

ENV_PYTHONPATH = ":".join(str(GENIE / r) for r in
                          ("genie-core", "genie-transcript", "genie-vid2pdf"))
sys.path.insert(0, str(GENIE / "genie-core"))

# 轉寫後端:local(mlx-whisper,不外傳音訊)或 groq(雲端 whisper-large-v3,
# 約 100x 實時、對嘈雜音源更準,音訊會離開本機)。
_backend = "local"

app = Flask(__name__)

_lock = threading.Lock()
_cond = threading.Condition(_lock)
_pending: list = []          # ordered [(video_name, task)] — head runs next
_current = None              # {"name","task","started","accum","paused"}
_intent = None               # None | "restart" | "abort" (set before killing)
_results: dict = {}          # (name, task) -> {"ok": bool, "detail": str}
_durations: dict = {}        # name -> seconds
_proc = None                 # running subprocess


def video_files():
    return sorted((f for f in MOVIES.glob("*.mp4")), key=lambda f: f.stat().st_mtime,
                  reverse=True)


def duration_of(f: Path) -> float:
    name = f.stem
    if name not in _durations:
        try:
            out = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(f)],
                capture_output=True, text=True, timeout=30).stdout.strip()
            _durations[name] = float(out)
        except Exception:
            _durations[name] = 0.0
    return _durations[name]


NOTES_FACTOR = 0.12   # 實測:筆記(whisper+LLM)約為片長的 12%
SLIDES_FACTOR = 0.09  # 實測:圖文 PDF(ffmpeg)約為片長的 9%


def video_date(f: Path) -> str:
    """YYYY-MM-DD 取自檔名前綴;無日期檔名回空字串。

    刻意不用檔案 mtime 當 fallback:拷貝/下載會刷新 mtime,
    會讓舊內容被誤算進「近一月」這類時間範圍。無日期的影片
    只能經由「全部」或單部按鈕排程。"""
    name = f.stem
    if len(name) >= 10 and name[:4].isdigit() and name[4] == "-":
        return name[:10]
    return ""


def mean_known_duration() -> float:
    vals = [v for v in _durations.values() if v > 60]
    return sum(vals) / len(vals) if vals else 2700.0


def est_minutes(name: str, tasks) -> float:
    dur = _durations.get(name) or mean_known_duration()
    m = 0.0
    if "notes" in tasks:
        m += dur * NOTES_FACTOR
    if "slides" in tasks:
        m += dur * SLIDES_FACTOR
    return m / 60.0


def fmt_est(minutes: float) -> str:
    if minutes < 1:
        return "<1 分"
    if minutes >= 90:
        return "%.1f 時" % (minutes / 60)
    return "%d 分" % round(minutes)


def fmt_dur(secs: float) -> str:
    if secs <= 0:
        return "?"
    if secs >= 3600:
        return "%d:%02d 時" % (secs // 3600, (secs % 3600) // 60)
    return "%d 分" % (secs // 60)


def task_state(name: str, task: str) -> str:
    out = NOTES / name
    done = (out / "structured.json").is_file() if task == "notes" \
        else (out / "slides.pdf").is_file()
    if done:
        return "done"
    with _lock:
        if _current and _current["name"] == name and _current["task"] == task:
            return "running"
        if (name, task) in _pending:
            return "queued"
        r = _results.get((name, task))
    if r and not r["ok"]:
        return "failed"
    return "missing"


def build_index():
    entries = []
    for d in sorted(NOTES.iterdir()) if NOTES.exists() else []:
        sj = d / "structured.json"
        if not d.is_dir() or not sj.exists():
            continue
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            continue
        video = MOVIES / (d.name + ".mp4")
        dur = fmt_dur(duration_of(video)) if video.exists() else ""
        topics = data.get("topics") or []
        entries.append({
            "name": d.name,
            "date": d.name[:10] if d.name[:4].isdigit() else "",
            "title": data.get("title", d.name),
            "summary": data.get("overall_summary", ""),
            "topics": [t.get("title", "") for t in topics if isinstance(t, dict)],
            "duration": dur,
            "has_slides": (d / "slides.pdf").exists(),
        })
    entries.sort(key=lambda e: e["date"] or "0000", reverse=True)

    md = ["# 影片筆記總覽", "", "共 %d 部。" % len(entries), ""]
    rows = []
    for e in entries:
        md.append("## %s — %s" % (e["date"] or e["name"], e["title"]))
        meta = (["時長 " + e["duration"]] if e["duration"] else []) + \
               ["[筆記](%s/notes.html)" % e["name"]] + \
               (["[圖文 PDF](%s/slides.pdf)" % e["name"]] if e["has_slides"] else [])
        md += ["*" + " · ".join(meta) + "*", ""]
        if e["summary"]:
            md += [e["summary"], ""]
        if e["topics"]:
            md += ["**主題**:" + "、".join(t for t in e["topics"] if t), ""]

        links = ['<a href="%s/notes.html">筆記</a>' % html.escape(e["name"], quote=True)]
        if e["has_slides"]:
            links.append('<a href="%s/slides.pdf">圖文 PDF</a>'
                         % html.escape(e["name"], quote=True))
        topics_html = "、".join(html.escape(t) for t in e["topics"] if t)
        rows.append(
            '<div class="card"><div class="meta">%s%s · %s</div><h2>%s</h2>'
            '<p>%s</p>%s</div>' % (
                html.escape(e["date"]),
                (" · " + e["duration"]) if e["duration"] else "",
                " · ".join(links),
                html.escape(e["title"]),
                html.escape(e["summary"]),
                ('<div class="topics">主題:%s</div>' % topics_html) if topics_html else ""))

    (NOTES / "INDEX.md").write_text("\n".join(md), encoding="utf-8")
    (NOTES / "INDEX.html").write_text("""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"><title>影片筆記總覽</title>
<style>
body{font-family:-apple-system,"PingFang TC",sans-serif;max-width:900px;margin:0 auto;padding:24px;line-height:1.6;background:#fafafa}
.card{background:#fff;border:1px solid #e2e2e2;border-radius:8px;padding:16px 20px;margin:14px 0}
.card h2{margin:4px 0 8px;font-size:1.15em;color:#16213e}
.meta{color:#888;font-size:.85em}.meta a{color:#3466aa}
.topics{color:#555;font-size:.9em;margin-top:6px}
</style></head><body><h1>影片筆記總覽</h1><p>共 %d 部</p>%s</body></html>"""
        % (len(entries), "\n".join(rows)), encoding="utf-8")
    return len(entries)


def worker():
    global _current, _proc
    while True:
        with _cond:
            while not _pending:
                _cond.wait()
            name, task = _pending.pop(0)
            _current = {"name": name, "task": task, "started": time.time(),
                        "accum": 0.0, "paused": False,
                        "est_total": est_minutes(name, [task])}
        video = MOVIES / (name + ".mp4")
        out = NOTES / name
        out.mkdir(parents=True, exist_ok=True)
        if task == "notes":
            cmd = [PY, "-m", "genie_transcript.cli", str(video), "-o", str(out)]
            if _backend == "groq":
                cmd += ["--whisper-backend", "groq"]
            log = NOTES / (name + ".log")
        else:
            cmd = [PY, "-m", "genie_vid2pdf.cli", str(video),
                   "-o", str(out / "slides.pdf"),
                   "--transcript", str(out / "transcript.json")]
            log = out / "slides.log"
        ok, detail = False, ""
        try:
            with open(log, "w") as lf:
                env = dict(os.environ,
                           PYTHONPATH=ENV_PYTHONPATH, HF_HUB_OFFLINE="1",
                           PATH="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""))
                # 獨立 process group:暫停/中止時可涵蓋 ffmpeg/whisper 子程序
                _proc = subprocess.Popen(cmd, stdout=lf, stderr=lf,
                                         stdin=subprocess.DEVNULL, env=env,
                                         start_new_session=True)
                rc = _proc.wait()
            ok = rc == 0
            if not ok:
                detail = log.read_text(encoding="utf-8", errors="replace")[-300:]
        except Exception as e:
            detail = str(e)
        finally:
            _proc = None
        global _intent
        with _cond:
            intent, _intent = _intent, None
            _current = None
            if intent == "restart":
                _pending.insert(0, (name, task))
                _cond.notify()
            elif intent == "abort":
                _results.pop((name, task), None)
            else:
                _results[(name, task)] = {"ok": ok, "detail": detail}
        if ok and intent is None and task == "notes":
            try:
                build_index()
            except Exception:
                pass


@app.route("/")
def index():
    return PAGE


@app.route("/notes/<path:p>")
def serve_notes(p):
    return send_from_directory(str(NOTES), p)


def prefetch_durations():
    for f in video_files():
        duration_of(f)


@app.route("/api/state")
def state():
    vids = []
    for f in video_files():
        name = f.stem
        dur = _durations.get(name)  # 只讀快取,背景執行緒會補
        if dur is None:
            dur = 0.0
        if 0 < dur < 60:
            continue  # 誤觸短片不顯示
        item = {"name": name, "duration": fmt_dur(dur) if dur else "…",
                "notes": task_state(name, "notes"),
                "slides": task_state(name, "slides")}
        for task in ("notes", "slides"):
            r = _results.get((name, task))
            if r and not r["ok"]:
                item[task + "_error"] = r["detail"][-200:]
        vids.append(item)
    with _lock:
        cur = dict(_current) if _current else None
        pending = list(_pending)
        qn = len(pending)
    if cur:
        run = 0.0 if cur["paused"] else (time.time() - cur["started"])
        cur["elapsed"] = int(cur["accum"] + run)
        cur["remaining"] = fmt_est(max(cur["est_total"] - cur["elapsed"] / 60.0, 0.5))

    # 佇列預估剩餘(含進行中任務的殘餘)
    queue_min = sum(est_minutes(n, [t]) for n, t in pending)
    if cur:
        queue_min += max(cur["est_total"] - cur["elapsed"] / 60.0, 0.5)

    # 各時間範圍的未處理量與預估
    now = time.time()
    ranges = {}
    for key, days in (("month", 31), ("quarter", 92), ("half", 183), ("all", 36500)):
        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - days * 86400))
        cnt, mins = 0, 0.0
        for f in video_files():
            name = f.stem
            d = _durations.get(name)
            if d is not None and 0 < d < 60:
                continue
            vd = video_date(f)
            if key != "all" and (not vd or vd < cutoff):
                continue
            missing = [t for t in ("notes", "slides")
                       if task_state(name, t) in ("missing", "failed")]
            if missing:
                cnt += 1
                mins += est_minutes(name, missing)
        ranges[key] = {"count": cnt, "est": fmt_est(mins)}

    from genie_core.audio.transcribe import read_env_value
    return jsonify({"backend": _backend,
                    "has_groq_key": bool(os.environ.get("GROQ_API_KEY")
                                         or read_env_value("GROQ_API_KEY")),
                    "videos": vids, "current": cur, "queued": qn,
                    "queue": [{"name": n, "task": t, "est": fmt_est(est_minutes(n, [t]))}
                              for n, t in pending],
                    "queue_est": fmt_est(queue_min) if (qn or cur) else "",
                    "ranges": ranges})


@app.route("/api/enqueue", methods=["POST"])
def enqueue():
    data = request.get_json(silent=True) or {}
    name, tasks = data.get("name"), data.get("tasks") or []
    if not name:
        return jsonify({"error": "name required"}), 400
    added = _enqueue_missing([name], tasks)
    return jsonify({"queued": added})


@app.route("/api/enqueue_all", methods=["POST"])
def enqueue_all():
    data = request.get_json(silent=True) or {}
    tasks = data.get("tasks") or ["notes", "slides"]
    names = [f.stem for f in video_files() if duration_of(f) >= 60]
    added = _enqueue_missing(names, tasks)
    return jsonify({"queued": added})


def _enqueue_missing(names, tasks):
    added = 0
    for name in names:
        for task in ("notes", "slides"):
            if task not in tasks:
                continue
            if task_state(name, task) in ("done", "queued", "running"):
                continue
            if task == "slides" and task_state(name, "notes") != "done" \
                    and "notes" not in tasks:
                continue  # slides 需要 transcript
            with _cond:
                _pending.append((name, task))
                _cond.notify()
            added += 1
    return added


@app.route("/api/enqueue_range", methods=["POST"])
def enqueue_range():
    data = request.get_json(silent=True) or {}
    days = int(data.get("days") or 31)
    tasks = data.get("tasks") or ["notes", "slides"]
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    everything = days >= 36500
    names = [f.stem for f in video_files()
             if (everything or (video_date(f) and video_date(f) >= cutoff))
             and not (0 < (_durations.get(f.stem) or 0) < 60)]
    added = _enqueue_missing(names, tasks)
    return jsonify({"queued": added})


@app.route("/api/clear_queue", methods=["POST"])
def clear_queue():
    with _lock:
        _pending.clear()
    return jsonify({"status": "cleared"})


@app.route("/api/backend", methods=["POST"])
def set_backend():
    global _backend
    from genie_core.audio.transcribe import read_env_value
    data = request.get_json(silent=True) or {}
    b = data.get("backend")
    if b not in ("local", "groq"):
        return jsonify({"error": "backend must be local or groq"}), 400
    if b == "groq" and not (os.environ.get("GROQ_API_KEY")
                            or read_env_value("GROQ_API_KEY")):
        return jsonify({"error": "no_key"}), 428   # 前端據此彈出輸入框
    _backend = b
    return jsonify({"backend": _backend})


@app.route("/api/groq_key", methods=["POST"])
def save_groq_key():
    """驗證金鑰有效後才寫入 ~/.env(權限 600)。"""
    global _backend
    from genie_core.audio.transcribe import verify_groq_key, write_env_value
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "message": "請輸入金鑰"}), 400
    ok, message = verify_groq_key(key)
    if not ok:
        return jsonify({"ok": False, "message": message}), 400
    write_env_value("GROQ_API_KEY", key)
    os.environ["GROQ_API_KEY"] = key
    _backend = "groq"
    return jsonify({"ok": True, "message": message})


@app.route("/api/current_action", methods=["POST"])
def current_action():
    global _intent
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    with _lock:
        if not _current or _proc is None:
            return jsonify({"error": "no running job"}), 404
        pgid = _proc.pid
        now = time.time()
        if action == "pause" and not _current["paused"]:
            os.killpg(pgid, signal.SIGSTOP)
            _current["accum"] += now - _current["started"]
            _current["paused"] = True
        elif action == "resume" and _current["paused"]:
            os.killpg(pgid, signal.SIGCONT)
            _current["started"] = now
            _current["paused"] = False
        elif action in ("restart", "abort"):
            _intent = action
            if _current["paused"]:
                os.killpg(pgid, signal.SIGCONT)
            os.killpg(pgid, signal.SIGTERM)
        else:
            return jsonify({"error": "bad action"}), 400
    return jsonify({"status": "ok"})


@app.route("/api/queue_move", methods=["POST"])
def queue_move():
    data = request.get_json(silent=True) or {}
    key = (data.get("name"), data.get("task"))
    action = data.get("action")
    with _lock:
        if key not in _pending:
            return jsonify({"error": "not in queue"}), 404
        i = _pending.index(key)
        _pending.pop(i)
        if action == "top":
            _pending.insert(0, key)
        elif action == "up":
            _pending.insert(max(0, i - 1), key)
        elif action == "down":
            _pending.insert(min(len(_pending), i + 1), key)
        elif action == "remove":
            pass
        else:
            _pending.insert(i, key)
            return jsonify({"error": "unknown action"}), 400
    return jsonify({"status": "ok"})


@app.route("/api/rebuild_index", methods=["POST"])
def rebuild():
    return jsonify({"entries": build_index()})


PAGE = """<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8"><title>Genie 影片處理面板</title>
<style>
body{font-family:-apple-system,"PingFang TC",sans-serif;max-width:1000px;margin:0 auto;padding:20px;background:#fafafa;line-height:1.5}
h1{color:#16213e;font-size:1.4em}
.bar{display:flex;gap:10px;align-items:center;margin:12px 0;flex-wrap:wrap}
button{border:1px solid #3466aa;background:#fff;color:#3466aa;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:.95em}
button:hover{background:#eef4ff}
button.primary{background:#3466aa;color:#fff}
.status{color:#666;font-size:.9em}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e2e2;border-radius:8px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #eee;font-size:.95em}
th{background:#f4f6fa;color:#444}
.chip{display:inline-block;padding:1px 10px;border-radius:10px;font-size:.85em}
.done{background:#e2f4e6;color:#1d7a35}.missing{background:#f0f0f0;color:#888}
.queued{background:#fff3d6;color:#9a6c00}.running{background:#dbeafe;color:#1d4ed8}
.failed{background:#fde2e2;color:#b91c1c;cursor:help}
.mini{padding:2px 8px;font-size:.82em}
#qlist li{padding:4px 6px;border-bottom:1px solid #f0f0f0;font-size:.92em}
#qlist li:last-child{border-bottom:none}
.qmeta{color:#888;font-size:.85em;margin-left:6px}
.qbtns{float:right;display:inline-flex;gap:4px}
a{color:#3466aa}
</style></head><body>
<h1>Genie 影片處理面板</h1>
<div class="bar">
  <button class="primary" id="btn-month" onclick="act('/api/enqueue_range',{days:31,tasks:['notes','slides']})">近一月</button>
  <button class="primary" id="btn-quarter" onclick="act('/api/enqueue_range',{days:92,tasks:['notes','slides']})">近一季</button>
  <button class="primary" id="btn-half" onclick="act('/api/enqueue_range',{days:183,tasks:['notes','slides']})">近半年</button>
  <button id="btn-all" onclick="act('/api/enqueue_all',{tasks:['notes','slides']})">全部</button>
  <button onclick="act('/api/clear_queue',{})">清空佇列</button>
  <button onclick="act('/api/rebuild_index',{})">重建總覽</button>
  <a href="/notes/INDEX.html" target="_blank">開啟總覽 INDEX</a>
</div>
<div class="bar">
  <span style="color:#444">轉寫引擎:</span>
  <label><input type="radio" name="be" value="local" id="be-local"> 本地(mlx,音訊不外傳)</label>
  <label><input type="radio" name="be" value="groq" id="be-groq"> Groq 雲端(約 100× 快、更準,音訊上傳)</label>
  <span id="bekey" class="status"></span>
</div>
<div class="bar"><span class="status" id="status">載入中…</span></div>
<div id="keymodal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:9">
  <div style="background:#fff;max-width:520px;margin:12vh auto;padding:20px 24px;border-radius:10px">
    <h3 style="margin:0 0 8px">輸入 Groq API 金鑰</h3>
    <p style="color:#666;font-size:.9em;margin:0 0 12px">
      金鑰會即時驗證,通過後存於 <code>~/.env</code>(權限 600),日後自動沿用。
      免費申請:<a href="https://console.groq.com/keys" target="_blank">console.groq.com/keys</a>
    </p>
    <input type="password" id="keyinput" placeholder="gsk_…" autocomplete="off"
           style="width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;font-family:monospace">
    <div id="keymsg" style="min-height:20px;margin:8px 0;font-size:.9em"></div>
    <div style="text-align:right">
      <button onclick="closeKey()">取消</button>
      <button class="primary" id="keysave" onclick="saveKey()">驗證並儲存</button>
    </div>
  </div>
</div>
<div id="queuebox" style="display:none">
  <h3 style="margin:6px 0;color:#444;font-size:1em">處理佇列(由上而下依序執行)</h3>
  <ol id="qlist" style="background:#fff;border:1px solid #e2e2e2;border-radius:8px;margin:0 0 14px;padding:8px 8px 8px 32px"></ol>
</div>
<table><thead><tr><th>影片</th><th>時長</th><th>筆記</th><th>圖文 PDF</th><th>連結</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
const CH={done:"\u2713 \u5b8c\u6210",missing:"\u672a\u8655\u7406",queued:"\u6392\u7a0b\u4e2d",running:"\u8655\u7406\u4e2d\u2026",failed:"\u5931\u6557"};
function escHtml(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;");}
function escAttr(s){return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;");}
function chip(s,err){
  const t=CH[s]||s;
  const title=err?(' title="'+escAttr(err)+'"'):"";
  return '<span class="chip '+s+'"'+title+'>'+t+'</span>';
}
async function act(url,body){
  await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  refresh();
}
async function refresh(){
  try{
    const r=await fetch("/api/state");
    const d=await r.json();
    let cur="\u9592\u7f6e";
    if(d.current){cur=(d.current.paused?"\u5df2\u66ab\u505c\uff1a":"\u8655\u7406\u4e2d\uff1a")+d.current.name+"\uff08\u9810\u4f30\u5269 "+d.current.remaining+"\uff09";}
    document.getElementById("be-"+(d.backend||"local")).checked=true;
    const bk=document.getElementById("bekey");
    if(d.backend==="groq"){bk.innerHTML='\u2713 \u91d1\u9470\u5df2\u8a2d\u5b9a <button class="mini" onclick="openKey()">\u66f4\u63db</button>';}
    else if(d.has_groq_key){bk.innerHTML='<button class="mini" onclick="openKey()">\u66f4\u63db Groq \u91d1\u9470</button>';}
    else{bk.textContent="";}
    let st=cur+" \u00b7 \u4f47\u5217 "+d.queued+" \u9805";
    if(d.queue_est){st+="\uff08\u9810\u4f30\u5269 "+d.queue_est+"\uff09";}
    document.getElementById("status").textContent=st;
    const R={month:"\u8fd1\u4e00\u6708","quarter":"\u8fd1\u4e00\u5b63","half":"\u8fd1\u534a\u5e74","all":"\u5168\u90e8"};
    for(const k in R){
      const b=document.getElementById("btn-"+k);
      if(b&&d.ranges&&d.ranges[k]){
        const g=d.ranges[k];
        b.textContent=R[k]+(g.count?"\uff08"+g.count+" \u90e8\u00b7\u7d04 "+g.est+"\uff09":"\uff08\u5df2\u5168\u90e8\u5b8c\u6210\uff09");
        b.disabled=!g.count;
      }
    }
    const rows=d.videos.map(function(v){
      const links=[];
      if(v.notes==="done"){links.push('<a href="/notes/'+encodeURIComponent(v.name)+'/notes.html" target="_blank">\u7b46\u8a18</a>');}
      if(v.slides==="done"){links.push('<a href="/notes/'+encodeURIComponent(v.name)+'/slides.pdf" target="_blank">PDF</a>');}
      let nbtn="";
      if(v.notes==="missing"||v.notes==="failed"){nbtn=' <button class="mini enq" data-name="'+escAttr(v.name)+'" data-task="notes">\u6392\u7a0b</button>';}
      let sbtn="";
      if((v.slides==="missing"||v.slides==="failed")&&v.notes==="done"){sbtn=' <button class="mini enq" data-name="'+escAttr(v.name)+'" data-task="slides">\u6392\u7a0b</button>';}
      return "<tr><td>"+escHtml(v.name)+"</td><td>"+v.duration+"</td><td>"+chip(v.notes,v.notes_error)+nbtn+"</td><td>"+chip(v.slides,v.slides_error)+sbtn+"</td><td>"+links.join(" \u00b7 ")+"</td></tr>";
    }).join("");
    document.getElementById("rows").innerHTML=rows;
    const qb=document.getElementById("queuebox");
    if((d.queue&&d.queue.length)||d.current){
      qb.style.display="";
      let items=[];
      if(d.current){
        const c=d.current;
        const task=c.task==="notes"?"\u7b46\u8a18":"\u5716\u6587PDF";
        const pb=c.paused
          ?'<button class="mini cur" data-action="resume">\u25b6 \u7e7c\u7e8c</button>'
          :'<button class="mini cur" data-action="pause">\u23f8 \u66ab\u505c</button>';
        items.push('<li style="background:#eef4ff;border-radius:6px">'+
          '<b>'+(c.paused?"\u23f8 \u5df2\u66ab\u505c":"\u25b6 \u8655\u7406\u4e2d")+'</b> '+escHtml(c.name)+
          '<span class="qmeta">'+task+" \u00b7 \u5df2\u8dd1 "+Math.floor(c.elapsed/60)+" \u5206 \u00b7 \u9810\u4f30\u5269 "+c.remaining+"</span>"+
          '<span class="qbtns">'+pb+
          '<button class="mini cur" data-action="restart">\u91cd\u555f</button>'+
          '<button class="mini cur" data-action="abort">\u4e2d\u6b62</button></span></li>');
      }
      items=items.concat((d.queue||[]).map(function(q){
        const task=q.task==="notes"?"\u7b46\u8a18":"\u5716\u6587PDF";
        const btn=function(a,l){return '<button class="mini qmv" data-name="'+escAttr(q.name)+'" data-task="'+q.task+'" data-action="'+a+'">'+l+'</button>';};
        return "<li>"+escHtml(q.name)+'<span class="qmeta">'+task+" \u00b7 \u7d04 "+q.est+"</span>"+
          '<span class="qbtns">'+btn("top","\u2b06\u9802")+btn("up","\u4e0a\u79fb")+btn("down","\u4e0b\u79fb")+btn("remove","\u79fb\u9664")+"</span></li>";
      }));
      document.getElementById("qlist").innerHTML=items.join("");
    }else{
      qb.style.display="none";
    }
  }catch(e){
    document.getElementById("status").textContent="\u8f09\u5165\u5931\u6557\uff1a"+e;
  }
}
function openKey(){document.getElementById("keymodal").style.display="";document.getElementById("keyinput").focus();}
function closeKey(){document.getElementById("keymodal").style.display="none";document.getElementById("keymsg").textContent="";document.getElementById("keyinput").value="";refresh();}
async function saveKey(){
  const btn=document.getElementById("keysave"),msg=document.getElementById("keymsg");
  const key=document.getElementById("keyinput").value.trim();
  if(!key){msg.style.color="#b91c1c";msg.textContent="\u8acb\u8f38\u5165\u91d1\u9470";return;}
  btn.disabled=true;msg.style.color="#666";msg.textContent="\u9a57\u8b49\u4e2d\u2026";
  try{
    const r=await fetch("/api/groq_key",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:key})});
    const d=await r.json();
    if(d.ok){msg.style.color="#1d7a35";msg.textContent="\u2713 "+d.message;setTimeout(closeKey,900);}
    else{msg.style.color="#b91c1c";msg.textContent=d.message;}
  }catch(e){msg.style.color="#b91c1c";msg.textContent="\u9a57\u8b49\u5931\u6557\uff1a"+e;}
  btn.disabled=false;
}
async function setBackend(b){
  const r=await fetch("/api/backend",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({backend:b})});
  if(r.status===428){openKey();return;}   // 尚未設定金鑰
  refresh();
}
document.getElementById("be-local").addEventListener("change",function(){setBackend("local");});
document.getElementById("be-groq").addEventListener("change",function(){setBackend("groq");});
document.getElementById("rows").addEventListener("click",function(ev){
  const b=ev.target.closest("button.enq");
  if(b){act("/api/enqueue",{name:b.dataset.name,tasks:[b.dataset.task]});}
});
document.getElementById("qlist").addEventListener("click",function(ev){
  const c=ev.target.closest("button.cur");
  if(c){act("/api/current_action",{action:c.dataset.action});return;}
  const b=ev.target.closest("button.qmv");
  if(b){act("/api/queue_move",{name:b.dataset.name,task:b.dataset.task,action:b.dataset.action});}
});
refresh();
setInterval(refresh,4000);
</script></body></html>"""


def _shutdown(signum=None, frame=None):
    """面板結束時終止進行中的子程序 group,避免孤兒繼續佔 GPU。"""
    p = _proc
    if p is not None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            pass
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    NOTES.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=prefetch_durations, daemon=True).start()
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
